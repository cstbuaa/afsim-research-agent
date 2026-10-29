"""K3: IR capability consistency check (capability-catalog-integration-v1 §3.3)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from afsim_research_agent.capability import CapabilityCatalog, assess_ir_element, gap_id
from afsim_research_agent.capability.ir_check import (
    afsim_gate,
    annotate_btcpp_xml,
    check_ir,
    load_crow_gaps,
    load_decisions,
    render_section,
)
from afsim_research_agent.ir.backends.btcpp_xml import verify_role_xml
from afsim_research_agent.ir.pipeline import build_package

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = ROOT / "examples" / "armor_platoon_terrain_formation"

# the lab implements no generic messaging, no terrain and no bounding coordination
SEND = "ir:send_message"
RECEIVED = "ir:message_received"
TERRAIN_CLASS = "ir:terrain_value.class"
TERRAIN_DISTANCE = "ir:terrain_value.distance_to_class"
BOUNDING = "ir:bounding_turn"
ROUTE = "movement.follow_route"
EXPECTED_GAPS = {SEND, RECEIVED, TERRAIN_CLASS, TERRAIN_DISTANCE, BOUNDING}


@pytest.fixture(scope="module")
def catalog(capability_set) -> CapabilityCatalog:
    return capability_set


@pytest.fixture(scope="module")
def ir() -> dict:
    return json.loads((EXAMPLE / "normalized_ir.json").read_text(encoding="utf-8"))


def test_armor_example_against_lab_capabilities(catalog, ir) -> None:
    check = check_ir(ir, catalog)
    assert check.level == "needs_development"
    assert {g.contract for g in check.gaps} == EXPECTED_GAPS
    assert all(g.gap_type == "develop" for g in check.gaps)
    # gap IDs are the same as Crow's (same engine, same contract)
    assert gap_id(SEND) in {g.id for g in check.gaps}
    by_name = {e.name: e for e in check.elements}
    # every element row agrees with the per-element engine; only gap rows carry gaps
    for name, row in by_name.items():
        if row.kind != "dependency":
            assert (
                row.conclusion == assess_ir_element(catalog, name, "ground_vehicle")[1].conclusion
            )
        assert bool(row.gaps) == (row.conclusion in ("confirm", "develop", "ir_extension")), name
    # keep_station: adopted slot + move to slot from the hierarchical formation slice
    assert by_name["keep_station"].label == "可组合实现"
    assert by_name["report_contact"].conclusion == "ready"
    # standoff: implemented only inside one tactic
    assert by_name["standoff_reached"].label == "部分可用"


def test_usage_locations_reach_tree_nodes_and_acceptance(catalog, ir) -> None:
    check = check_ir(ir, catalog)
    send = next(g for g in check.gaps if g.contract == SEND)
    assert set(send.roles) == {"platoon_leader", "wingman"}
    assert "behavior:coord_bounding__announce_bound" in send.used_at
    assert "leader_ack" in send.tree_nodes
    assert any(a.startswith("L2:") for a in send.acceptance)
    # transition guards are attributed to the role's transition evaluator
    terrain = next(g for g in check.gaps if g.contract == TERRAIN_CLASS)
    assert terrain.roles == ["platoon_leader"] and terrain.tree_nodes == [
        "platoon_leader_transitions"
    ]
    # a gap inside a referenced condition reaches the behavior node that uses it
    assert check.node_gaps["coord_bounding__bound_node"] == [gap_id(BOUNDING)]


def test_crow_estimate_is_confirmed_resolved_or_unused(catalog, ir, tmp_path: Path) -> None:
    crow = tmp_path / "capability_gaps.yaml"
    crow.write_text(
        yaml.safe_dump(
            {
                "format": "CapabilityGaps",
                "gaps": [
                    {
                        "id": gap_id(SEND),
                        "contract": SEND,
                        "display_name": "发送",
                        "gap_type": "confirm",
                        "used_by": [{"candidate": "tc_01"}],
                    },
                    {
                        "id": gap_id(ROUTE),
                        "contract": ROUTE,
                        "display_name": "沿路线行进",
                        "gap_type": "confirm",
                        "used_by": [{"candidate": "tc_01"}],
                    },
                    {
                        "id": gap_id("火力扇区几何"),
                        "contract": "火力扇区几何",
                        "display_name": "火力扇区几何",
                        "gap_type": "develop",
                        "used_by": [{"candidate": "tc_01"}],
                    },
                    {
                        "id": "cap-gap-other",
                        "contract": "x",
                        "gap_type": "develop",
                        "used_by": [{"candidate": "tc_02"}],
                    },
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    gaps = load_crow_gaps(crow, "tc_01")
    assert len(gaps) == 3
    check = check_ir(ir, catalog, crow_gaps=gaps, crow_source="capability_gaps.yaml#tc_01")
    states = {c.contract: c.in_ir for c in check.crow_estimate}
    assert states == {SEND: "confirmed", ROUTE: "resolved", "火力扇区几何": "not_used"}
    send = next(g for g in check.gaps if g.contract == SEND)
    assert send.source == "crow_estimate+ir_check" and send.crow_candidates == ["tc_01"]
    assert not send.new_in_ir
    assert next(g for g in check.gaps if g.contract == TERRAIN_CLASS).new_in_ir
    text = "\n".join(render_section(check))
    assert "与 Crow 预估对照" in text and "★新" in text and "IR 未使用" in text


def test_decisions_open_the_afsim_gate_only(catalog, ir, tmp_path: Path) -> None:
    check = check_ir(ir, catalog)
    assert afsim_gate(check, "afsim") == check.unresolved and len(check.unresolved) == 5
    assert afsim_gate(check, "btcpp_xml") == [] and afsim_gate(None, "afsim") == []
    ids = [g.id for g in check.gaps]
    (tmp_path / "capability_decisions.yaml").write_text(
        yaml.safe_dump(
            {
                "decisions": {
                    **{gid: {"decision": "confirmed", "note": "已在本地验证"} for gid in ids[:-1]},
                    ids[-1]: {"decision": "maybe"},  # not a valid decision: ignored
                }
            }
        ),
        encoding="utf-8",
    )
    decided = check_ir(ir, catalog, decisions=load_decisions(tmp_path))
    assert decided.unresolved == [ids[-1]]
    assert decided.gaps[0].decision == "confirmed" and decided.gaps[0].status == "decided"
    assert "已确认平台支持" in "\n".join(render_section(decided))


def test_tree_annotation_keeps_xml_valid(catalog, ir) -> None:
    check = check_ir(ir, catalog)
    role = next(r for r in ir["roles"] if r["id"] == "platoon_leader")
    original = (EXAMPLE / "generated" / "btcpp_xml" / "platoon_leader.xml").read_text(
        encoding="utf-8"
    )
    original = "\n".join(x for x in original.splitlines() if "capability_status" not in x) + "\n"
    annotated = annotate_btcpp_xml(original, check)
    assert f'<!-- capability_status="gap:{gap_id(SEND)}" 需开发 ' in annotated
    lines = annotated.splitlines()
    i = next(n for n, line in enumerate(lines) if 'name="leader_ack"' in line)
    assert "capability_status" in lines[i - 1]
    assert verify_role_xml(ir, role, annotated) == []
    assert annotate_btcpp_xml(original, check) == annotated  # deterministic


def test_build_writes_gaps_review_and_annotated_tree(tmp_path: Path, capability_set) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    shutil.copy(EXAMPLE / "behavior_spec.yaml", package / "behavior_spec.yaml")
    result = build_package(package, run_l3=False)
    assert result.status == "passed"  # open gaps never fail L1/L2/L3 or the existing backends
    assert any(d.code == "CAP002" for d in result.diagnostics)
    gaps = yaml.safe_load((package / "capability_gaps.yaml").read_text(encoding="utf-8"))
    # coarse by default: only maneuver, formation and terrain are checked precisely
    assert gaps["phase"] == "ir_check" and len(gaps["gaps"]) == 3
    assert gaps["granularity"] == {"granularity": "coarse", "refine": []}
    assert {i["element"] for i in gaps["simplified"]} == {"send_message", "message_received"}
    assert "## 8. 平台能力核对" in (package / "review.md").read_text(encoding="utf-8")
    leader = (package / "generated" / "btcpp_xml" / "platoon_leader.xml").read_text(
        encoding="utf-8"
    )
    assert "capability_status" in leader
    report = json.loads((package / "validation_report.json").read_text(encoding="utf-8"))
    assert report["capability"]["afsim_ready"] is False
    manifest = json.loads((package / "generated" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["capability_catalog"].startswith("sha256:")

    # the AFSIM backend is gated on open gaps
    gated = build_package(package, backends=("afsim",), run_l3=False, write=False)
    assert any(d.code == "CAP001" for d in gated.diagnostics)

    # no catalog: the check is skipped, the build is not affected, stale gaps are removed
    skipped = build_package(package, run_l3=False, capability_catalog=tmp_path / "none")
    assert skipped.status == "passed" and skipped.capability is None
    assert any(d.code == "CAP000" for d in skipped.diagnostics)
    assert not (package / "capability_gaps.yaml").exists()
    assert "capability_status" not in (
        package / "generated" / "btcpp_xml" / "platoon_leader.xml"
    ).read_text(encoding="utf-8")


# ---------------------------------------------------------------------- granularity
CORE_GAPS = {TERRAIN_CLASS, TERRAIN_DISTANCE, BOUNDING}


def test_coarse_checks_core_groups_and_simplifies_the_rest(catalog, ir) -> None:
    from afsim_research_agent.capability.granularity import CapabilityPolicy

    check = check_ir(ir, catalog, policy=CapabilityPolicy.make())
    assert {g.contract for g in check.gaps} == CORE_GAPS
    simplified = {i.element: i for i in check.simplified}
    assert set(simplified) == {"send_message", "message_received"}
    assert simplified["send_message"].group_name == "通信报告"
    assert simplified["send_message"].simplification == "理想通信：消息即时送达、不丢失"
    assert "简化处理 2 项" in "\n".join(render_section(check))
    # simplified items do not reach the behavior tree or the AFSIM gate
    assert "leader_ack" not in check.node_gaps
    assert len(afsim_gate(check, "afsim")) == 3

    # the user refines communication: its gaps come back, with the same IDs as before
    refined = check_ir(ir, catalog, policy=CapabilityPolicy.make("coarse", ["comms"]))
    assert {g.contract for g in refined.gaps} == CORE_GAPS | {SEND, RECEIVED}
    fine = check_ir(ir, catalog, policy=CapabilityPolicy.make("fine"))
    assert {g.contract for g in fine.gaps} == EXPECTED_GAPS and not fine.simplified


def test_policy_comes_from_the_decisions_file(catalog, ir, tmp_path: Path) -> None:
    from afsim_research_agent.capability.granularity import CapabilityPolicy
    from afsim_research_agent.capability.ir_check import decisions_template, load_policy

    assert load_policy(tmp_path).summary() == {"granularity": "coarse", "refine": []}
    (tmp_path / "capability_decisions.yaml").write_text(
        decisions_template(CapabilityPolicy.make("coarse", ["comms"])), encoding="utf-8"
    )
    policy = load_policy(tmp_path)
    assert policy.refine == {"comms"}
    assert load_policy(tmp_path, "fine").granularity == "fine"  # the command line wins
    with pytest.raises(ValueError, match="unknown capability groups"):
        load_policy(tmp_path, refine=["radio"])
    refined = check_ir(ir, catalog, policy=policy)
    assert SEND in {g.contract for g in refined.gaps}


def test_groups_cover_the_catalog_once(catalog) -> None:
    from afsim_research_agent.capability.granularity import GroupTable

    table = GroupTable.default()
    members = [c for g in table.groups.values() for c in g.capabilities | g.detail]
    assert len(members) == len(set(members))
    assert set(members) == set(catalog.capabilities)
    assert table.core_of("ground_vehicle") == ("maneuver", "formation", "terrain")
    assert table.is_detail("perception.command_sensor_on")
    assert not table.is_detail("movement.move_to_point")
