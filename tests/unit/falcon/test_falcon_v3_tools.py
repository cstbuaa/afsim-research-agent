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
