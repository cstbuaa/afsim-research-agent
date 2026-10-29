"""Falcon v2: behavior trees as Python functions (Code-BT style) and the closed
vocabulary from the capability catalog (unknown / unavailable nodes refused, ``needs``
→ capability gaps)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from afsim_research_agent.capability.constraints import vocabulary_constraints
from afsim_research_agent.capability.ir_check import check_ir
from afsim_research_agent.falcon.models import FalconRequest, RequirementClause
from afsim_research_agent.falcon.sheet import prompts as P
from afsim_research_agent.falcon.sheet.blocks import split_sheet
from afsim_research_agent.falcon.sheet.compiler import compile_sheet
from afsim_research_agent.falcon.sheet.generate import needs_document
from afsim_research_agent.falcon.sheet.pytree import parse_pytree
from afsim_research_agent.ir.catalog import load_catalog

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = ROOT / "examples" / "armor_platoon_terrain_formation"
GOLD = EXAMPLE / "tactic_sheet.md"
GOLD_PY = EXAMPLE / "tactic_sheet_python.md"  # formations + once(): generated blackboard use
# the same model with every blackboard write spelled out (still accepted)
GOLD_PY_EXPLICIT = Path(__file__).parent / "fixtures" / "tactic_sheet_python_explicit.md"
REQUEST = FalconRequest(requirement="x", model_id="armor_v2", crow_result="x")
RQ = [RequirementClause(id=f"rq_0{i}", text="t", kind="behavior") for i in range(1, 7)]


@pytest.fixture(scope="module")
def catalogs(capability_set):
    return load_catalog(), capability_set


def _compile(text: str, catalog, **kw):
    return compile_sheet(text, request=REQUEST, clauses=RQ, catalog=catalog, **kw)


def _dump(result) -> dict:
    return {k: v.model_dump(mode="json") for k, v in result.sections.items()}


# ---------------------------------------------------------------------- Python trees
def test_python_trees_compile_to_the_same_model_as_xml(catalogs) -> None:
    catalog, _ = catalogs
    xml = _compile(GOLD.read_text(encoding="utf-8"), catalog)
    py = _compile(GOLD_PY_EXPLICIT.read_text(encoding="utf-8"), catalog)
    assert xml.errors == [] and py.errors == [], [p.render() for p in py.errors]
    assert _dump(py)["control"] == _dump(xml)["control"]
    assert _dump(py)["behaviors"] == _dump(xml)["behaviors"]
    assert _dump(py)["conditions"] == _dump(xml)["conditions"]
    # source map points at the Python block and the line of the call
    where = py.source_map["node:leader_move_open"]
    assert where["block"] == "pytree:platoon_leader"
    line = GOLD_PY_EXPLICIT.read_text(encoding="utf-8").splitlines()[where["line"] - 1]
    assert line.strip().startswith('FollowRoute(name="leader_move_open"')


def test_python_statement_mapping() -> None:
    text = '''def s():
    """说明"""
    bb.flag = "x"
    if not ready:
        Halt()
    if armed:
        Wait(duration="3 s")
    else:
        Wait(duration=hold_time)
    try:
        MoveToPosition(position="p1", speed=v)
    except:
        Halt()
    with retry(3):
        FollowRoute(route="r", success_if="done")
        coord.go()
    helper()

def helper():
    assert ready
    coord.tree()
'''
    root, problems, failed = parse_pytree(text, "pytree:r", 10)
    assert not problems and not failed
    s, helper = root.children
    seq = s.children[0]
    assert seq.tag == "Sequence"
    tags = [c.tag for c in seq.children]
    assert tags == [
        "SetBlackboard",
        "ReactiveFallback",
        "ReactiveFallback",
        "Fallback",
        "RetryUntilSuccessful",
        "SubTree",
    ]
    assert seq.children[0].attrs == {"key": "flag", "value": "x"} and seq.children[0].line == 12
    if_only = seq.children[1]
    assert [c.tag for c in if_only.children] == ["Condition", "Halt"]
    assert if_only.children[0].attrs["ID"] == "ready"  # not not ready
    branches = seq.children[2].children
    assert [b.children[0].attrs["ID"] for b in branches] == ["armed", "not armed"]
    assert branches[1].children[1].attrs == {"duration": "hold_time"}
    retry = seq.children[4]
    assert retry.attrs == {"num_attempts": "3"} and retry.children[0].tag == "Sequence"
    follow, go = retry.children[0].children
    assert follow.attrs["_successIf"] == "done" and go.attrs == {"use": "coord.go"}
    assert [c.tag for c in helper.children[0].children] == ["Condition", "SubTree"]


def test_python_refuses_what_it_cannot_map() -> None:
    text = """import os

def s():
    while True:
        Halt()
    return

def t(x):
    Halt()

def u():
    FollowRoute("r")
    os.system("x")()
"""
    root, problems, failed = parse_pytree(text, "pytree:r", 1)
    messages = " / ".join(p.message for p in problems)
    assert "顶层只能写 def" in messages and "while/for" in messages and "return" in messages
    assert "不能有参数" in messages and "端口=值" in messages
    assert failed >= {"s", "u"}
    _, problems, _ = parse_pytree("def s(:\n  pass", "pytree:r", 5)
    assert problems[0].line == 5 and "语法错误" in problems[0].message


def test_a_failing_python_tree_is_a_hole_not_a_half_tree(catalogs) -> None:
    catalog, _ = catalogs
    text = GOLD_PY_EXPLICIT.read_text(encoding="utf-8").replace(
        "        coord_leader_follower.keep_station()\n",
        "        coord_leader_follower.keep_station()\n        while x:\n            Halt()\n",
    )
    result = _compile(text, catalog)
    keys = {(p.block, "while/for" in p.message) for p in result.errors}
    assert ("pytree:wingman", True) in keys
    assert not any("没有行为树" in p.message for p in result.errors)  # reported once
    assert "wingman_travel_urban_tree" in result.symbols["failed_trees"]


# ---------------------------------------------------------------------- closed vocabulary
def _ground(catalogs, classes=("ground_vehicle",)):
    catalog, capability = catalogs
    return vocabulary_constraints(catalog, capability, list(classes))


def test_unavailable_and_unknown_nodes_are_refused(catalogs) -> None:
    catalog, _ = catalogs
    constraints = _ground(catalogs)
    text = GOLD.read_text(encoding="utf-8").replace(
        '<FollowRoute name="leader_move_open" route="main_route" speed="travel_speed_open"/>',
        '<SetAltitude altitude="100 m"/>\n      <DeploySmoke/>',
    )
    plain = _compile(text, catalog)
    assert not any("SetAltitude" in p.message for p in plain.errors)  # no catalog: open
    result = _compile(text, catalog, constraints=constraints)
    altitude = next(p for p in result.errors if "SetAltitude" in p.message)
    assert "不具备 set_altitude" in altitude.message and "yaml needs" in altitude.message
    assert altitude.block == "xml:platoon_leader" and "FollowRoute" in altitude.candidates
    smoke = next(p for p in result.errors if "DeploySmoke" in p.message)
    assert "动作表之外" in smoke.message
    # a mixed model: set_altitude exists, but only for the air role
    mixed = _ground(catalogs, ("ground_vehicle", "air_vehicle"))
    result = _compile(text, catalog, constraints=mixed)
    assert any("不具备 set_altitude" in p.message for p in result.errors)


def test_reads_are_checked_per_role(catalogs) -> None:
    catalog, capability = catalogs
    constraints = vocabulary_constraints(catalog, capability, ["ground_vehicle"])
    constraints.tiers["terrain_value.class"] = "unavailable"  # as if the catalog lacked it
    result = _compile(GOLD.read_text(encoding="utf-8"), catalog, constraints=constraints)
    reads = [p for p in result.errors if "terrain_value.class" in p.message]
    assert reads and all(p.block == "conditions" for p in reads)
    assert any("platoon_leader 使用" in p.message for p in reads)


def test_action_table_comes_from_the_capability_catalog(catalogs) -> None:
    catalog, _ = catalogs
    constraints = _ground(catalogs)
    table = P.action_table(catalog, constraints, "python")
    assert "FollowRoute(route=route!, speed=speed)" in table
    assert "目录：" in table and "机动：" in table
    assert "SetAltitude(" not in table.split("（平台不具备")[0]
    assert "平台不具备、未列出：SetAltitude" in table
    xml = P.action_table(catalog, constraints, "xml")
    assert '<FollowRoute route="route!" speed="speed"/>' in xml
    prompt = P.control_guide("python").format(namespaces="", functions="")
    assert "def 状态名()" in prompt and "yaml needs" in prompt
    gaps = [{"id": "cap-gap-0123456789", "display_name": "隐蔽阵地判断", "gap_type": "develop"}]
    assert "cap-gap-0123456789：隐蔽阵地判断" in P.known_gaps_block(gaps)


# ---------------------------------------------------------------------- needs → gaps
NEEDS = """```yaml needs
smoke_cover:
  need: 施放烟幕掩护交替跃进
  used_by: [platoon_leader.contact]
  substitute: 不施放烟幕，直接交替掩护
  clauses: [rq_06]
concealment:
  need: 判断隐蔽阵地
  gap: cap-gap-0123456789
  used_by: [wingman]
  substitute: 原地停车
```
"""
CROW = [
    {
        "id": "cap-gap-0123456789",
        "contract": "terrain.concealment:query",
        "display_name": "隐蔽阵地判断",
        "gap_type": "develop",
        "used_by": [{"candidate": "tc_01"}],
    }
]


def test_needs_become_capability_gaps(tmp_path, catalogs) -> None:
    catalog, capability = catalogs
    text = GOLD.read_text(encoding="utf-8") + "\n" + NEEDS
    result = _compile(text, catalog, known_gaps=CROW)
    assert result.errors == [], [p.render() for p in result.errors]
    needs = {n["id"]: n for n in result.needs}
    assert needs["concealment"]["gap_id"] == "cap-gap-0123456789"
    assert needs["concealment"]["contract"] == "terrain.concealment:query"
    assert needs["smoke_cover"]["gap_id"].startswith("cap-gap-")
    assert needs["smoke_cover"]["roles"] == ["platoon_leader"]
    doc = yaml.safe_load(needs_document(result))
    assert doc["format"] == "SheetCapabilityNeeds" and len(doc["needs"]) == 2
    # the IR capability check lists them as gaps, matched against Crow's estimate
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    from test_falcon_sheet import _package  # the shared compile → package helper

    _assembled, check = _package(pkg, result, catalog)
    report = check_ir(
        check.ir, capability, crow_gaps=CROW, crow_source="x", sheet_needs=result.needs
    )
    by_id = {g.id: g for g in report.gaps}
    smoke = by_id[needs["smoke_cover"]["gap_id"]]
    assert smoke.source == "falcon_sheet" and smoke.gap_type == "develop"
    assert smoke.used_at == ["platoon_leader.contact"] and "直接交替掩护" in smoke.alternatives[0]
    assert by_id["cap-gap-0123456789"].source == "crow_estimate+falcon_sheet"
    assert report.level == "needs_development" and report.counts["sheet_needs"] == 2
    crow_row = report.crow_estimate[0]
    assert crow_row.in_ir == "confirmed" and "战术稿" in crow_row.note


def test_needs_entries_are_validated(catalogs) -> None:
    catalog, _ = catalogs
    bad = """```yaml needs
a: {need: x, used_by: [nobody]}
b: {used_by: [wingman]}
c: {need: y, gap: cap-gap-ffffffffff, used_by: [wingman]}
d: {need: z, used_by: [wingman], clauses: [rq_99]}
e: {need: w, used_by: [wingman]}
```
"""
    result = _compile(GOLD.read_text(encoding="utf-8") + "\n" + bad, catalog)
    messages = [p.message for p in result.errors]
    assert any("needs.a 的 used_by" in m for m in messages)
    assert any("needs.b 缺少 need" in m for m in messages)
    assert any("不是所选战术已识别的能力缺口" in m for m in messages)
    assert any("rq_99" in m for m in messages)
    assert [n["id"] for n in result.needs] == ["e"]
    assert any("needs.e 没有写 substitute" in p.message for p in result.warnings)


def test_needs_cover_clauses(catalogs) -> None:
    catalog, _ = catalogs
    clauses = [*RQ, RequirementClause(id="rq_07", text="施放烟幕", kind="behavior")]
    text = GOLD.read_text(encoding="utf-8")
    result = compile_sheet(text, request=REQUEST, clauses=clauses, catalog=catalog)
    assert any("rq_07" in p.message for p in result.errors)
    needs = "```yaml needs\nsmoke: {need: 施放烟幕, used_by: [platoon_leader], substitute: 不施放, clauses: [rq_07]}\n```\n"
    result = compile_sheet(text + "\n" + needs, request=REQUEST, clauses=clauses, catalog=catalog)
    assert not any("rq_07" in p.message for p in result.errors)


def test_block_kinds_for_python_trees_and_needs() -> None:
    blocks, problems = split_sheet(
        "```python conditions\na = true\n```\n```python wingman\ndef s():\n  Halt()\n```\n"
        "```yaml needs\nx: {need: y, used_by: [wingman]}\n```\n```python\nb = true\n```\n"
    )
    kinds = {b.key for b in blocks}
    assert {"conditions", "pytree:wingman", "needs"} <= kinds
    assert next(b for b in blocks if b.key == "pytree:wingman").label == "python wingman"
    assert any("重复" in p.message for p in problems)  # bare python = conditions again


# ---------------------------------------------------------------------- drafting
def test_v2_python_draft_repairs_python_blocks_and_writes_needs(tmp_path, capability_set) -> None:
    import asyncio
    import json

    from falcon_fixtures import crow_result, write_request
    from test_falcon_sheet import _v2_script

    from afsim_research_agent.falcon.llm import ScriptedModel
    from afsim_research_agent.falcon.pipeline import run_draft
    from afsim_research_agent.ir.pipeline import build_package

    # the scripted structure stage is the XML gold's model block (formation_from), so the
    # trees are the explicit-style ones that write the formation themselves
    py_blocks, _ = split_sheet(GOLD_PY_EXPLICIT.read_text(encoding="utf-8"))
    gold, _ = split_sheet(GOLD.read_text(encoding="utf-8"))
    control = [b for b in gold if b.kind in ("conditions", "basis")]
    wingman = next(b for b in py_blocks if b.key == "pytree:wingman")
    leader = next(b for b in py_blocks if b.key == "pytree:platoon_leader")
    broken = wingman.fence().replace("send_report()", "send_reprt()")
    needs = NEEDS.replace("rq_06", "rq_01").split("concealment:")[0] + "```\n"
    script = _v2_script()
    script["sheet:control"] = [
        "\n\n".join([*(b.fence() for b in control), leader.fence(), broken, needs])
    ]
    script["sheet:repair:pytree"] = [wingman.fence()]
    request = write_request(tmp_path, crow_result(), generator="v2")
    model = ScriptedModel(script)
    result = asyncio.run(run_draft(request, model))
    assert result.status == "draft_valid", result.problems
    stages = [s for s, _ in model.calls]
    assert "sheet:repair:pytree" in stages and "sheet:repair:xml" not in stages
    repair = next(p for s, p in model.calls if s == "sheet:repair:pytree")
    assert "```python wingman```" in repair and "send_reprt" in repair and "def 状态名()" in repair
    pkg = result.package_dir
    sheet = (pkg / "tactic_sheet.md").read_text(encoding="utf-8")
    assert "```python wingman" in sheet and "```yaml needs" in sheet
    needs_doc = yaml.safe_load((pkg / "capability_needs.yaml").read_text(encoding="utf-8"))
    assert [n["id"] for n in needs_doc["needs"]] == ["smoke_cover"]
    gaps = yaml.safe_load((pkg / "capability_gaps.yaml").read_text(encoding="utf-8"))
    assert any(g["source"] == "falcon_sheet" for g in gaps["gaps"])
    review = (pkg / "falcon_review.md").read_text(encoding="utf-8")
    assert "☆稿" in review and "词表外能力需求" in review
    assert "单步测试的机械结果" in review
    observed = yaml.safe_load((pkg / "observed_expectations.yaml").read_text(encoding="utf-8"))
    assert observed["format"] == "ObservedTestExpectations" and observed["tests"]
    trace = json.loads((pkg / "falcon_trace.json").read_text(encoding="utf-8"))
    assert trace["sheet"]["needs"][0]["need"] == "施放烟幕掩护交替跃进"
    # ir build picks the needs up from the package by itself
    build = build_package(pkg, draft=True, run_l3=False)
    rebuilt = yaml.safe_load((pkg / "capability_gaps.yaml").read_text(encoding="utf-8"))
    assert any(g["source"] == "falcon_sheet" for g in rebuilt["gaps"]), build


def test_falcon_compile_keeps_the_closed_vocabulary(tmp_path, capability_set) -> None:
    import asyncio
    import json

    from falcon_fixtures import crow_result, write_request
    from test_falcon_sheet import _v2_script

    from afsim_research_agent.falcon.__main__ import main as falcon_main
    from afsim_research_agent.falcon.llm import ScriptedModel
    from afsim_research_agent.falcon.pipeline import run_draft

    request = write_request(tmp_path, crow_result(), generator="v2")
    pkg = asyncio.run(run_draft(request, ScriptedModel(_v2_script()))).package_dir
    sheet = pkg / "tactic_sheet.md"
    sheet.write_text(
        sheet.read_text(encoding="utf-8").replace(
            '<FollowRoute name="leader_move_open"',
            '<SetAltitude altitude="1 km"/>\n<FollowRoute name="leader_move_open"',
        ),
        encoding="utf-8",
    )
    assert falcon_main(["compile", str(pkg)]) != 0
    report = json.loads((pkg / "validation_report.json").read_text(encoding="utf-8"))
    assert any("不具备 set_altitude" in p for p in report["problems"])


# ---------------------------------------------------------------------- first Python online run
def test_checks_from_the_first_python_online_run(catalogs) -> None:
    catalog, _ = catalogs
    constraints = _ground(catalogs)
    gold = GOLD_PY.read_text(encoding="utf-8")
    text = (
        gold.replace("speed=travel_speed_open", "speed=column_interval")
        .replace(
            "  travel_open --> contact : in_contact [sync]\n",
            "  travel_open --> contact : in_contact [sync]\n  travel_open --> contact : in_contact\n",
        )
        .replace(
            'FollowRoute(name="leader_move_urban"',
            'SetHeading(heading=self.heading)\n        FollowRoute(name="leader_move_urban"',
        )
    )
    result = _compile(text, catalog, constraints=constraints)
    messages = [p.message for p in result.errors]
    speed = next(p for p in result.errors if "speed 端口不能用 column_interval" in p.message)
    assert speed.block == "pytree:platoon_leader" and "travel_speed_open" in speed.candidates
    assert any("写了两次" in m for m in messages)
    assert any("运行时属性 self.heading" in m for m in messages)
    # the gold wingman's own contact transition (it sees the enemy itself) is only a hint
    plain = _compile(gold, catalog, constraints=constraints)
    assert plain.errors == [] and any(
        "也由 platoon_leader 的 [sync]" in p.message for p in plain.warnings
    )


def test_needs_for_vocabulary_items_and_test_inputs_are_refused(catalogs) -> None:
    catalog, capability = catalogs
    constraints = _ground(catalogs)
    terrain = {
        "id": "cap-gap-4935405097",
        "contract": "ir:terrain_value.class",
        "display_name": "当前地形类别",
        "gap_type": "develop",
    }
    assert constraints.element_for(terrain["contract"]) == "terrain_value.class"
    needs = (
        "```yaml needs\nterrain: {need: 当前地形类别, gap: cap-gap-4935405097, "
        "used_by: [platoon_leader], substitute: 固定路线点}\n```\n"
    )
    text = GOLD.read_text(encoding="utf-8") + "\n" + needs
    result = _compile(text, catalog, constraints=constraints, known_gaps=[terrain])
    assert any("词表中已有：terrain_value.class" in p.message for p in result.errors)
    block = P.known_gaps_block([terrain, CROW[0]], constraints)
    assert "词表已有 terrain_value.class" in block and "cap-gap-0123456789" in block
    # a condition name used as a test's blackboard input; event ids are normalized
    gold = GOLD.read_text(encoding="utf-8")
    broken = gold.replace(
        "given: {terrain: {distance_to_class.urban: 150 m}}",
        "given: {terrain: {distance_to_class.urban: 150 m}, blackboard: {in_contact: true}}",
        1,
    )
    broken = broken.replace("id: leader_near_urban", "id: LeaderNearUrban").replace(
        "after: leader_near_urban", "after: LeaderNearUrban"
    )
    result = _compile(broken, catalog)
    assert any("是条件名，不是黑板变量" in p.message for p in result.errors)
    events = [
        e["id"]
        for s in result.sections["verification"].model_dump()["scenarios"]
        for e in s["events"]
    ]
    assert "leadernearurban" in events


def test_harmless_spellings_from_the_max_run_are_accepted(catalogs, tmp_path) -> None:
    catalog, _ = catalogs
    text = (
        GOLD.read_text(encoding="utf-8")
        .replace("intent: formation_transition", "intent: [formation_transition]")
        .replace("rq_05: [coord_bounding]", "rq_05: [coord_bounding.bound, coord_bounding.my_turn]")
        .replace(
            "rq_04: [coord_contact_report, wingman.contact]",
            'rq_04: ["not coord_contact_report.report_received", wingman.contact]',
        )
        .replace(
            "observable: min_interval, role: wingman, gte: 10 m",
            "observable: min_interval, formation: wedge, gte: 10 m",
        )
    )
    result = _compile(text, catalog)
    assert result.errors == [], [p.render() for p in result.errors]
    from test_falcon_sheet import _package

    _assembled, check = _package(tmp_path, result, catalog)
    assert check.ok, [p.render() for p in check.problems]


def test_mover_flags_and_bad_role_fields_do_not_crash(catalogs) -> None:
    catalog, _ = catalogs
    gold = GOLD.read_text(encoding="utf-8")
    text = gold.replace(
        "    platform: ground_vehicle\n", "    platform: ground_vehicle\n    mover: true\n", 1
    )
    result = _compile(text, catalog)
    assert result.errors == []
    leader = result.skeleton.roles[0]
    assert leader.mover_capability == "route_following_ground"
    text = gold.replace(
        "    platform: ground_vehicle\n",
        "    platform: ground_vehicle\n    mover: 3\n    superior: [a, b]\n",
        1,
    )
    result = _compile(text, catalog)  # holes, not an exception
    assert any("superior" in p.message for p in result.errors)
    assert any("mover" in p.message for p in result.warnings)


def test_test_inputs_written_like_the_max_run(catalogs, tmp_path) -> None:
    catalog, _ = catalogs
    gold = GOLD.read_text(encoding="utf-8")
    text = gold.replace(
        "{coord_leader_follower.transition: {to_state: travel_urban}}",
        "{coord_leader_follower.transition: {from: travel_open, to: travel_urban}}",
    ).replace(
        "actions: [ReportContact], messages: [coord_contact_report.report]",
        "actions: [coord_contact_report.send_report], messages: [coord_contact_report.report]",
    )
    assert text != gold
    result = _compile(text, catalog)
    assert result.errors == []
    from test_falcon_sheet import _package

    _assembled, check = _package(tmp_path, result, catalog)
    assert check.ok, [p.render() for p in check.problems]


# ---------------------------------------------------------------------- intent-only tests (方案 1)
def test_sheet_tests_state_intent_and_mechanics_are_recorded(catalogs, tmp_path) -> None:
    from test_falcon_sheet import _package

    from afsim_research_agent.falcon.sheet.generate import record_observed

    catalog, _ = catalogs
    gold = GOLD.read_text(encoding="utf-8")
    first = _compile(gold, catalog)
    # what the sheet wrote about actions / messages is kept aside, not checked
    assert first.symbols["written_mechanics"]
    assert all(
        t.expect.issued_actions is None and t.expect.sent_messages is None
        for t in first.sections["verification"].tests
    )
    pkg = tmp_path / "a"
    pkg.mkdir()
    _assembled, check = _package(pkg, first, catalog)
    assert check.ok
    observed = record_observed({}, check.l2)
    assert len(observed) == len(check.l2["tests"])
    some = next(iter(observed))
    assert set(observed[some]) == {"tree_status", "issued_actions", "sent_messages"}
    # recorded results are then checked strictly; an edited (wrong) one fails
    again = _compile(gold, catalog, observed=observed)
    pkg2 = tmp_path / "b"
    pkg2.mkdir()
    _assembled, check = _package(pkg2, again, catalog)
    assert check.ok
    wrong = {k: dict(v) for k, v in observed.items()}
    wrong[some]["tree_status"] = "failure" if wrong[some]["tree_status"] != "failure" else "success"
    pkg3 = tmp_path / "c"
    pkg3.mkdir()
    _assembled, check = _package(pkg3, _compile(gold, catalog, observed=wrong), catalog)
    assert not check.ok


def test_missing_test_inputs_are_named(catalogs) -> None:
    catalog, _ = catalogs
    text = GOLD.read_text(encoding="utf-8").replace(
        "given: {terrain: {distance_to_class.urban: 150 m}}\n    expect: {next_state: travel_urban",
        "given: {tracks: {hostile_count: 0}}\n    expect: {next_state: travel_urban",
    )
    result = _compile(text, catalog)
    hints = result.symbols["test_inputs"]["leader_switches_to_column_near_urban"]
    assert "given.terrain.distance_to_class" in hints
    assert (
        "leader_switches_to_column_near_urban"
        not in _compile(GOLD.read_text(encoding="utf-8"), catalog).symbols["test_inputs"]
    )


def test_falcon_compile_checks_edited_recorded_results(tmp_path) -> None:
    import asyncio

    from falcon_fixtures import crow_result, write_request
    from test_falcon_sheet import _v2_script

    from afsim_research_agent.falcon.__main__ import main as falcon_main
    from afsim_research_agent.falcon.llm import ScriptedModel
    from afsim_research_agent.falcon.pipeline import run_draft

    request = write_request(tmp_path, crow_result(), generator="v2")
    pkg = asyncio.run(run_draft(request, ScriptedModel(_v2_script()))).package_dir
    path = pkg / "observed_expectations.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert falcon_main(["compile", str(pkg)]) == 0
    first = next(iter(doc["tests"]))
    doc["tests"][first]["issued_actions"] = ["halt"]  # a person tightens / edits a result
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    assert falcon_main(["compile", str(pkg)]) != 0
    del doc["tests"][first]  # removed → recorded again from the next run
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    assert falcon_main(["compile", str(pkg)]) == 0
    again = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert first in again["tests"]


def test_count_observables_need_what_they_count(catalogs) -> None:
    catalog, _ = catalogs
    text = GOLD.read_text(encoding="utf-8").replace(
        "observable: min_interval, role: wingman, gte: 10 m",
        "observable: messages_sent, role: wingman, gte: 1",
    )
    result = _compile(text, catalog)
    problem = next(p for p in result.errors if "messages_sent 必须写 message" in p.message)
    assert "coord_contact_report.report" in problem.candidates


def test_function_inputs_are_named(catalogs) -> None:
    catalog, _ = catalogs
    gold = GOLD.read_text(encoding="utf-8")
    text = gold.replace(
        "given: {terrain: {distance_to_class.urban: 150 m}}\n    expect: {next_state: travel_urban",
        "given: {terrain: {distance_to_class.urban: 150 m}}\n    expect: {next_state: travel_urban",
    )
    compiler_mod = __import__(
        "afsim_research_agent.falcon.sheet.compiler", fromlist=["FUNCTION_INPUTS", "given_path"]
    )
    assert compiler_mod.given_path("track_value.current_target_range", {}) == (
        "tracks",
        "current_target_range",
    )
    assert compiler_mod.given_path("elapsed.state", {}) == ("elapsed", "state")
    assert "range_to_current_target_lte" in compiler_mod.FUNCTION_INPUTS
    assert _compile(text, catalog).errors == []


def test_checks_from_the_py5_run(catalogs) -> None:
    from afsim_research_agent.falcon.sheet.locate import locate

    catalog, _ = catalogs
    gold = GOLD.read_text(encoding="utf-8")
    # a self-loop in the state diagram
    text = gold.replace(
        "  contact --> travel_open : contact_cleared [sync]\n",
        "  contact --> travel_open : contact_cleared [sync]\n  contact --> contact : not contact_cleared\n",
    )
    assert any("回到本状态" in p.message for p in _compile(text, catalog).errors)
    # the receiver of the report waiting for the acknowledgement it sends itself
    text = gold.replace(
        '<Condition ID="coord_contact_report.report_received"/>',
        '<Condition ID="coord_contact_report.ack_received"/>',
    )
    wrong = next(
        p for p in _compile(text, catalog).errors if "ack_received 只有 wingman" in p.message
    )
    assert "coord_contact_report.report_received" in wrong.candidates
    # "a transition no test exercises" goes to the tests, not to the state diagram
    assert locate(
        "roles.r.transitions.t",
        "control",
        {},
        {},
        ["verification", "mermaid:r"],
        "transition 't' is not exercised by any L2 test",
    ) == ("verification", None)


def test_generated_condition_flags_become_messages(catalogs) -> None:
    catalog, _ = catalogs
    gold = GOLD.read_text(encoding="utf-8")
    test = (
        "  - id: leader_acks_report\n    category: nominal\n    role: platoon_leader\n"
        "    state: contact\n"
        "    given: {coordination: {coord_contact_report: {report_received: true}}}\n"
        "    expect: {next_state: contact}\n"
    )
    text = gold.replace("tests:\n", "tests:\n" + test, 1)
    result = _compile(text, catalog)
    assert result.errors == [], [p.render() for p in result.errors]
    case = next(t for t in result.sections["verification"].tests if t.id == "leader_acks_report")
    assert case.given.messages == [{"message": "coord_contact_report__report", "fields": {}}]
    assert "coord_contact_report" not in case.given.coordination


# ---------------------------------------------------------------------- task-neutral prompts
UAV = ROOT / "examples" / "uav_route_recon" / "tactic_sheet.md"
ARMOR_TERMS = (
    "僚车",
    "长车",
    "排长",
    "全排",
    "wingman",
    "platoon_leader",
    "vehicle_sight",
    "platoon_net",
    "main_gun",
    "travel_open",
)


def test_a_different_task_compiles_and_passes(catalogs, tmp_path) -> None:
    from test_falcon_sheet import _package

    catalog, _ = catalogs
    result = _compile(UAV.read_text(encoding="utf-8"), catalog)
    assert result.errors == [], [p.render() for p in result.errors]
    _assembled, check = _package(tmp_path, result, catalog)
    assert check.ok, [p.render() for p in check.problems]
    assert all(all(e["passed"] for e in s["expectations"]) for s in check.l3)


def test_prompts_for_a_different_task_carry_no_armor_specifics(catalogs) -> None:
    from afsim_research_agent.falcon.fill import OBSERVABLES
    from afsim_research_agent.falcon.models import CrowRef, EvidencePackage, PlatformHint
    from afsim_research_agent.falcon.prompts import requirements_prompt
    from afsim_research_agent.falcon.sheet.generate import (
        STRUCTURE_EXAMPLE,
        SheetContext,
        compile_blocks,
        guide_for,
        usage,
    )

    catalog, capability = catalogs
    request = FalconRequest(
        requirement="两架无人机沿路线侦察，发现目标向地面站报告",
        model_id="uav",
        crow_result="x",
        platforms=[
            PlatformHint(role_hint="scout", capability_class="air_vehicle"),
            PlatformHint(role_hint="ground_station", capability_class="static_node"),
        ],
    )
    constraints = vocabulary_constraints(catalog, capability, ["air_vehicle", "static_node"])
    package = EvidencePackage(package_id="p", crow=CrowRef(gate_version="g"))
    ctx = SheetContext(
        request=request,
        clauses=RQ,
        user_values=[],
        package=package,
        catalog=catalog,
        constraints=constraints,
    )
    blocks, _ = split_sheet(UAV.read_text(encoding="utf-8"))
    struct_text, struct = compile_blocks(
        ctx, [b for b in blocks if b.kind in ("model", "mermaid")], "structure"
    )
    full_text, full = compile_blocks(ctx, blocks, "full")
    prompts = {
        "requirements": requirements_prompt(request.requirement, []),
        "structure": P.structure_prompt(
            requirement=request.requirement,
            clauses=[],
            tactic="",
            claims=[],
            platforms=[],
            catalog=catalog,
            intents=[],
            concepts=[],
            terrain=[],
            example=STRUCTURE_EXAMPLE,
        ),
        "control": P.control_prompt(
            sheet=struct_text,
            symbols=struct.symbols,
            usage=usage(ctx, struct),
            catalog=catalog,
            constraints=constraints,
            claims=[],
            tactic="",
        ),
        "verification": P.verification_prompt(
            sheet=full_text, symbols=full.symbols, usage=usage(ctx, full), observables=OBSERVABLES
        ),
        "repair": guide_for("pytree", ctx, full.symbols)
        + guide_for("verification", ctx, full.symbols),
    }
    for name, text in prompts.items():
        found = [t for t in ARMOR_TERMS if t in text]
        assert not found, (name, found)
    # only the rules of the coordination patterns this model uses
    assert "report_and_wait：reporter 用" in prompts["control"]
    assert "keep_station 保持站位" not in prompts["control"]
    assert "bounding_overwatch：" not in prompts["control"]
    assert "ack_received" in prompts["verification"] and "to_state" not in prompts["verification"]
    # the model's own component IDs are offered
    assert "eo_camera" in prompts["control"] and "uplink" in prompts["control"]


# ---------------------------------------------------------------------- py6 run: layered checks
def test_untested_transitions_are_listed_before_l2(catalogs) -> None:
    catalog, _ = catalogs
    gold = GOLD.read_text(encoding="utf-8")
    assert _compile(gold, catalog).symbols["untested"] == []
    # drop the only test of travel_urban -> travel_open
    text = gold.replace(
        "expect: {next_state: travel_open, transition: travel_urban -> travel_open}",
        "expect: {next_state: travel_open}",
    )
    result = _compile(text, catalog)
    error = next(p for p in result.errors if "没有单步测试" in p.message)
    assert error.block == "verification"
    assert (
        "travel_urban -> travel_open" in error.message and "需设定 given.terrain" in error.message
    )
    assert result.symbols["untested"] == ["platoon_leader_travel_urban_to_travel_open"]
    # the verification prompt lists every transition a test must fire
    checklist = P.transition_checklist(result.symbols)
    assert (
        "必须测试的转换" in checklist and "platoon_leader: travel_urban -> travel_open" in checklist
    )
    prompt = P.verification_prompt(
        sheet="", symbols=result.symbols, usage=None, observables="role_state"
    )
    assert checklist.strip() in prompt


def test_generated_condition_in_given_blackboard_becomes_a_message(catalogs) -> None:
    catalog, _ = catalogs
    gold = GOLD.read_text(encoding="utf-8")
    test = (
        "  - id: leader_gets_report\n    category: nominal\n    role: platoon_leader\n"
        "    state: contact\n"
        "    given: {blackboard: {coord_contact_report.report_received: true,"
        " coord_contact_report.ack_received: false}}\n"
        "    expect: {next_state: contact}\n"
    )
    result = _compile(gold.replace("tests:\n", "tests:\n" + test, 1), catalog)
    assert result.errors == [], [p.render() for p in result.errors]
    assert sum("不是黑板变量" in w.message for w in result.warnings) == 2
    case = next(t for t in result.sections["verification"].tests if t.id == "leader_gets_report")
    assert case.given.messages == [{"message": "coord_contact_report__report", "fields": {}}]
    assert not case.given.blackboard


def test_scenario_thresholds_need_the_observables_dimension(catalogs, tmp_path) -> None:
    from test_falcon_sheet import _package

    catalog, _ = catalogs
    gold = GOLD.read_text(encoding="utf-8")
    text = gold.replace(
        "observable: min_interval, role: wingman, gte: 10 m",
        "observable: min_interval, role: wingman, gte: 0 s",
    )
    (tmp_path / "a").mkdir()
    _assembled, check = _package(tmp_path / "a", _compile(text, catalog), catalog)
    assert any(p.code == "UNIT001" and "is a distance" in p.message for p in check.problems)
    text = gold.replace(
        "observable: min_interval, role: wingman, gte: 10 m",
        "observable: min_interval, role: wingman, gte: 0 m",
    )
    (tmp_path / "b").mkdir()
    _assembled, check = _package(tmp_path / "b", _compile(text, catalog), catalog)
    assert any("always holds" in p.message for p in check.problems)
    # only the scenarios fail L1: the L2 tests still run, so their results are visible too
    assert check.l2 is not None and check.l2["passed"]
    assert check.l3 is None


def test_repairs_that_reach_l2_are_not_undone() -> None:
    from types import SimpleNamespace as NS

    from afsim_research_agent.falcon.sheet.generate import _score

    compiled = NS(errors=[])
    stopped = NS(loaded=True, l2=None, problems=[1, 2, 3])
    ran = NS(loaded=True, l2={"tests": []}, problems=list(range(14)))
    unloaded = NS(loaded=False, l2=None, problems=[1])
    # more problems after L2 runs still counts as progress
    assert _score(compiled, ran) < _score(compiled, stopped) < _score(compiled, unloaded)
    prompt = P.repair_prompt(
        label="yaml verification",
        numbered="1 tests: []",
        problems=["- a"],
        sheet="",
        symbols={},
        usage=None,
        guide="",
        reverted=["- b"],
    )
    assert "那次修改已撤回" in prompt and "- b" in prompt


def test_a_failing_test_names_what_the_fired_transition_reads() -> None:
    from afsim_research_agent.falcon.sheet.generate import _fired_hint

    symbols = {
        "transition_checklist": [
            {
                "id": "r_a_to_b",
                "role": "r",
                "transition": "a -> b",
                "when": "near_urban",
                "inputs": ["given.terrain.distance_to_class"],
            }
        ],
        "test_given": {
            "t1": ["given.terrain.distance_to_class.urban", "given.components.eye.available"]
        },
    }
    message = (
        "next_state: expected 'a', got 'b'; fired_transition: expected 'r_a_stay', got 'r_a_to_b'"
    )
    hint = _fired_hint(message, "t1", symbols)
    assert "实际触发了 a -> b" in hint and "given.components.eye.available" in hint
    assert "distance_to_class.urban" not in hint.split("测试设定的")[1]


# ---------------------------------------------------------------------- py7 run: test inputs
def test_test_inputs_written_like_the_py7_run(catalogs, tmp_path) -> None:
    from test_falcon_sheet import _package

    catalog, _ = catalogs
    gold = GOLD.read_text(encoding="utf-8")
    test = (
        "  - id: leader_near_urban_nested\n    category: nominal\n    role: platoon_leader\n"
        "    state: travel_open\n"
        "    given:\n"
        "      terrain:\n        distance_to_class:\n          urban: 150 m\n"
        "      blackboard: {team.formation_error_rms: 5 m}\n"
        "      messages:\n        - coord_leader_follower.transition: {to_state: travel_urban}\n"
        "    expect: {next_state: travel_urban, transition: travel_open -> travel_urban}\n"
    )
    result = _compile(gold.replace("tests:\n", "tests:\n" + test, 1), catalog)
    assert result.errors == [], [p.render() for p in result.errors]
    warnings = " ".join(w.message for w in result.warnings)
    assert "已移到 given.team.formation_error_rms" in warnings
    assert "platoon_leader 收不到 coord_leader_follower.transition" in warnings
    case = next(
        t for t in result.sections["verification"].tests if t.id == "leader_near_urban_nested"
    )
    assert "distance_to_class.urban" in case.given.terrain
    assert "formation_error_rms" in case.given.team
    assert not case.given.messages and not case.given.blackboard
    _assembled, check = _package(tmp_path, result, catalog)
    assert check.ok, [p.render() for p in check.problems]


def test_a_test_whose_inputs_crash_the_tick_fails_alone(catalogs, tmp_path) -> None:
    from test_falcon_sheet import _package

    from afsim_research_agent.ir.l2 import run_tests

    catalog, _ = catalogs
    result = _compile(GOLD.read_text(encoding="utf-8"), catalog)
    _assembled, check = _package(tmp_path, result, catalog)
    ir = check.ir
    bad = dict(ir["tests"][1])
    bad["id"] = "bad_input"
    bad["given"] = {**bad["given"], "terrain": {"distance_to_class": {"urban": 150.0}}}
    report = run_tests({**ir, "tests": [*ir["tests"], bad]})
    outcome = next(o for o in report.outcomes if o.test_id == "bad_input")
    assert not outcome.passed and "could not run" in outcome.failures[0]
    assert sum(o.passed for o in report.outcomes) == len(ir["tests"])


# ---------------------------------------------------------------------- generated blackboard use
def test_formations_and_once_replace_hand_written_blackboard(catalogs, tmp_path) -> None:
    from test_falcon_sheet import _package

    catalog, _ = catalogs
    text = GOLD_PY.read_text(encoding="utf-8")
    assert "bb." not in text and "formation_from" not in text and "blackboard" not in text
    result = _compile(text, catalog)
    assert result.errors == [], [p.render() for p in result.errors]
    xml = _compile(GOLD.read_text(encoding="utf-8"), catalog)
    leader = [t for t in _dump(result)["control"]["trees"] if t["id"].startswith("platoon_")]
    gold = [t for t in _dump(xml)["control"]["trees"] if t["id"].startswith("platoon_")]
    assert leader == gold  # the generated formation writes and announcements
    # the formation each state declares is checked by the tests ending in it
    tests = {t.id: t for t in result.sections["verification"].tests}
    assert tests["leader_switches_to_column_near_urban"].expect.blackboard == {
        "mission.current_formation": "column"
    }
    # once(): a flag set after the report, checked before it, cleared by the other states
    trees = {t.id: t for t in result.sections["control"].trees}
    contact = trees["wingman_contact_tree"]
    assert any(n.node_id == "wingman_once_contact" for n in contact.nodes)
    first = trees["wingman_travel_open_tree"].nodes[0]
    assert first.behavior == "set_once_contact_false"
    assembled, check = _package(tmp_path, result, catalog)
    assert check.ok, [p.render() for p in check.problems]
    assert all(all(e["passed"] for e in s["expectations"]) for s in check.l3)
    wingman = next(r for r in assembled.data["roles"] if r["id"] == "wingman")
    flags = {v["name"]: v["default"] for v in wingman["blackboards"][0]["variables"]}
    assert flags["once_contact"] is False


def test_formations_and_once_are_checked(catalogs) -> None:
    catalog, _ = catalogs
    gold = GOLD_PY.read_text(encoding="utf-8")
    text = gold.replace(
        "formations: {travel_open: wedge, travel_urban: column}",
        "formations: {travel_open: wedge, travel_city: colum}",
    )
    messages = " / ".join(p.message for p in _compile(text, catalog).errors)
    assert "编队 'colum' 未定义" in messages
    assert "没有把编队 ['column'] 分配给任何 platoon_leader 状态" in messages
    text = gold.replace(
        "formations: {travel_open: wedge, travel_urban: column}",
        "formations: {travel_open: wedge, travel_city: column}",
    )
    assert any(
        "'travel_city' 不是 platoon_leader 的状态" in p.message
        for p in _compile(text, catalog).errors
    )
    # once() belongs to a state tree; in a subtree it has no state to re-arm it
    text = gold.replace(
        "def contact():\n    # 每次接敌报告一次，然后参加交替掩护\n    with once():\n"
        '        coord_contact_report.send_report()\n    coord_bounding.tree(name="wingman_bounding")\n',
        'def contact():\n    report()\n    coord_bounding.tree(name="wingman_bounding")\n\n'
        "def report():\n    with once():\n        coord_contact_report.send_report()\n",
    )
    assert any("只能写在状态树里" in p.message for p in _compile(text, catalog).errors)
    # a test input on a variable the role does not have is dropped with a warning
    test = (
        "  - id: stale_flag\n    category: nominal\n    role: wingman\n    state: contact\n"
        "    given: {blackboard: {contact_reported: true}}\n    expect: {next_state: contact}\n"
    )
    result = _compile(gold.replace("tests:\n", "tests:\n" + test, 1), catalog)
    assert result.errors == []
    assert any("黑板没有变量 contact_reported" in w.message for w in result.warnings)


def test_xml_once_and_prompts_use_the_generated_forms(catalogs) -> None:
    catalog, _ = catalogs
    gold = GOLD.read_text(encoding="utf-8")
    start = gold.index('<BehaviorTree ID="contact">', gold.index("```xml wingman"))
    end = gold.index("</BehaviorTree>", start)
    tree = (
        '<BehaviorTree ID="contact">\n      <Sequence>\n        <Once>\n'
        '          <Coordination use="coord_contact_report.send_report"/>\n        </Once>\n'
        '        <SubTree ID="coord_bounding.tree"/>\n      </Sequence>\n    '
    )
    result = _compile(gold[:start] + tree + gold[end:], catalog)
    assert not [p for p in result.errors if "Once" in p.message], [
        p.render() for p in result.errors
    ]
    assert any(
        n.node_id == "wingman_once_contact"
        for t in result.sections["control"].trees
        for n in t.nodes
    )
    py_guide = P.control_guide("python", ["leader_follower"])
    assert "with once():" in py_guide and "formations" in py_guide
    assert "formations" in P.PATTERN_FIELDS["leader_follower"]
    assert "blackboard: {{变量" not in P.VERIFICATION_GUIDE


# ---------------------------------------------------------------------- py8 review: exceptions, timing
def test_star_transition_is_a_global_exception(catalogs, tmp_path) -> None:
    from test_falcon_sheet import _package

    from afsim_research_agent.falcon.sheet.mermaid import parse_diagram

    diagram, problems = parse_diagram(
        "stateDiagram-v2\n  [*] --> a\n  a --> b : go\n  b --> c : go\n  * --> c : hit\n"
        "  c --> a : clear\n  c --> [*]\n",
        "r",
        "mermaid:r",
        1,
    )
    assert problems == []
    arrows = [(t.source, t.target, t.label) for t in diagram.transitions]
    # added ahead of each state's own transitions; not to itself; b already goes to c
    assert arrows[0] == ("a", "c", "hit") and ("b", "c", "hit") not in arrows
    assert not any(s == "c" and t == "c" for s, t, _ in arrows)

    catalog, _ = catalogs
    gold = GOLD_PY.read_text(encoding="utf-8")
    text = gold.replace(
        "  travel_open --> contact : in_contact [sync]\n", "  * --> contact : in_contact [sync]\n"
    ).replace("  travel_urban --> contact : in_contact [sync]\n", "")
    text = text.replace(
        "  travel_open -> contact: user_specified\n", '  "* -> contact": user_specified\n'
    ).replace("  travel_urban -> contact: user_specified\n", "")
    # only one contact test from the leader: the compiler repeats it from the other state
    text = text.replace(
        "  - id: leader_contact_in_urban\n    category: priority_transition\n    role: platoon_leader\n"
        "    state: travel_urban\n"
        "    given: {terrain: {class: urban, distance_to_class.urban: 0}, tracks: {nearest_hostile_range: 800 m}}\n"
        "    expect: {next_state: contact, transition: travel_urban -> contact}\n",
        "",
    )
    result = _compile(text, catalog)
    assert result.errors == [], [p.render() for p in result.errors]
    assert result.symbols["generated_tests"] == [
        "leader_contact_preempts_urban_entry_from_travel_urban"
    ]
    assembled, check = _package(tmp_path, result, catalog)
    assert check.ok, [p.render() for p in check.problems]
    leader = assembled.data["roles"][0]["state_machine"]["states"]
    urban = next(s for s in leader if s["id"] == "travel_urban")
    assert urban["transitions"][0]["to"] == "contact"  # the exception comes first
    # the `* -> contact` basis entry reached the expanded transition like a written one
    written = next(t for t in urban["transitions"] if t["to"] == "travel_open")
    assert urban["transitions"][0]["basis"] == written["basis"]
    dumped = str(_dump(result))
    start = dumped.index("platoon_leader_travel_urban_to_contact")
    assert "user_specified" in dumped[start : start + 600]


def test_scenario_deadlines_are_checked_against_distance_and_speed(catalogs) -> None:
    catalog, _ = catalogs
    gold = GOLD_PY.read_text(encoding="utf-8")
    scenario = (
        "  - id: too_fast\n    description: x\n    route: [[0, 0], [5000, 0]]\n"
        "    terrain: {default: open, regions: [{class: urban, x: [2500, 4000], y: [-1500, 1500]}]}\n"
        "    duration: 8 min\n    seed: 3\n    events: []\n"
        "    expect:\n"
        "      - {id: e_urban, observable: role_state, role: platoon_leader, equals: travel_urban, "
        "within: {after: start, time: 60 s}}\n"
    )
    result = _compile(gold.replace("scenarios:\n", "scenarios:\n" + scenario, 1), catalog)
    error = next(p for p in result.errors if "too_fast" in p.message)
    # 2300 m to within 200 m of the town at 30 km/h ≈ 276 s
    assert "约 2300 m" in error.message and "276 s" in error.message and "marker" in error.message
    ok = scenario.replace("time: 60 s", "time: 300 s")
    result = _compile(gold.replace("scenarios:\n", "scenarios:\n" + ok, 1), catalog)
    assert not any("too_fast" in p.message for p in result.errors)
    guide = P.verification_guide("role_state", None)
    assert "marker 事件" in guide and "路线距离 ÷ 速度" in guide
    assert "* --> 目标状态" in P.STRUCTURE_GUIDE


# ---------------------------------------------------------------------- py9 run
def test_py9_spellings_and_hints(catalogs) -> None:
    catalog, _ = catalogs
    gold = GOLD_PY.read_text(encoding="utf-8")
    # basis written under a "*" key holds for every role that has the transition
    text = gold.replace(
        "  travel_open --> contact : in_contact [sync]\n", "  * --> contact : in_contact [sync]\n"
    ).replace("  travel_urban --> contact : in_contact [sync]\n", "")
    text = text.replace("  travel_open -> contact: user_specified\n", "", 1).replace(
        "  travel_urban -> contact: user_specified\n", "", 1
    )
    text = text.replace(
        "```yaml basis\n", '```yaml basis\n"*":\n  "* -> contact": user_specified\n'
    )
    result = _compile(text, catalog)
    assert not any("basis 的键应是角色名" in p.message for p in result.errors)
    dumped = str(_dump(result))
    start = dumped.index("platoon_leader_travel_open_to_contact")
    assert "user_specified" in dumped[start : start + 600]
    # a generated behavior used as a transition condition
    text = gold.replace(
        "  contact --> travel_open : contact_cleared [sync]\n",
        "  contact --> travel_open : coord_contact_report.acknowledge [sync]\n",
    )
    error = next(p for p in _compile(text, catalog).errors if "acknowledge" in p.message)
    assert "是协同生成的行为" in error.message and "time_in_state()" in error.message


def test_repair_prompt_says_when_a_block_did_not_change() -> None:
    prompt = P.repair_prompt(
        label="mermaid r",
        numbered="1 x",
        problems=["- a"],
        sheet="",
        symbols={},
        usage=None,
        guide="",
        stuck=True,
    )
    assert "原样还在" in prompt and "换一种写法" in prompt


def test_reaction_probes_find_deaf_states(catalogs, tmp_path) -> None:
    from test_falcon_sheet import _package

    from afsim_research_agent.falcon.sheet.probes import reaction_probes

    catalog, _ = catalogs
    gold = GOLD_PY.read_text(encoding="utf-8")
    result = _compile(gold, catalog)
    (tmp_path / "a").mkdir()
    _assembled, check = _package(tmp_path / "a", result, catalog)
    assert reaction_probes(check.ir) == []
    # contact only from the urban state: the open-terrain march ignores the enemy
    text = gold.replace("  travel_open --> contact : in_contact [sync]\n", "")
    text = text.replace("  travel_open -> contact: user_specified\n", "")
    text = text.replace("  travel_open --> contact : in_contact\n", "")
    text = text.replace("  wingman:\n  travel_open -> contact: user_specified\n", "  wingman:\n")
    result = _compile(text, catalog)
    (tmp_path / "b").mkdir()
    _assembled, check = _package(tmp_path / "b", result, catalog)
    found = [m for _p, m in reaction_probes(check.ir)]
    assert any("travel_open 沿路线行进时发现敌人" in m and "* --> contact" in m for m in found)
    assert any(p.code == "PROBE001" for p in check.problems)
