from __future__ import annotations

import copy
import importlib.util
import json
import math
from pathlib import Path
from typing import Any

import pytest
from ir_specs import ROOT, example_data

from afsim_research_agent.ir.binding import bind, bind_external
from afsim_research_agent.ir.cosim import (
    CoSimRunner,
    EngineMapping,
    EntityObservation,
    Observation,
    TrackObservation,
)
from afsim_research_agent.ir.cosim.convert import (
    LocalFrame,
    heading_from_engine,
    heading_to_engine,
    speed_from_engine,
)
from afsim_research_agent.ir.cosim.reference import ReferenceEngine
from afsim_research_agent.ir.cosim.runner import ObservationWorld, _Ent
from afsim_research_agent.ir.cosim.trace import TraceError, judge_trace
from afsim_research_agent.ir.normalize import normalize
from afsim_research_agent.ir.runtime import RoleController
from afsim_research_agent.ir.sim import run_scenario
from afsim_research_agent.ir.validate import validate_data


@pytest.fixture(scope="module")
def ir() -> dict[str, Any]:
    result = normalize(validate_data(example_data()))
    assert result.ir is not None
    return result.ir


@pytest.fixture(scope="module")
def ctx(ir: dict[str, Any]) -> dict[str, Any]:
    context, _ = bind(ir, "python_sim")
    assert context is not None
    return context


@pytest.mark.parametrize("messaging", ["adapter", "engine"])
def test_cosim_with_reference_engine_matches_builtin_simulation(ir, ctx, messaging: str) -> None:
    for scenario in ir["scenarios"]:
        builtin = run_scenario(ir, ctx, scenario)
        cosim = CoSimRunner(
            ir, ReferenceEngine(ir, ctx, messaging=messaging), scenario, bindings=ctx
        ).run()
        assert cosim.passed == builtin.passed
        assert cosim.messages_sent == builtin.messages_sent
        assert cosim.final_states == builtin.final_states
        assert [e.measured for e in cosim.expectations] == [
            e.measured for e in builtin.expectations
        ]


def test_trace_round_trip_and_rejections(ir, ctx, tmp_path: Path) -> None:
    scenario = ir["scenarios"][0]
    path = tmp_path / "run.jsonl"
    direct = CoSimRunner(ir, ReferenceEngine(ir, ctx), scenario, bindings=ctx).run(trace_path=path)
    judged = judge_trace(ir, scenario, path)
    assert [e.measured for e in judged.expectations] == [e.measured for e in direct.expectations]

    other = copy.deepcopy(ir)
    other["ir_hash"] = "sha256:different"
    with pytest.raises(TraceError):
        judge_trace(other, scenario, path)
    with pytest.raises(TraceError):
        judge_trace(ir, ir["scenarios"][1], path)


def test_engine_ids_come_from_mapping(ir, ctx, tmp_path: Path) -> None:
    mapping = EngineMapping(entities={"platoon_leader[0]": "tank_01", "wingman[0]": "tank_02"})
    path = tmp_path / "ids.jsonl"
    CoSimRunner(
        ir, ReferenceEngine(ir, ctx), ir["scenarios"][0], mapping=mapping, bindings=ctx
    ).run(trace_path=path)
    header = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert header["entities"]["tank_01"] == "platoon_leader[0]"
    assert header["entities"]["wingman_1"] == "wingman[1]"


class _NoEventsEngine(ReferenceEngine):
    def apply_event(self, event: dict[str, Any]) -> bool:
        return False


def test_unsupported_events_are_reported(ir, ctx) -> None:
    scenario = next(s for s in ir["scenarios"] if s["id"] == "contact_bounding_overwatch")
    runner = CoSimRunner(ir, _NoEventsEngine(ir, ctx), scenario, bindings=ctx)
    result = runner.run()
    assert any("hostile_appears" in w for w in runner.warnings)
    assert not result.passed  # no hostile ever appears


def test_external_binding_requires_every_capability(ir) -> None:
    context, items = bind_external(ir, "lab_engine", {"route_following_ground": {"component": "M"}})
    assert context is None
    assert {d.code for d in items} == {"DEP001"}
    full = {
        "route_following_ground": {"component": "LAB_MOVER"},
        "visual_search": {"component": "LAB_EYES", "settings": {"range_m": 2500}},
        "voice_data": {"component": "LAB_RADIO"},
    }
    context, items = bind_external(ir, "lab_engine", full)
    assert context is not None and not items
    assert context["roles"]["wingman"]["components"]["vehicle_sight"]["component"] == "LAB_EYES"


def _view(ir: dict[str, Any], ob: EntityObservation) -> tuple[ObservationWorld, CoSimRunner]:
    class _Dummy:
        messaging = "engine"

    runner = CoSimRunner(ir, _Dummy(), ir["scenarios"][0])  # type: ignore[arg-type]
    runner.obs = Observation(10.0, {"platoon_leader_0": ob})
    ent = _Ent(
        "platoon_leader[0]",
        "platoon_leader",
        0,
        "platoon_leader_0",
        RoleController(ir, "platoon_leader"),
    )
    return ObservationWorld(runner, ent, ob), runner


def test_observation_mapping(ir) -> None:
    ob = EntityObservation(
        x=0.0,
        y=0.0,
        heading_deg=450.0,
        speed_mps=5.0,
        tracks=[
            TrackObservation("red", 300.0, 400.0),
            TrackObservation("blue", 10.0, 0.0, hostile=False),
        ],
        terrain={"class": "open", "distance_to": {"urban": 180.0}},
        mover={"at_destination": True},
    )
    view, _ = _view(ir, ob)
    assert view.property("self_value", "heading", {}) == 90.0
    assert view.property("track_value", "nearest_hostile_range", {}) == 500.0
    assert view.property("track_value", "hostile_count", {}) == 1
    assert view.property("terrain_value", "distance_to_class", {"argument": "urban"}) == 180.0
    assert view.property("terrain_value", "distance_to_class", {"argument": "forest"}) == math.inf
    assert view.property("mover_value", "at_destination", {}) is True
    view.apply("bound_to_position", {"distance": 300.0})
    assert (
        view.property("mover_value", "at_destination", {}) is False
    )  # new command not yet complete
    assert view.commands[0].entity == "platoon_leader_0"


def test_conversions() -> None:
    assert heading_from_engine(0.0, "east_ccw_deg") == 90.0
    assert heading_from_engine(math.pi / 2, "east_ccw_rad") == pytest.approx(0.0)
    for conv in ("north_cw_deg", "north_cw_rad", "east_ccw_deg", "east_ccw_rad"):
        assert heading_from_engine(heading_to_engine(123.0, conv), conv) == pytest.approx(123.0)
    assert speed_from_engine(36.0, "km/h") == pytest.approx(10.0)
    frame = LocalFrame(39.9, 116.4)
    x, y = frame.to_local(39.91, 116.41)
    lat, lon = frame.to_geodetic(x, y)
    assert (lat, lon) == pytest.approx((39.91, 116.41))
    assert 800 < x < 900 and 1100 < y < 1120


def test_lab_template_imports_and_rejects_unmapped_commands(ir) -> None:
    path = ROOT / "integrations" / "lab_engine" / "lab_engine_adapter.py"
    spec = importlib.util.spec_from_file_location("lab_template", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    engine = module.make_engine(ir, {}, {})
    from afsim_research_agent.ir.cosim import Command

    with pytest.raises(NotImplementedError):
        engine.apply_commands([Command("x", "fire_weapon", {}, "r", 0, "")])
    example = json.loads(
        (ROOT / "integrations" / "lab_engine" / "bindings.example.json").read_text(encoding="utf-8")
    )
    context, items = bind_external(ir, "lab_engine", example["capabilities"])
    assert context is not None, items
