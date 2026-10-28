"""`falcon fix`: the self-checking repair loop (agent-loop-deepagents-v1 §4)."""

from __future__ import annotations

import asyncio
import json
import re

import yaml

from afsim_research_agent.falcon.llm import ModelRunner, ScriptedModel
from afsim_research_agent.falcon.sheet.fixer import fix_package

BAD = '<SetAltitude altitude="1 km"/>\n'


def _broken_package(tmp_path):
    from falcon_fixtures import crow_result, write_request
    from test_falcon_sheet import _v2_script

    from afsim_research_agent.falcon.pipeline import run_draft
    from afsim_research_agent.falcon.sheet.recompile import recompile_package

    request = write_request(tmp_path, crow_result(), generator="v2")
    pkg = asyncio.run(run_draft(request, ScriptedModel(_v2_script()))).package_dir
    sheet = pkg / "tactic_sheet.md"
    sheet.write_text(
        sheet.read_text(encoding="utf-8").replace(
            '<FollowRoute name="leader_move_open"', BAD + '<FollowRoute name="leader_move_open"'
        ),
        encoding="utf-8",
    )
    assert recompile_package(pkg).status != "draft_valid"
    return pkg


def _steps(*answers):
    """A scripted agent: one answer per step; it records every prompt it was shown."""
    seen: list[str] = []

    def handler(prompt, _schema, index):
        seen.append(prompt)
        return answers[min(index, len(answers) - 1)]

    return handler, seen


def test_fix_loop_repairs_a_copy_and_logs_every_edit(tmp_path) -> None:
    pkg = _broken_package(tmp_path)
    original = (pkg / "tactic_sheet.md").read_text(encoding="utf-8")
    verification = yaml.safe_load(original.split("```yaml verification\n", 1)[1].split("```", 1)[0])
    first_test = verification["tests"][0]["id"]
    handler, seen = _steps(
        {"thought": "先看问题", "action": "check", "args": {}},
        {"thought": "看领队的树", "action": "show", "args": {"block": "xml:platoon_leader"}},
        {"thought": "问题 1 是什么", "action": "explain", "args": {"problem": 1}},
        # deleting a test to pass is refused by the tool, not by the prompt
        {
            "thought": "删掉一个测试",
            "action": "patch",
            "args": {"block": "verification", "old": f"  - id: {first_test}\n", "new": ""},
        },
        {
            "thought": "无效的 old",
            "action": "patch",
            "args": {"block": "xml:platoon_leader", "old": "nope", "new": ""},
        },
        {
            "thought": "去掉平台不具备的 SetAltitude",
            "action": "patch",
            "args": {
                "block": "xml:platoon_leader",
                "old": BAD,
                "new": "",
                "reason": "地面车辆没有 set_altitude",
            },
        },
        {"thought": "should not be reached", "action": "finish", "args": {}},
    )
    model = ScriptedModel({"fix": handler})
    result = asyncio.run(fix_package(pkg, ModelRunner(model), max_steps=10))
    assert result.status == "draft_valid", result.problems
    assert result.exit_code == 0
    assert len(result.steps) == 6 and len(result.edits) == 1
    actions = [s.action for s in result.steps]
    assert actions == ["check", "show", "explain", "patch", "patch", "patch"]
    assert "拒绝修改" in result.steps[3].result
    assert "恰好 1 次" in result.steps[4].result
    assert "set_altitude" in result.steps[0].result
    assert "变好" in result.steps[5].result
    # the original package is untouched; the copy holds the repaired sheet
    assert (pkg / "tactic_sheet.md").read_text(encoding="utf-8") == original
    fixed = result.package_dir
    assert fixed.name == pkg.name + "_fix"
    assert BAD not in (fixed / "tactic_sheet.md").read_text(encoding="utf-8")
    log = json.loads((fixed / "fix_log.json").read_text(encoding="utf-8"))
    assert log["status"] == "draft_valid" and log["edits"][0]["block"] == "xml:platoon_leader"
    review = (fixed / "falcon_review.md").read_text(encoding="utf-8")
    assert "自检修改记录" in review and re.search(r"\n-\s*<SetAltitude", review)
    # the prompt carries the rules, the current problems and the recent steps
    assert "每一步只能调用一个工具" in seen[0] and "#1" in seen[0]
    assert "第 4 步 patch" in seen[5]


def test_fix_loop_stops_for_a_question_and_keeps_the_best_version(tmp_path) -> None:
    pkg = _broken_package(tmp_path)
    handler, _ = _steps(
        {
            "thought": "试一下",
            "action": "patch",
            "args": {
                "block": "xml:platoon_leader",
                "old": BAD,
                "new": BAD + BAD.replace("1 km", "2 km"),
            },
        },
        {
            "thought": "领队这一阶段要不要升空？",
            "action": "ask_human",
            "args": {"question": "是否需要高度控制？"},
        },
    )
    out = tmp_path / "fixed"
    result = asyncio.run(
        fix_package(pkg, ModelRunner(ScriptedModel({"fix": handler})), out_dir=out, max_steps=5)
    )
    assert result.package_dir == out and result.question == "是否需要高度控制？"
    assert result.exit_code == 4
    assert "变差" in result.steps[0].result
    sheet = (out / "tactic_sheet.md").read_text(encoding="utf-8")
    assert 'altitude="2 km"' not in sheet  # the worse edit is not what is written back
    assert "需要你决定" in (out / "falcon_review.md").read_text(encoding="utf-8")


def test_try_tick_runs_one_situation_without_changing_the_sheet(tmp_path) -> None:
    from afsim_research_agent.falcon.sheet.fixer import SheetFixer
    from afsim_research_agent.falcon.sheet.recompile import open_workspace

    pkg = _broken_package(tmp_path)
    sheet = (pkg / "tactic_sheet.md").read_text(encoding="utf-8")
    (pkg / "tactic_sheet.md").write_text(sheet.replace(BAD, ""), encoding="utf-8")
    fixer = SheetFixer(open_workspace(pkg))
    before = fixer.sheet()
    out = fixer.try_tick(
        "platoon_leader", "travel_open", {"tracks": {"nearest_hostile_range": "1 km"}}
    )
    assert "下一状态 contact" in out, out
    assert fixer.sheet() == before


def test_with_l3_a_failing_scenario_is_not_reported_valid(tmp_path, monkeypatch) -> None:
    from afsim_research_agent.falcon.sheet import fixer as F

    pkg = _broken_package(tmp_path)
    sheet = (pkg / "tactic_sheet.md").read_text(encoding="utf-8")
    (pkg / "tactic_sheet.md").write_text(sheet.replace(BAD, ""), encoding="utf-8")
    real = F.SheetFixer.evaluate

    def failing(self):
        ev = real(self)
        ev.l3_failures = ["[verification] L3 想定 s1 的期望 e1 未通过：实测 0（测试）"]
        ev.score = (ev.score[0], ev.score[1], 1)
        return ev

    monkeypatch.setattr(F.SheetFixer, "evaluate", failing)
    handler, _ = _steps({"thought": "", "action": "check", "args": {}})
    result = asyncio.run(
        fix_package(pkg, ModelRunner(ScriptedModel({"fix": handler})), max_steps=1, with_l3=True)
    )
    assert result.status == "l3_failed" and result.exit_code == 4
    assert "L3 想定 s1" in result.problems[0]


def test_show_parts_of_a_long_block_and_the_latest_result_is_whole(tmp_path) -> None:
    from afsim_research_agent.falcon.sheet.fixer import SheetFixer, StepRecord, _history
    from afsim_research_agent.falcon.sheet.recompile import open_workspace

    pkg = _broken_package(tmp_path)
    fixer = SheetFixer(open_workspace(pkg))
    v = fixer.block("verification")
    found = fixer.show("verification", find="leader_returns_to_open")
    assert "找到 1 处" in found and "travel_urban -> travel_open" in found
    part = fixer.show("verification", start=v.line + 2, end=v.line + 4)
    assert len(part.splitlines()) == 5 and f"{v.line + 2:4d} |" in part
    long = "x" * 5000
    records = [StepRecord(i, "", "show", {}, long, 0.0) for i in range(1, 4)]
    text = _history(records)
    assert text.count("截断") == 2 and long in text


def test_a_rewritten_test_is_recorded_again(tmp_path) -> None:
    """py9 fix2: the loop changed what a test expects; the results recorded for the old
    test (actions, messages) must not be held against the new one."""
    pkg = _broken_package(tmp_path)
    observed = yaml.safe_load((pkg / "observed_expectations.yaml").read_text(encoding="utf-8"))
    assert "follow_route" in observed["tests"]["leader_travels_in_wedge"]["issued_actions"]
    old = (
        "  - id: leader_travels_in_wedge\n    category: nominal\n    role: platoon_leader\n"
        "    state: travel_open\n    expect: {next_state: travel_open, actions: [SendMessage, "
        "FollowRoute], blackboard: {current_formation: wedge}}"
    )
    new = (
        "  - id: leader_travels_in_wedge\n    category: nominal\n    role: platoon_leader\n"
        "    state: travel_open\n    given: {tracks: {nearest_hostile_range: 1 km}}\n"
        "    expect: {next_state: contact, transition: travel_open -> contact}"
    )
    handler, _ = _steps(
        {
            "thought": "改测试",
            "action": "patch",
            "args": {"block": "verification", "old": old, "new": new},
        },
        {
            "thought": "去掉 SetAltitude",
            "action": "patch",
            "args": {"block": "xml:platoon_leader", "old": BAD, "new": ""},
        },
    )
    result = asyncio.run(
        fix_package(pkg, ModelRunner(ScriptedModel({"fix": handler})), max_steps=4)
    )
    assert result.status == "draft_valid", result.problems
    fixed = result.package_dir
    again = yaml.safe_load((fixed / "observed_expectations.yaml").read_text(encoding="utf-8"))
    assert "follow_route" not in again["tests"]["leader_travels_in_wedge"]["issued_actions"]
    log = json.loads((fixed / "fix_log.json").read_text(encoding="utf-8"))
    assert log["rerecorded_tests"] == ["leader_travels_in_wedge"]
    assert "重新记录" in (fixed / "falcon_review.md").read_text(encoding="utf-8")


def _deaf_package(tmp_path):
    """The broken package, and the leader's urban state no longer reacts to contact."""
    pkg = _broken_package(tmp_path)
    sheet = pkg / "tactic_sheet.md"
    text = sheet.read_text(encoding="utf-8")
    assert "  travel_urban --> contact : in_contact [sync]\n" in text
    sheet.write_text(
        text.replace("  travel_urban --> contact : in_contact [sync]\n", "", 1), encoding="utf-8"
    )
    return pkg


def test_cards_carry_location_text_and_quick_fixes(tmp_path) -> None:
    from afsim_research_agent.falcon.sheet.fixer import SheetFixer
    from afsim_research_agent.falcon.sheet.recompile import open_workspace

    pkg = _deaf_package(tmp_path)
    fixer = SheetFixer(open_workspace(pkg))
    fixer.step = 1
    out = fixer.edit([{"block": "xml:platoon_leader", "old": BAD, "new": ""}], "去掉 SetAltitude")
    assert "变好" in out
    # an edit that makes the checks stop earlier (a time without unit) is not applied
    diagram = fixer.block("mermaid:platoon_leader").text
    fixer.step = 2
    refused = fixer.edit(
        [
            {
                "block": "mermaid:platoon_leader",
                "old": "contact --> travel_open : contact_cleared [sync]",
                "new": "contact --> travel_open : time_in_state() >= 5 [sync]",
            }
        ],
        "time without unit",
    )
    assert "修改未应用" in refused and "UNIT001" in refused
    assert fixer.block("mermaid:platoon_leader").text == diagram
    cards = fixer.last.cards
    probe = next(c for c in cards if c.code == "PROBE001")
    assert "离开 travel_urban 的转换" in probe.context
    assert probe.fixes and "travel_urban --> contact : in_contact [sync]" in probe.fixes[0].title
    failing = next(c for c in cards if "测试" in c.text and "travel_urban -> contact" in c.text)
    assert "- id: leader_contact_in_urban" in failing.context
    index = cards.index(probe) + 1
    fixer.step = 3
    result = fixer.apply_fix(index, 1)
    assert fixer.last.ok, result
    # the urban expansion is exercised by the existing test, so nothing to synthesize
    assert (
        "  travel_urban --> contact : in_contact [sync]\n  travel_urban --> travel_open"
        in fixer.sheet()
    )
    assert "已解决" in result and fixer.ledger


def test_fix_loop_with_quick_fixes_finishes_in_few_steps(tmp_path) -> None:
    pkg = _deaf_package(tmp_path)
    seen: list[str] = []

    def handler(prompt, _schema, index):
        seen.append(prompt)
        if index == 0:
            return {
                "thought": "去掉 SetAltitude",
                "action": "edit",
                "args": {"changes": [{"block": "xml:platoon_leader", "old": BAD, "new": ""}]},
            }
        m = re.search(r'apply_fix \{"problem": (\d+), "fix": 1\}', prompt)
        assert m, prompt[-3000:]
        return {
            "thought": "用快速修复",
            "action": "apply_fix",
            "args": {"problem": int(m.group(1))},
        }

    result = asyncio.run(
        fix_package(pkg, ModelRunner(ScriptedModel({"fix": handler})), max_steps=6)
    )
    assert result.status == "draft_valid", result.problems
    assert result.metrics["steps"] == 2 and result.metrics["quick_fixes"] == 1
    assert result.metrics["reads"] == 0
    assert "进度（由程序记录）" in seen[1] and "已解决" in seen[1]
    log = json.loads((result.package_dir / "fix_log.json").read_text(encoding="utf-8"))
    assert log["metrics"]["solved"] and log["edits"][-1]["action"] == "apply_fix"


def test_edits_by_line_range_and_synthesized_tests_for_new_transitions(tmp_path) -> None:
    from afsim_research_agent.falcon.sheet.fixer import SheetFixer
    from afsim_research_agent.falcon.sheet.recompile import open_workspace

    pkg = _broken_package(tmp_path)
    sheet = pkg / "tactic_sheet.md"
    sheet.write_text(sheet.read_text(encoding="utf-8").replace(BAD, ""), encoding="utf-8")
    fixer = SheetFixer(open_workspace(pkg))
    assert fixer.last.ok
    diagram = fixer.block("mermaid:wingman")
    line = next(
        diagram.line + i
        for i, row in enumerate(diagram.text.splitlines())
        if row.strip().startswith("travel_urban --> contact")
    )
    fixer.step = 1
    out = fixer.edit(
        [
            {
                "block": "mermaid:wingman",
                "start": line + 1,
                "end": line,
                "text": '  travel_urban --> travel_open : time_in_state() >= "10 min"',
            }
        ],
        "城区停留过久回到开阔地",
    )
    assert "自动补测试" in out and fixer.last.ok, out
    test = fixer.generated[0]
    v = fixer.block("verification").text
    assert f"- id: {test}" in v and "elapsed" in v.split(f"- id: {test}", 1)[1][:300]
    # an edit that adds a transition whose test cannot be found says so
    fixer.step = 2
    out = fixer.edit(
        [
            {
                "block": "mermaid:platoon_leader",
                "old": "  contact --> travel_open : contact_cleared [sync]",
                "new": "  contact --> travel_open : contact_cleared [sync]\n"
                '  contact --> travel_urban : time_in_state() >= "10 min" [sync]',
            }
        ],
        "a transition that never wins over contact_cleared",
    )
    assert "测试需要手写" in out


def test_a_state_that_bounces_back_in_the_same_situation_is_reported(tmp_path) -> None:
    """py9 fix4: `* --> reporting : enemy_detected` also expanded to bounding overwatch, so
    a platoon that got the acknowledgement went straight back to reporting."""
    from afsim_research_agent.falcon.sheet.fixer import SheetFixer
    from afsim_research_agent.falcon.sheet.recompile import open_workspace

    pkg = _broken_package(tmp_path)
    sheet = pkg / "tactic_sheet.md"
    text = sheet.read_text(encoding="utf-8").replace(BAD, "")
    sheet.write_text(text, encoding="utf-8")
    assert SheetFixer(open_workspace(pkg)).last.ok  # the gold-derived sheet settles
    sheet.write_text(
        text.replace(
            "contact --> travel_open : contact_cleared [sync]",
            "contact --> travel_open : in_contact [sync]",
        ),
        encoding="utf-8",
    )
    fixer = SheetFixer(open_workspace(pkg))
    card = next(c for c in fixer.last.cards if c.code == "PROBE002")
    assert "travel_open → contact → travel_open" in card.text
    assert "contact --> travel_open : in_contact" in card.context and "条件 in_contact" in card.text


# ---------------------------------------------------------------------- requirement properties
CONTACT_PROPERTIES = """format: RequirementProperties
properties:
  - id: p01_contact
    clauses: [rq_06]
    text: 行进中接敌要转入交替跃进
    role: platoon_leader
    start: moving
    situation:
      tracks: {hostile_count: 1, nearest_hostile_range: 1 km, current_target_valid: true}
      terrain: {class: urban, distance_to_class.urban: 0 m}
    within: 3
    kind: settle
    actions: [bound_to_position]
"""


def test_requirement_properties_catch_what_the_sheet_tests_do_not(tmp_path) -> None:
    from afsim_research_agent.falcon.sheet.fixer import SheetFixer
    from afsim_research_agent.falcon.sheet.recompile import open_workspace

    pkg = _broken_package(tmp_path)
    sheet = pkg / "tactic_sheet.md"
    text = sheet.read_text(encoding="utf-8").replace(BAD, "")
    sheet.write_text(text, encoding="utf-8")
    (pkg / "requirement_properties.yaml").write_text(CONTACT_PROPERTIES, encoding="utf-8")
    assert SheetFixer(open_workspace(pkg)).last.ok  # the gold-derived sheet meets it
    # urban travel ignores contact; its test is rewritten to match, so the sheet's own
    # checks pass, but the property (which the loop cannot edit) does not
    deaf = text.replace("  travel_urban --> contact : in_contact [sync]\n", "", 1)
    deaf = deaf.replace("  travel_urban -> contact: user_specified\n", "", 1)
    deaf = re.sub(
        r"(  - id: leader_contact_in_urban\n(?:    .*\n)*?)    expect: \{next_state: contact, transition: travel_urban -> contact\}",
        r"\1    expect: {next_state: travel_urban}",
        deaf,
    )
    sheet.write_text(deaf, encoding="utf-8")
    observed = pkg / "observed_expectations.yaml"
    doc = yaml.safe_load(observed.read_text(encoding="utf-8"))
    del doc["tests"]["leader_contact_in_urban"]  # recorded again for the rewritten test
    observed.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    fixer = SheetFixer(open_workspace(pkg))
    codes = [c.code for c in fixer.last.cards]
    assert "TEST001" not in codes and "SHEET" not in codes, fixer.summary()
    assert codes.count("PROP001") == 1, fixer.summary()
    card = next(c for c in fixer.last.cards if c.code == "PROP001")
    assert card.block == "mermaid:platoon_leader" and "从 travel_urban 出发" in card.text
    assert "travel_urban --> travel_open" in card.context and "不能修改" in card.notes[-1]


def test_properties_are_written_independently_and_cannot_change_in_the_loop(tmp_path) -> None:
    from afsim_research_agent.falcon.sheet.properties import load_properties

    pkg = _broken_package(tmp_path)
    prompts: list[str] = []

    def write(prompt, _schema, _index):
        prompts.append(prompt)
        return {
            "properties": [
                yaml.safe_load(CONTACT_PROPERTIES)["properties"][0],
                {"id": "p02", "role": "platoon_leader", "actions": ["teleport"], "text": "x"},
                {  # the flat spelling qwen used for all py9 properties
                    "id": "p03",
                    "role": "platoon_leader",
                    "start": "moving",
                    "situation": {
                        "tracks.hostile_count": 1,
                        "tracks.nearest_hostile_range": "1 km",
                        "terrain.class": "urban",
                        "components.comms.available": True,
                        "messages": ["contact_report.ack"],
                    },
                    "kind": "respond",
                    "within": 3,
                    "actions": ["bound_to_position"],
                },
            ],
            "skipped": [{"clause": "rq_05", "reason": "车距是数值约束，由参数检查"}],
        }

    def step(_prompt, _schema, _index):
        return {
            "thought": "",
            "action": "patch",
            "args": {"block": "xml:platoon_leader", "old": BAD, "new": ""},
        }

    model = ScriptedModel({"properties": write, "fix": step})
    result = asyncio.run(fix_package(pkg, ModelRunner(model), max_steps=3))
    assert result.status == "draft_valid", result.problems
    # the writer saw the requirement and the vocabulary, not the sheet's diagrams or tests
    assert "bound_to_position" in prompts[0] and "stateDiagram" not in prompts[0]
    assert "leader_contact_in_urban" not in prompts[0]
    props = load_properties(result.package_dir)
    assert [p["id"] for p in props] == ["p01_contact", "p03"] and props[0][
        "status"
    ] == "unconfirmed"
    assert props[1]["situation"] == {
        "tracks": {"hostile_count": 1, "nearest_hostile_range": "1 km"},
        "terrain": {"class": "urban"},
        "components": {"comms.available": True},
        "messages": ["ack"],
    }
    doc = yaml.safe_load(
        (result.package_dir / "requirement_properties.yaml").read_text(encoding="utf-8")
    )
    assert doc["rejected"] and "teleport" in doc["rejected"][0]["why"]
    review = (result.package_dir / "falcon_review.md").read_text(encoding="utf-8")
    assert "已生成需求属性 2 条" in review
    log = json.loads((result.package_dir / "fix_log.json").read_text(encoding="utf-8"))
    assert log["properties"]


def test_falcon_properties_does_not_overwrite_without_force(tmp_path) -> None:
    from afsim_research_agent.falcon.errors import FalconError
    from afsim_research_agent.falcon.sheet.fixer import write_properties

    pkg = _broken_package(tmp_path)
    (pkg / "requirement_properties.yaml").write_text(CONTACT_PROPERTIES, encoding="utf-8")
    model = ScriptedModel({"properties": [{"properties": [], "skipped": []}]})
    try:
        asyncio.run(write_properties(pkg, ModelRunner(model)))
    except FalconError as exc:
        assert "--force" in str(exc)
    else:
        raise AssertionError("an existing properties file was overwritten")
    lines, failures = asyncio.run(write_properties(pkg, ModelRunner(model), force=True))
    assert "已生成需求属性 0 条" in lines[0] and not failures


# ---------------------------------------------------------------------- after py9 fix6
def test_end_states_take_no_condition_and_a_named_end_state_works(tmp_path) -> None:
    """fix6 wrote `open_advance --> [*] : vehicle_disabled`, which silently made
    open_advance an end state, then `* --> [*]` and tests expecting "[*]"."""
    from afsim_research_agent.falcon.sheet.fixer import SheetFixer
    from afsim_research_agent.falcon.sheet.recompile import open_workspace

    pkg = _broken_package(tmp_path)
    sheet = pkg / "tactic_sheet.md"
    sheet.write_text(sheet.read_text(encoding="utf-8").replace(BAD, ""), encoding="utf-8")
    fixer = SheetFixer(open_workspace(pkg))
    fixer.step = 1
    out = fixer.edit(
        [
            {
                "block": "mermaid:wingman",
                "old": "  travel_urban --> contact : in_contact\n",
                "new": "  travel_urban --> contact : in_contact\n  travel_open --> [*] : in_contact\n",
            }
        ],
        "conditional end",
    )
    assert "不能带条件" in out and "disabled --> [*]" in out
    if "修改未应用" not in out:
        fixer.undo()
    fixer.step = 2
    out = fixer.edit(
        [
            {
                "block": "mermaid:wingman",
                "old": "  travel_urban --> contact : in_contact\n",
                "new": "  travel_urban --> contact : in_contact\n"
                '  contact --> disabled : time_in_state() >= "1 h"\n  disabled --> [*]\n',
            }
        ],
        "named end state",
    )
    assert "自动补测试" in out and fixer.last.ok, out


def test_edits_that_break_the_verification_block_are_refused_and_undo_restores(tmp_path) -> None:
    from afsim_research_agent.falcon.sheet.fixer import SheetFixer
    from afsim_research_agent.falcon.sheet.recompile import open_workspace

    pkg = _broken_package(tmp_path)
    fixer = SheetFixer(open_workspace(pkg))
    v = fixer.block("verification")
    start = next(
        v.line + i
        for i, row in enumerate(v.text.splitlines())
        if "- id: leader_returns_to_open" in row
    )
    fixer.step = 1
    # a line range that stops in the middle of a test leaves a second `expect:` behind
    out = fixer.edit(
        [
            {
                "block": "verification",
                "start": start,
                "end": start + 3,
                "text": "  - id: leader_returns_to_open\n    category: nominal\n    role: platoon_leader\n"
                "    state: travel_urban\n    expect: {next_state: travel_urban}",
            }
        ],
        "half a test",
    )
    assert "结构损坏" in out and "重复的键" in out
    out = fixer.edit([{"block": "verification", "old": "tests:\n", "new": "tests:\n  - [\n"}], "x")
    assert "YAML 有误" in out and "减少到 0" not in out
    # a two-block edit and its undo
    fixer.step = 2
    before = {b.key: b.text for b in fixer.blocks}
    fixer.edit(
        [
            {"block": "xml:platoon_leader", "old": BAD, "new": ""},
            {
                "block": "verification",
                "old": "leader_returns_to_open",
                "new": "leader_back_to_open",
            },
        ],
        "two blocks",
    )
    assert fixer.block("verification").text != before["verification"]
    fixer.step = 3
    out = fixer.undo()
    assert {b.key: b.text for b in fixer.blocks} == before, out


def test_a_loop_without_progress_stops_and_asks(tmp_path) -> None:
    pkg = _broken_package(tmp_path)
    handler, _ = _steps({"thought": "再看看", "action": "check", "args": {}})
    result = asyncio.run(
        fix_package(
            pkg, ModelRunner(ScriptedModel({"fix": handler})), max_steps=30, properties=False
        )
    )
    assert result.question and "没有超过" in result.question and "需要你决定" in result.question
    assert len(result.steps) == 16 and result.exit_code == 4
