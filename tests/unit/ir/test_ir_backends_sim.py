from __future__ import annotations

import copy
import importlib.util
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from ir_specs import EXAMPLE_DIR, ROOT, example_data

from afsim_research_agent.ir.__main__ import schema_text
from afsim_research_agent.ir.backends import CompileRefused, compile_backend
from afsim_research_agent.ir.backends.btcpp_xml import verify_role_xml
from afsim_research_agent.ir.binding import bind
from afsim_research_agent.ir.normalize import normalize
from afsim_research_agent.ir.pipeline import ForeignOutputError, build_package
from afsim_research_agent.ir.sim import run_scenario, run_scenarios
from afsim_research_agent.ir.sim.world import SimWorld, Terrain, _resume_index
from afsim_research_agent.ir.validate import validate_data


@pytest.fixture(scope="module")
def ir() -> dict[str, Any]:
    result = normalize(validate_data(example_data()))
    assert result.ir is not None
    return result.ir


@pytest.fixture(scope="module")
def sim_context(ir: dict[str, Any]) -> dict[str, Any]:
    context, items = bind(ir, "python_sim")
    assert context is not None, items
    return context


def test_compile_refuses_drafts_and_mismatched_inputs(
    ir: dict[str, Any], sim_context: dict[str, Any]
) -> None:
    draft = copy.deepcopy(ir)
    draft["draft"] = True
    with pytest.raises(CompileRefused):
        compile_backend("mermaid", draft, sim_context)
    tampered = copy.deepcopy(ir)
    tampered["parameters"]["bound_distance"]["value"] = 1.0
    with pytest.raises(CompileRefused):
        compile_backend("mermaid", tampered, sim_context)
    other = dict(sim_context, ir_hash="sha256:other")
    with pytest.raises(CompileRefused):
        compile_backend("mermaid", ir, other)


def test_unbindable_required_capability_is_dep001(ir: dict[str, Any]) -> None:
    changed = copy.deepcopy(ir)
    changed["roles"][0]["dependencies"]["sensors"][0]["capability"] = "lidar_magic"
    context, items = bind(changed, "python_sim")
    assert context is None
    assert any(d.code == "DEP001" for d in items)
    context, _ = bind(
        changed,
        "python_sim",
        {"lidar_magic": {"component": "sim.RangeSensor", "settings": {"range_m": 100}}},
    )
    assert context is not None


def test_btcpp_xml_round_trips_and_detects_drift(ir: dict[str, Any]) -> None:
    context, _ = bind(ir, "btcpp_xml")
    files = compile_backend("btcpp_xml", ir, context)
    for role in ir["roles"]:
        text = files[f"{role['id']}.xml"]
        assert verify_role_xml(ir, role, text) == []
        broken = text.replace('behavior="coord_leader_follower__keep_station"', 'behavior="other"')
        if broken != text:
            assert verify_role_xml(ir, role, broken)


def test_backends_are_deterministic(ir: dict[str, Any]) -> None:
    for backend in ("mermaid", "btcpp_xml", "python_sim"):
        context, _ = bind(ir, backend)
        assert compile_backend(backend, ir, context) == compile_backend(backend, ir, context)


def test_generated_python_module_pins_hash(
    ir: dict[str, Any], sim_context: dict[str, Any], tmp_path: Path
) -> None:
    files = compile_backend("python_sim", ir, sim_context)
    path = tmp_path / "wingman.py"
    path.write_text(files["roles/wingman.py"], encoding="utf-8")
    spec = importlib.util.spec_from_file_location("gen_wingman", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    controller = module.make_controller(ir, 2)
    assert controller.role_id == "wingman" and controller.instance == 2
    with pytest.raises(RuntimeError):
        module.make_controller(dict(ir, ir_hash="sha256:x"))


def test_example_scenarios_pass(ir: dict[str, Any], sim_context: dict[str, Any]) -> None:
    results = run_scenarios(ir, sim_context)
    assert [r.id for r in results] == [s["id"] for s in ir["scenarios"]]
    for result in results:
        assert result.passed, [
            (e.id, e.measured, e.threshold, e.detail) for e in result.expectations if not e.passed
        ]


def test_comm_loss_blocks_synchronization(ir: dict[str, Any], sim_context: dict[str, Any]) -> None:
    scenario = next(s for s in ir["scenarios"] if s["id"] == "comm_loss_during_transition")
    result = run_scenario(ir, sim_context, scenario)
    assert result.final_states["platoon_leader[0]"] == "travel_urban"
    assert all(
        v == "travel_open" for k, v in result.final_states.items() if k.startswith("wingman")
    )


def test_bounding_groups_alternate(ir: dict[str, Any], sim_context: dict[str, Any]) -> None:
    scenario = next(s for s in ir["scenarios"] if s["id"] == "contact_bounding_overwatch")
    world = SimWorld(ir, sim_context, scenario)
    while world.now <= 420:
        world.step()
    senders = [k for _, k, m in world.sent_log if m == "coord_bounding__bound_complete"]
    groups = ["B" if k in ("platoon_leader[0]", "wingman[2]") else "O" for k in senders]
    pairs = ["".join(sorted(groups[i : i + 2])) for i in range(0, len(groups) - 1, 2)]
    assert pairs[:4] == ["BB", "OO", "BB", "OO"]


def test_failing_expectation_is_reported(ir: dict[str, Any], sim_context: dict[str, Any]) -> None:
    scenario = copy.deepcopy(
        next(s for s in ir["scenarios"] if s["id"] == "open_to_urban_transition")
    )
    scenario["expect"][0]["within"]["value"] = 0
    scenario["expect"][0]["within"]["after_event"] = "start"
    result = run_scenario(ir, sim_context, scenario)
    assert not result.passed
    assert not result.expectations[0].passed
    # later expectation anchored on the failed one is also not satisfied
    assert not result.expectations[2].passed


def test_terrain_and_route_helpers() -> None:
    terrain = Terrain.from_mapping(
        {
            "default_class": "open",
            "regions": [{"class": "urban", "x_min": 100, "x_max": 200, "y_min": -50, "y_max": 50}],
        }
    )
    assert terrain.class_at(150, 0) == "urban"
    assert terrain.distance_to_class(0, 0, "urban") == 100
    assert terrain.distance_to_class(0, 0, "forest") == float("inf")
    route = [(0.0, 0.0), (1000.0, 0.0), (2000.0, 0.0)]
    assert _resume_index(route, 1500.0, 10.0) == 2
    assert _resume_index(route, 10.0, 0.0) == 1


def test_build_package_end_to_end(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    shutil.copy(EXAMPLE_DIR / "behavior_spec.yaml", package / "behavior_spec.yaml")
    result = build_package(package)
    assert result.status == "passed", [
        d.render() for d in result.diagnostics if d.severity.value == "error"
    ]
    manifest = json.loads((package / "generated" / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["backends"]) == {"mermaid", "btcpp_xml", "python_sim"}
    assert (package / "generated" / "btcpp_xml" / "wingman.xml").exists()
    assert (package / "review.md").read_text(encoding="utf-8").startswith("# 审查报告")
    first = {p.relative_to(package): p.read_bytes() for p in package.rglob("*") if p.is_file()}
    build_package(package)
    second = {p.relative_to(package): p.read_bytes() for p in package.rglob("*") if p.is_file()}
    assert first == second

    # a pending parameter is a reference value: the build passes and review.md lists it
    original = (package / "behavior_spec.yaml").read_text(encoding="utf-8")
    confirmed = "bound_distance: {type: distance, value: 300, unit: m, description: 每次跃进距离（示例值）, basis: {status: user_specified, confirmation: confirmed}}"
    spec = original.replace(
        confirmed,
        "bound_distance: {type: distance, value: 300, unit: m, description: 每次跃进距离, basis: {status: unknown, rationale: 参考常见做法, confirmation: pending}}",
    )
    (package / "behavior_spec.yaml").write_text(spec, encoding="utf-8")
    referenced = build_package(package, run_l3=False)
    assert referenced.status == "passed"
    review = (package / "review.md").read_text(encoding="utf-8")
    assert "参考值（已用于构建，待人工复核调参）" in review and "| `bound_distance` | 300 m | ⚠ 无依据" in review
    report = json.loads((package / "validation_report.json").read_text(encoding="utf-8"))
    assert report["reference_values"][0]["name"] == "bound_distance"
    assert build_package(package, run_l3=False, strict_parameters=True, write=False).status == "blocked"

    # a rejected parameter blocks the build and removes stale generated code
    spec = original.replace(
        confirmed,
        "bound_distance: {type: distance, value: 300, unit: m, description: 每次跃进距离, basis: {status: inferred, evidence: [ev_1], rationale: r, confirmation: rejected}}",
    )
    (package / "behavior_spec.yaml").write_text(spec, encoding="utf-8")
    blocked = build_package(package)
    assert blocked.status == "blocked"
    assert not (package / "generated" / "manifest.json").exists()
    assert not (package / "generated" / "btcpp_xml" / "wingman.xml").exists()
    assert "待确认" in (package / "review.md").read_text(encoding="utf-8")


def test_foreign_generated_directory_is_not_overwritten(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    (package / "generated").mkdir(parents=True)
    (package / "generated" / "mine.txt").write_text("user file", encoding="utf-8")
    shutil.copy(EXAMPLE_DIR / "behavior_spec.yaml", package / "behavior_spec.yaml")
    with pytest.raises(ForeignOutputError):
        build_package(package)
    assert (package / "generated" / "mine.txt").read_text(encoding="utf-8") == "user file"


def test_committed_json_schema_is_current() -> None:
    path = ROOT / "src" / "afsim_research_agent" / "ir" / "schema" / "behavior_spec.schema.json"
    assert path.read_text(encoding="utf-8") == schema_text()


def test_bound_target_geometry() -> None:
    from afsim_research_agent.ir.sim.world import bound_target

    assert bound_target(0, 0, 90, 300, None) == pytest.approx((300, 0))
    assert bound_target(0, 0, 90, 300, (1000, 0), 800) == pytest.approx((200, 0))
    assert bound_target(0, 0, 90, 300, (500, 0), 800) == (0, 0)  # never moves away or closer
    assert bound_target(0, 0, 0, 100, (0, 1000), 0) == pytest.approx((0, 100))


def test_resync_repairs_lossy_synchronization(
    ir: dict[str, Any], sim_context: dict[str, Any]
) -> None:
    scenario = copy.deepcopy(
        next(s for s in ir["scenarios"] if s["id"] == "open_to_urban_transition")
    )
    switched = 0
    for seed in range(8):
        scenario["environment"]["random_seed"] = seed
        scenario["events"] = [
            {
                "id": "jam",
                "type": "comm_degradation",
                "at": {"value": 0, "unit": "sec"},
                "duration": {"value": 10, "unit": "min"},
                "loss": 0.5,
            }
        ]
        world = SimWorld(ir, sim_context, scenario)
        while world.now <= world.duration:
            world.step()
        switched += all(
            e.controller.state == "travel_urban" for e in world.entities if e.role == "wingman"
        )
    assert switched == 8
