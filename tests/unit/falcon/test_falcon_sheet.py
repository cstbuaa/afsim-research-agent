"""Falcon v2: tactic sheet parsing, compiling, drafting with block repair, falcon compile."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml
from falcon_fixtures import crow_result, gold_script, write_request

from afsim_research_agent.falcon.assemble import assemble
from afsim_research_agent.falcon.llm import ScriptedModel
from afsim_research_agent.falcon.models import (
    CrowRef,
    EvidencePackage,
    FalconRequest,
    RequirementClause,
)
from afsim_research_agent.falcon.pipeline import run_draft
from afsim_research_agent.falcon.repair import check_package, write_draft
from afsim_research_agent.falcon.sheet.blocks import join_sheet, split_sheet
from afsim_research_agent.falcon.sheet.btxml import parse_xml
from afsim_research_agent.falcon.sheet.compiler import compile_sheet
from afsim_research_agent.falcon.sheet.generate import SheetContext, to_state
from afsim_research_agent.falcon.sheet.mermaid import parse_diagram
from afsim_research_agent.falcon.sheet.pyexpr import ExprCompiler, ExprSymbols
from afsim_research_agent.ir.catalog import load_catalog

ROOT = Path(__file__).resolve().parents[3]
GOLD = ROOT / "examples" / "armor_platoon_terrain_formation" / "tactic_sheet.md"
REQUEST = FalconRequest(requirement="x", model_id="armor_v2", crow_result="x")
RQ = [RequirementClause(id=f"rq_0{i}", text="t", kind="behavior") for i in range(1, 7)]


@pytest.fixture(scope="module")
def catalog():
    return load_catalog()


def _symbols(catalog) -> ExprSymbols:
    return ExprSymbols(
        catalog=catalog,
        parameters={"contact_range", "contact_hold_time"},
        conditions={"in_contact"},
        coordination={"coord_bounding": "bounding_overwatch"},
        roles={"platoon_leader"},
    )


# ---------------------------------------------------------------------- parsing
def test_python_conditions_become_ir_expressions(catalog) -> None:
    c = ExprCompiler(_symbols(catalog), "conditions", 1)
    assert c.parse("track.nearest_hostile_range <= contact_range") == {
        "compare": {
            "left": {"track_value": {"property": "nearest_hostile_range"}},
            "operator": "lte",
            "right": {"parameter_ref": "contact_range"},
        }
    }
    assert c.parse("not in_contact and time_in_state() >= contact_hold_time") == {
        "all": [
            {"not": {"condition_ref": "in_contact"}},
            {
                "function_call": {
                    "name": "time_in_state_gte",
                    "arguments": {"duration": {"parameter_ref": "contact_hold_time"}},
                }
            },
        ]
    }
    assert c.parse('terrain.class != "urban"')["compare"]["left"] == {
        "terrain_value": {"property": "class"}
    }
    assert c.parse('terrain.distance_to_class("urban") <= "200 m"')["compare"] == {
        "left": {"terrain_value": {"property": "distance_to_class", "class": "urban"}},
        "operator": "lte",
        "right": {"value": 200, "unit": "m"},
    }
    assert c.parse('sensor("vehicle_sight").on') == {
        "sensor_value": {"property": "on", "sensor": "vehicle_sight"}
    }
    assert c.parse("coord_bounding.my_turn or platoon_leader.bb.done") == {
        "any": [
            {"condition_ref": "coord_bounding__my_turn"},
            {"blackboard_value": "platoon_leader.mission.done"},
        ]
    }
    assert not c.problems
    assert c.parse("contact_rnge > 3") is None
    assert "contact_range" in c.problems[-1].candidates
    assert c.parse("__import__('os').system('x')") is None  # only whitelisted calls
    assert c.parse("track.nearest_hostile_range + 1 > 2") is None


def test_mermaid_priorities_sync_and_fallbacks() -> None:
    text = """stateDiagram-v2
  [*] --> a
  a --> b : first [sync]
  a --> c : not second
  b --> [*]
  a : 说明"""
    diagram, problems = parse_diagram(text, "r", "mermaid:r", 10)
    assert not problems and diagram.initial == "a" and diagram.terminal == {"b"}
    assert [(t.target, t.sync, t.line) for t in diagram.transitions] == [
        ("b", True, 12),
        ("c", False, 13),
    ]
    assert diagram.descriptions["a"] == "说明"
    _, problems = parse_diagram("a --> b", "r", "mermaid:r", 1)
    assert "缺少条件" in problems[0].message


def test_xml_refuses_entities_and_keeps_lines() -> None:
    root, problems = parse_xml(
        '<root>\n  <BehaviorTree ID="s">\n    <Halt/>\n  </BehaviorTree>\n</root>', "xml:r", 5
    )
    assert not problems and root.children[0].children[0].line == 7
    root, problems = parse_xml('<!DOCTYPE x [<!ENTITY e "boom">]><root>&e;</root>', "xml:r", 1)
    assert root is None and "DOCTYPE" in problems[0].message
    _, problems = parse_xml("<root><a></root>", "xml:r", 3)
    assert "XML 格式错误" in problems[0].message


def test_join_and_split_keep_line_numbers() -> None:
    blocks, _ = split_sheet(GOLD.read_text(encoding="utf-8"))
    text = join_sheet(blocks, "x")
    again, problems = split_sheet(text)
    assert not problems and [b.key for b in again] == [b.key for b in blocks]
    lines = text.splitlines()
    for block in again:
        assert lines[block.line - 1] == block.text.splitlines()[0]


# ---------------------------------------------------------------------- compiling
def _package(tmp: Path, result, catalog) -> tuple:
    pkg = EvidencePackage(package_id="p", crow=CrowRef(gate_version="g"))
    ctx = SheetContext(request=REQUEST, clauses=RQ, user_values=[], package=pkg, catalog=catalog)
    (tmp / "evidence_package.json").write_text(pkg.model_dump_json(), encoding="utf-8")
    assembled, notes = write_draft(to_state(ctx, result), tmp)
    return assembled, check_package(tmp, assembled.problems, with_l3=True)


def test_gold_sheet_compiles_to_a_passing_model(tmp_path, catalog) -> None:
    result = compile_sheet(
        GOLD.read_text(encoding="utf-8"), request=REQUEST, clauses=RQ, catalog=catalog
    )
    assert result.errors == []
    assembled, check = _package(tmp_path, result, catalog)
    assert check.ok, [p.render() for p in check.problems]
    assert all(t["passed"] for t in check.l2["tests"]) and len(check.l2["tests"]) == 11
    assert all(all(e["passed"] for e in s["expectations"]) for s in check.l3)
    data = assembled.data
    leader = data["roles"][0]
    open_state = leader["state_machine"]["states"][0]
    # written order → priority, [sync] → synchronize_with, fallback added
    assert [
        (t["to"], t["priority"], t.get("synchronize_with")) for t in open_state["transitions"]
    ] == [
        ("contact", 100, "coord_leader_follower"),
        ("travel_urban", 90, "coord_leader_follower"),
        ("travel_open", 0, None),
    ]
    # blackboard declarations inferred from SetBlackboard / bb.x
    assert leader["blackboards"][0]["variables"][0] == {
        "name": "current_formation",
        "type": "string",
        "default": "wedge",
    }
    behaviors = {b["id"]: b for b in data["behaviors"]}
    move = behaviors["follow_route_main_route_travel_speed_open"]
    assert move["success"] == {"expression": {"mover_value": {"property": "route_complete"}}}
    assert move["claims"] == ["movement_control"] and "precondition" in move
    assert data["scenarios"][0]["initial"]["wingman"] == {
        "state": "travel_open",
        "formation": "wedge",
    }
    assert set(data["scenarios"][0]["traces_to"]) >= {"wedge", "column", "coord_leader_follower"}
    # source map: every element points back at a line of the sheet
    assert result.source_map["condition:entering_urban"]["block"] == "conditions"
    assert result.source_map["node:leader_move_open"]["block"] == "xml:platoon_leader"


def test_holes_are_reported_with_lines_and_candidates(catalog) -> None:
    text = GOLD.read_text(encoding="utf-8")
    broken = (
        text.replace('speed="travel_speed_open"', 'speed="travel_sped_open"')
        .replace("<Halt/>", "<Hlt/>")
        .replace(
            '<Coordination use="coord_leader_follower.announce_formation"/>\n      <FollowRoute name="leader_move_urban"',
            '<Coordination use="coord_leader_follower.annonce"/>\n      <FollowRoute name="leader_move_urban"',
        )
    )
    result = compile_sheet(broken, request=REQUEST, clauses=RQ, catalog=catalog)
    messages = {p.message: p for p in result.errors}
    speed = next(p for m, p in messages.items() if "travel_sped_open" in m)
    assert speed.block == "xml:platoon_leader" and "travel_speed_open" in speed.candidates
    assert broken.splitlines()[speed.line - 1].strip().startswith("<FollowRoute")
    assert any("annonce" in m for m in messages)
    # the rest still compiles
    assert result.skeleton is not None and len(result.sections["conditions"].conditions) == 5


# ---------------------------------------------------------------------- drafting
def _blocks(*kinds: str, text: str | None = None) -> str:
    blocks, _ = split_sheet(text or GOLD.read_text(encoding="utf-8"))
    return "\n\n".join(b.fence() for b in blocks if b.kind in kinds)


def _block(kind: str, role: str) -> str:
    blocks, _ = split_sheet(GOLD.read_text(encoding="utf-8"))
    return next(b.fence() for b in blocks if b.kind == kind and b.role == role)


def _v2_script(*, broken: bool = False) -> dict:
    script = gold_script()
    gold = GOLD.read_text(encoding="utf-8")
    control_text = gold
    if broken:
        # a condition the state diagram needs is missing, and the wingman uses the
        # acknowledge behavior that belongs to the report receiver (IR-level error)
        control_text = gold.replace(
            'leaving_urban = not entering_urban and terrain.class != "urban"  # 已离开城区\n', ""
        ).replace(
            '<Coordination use="coord_contact_report.send_report"/>',
            '<Coordination use="coord_contact_report.acknowledge"/>',
        )
    script.update(
        {
            "sheet:structure": [_blocks("model", "mermaid")],
            "sheet:control": [_blocks("conditions", "xml", "basis", text=control_text)],
            "sheet:verification": [_blocks("verification")],
            "sheet:repair:conditions": [_blocks("conditions")],
            "sheet:repair:xml": [_block("xml", "wingman")],
        }
    )
    return script


def test_v2_draft_writes_sheet_and_valid_model(tmp_path) -> None:
    request = write_request(tmp_path, crow_result(), generator="v2")
    model = ScriptedModel(_v2_script())
    result = asyncio.run(run_draft(request, model))
    assert result.status == "draft_valid", result.problems
    pkg = result.package_dir
    for name in ("tactic_sheet.md", "source_map.json", "behavior_spec.yaml", "falcon_review.md"):
        assert (pkg / name).is_file()
    stages = [s for s, _ in model.calls]
    assert stages.count("sheet:structure") == 1 and "skeleton" not in stages
    assert not any(s.startswith("section:") for s in stages)
    trace = json.loads((pkg / "falcon_trace.json").read_text(encoding="utf-8"))
    assert trace["generator"] == "v2" and trace["sheet"]["holes"] == []
    assert "## 9. 战术稿（Falcon v2）" in (pkg / "falcon_review.md").read_text(encoding="utf-8")
    control_prompt = next(p for s, p in model.calls if s == "sheet:control")
    assert "需要定义的条件" in control_prompt and "FollowRoute(" in control_prompt


def test_v2_repairs_only_the_broken_blocks(tmp_path) -> None:
    request = write_request(tmp_path, crow_result(), generator="v2")
    model = ScriptedModel(_v2_script(broken=True))
    result = asyncio.run(run_draft(request, model))
    assert result.status == "draft_valid", result.problems
    stages = [s for s, _ in model.calls]
    assert "sheet:repair:conditions" in stages  # compile hole, before the verification draft
    assert stages.index("sheet:repair:conditions") < stages.index("sheet:verification")
    assert "sheet:repair:xml" in stages  # IR error mapped back to the wingman XML
    assert not any(s in stages for s in ("sheet:repair:model", "sheet:repair:mermaid"))
    xml_prompt = next(p for s, p in model.calls if s == "sheet:repair:xml")
    assert "```xml wingman```" in xml_prompt and "acknowledge" in xml_prompt
    cond_prompt = next(p for s, p in model.calls if s == "sheet:repair:conditions")
    assert "leaving_urban" in cond_prompt


def test_falcon_compile_rebuilds_from_an_edited_sheet(tmp_path) -> None:
    from afsim_research_agent.falcon.__main__ import main as falcon_main

    request = write_request(tmp_path, crow_result(), generator="v2")
    pkg = asyncio.run(run_draft(request, ScriptedModel(_v2_script()))).package_dir
    confirmations = pkg / "confirmations.yaml"
    data = yaml.safe_load(confirmations.read_text(encoding="utf-8"))
    target = next(i for i in data["items"] if i["kind"] == "parameter")
    target["decision"] = "confirm"
    header = confirmations.read_text(encoding="utf-8").split("items:")[0]
    confirmations.write_text(header + yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    sheet = pkg / "tactic_sheet.md"
    sheet.write_text(
        sheet.read_text(encoding="utf-8").replace("value: 50 m", "value: 60 m"), encoding="utf-8"
    )
    assert falcon_main(["compile", str(pkg)]) == 0
    spec = yaml.safe_load((pkg / "behavior_spec.yaml").read_text(encoding="utf-8"))
    assert spec["parameters"]["column_interval"]["value"] == 60
    kept = yaml.safe_load(confirmations.read_text(encoding="utf-8"))["items"]
    assert next(i for i in kept if i["id"] == target["id"])["decision"] == "confirm"


def test_clauses_must_be_covered_or_excused(catalog) -> None:
    clauses = [
        *RQ,
        RequirementClause(id="tc_01_x5", text="接替指挥", kind="condition", origin="tactic"),
    ]
    text = GOLD.read_text(encoding="utf-8")
    result = compile_sheet(text, request=REQUEST, clauses=clauses, catalog=catalog)
    assert any("tc_01_x5" in p.message for p in result.errors)
    excused = text.replace(
        "implements:\n", "out_of_scope:\n  tc_01_x5: 行为模型无法表达指挥权接替\nimplements:\n"
    )
    result = compile_sheet(excused, request=REQUEST, clauses=clauses, catalog=catalog)
    assert not result.errors
    package = EvidencePackage(package_id="p", crow=CrowRef(gate_version="g"))
    assert assemble(REQUEST, result.skeleton, result.sections, package).data


def test_tactical_logic_mistakes_from_the_first_online_run(catalog) -> None:
    """armor_tc01_sheet_v1: trees gated on their own transition condition, one
    leader_follower per formation, followers driving KeepStation themselves, speeds read
    from the blackboard, string arguments, ``continuous`` and undeclared messages."""
    gold = GOLD.read_text(encoding="utf-8")
    text = (
        gold.replace(
            '    <Sequence name="leader_open_root">\n',
            '    <Sequence name="leader_open_root">\n      <Condition ID="entering_urban"/>\n',
        )
        .replace(
            "  coord_bounding:\n",
            "  coord_twin:\n    pattern: leader_follower\n    leader: platoon_leader\n    followers: wingman\n"
            "    formation_from: platoon_leader.current_formation\n  coord_bounding:\n",
        )
        .replace(
            '<Coordination name="wingman_keep_station" use="coord_leader_follower.keep_station"/>',
            '<KeepStation leader="platoon_leader" formation="wedge"/>',
        )
        .replace('speed="travel_speed_urban"', 'speed="{urban_speed}"')
        .replace(
            '<SubTree name="leader_bounding" ID="coord_bounding.tree"/>',
            '<Sequence><FireWeapon weapon="main_gun" track="t1"/><SendMessage message="probe"/>'
            '<HoldOverwatchPosition continuous="true"/></Sequence>',
        )
        .replace(
            "comms: {platoon_net: voice_data}\n  wingman:",
            "comms: {platoon_net: voice_data}\n    weapons: {main_gun: direct_fire}\n  wingman:",
        )
    )
    result = compile_sheet(text, request=REQUEST, clauses=RQ, catalog=catalog)
    messages = [p.message for p in result.errors]
    assert any("entering_urban 是状态 travel_open 的转换条件" in m for m in messages)
    assert any("coord_twin 与 coord_leader_follower 是同一组" in m for m in messages)
    assert any('用 <Coordination use="coord_leader_follower.keep_station"/>' in m for m in messages)
    assert any("speed 端口不能读黑板变量 {urban_speed}" in m for m in messages)
    assert not any("continuous" in m for m in messages)
    assert any("自定义消息 probe" in w.message for w in result.warnings)
    behaviors = {b.id: b for b in result.sections["behaviors"].behaviors}
    fire = next(b for b in behaviors.values() if b.actions[0].command == "fire_weapon")
    assert fire.actions[0].arguments["track"] == {"literal": "t1"}
    hold = next(b for b in behaviors.values() if b.actions[0].command == "hold_overwatch_position")
    assert hold.continuous is True
    assert result.sections["structure"].messages[0].id == "probe"
