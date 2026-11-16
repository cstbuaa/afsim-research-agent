# ruff: noqa: F811 - pytest fixtures imported from test_falcon_v3
"""Repair tools v2 (falcon.v3.tools): explaining a failed check, evaluating conditions,
field writers, structured edits, the change lint and the loop rules around them."""

from __future__ import annotations

import asyncio

from test_falcon_v3 import SHEET, _fix_drafter, contracts, set_root  # noqa: F401 - fixtures

from afsim_research_agent.falcon.v3 import tools as T
from afsim_research_agent.falcon.v3.fixer import SheetFixer, fix_sheet
from afsim_research_agent.ir.v2.contracts import Contracts

GUARDED = SHEET.replace(
    "target_seen = sense.target.valid",
    "target_seen = sense.target.valid and time_in_state() >= 3 * s",
)


def _fixer(contracts: Contracts, text: str = SHEET) -> SheetFixer:
    return SheetFixer(_fix_drafter(contracts, text))


def test_explain_check_names_the_term_that_decides_a_failed_l2_test(contracts: Contracts):
    fixer = _fixer(contracts, GUARDED)
    assert fixer.last.l1_ok
    failed = [t["id"] for t in fixer.last.l2 if not t["passed"]]
    assert failed == ["sees_target"]
    text = T.explain_check(fixer, "sees_target")
    assert "期望" in text and "OBSERVE" in text
    assert "sense.target.valid=True" in text
    assert "time_in_state()=0s >= 3s → 假" in text and "决定结果的项" in text
    assert T.explain_check(fixer, "L2: sees_target") == text
    assert "未通过的：['sees_target']" in T.explain_check(fixer, "nope")
    assert T.first_failure(fixer) == "sees_target"
    # an L1 problem by number, with the writers of the fields it names
    broken = _fixer(contracts, SHEET.replace("nav.hold()", "nav.hodl()"))
    assert "L1 未通过" in T.explain_check(broken, "sees_target")
    assert "nav.hodl" in T.explain_check(broken, 1)


def test_eval_condition_and_who_writes(contracts: Contracts):
    fixer = _fixer(contracts)
    text = T.eval_condition(
        fixer, "scout", "MOVE", "target_seen", {"blackboard": {"sense.target.valid": True}}
    )
    assert "target_seen（条件）= 真" in text
    exits = T.eval_condition(fixer, "scout", "MOVE", "", {})
    assert "MOVE → OBSERVE 当 target_seen" in exits and "cancelled" in exits
    assert "未收到" in exits  # the external event has not come
    writers = T.who_writes(fixer, "sense.target.valid")
    assert "读它的条件：target_seen" in writers
    assert "相近的有" in T.who_writes(fixer, "sense.target.vlid")


def test_structured_edits_change_the_named_part_only(contracts: Contracts):
    fixer = _fixer(contracts)
    texts = T.structured(
        fixer, "set_condition", {"name": "stopped", "expr": "self.speed_mps <= 0.5"}
    )
    assert "stopped = self.speed_mps <= 0.5" in texts["conditions"]
    assert "target_seen = sense.target.valid" in texts["conditions"]
    texts = T.structured(
        fixer,
        "add_transition",
        {"role": "scout", "from": "OBSERVE", "to": "SHUTDOWN", "trigger": "stopped"},
    )
    rows = texts["mermaid:scout"].splitlines()
    assert "  OBSERVE --> SHUTDOWN : stopped" in rows
    assert rows.index("  OBSERVE --> SHUTDOWN : stopped") < rows.index("  DONE --> [*]")
    note = T.structured(
        fixer,
        "add_transition",
        {"role": "scout", "from": "OBSERVE", "to": "SHUTDOWN", "trigger": "ghost"},
    )
    assert "没有定义" in note["_note"]
    texts = T.structured(
        fixer,
        "set_trigger",
        {"role": "scout", "from": "OBSERVE", "to": "MOVE", "trigger": "lost_long"},
    )
    assert "OBSERVE --> MOVE : lost_long" in texts["mermaid:scout"]
    texts = T.structured(
        fixer, "remove_transition", {"role": "scout", "from": "OBSERVE", "to": "MOVE"}
    )
    assert "OBSERVE --> MOVE" not in texts["mermaid:scout"]
    assert "没有状态 NOPE" in T.structured(
        fixer, "remove_transition", {"role": "scout", "from": "NOPE", "to": "MOVE"}
    )
    texts = T.structured(
        fixer, "declare_event", {"name": "observed", "source": "tree", "result": "success"}
    )
    assert "  observed: {result: success, source: tree}" in texts["model"].splitlines()
    assert "  lost_timeout:" in texts["model"]  # the other events stay
    texts = T.structured(
        fixer, "rename", {"kind": "trigger", "old": "target_seen", "new": "seen_target"}
    )
    assert "MOVE --> OBSERVE : seen_target" in texts["mermaid:scout"]
    assert texts["conditions"].startswith("seen_target = ")
    texts = T.structured(
        fixer, "attach_subtree", {"role": "scout", "state": "SHUTDOWN", "subtree": "sense.track"}
    )
    assert (
        'def SHUTDOWN():\n    nav.stop()\n    sense.off()\n    tactic("sense.track")'
        in texts["pytree:scout"]
    )
    assert "相近的有" in T.structured(
        fixer, "attach_subtree", {"role": "scout", "state": "SHUTDOWN", "subtree": "sense.trak"}
    )
    texts = T.structured(
        fixer,
        "set_state_tree",
        {"role": "scout", "state": "SHUTDOWN", "code": "def SHUTDOWN():\n    nav.stop()"},
    )
    assert "def SHUTDOWN():\n    nav.stop()" in texts["pytree:scout"]
    assert "sense.off()" not in texts["pytree:scout"]
    assert "def OBSERVE():" in texts["pytree:scout"]


def test_loop_rules_basis_lint_dedupe_dry_run_and_revert(contracts: Contracts):
    fixer = _fixer(contracts, GUARDED)
    fixer.step = 1
    fix = {"name": "target_seen", "expr": "sense.target.valid"}
    # once L1 passes a change needs a basis; the refusal carries the explanation
    refused = fixer.apply("set_condition", fix, "去掉等待")
    assert "先用 diagnose" in refused and "决定结果的项" in refused
    # a dry run reports the score and leaves the sheet as it was
    dry = fixer.try_edit({"op": "set_condition", "args": fix}, "试改")
    assert dry.startswith("试改结果（未保存）") and "变好" in dry
    assert "time_in_state() >= 3 * s" in fixer._block("conditions").text
    # a constant condition is refused
    assert "常量" in fixer.apply("set_condition", {"name": "target_seen", "expr": "True"}, "x")
    # with because the change is its own diagnosis; expect is checked
    done = fixer.apply(
        "set_condition",
        fix | {"because": "第 1 步 explain_check：等待项在进入时为假", "expect": ["sees_target"]},
        "去掉等待",
    )
    assert done.startswith("已修改") and "预期达成" in done and fixer.solved
    # adding the guard back is warned about (not refused), and the same sheet twice is refused
    fixer.step = 2
    back = fixer.apply(
        "set_condition",
        {
            "name": "target_seen",
            "expr": "sense.target.valid and time_in_state() >= 3 * s",
            "because": "试",
        },
        "加回等待",
    )
    assert (
        back.startswith("已修改") and "注意：给条件 target_seen 加了 time_in_state() 等待" in back
    )
    fixer.step = 3
    twice = fixer.apply("set_condition", fix | {"because": "再试"}, "去掉等待")
    assert "第 1 步已经得到过完全相同的稿件" in twice
    assert "未生效" in T.history(fixer) and "第 2 步 conditions" in T.history(fixer)
    # revert one step: back to the sheet before step 2
    out = fixer.revert("", "撤销", step=2)
    assert out.startswith("已撤销第 2 步") and fixer.solved
    assert "target_seen = sense.target.valid\n" in fixer._block("conditions").text
    # an edit whose old text is gone shows the block as it is now
    miss = fixer.edit(
        [{"block": "conditions", "old": "no such text", "new": "x"}], "x", {"because": "y"}
    )
    assert "这个块现在的内容" in miss and "set_condition" in miss


def test_fix_loop_with_explain_check_and_a_structured_edit(contracts: Contracts):
    from afsim_research_agent.falcon.llm import ModelRunner, ScriptedModel

    drafter = _fix_drafter(contracts, GUARDED)
    steps = [
        {"thought": "看为什么没过", "action": "explain_check", "args": {"id": "sees_target"}},
        {
            "thought": "等待项在进入时为假",
            "action": "set_condition",
            "args": {
                "name": "target_seen",
                "expr": "sense.target.valid",
                "because": "第 1 步：time_in_state() 项为假",
                "expect": ["sees_target"],
            },
        },
        {"action": "finish", "args": {"summary": "去掉等待"}},
    ]
    model = ScriptedModel({"fix:v3:step": steps})
    outcome = asyncio.run(fix_sheet(drafter, ModelRunner(model), max_steps=6))
    assert outcome.solved, [r["result"][:300] for r in outcome.log]
    assert [r["action"] for r in outcome.log] == ["explain_check", "set_condition"]
    assert "决定结果的项" in outcome.log[0]["result"]
    assert "explain_check" in model.calls[0][1] and "set_condition" in model.calls[0][1]
    assert "本轮试过的修改：" not in model.calls[0][1]


def test_events_are_triggers_not_condition_terms(contracts: Contracts):
    fixer = _fixer(contracts)
    refused = T.structured(fixer, "set_condition", {"name": "target_seen", "expr": "cancelled"})
    assert isinstance(refused, str) and "set_trigger" in refused
    texts = T.structured(fixer, "declare_event", {"name": "seen_tree", "source": "tree"})
    assert "set_trigger" in texts["_note"]
    hint = T.entry_true_hint(
        fixer, "ENTRY_TRUE 状态 MOVE → OBSERVE 的条件 target_seen 在进入 MOVE 时就成立"
    )
    assert '"role": "scout", "from": "MOVE", "to": "OBSERVE"' in hint and "declare_event" in hint
    assert "常量" in T.lint(fixer, {"conditions": "a = x"}, {"conditions": "a = false"})[0]


def test_hints_in_the_check_result_and_the_prompt(contracts: Contracts):
    from afsim_research_agent.falcon.v3.fixer import _prompt

    fixer = _fixer(contracts)
    msg = (
        '树事件 target_seen 要等 MOVE 的 parallel("all") 中所有分支都完成才触发……改用字段条件'
        "（如 sense.target.valid）"
    )
    hint = T.problem_hint(fixer, msg)
    assert '"expr": "sense.target.valid"' in hint and '"old_trigger": "target_seen"' in hint
    assert '"role": "scout", "from": "MOVE", "to": "OBSERVE"' in hint
    # an event written into a condition is refused for a text edit too
    out = fixer.edit(
        [
            {
                "block": "conditions",
                "old": "target_seen = sense.target.valid",
                "new": "target_seen = cancelled",
            }
        ],
        "x",
        {"because": "试"},
    )
    assert "引用了事件 cancelled" in out
    clash = fixer.apply(
        "set_condition", {"name": "cancelled", "expr": "sense.target.valid", "because": "x"}, "x"
    )
    assert "同名" in clash
    # the same unchanged block is shown twice at most
    assert "def OBSERVE" in fixer.show("pytree:scout")
    assert "没有变化" in fixer.show("pytree:scout")
    assert fixer.show("pytree:scout").startswith("不再显示")
    # once L1 passes, the prompt carries the explanation of the first failed check
    guarded = _fixer(contracts, GUARDED)
    prompt = _prompt(guarded, [], 5)
    assert "第一个未通过项的解释" in prompt and "决定结果的项" in prompt


def test_use_tree_event_declares_and_switches_in_one_step(contracts: Contracts):
    import dataclasses

    from afsim_research_agent.falcon.sheet.blocks import SheetProblem

    fixer = _fixer(contracts)
    # L1 passes: only for ENTRY_TRUE problems, refused with the way forward
    refused = T.structured(
        fixer, "use_tree_event", {"role": "scout", "from": "SHUTDOWN", "to": "DONE"}
    )
    assert isinstance(refused, str) and "只用于 L1 的 ENTRY_TRUE" in refused
    fixer.last = dataclasses.replace(
        fixer.last,
        problems=[SheetProblem("mermaid:scout", None, "ENTRY_TRUE x", code="ENTRY_TRUE")],
    )
    texts = T.structured(
        fixer, "use_tree_event", {"role": "scout", "from": "SHUTDOWN", "to": "DONE"}
    )
    assert "  shutdown_tree_done: {result: success, source: tree}" in texts["model"].splitlines()
    assert "SHUTDOWN --> DONE : shutdown_tree_done" in texts["mermaid:scout"]
    assert "改由树事件 shutdown_tree_done" in texts["_note"]
    # a negative condition name leaves when the tree fails; several in one call
    many = T.structured(
        fixer,
        "use_tree_event",
        {"items": [
            {"role": "scout", "from": "MOVE", "to": "OBSERVE", "old_trigger": "no_target"},
            {"role": "scout", "from": "SHUTDOWN", "to": "DONE"},
        ]},
    )  # fmt: skip
    assert isinstance(many, str) and "MOVE → OBSERVE" in many  # no_target is not its trigger
    many = T.structured(
        fixer,
        "use_tree_event",
        {"items": [
            {"role": "scout", "from": "MOVE", "to": "OBSERVE", "result": "failure"},
            {"role": "scout", "from": "SHUTDOWN", "to": "DONE"},
        ]},
    )  # fmt: skip
    assert "MOVE --> OBSERVE : move_tree_failed" in many["mermaid:scout"]
    assert "SHUTDOWN --> DONE : shutdown_tree_done" in many["mermaid:scout"]
    assert T._result_of("validation_failed") == "failure"
    assert T._result_of("task_invalid") == "success"  # L2 stubs actions as succeeding
    assert T._result_of("target_seen") == "success"
    assert fixer._block("model").text.count("tree_done") == 0  # nothing saved by structured


def test_a_change_refused_for_a_basis_goes_through_when_repeated(contracts: Contracts):
    fixer = _fixer(contracts, GUARDED)
    fixer.step = 1
    fix = {"name": "target_seen", "expr": "sense.target.valid"}
    assert "先读下面的解释" in fixer.apply("set_condition", fix, "去掉等待")
    fixer.step = 2
    done = fixer.apply("set_condition", dict(fix), "去掉等待，再试")
    assert done.startswith("已修改") and "同一修改第二次提交" in done


def test_problems_unmasked_by_a_fix_are_not_called_worse():
    from types import SimpleNamespace as N

    from afsim_research_agent.falcon.v3.fixer import _unmasked

    before = N(problems=[N(message="树事件 target_acquired 要等 SEARCH 第 3 行")])
    after = N(problems=[N(message=f"ROLE_COUNT 模型只有 1 辆车 {i}") for i in range(5)])
    assert "不要撤销" in _unmasked(before, after)
    assert _unmasked(before, N(problems=before.problems + after.problems)) == ""


def test_triage_stuck_on_failing_actions_and_tree_events_are_not_enter_leave():
    from afsim_research_agent.falcon.v3 import triage

    model = {
        "roles": {"member": {"mission": {"terminal": ["DONE"], "transitions": [], "global": []}}},
        "events": {"validating_tree_done": {"source": "tree", "result": "success"}},
    }
    entry = {
        "id": "main",
        "passed": False,
        "duration_s": 30,
        "states": {"member_0": [[0, "WAIT"], [2, "VALIDATING"], [2, "SELECTING"]]},
        "transitions": {
            "member_0": [
                {"t": 2, "from": "WAIT", "to": "VALIDATING", "on": "task_received"},
                {"t": 2, "from": "VALIDATING", "to": "SELECTING", "on": "validating_tree_done"},
            ]
        },
        "failures": {
            "member_0": [
                [2, "VALIDATING", "engagement.evaluate_fire_safety", "FRIENDLY_IN_LINE_OF_FIRE"],
                [2, "SELECTING", "engagement.score_weapon_candidates", "INVALID_PARAMETER"],
            ]
        },
    }
    found = triage.from_scenario(entry, model, {"scenarios": [{"id": "main"}]})
    assert [f.symptom for f in found] == ["STUCK_ACTION_FAILED"]
    text = found[0].render()
    assert "FRIENDLY_IN_LINE_OF_FIRE" in text and "side by side" in text


def test_set_expectation_scenario_and_test_edit_the_verification_by_id(contracts: Contracts):
    import yaml

    fixer = _fixer(contracts)
    assert "because" in T.structured(
        fixer, "set_expectation", {"scenario": "cancel", "id": "shut", "set": {"within_s": 8}}
    )
    texts = T.structured(
        fixer,
        "set_expectation",
        {"scenario": "cancel", "id": "shut", "set": {"within_s": 8}, "because": "需求：8 s 内"},
    )
    data = yaml.safe_load(texts["verification"])
    exp = data["scenarios"][0]["expect"][0]
    assert exp == {"id": "shut", "role": "scout", "reaches": "SHUTDOWN", "within_s": 8}
    assert "没有想定 nope" in T.structured(
        fixer, "set_expectation", {"scenario": "nope", "id": "shut", "because": "x"}
    )
    assert "不再检查任何内容" in T.structured(
        fixer,
        "set_expectation",
        {"scenario": "cancel", "id": "shut", "unset": ["reaches"], "because": "x"},
    )
    texts = T.structured(
        fixer,
        "set_scenario",
        {
            "scenario": "cancel",
            "set": {"events": [{"at_s": 5, "event": "cancelled"}]},
            "because": "x",
        },
    )
    assert yaml.safe_load(texts["verification"])["scenarios"][0]["events"][0]["at_s"] == 5
    assert "只能改" in T.structured(
        fixer, "set_scenario", {"scenario": "cancel", "set": {"expect": []}, "because": "x"}
    )
    texts = T.structured(
        fixer,
        "set_test",
        {"id": "sees_target", "actions": {"nav.goto": "failure"}, "because": "无效时检查失败"},
    )
    test = yaml.safe_load(texts["verification"])["tests"][0]
    assert test["given"]["actions"] == {"nav.goto": "failure"}
    assert test["given"]["blackboard"] == {"sense.target.valid": True}
    # committed like any change; the counts of tests / scenarios / expectations stay
    fixer.step = 1
    out = fixer.apply(
        "set_expectation",
        {"scenario": "cancel", "id": "shut", "set": {"within_s": 8}, "because": "需求：8 s 内"},
        "改时限",
    )
    assert out.startswith("已修改"), out


def test_a_judging_state_gets_both_tree_events_and_its_failure_test_stubbed(contracts: Contracts):
    sheet = (
        SHEET.replace(
            "  MOVE --> OBSERVE : target_seen\n",
            "  MOVE --> OBSERVE : target_seen\n  MOVE --> DONE : no_target\n",
        )
        .replace(
            "target_seen = sense.target.valid\n",
            "target_seen = sense.target.valid\nno_target = not sense.target.valid\n",
        )
        .replace(
            "  - id: sees_target\n",
            "  - id: no_target_done\n"
            "    given: {role: scout, state: MOVE, blackboard: {sense.target.valid: false}}\n"
            "    expect: {next_state: DONE}\n"
            "  - id: sees_target\n",
        )
    )
    fixer = _fixer(contracts, sheet)
    hint = T._decision_hint(fixer, "scout", "MOVE", "DONE", "no_target")
    assert hint is not None and "判断状态" in hint
    assert (
        '"to": "OBSERVE", "result": "success"' in hint
        and '"to": "DONE", "result": "failure"' in hint
    )
    assert 'set_test {"id": "no_target_done"' in hint and '"failure"' in hint


def test_triage_sequence_strict_names_the_set_expectation_call():
    from afsim_research_agent.falcon.v3 import triage

    entry = {
        "id": "main",
        "passed": False,
        "duration_s": 30,
        "render": "scenario main: FAIL\n  [FAIL] leader_main: visited ['A', 'B', 'A', 'B'], "
        "expected ['A', 'B'] (state_sequence is the whole visit sequence; to check only "
        "that these states come in this order write visits_in_order)",
        "states": {},
        "transitions": {},
    }
    found = triage.from_scenario(entry, {"roles": {}}, {"scenarios": [{"id": "main"}]})
    assert [f.symptom for f in found] == ["SEQUENCE_STRICT"]
    text = found[0].render()
    assert '"scenario": "main", "id": "leader_main", "kind": "visits_in_order"' in text


def test_set_test_under_a_wrapped_verification_block_and_subtree_stubs(contracts: Contracts):
    import yaml

    from afsim_research_agent.ir.v2.sim.run import _stub_outcomes

    body = SHEET.split("```yaml verification\n", 1)[1].split("```", 1)[0]
    indented = "verification:\n" + "".join("  " + ln + "\n" for ln in body.splitlines())
    fixer = _fixer(contracts, SHEET.replace(body, indented))
    texts = T.structured(
        fixer,
        "set_test",
        {"id": "sees_target", "actions": {"nav.goto": "failure"}, "because": "x"},
    )
    assert not isinstance(texts, str), texts
    data = yaml.safe_load(texts["verification"])
    assert data["verification"]["tests"][0]["given"]["actions"] == {"nav.goto": "failure"}
    sub = next(t for t, e in contracts.tactics.items() if e.section == "subtrees")
    inner = contracts.tactics[sub].raw.get("uses", {}).get("actions") or []
    out = _stub_outcomes({sub: "failure"}, contracts)
    assert all(out[a] == "failure" for a in inner)


def test_role_total_needs_as_many_own_vehicles_as_the_requirement():
    from afsim_research_agent.falcon.v3.semantics import role_total_problems as R

    one = {"roles": {"leader": {"count": 1}}}
    three = {"roles": {"leader": {"count": 1}, "wing": {"count": 2}}}
    assert R("三辆装甲车组成纵队，沿路线搜索前进", one)[0][2] == "ROLE_TOTAL"
    assert R("长车带两辆僚车前出", one) and R("2 辆战车编队前进", one)
    assert not R("三辆装甲车组成纵队", three)
    assert not R("发现敌方三辆坦克后报告", one)
    assert not R("敌方 3 辆车组成编队，我车观察", one)
    assert not R("单车前出观察，发现目标编队后报告", one)


def test_the_loop_drafts_a_missing_verification_block_once_l1_passes(contracts: Contracts):
    from afsim_research_agent.falcon.llm import ModelRunner, ScriptedModel

    body = SHEET.split("```yaml verification\n", 1)[1].split("```", 1)[0]
    bare = SHEET.replace("```yaml verification\n" + body + "```", "")
    drafter = _fix_drafter(contracts, bare)
    model = ScriptedModel(
        {
            "v3:verification": [{"text": "```yaml verification\n" + body + "```"}],
            "v3": [{"text": "```yaml verification\n" + body + "```"}],
            "fix:v3:step": [{"action": "finish", "args": {"summary": "ok"}}],
        }
    )
    outcome = asyncio.run(fix_sheet(drafter, ModelRunner(model), max_steps=4))
    assert outcome.log[0]["action"] == "draft_verification", outcome.log[0]
    assert "已按验证阶段补写" in outcome.log[0]["result"]
    assert any(b.kind == "verification" for b in drafter.blocks)


def test_simplify_follower_rewrites_the_role_and_its_tests(contracts: Contracts):
    fixer = _fixer(contracts)
    texts = T.structured(fixer, "simplify_follower", {"role": "scout"})
    assert isinstance(texts, dict), texts
    mer = texts["mermaid:scout"]
    assert "[*] --> FOLLOW" in mer and "FOLLOW --> SHUTDOWN : cancelled" in mer
    assert "SHUTDOWN --> COMPLETE : scout_shutdown_done" in mer
    tree = texts["pytree:scout"]
    assert 'tactic("sense.search")' in tree and "sense.off()" in tree  # old shutdown body kept
    assert "def MOVE" not in tree
    assert "scout_shutdown_done: {result: success, source: tree}" in texts["model"]
    assert "scout.FOLLOW" in texts["model"] and "scout.MOVE" not in texts["model"]
    ver = texts["verification"]
    assert "sees_target" not in ver and "scout_cancel_shutdown" in ver
    assert "reaches: SHUTDOWN" in ver and "within_s" not in ver
    assert "MOVE" in texts["_note"]
    assert "没有角色" in T.structured(fixer, "simplify_follower", {"role": "nobody"})


def test_a_show_loop_applies_the_call_the_check_result_wrote(contracts: Contracts, monkeypatch):
    from afsim_research_agent.falcon.llm import ModelRunner, ScriptedModel
    from afsim_research_agent.falcon.v3.fixer import ready_calls

    text = 'x set_condition {"name": "a", "expr": "b"} y simplify_follower {"role": "<role>"}'
    assert ready_calls(text) == [("set_condition", {"name": "a", "expr": "b"})]
    explain = T.explain_check
    fix = ' 改法：set_condition {"name": "target_seen", "expr": "sense.target.valid"}'
    monkeypatch.setattr(T, "explain_check", lambda f, c: explain(f, c) + fix)
    drafter = _fix_drafter(contracts, GUARDED)
    look = {"thought": "再看看", "action": "show", "args": {"block": "conditions"}}
    model = ScriptedModel({"fix:v3:step": [look] * 5 + [{"action": "finish", "args": {}}]})
    outcome = asyncio.run(fix_sheet(drafter, ModelRunner(model), max_steps=8))
    assert outcome.solved, [r["result"][-300:] for r in outcome.log]
    auto = [r for r in outcome.log if "自动修改" in r["result"]]
    assert len(auto) == 1 and "set_condition" in auto[0]["result"]
