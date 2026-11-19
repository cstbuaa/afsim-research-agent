"""Trajectory reward for GRPO on the repair loop (``fix_sheet``), design draft.

One trajectory = one run of the repair loop on one sheet. The reward is computed after
the run from the loop's own records, so the environment needs no change:

- ``score_start`` / ``score_end``: ``SheetFixer.score`` — (L1 not passed 0/1, number of L1
  problems, failed L2 tests + failed L3 scenarios (+1 when there are none));
- the step log: action name and the head of each tool result (refusals start with
  ``修改被拒绝``);
- two checks run once at the end, outside the loop: a held-out L3 scenario the loop never
  saw, and the requirement-faithfulness judge (the back-translation judge v2).

R = outcome + progress + process + cost, then the faithfulness gate, clipped to
[-1, 1.2]. See ``design/rl-reward-repair-v1.md (project doc)`` for the reasoning.
"""

from __future__ import annotations

from dataclasses import dataclass, field

Score = tuple[int, int, int]

# outcome
R_SOLVED = 1.0  # visible checks and the held-out scenario pass
R_SOLVED_VISIBLE_ONLY = 0.3  # visible checks pass, the held-out scenario does not
# progress (only when not solved)
# the most an unsolved run can earn is 0.5, half of a real fix
W_L1_GATE, W_L1_PROBLEMS, W_BEHAVIOUR = 0.15, 0.15, 0.2
CAP = 10  # problem counts above this count as this
# process penalties
# a change the lint refuses as a check bypass (constant condition, event in a condition)
P_BYPASS = 0.5
P_BYPASS_CAP = 1.0
P_GUARD = 0.1  # a time guard added to a condition (warned, not refused: some requirements wait)
P_EXPECT = 0.3  # an expectation changed without a ``because`` citing a requirement clause
P_INVALID = 0.05  # refused / repeated / old text not found / unparsable action
P_INVALID_CAP = 0.3
P_NO_EDIT = 0.1  # the run ended unsolved without one accepted change ("only looked")
# cost
P_STEP = 0.02  # per step, solved runs only (see the note on unsolved runs below)

BYPASS_MARKS = ("被写成常量", "里引用了事件", "和 model 里的事件")
INVALID_MARKS = ("修改被拒绝", "出现 0 次", "无法解析", "未知工具")


@dataclass
class Step:
    action: str
    result: str = ""
    accepted_change: bool = False  # a change applied to the sheet
    guard_warning: bool = False  # lint warned: time guard added to a condition
    expectation_changed: bool = False
    because_cites_clause: bool = False


@dataclass
class Trajectory:
    score_start: Score
    score_end: Score
    solved: bool  # the loop's own verdict (visible L1/L2/L3 and obligations)
    steps: list[Step] = field(default_factory=list)
    heldout_passed: bool | None = None  # None: the sheet has no held-out scenario
    faithful: bool = True


def _potential(s: Score) -> float:
    """0 (solved) … -0.5 (everything failing). Lexicographic order is kept: passing L1 is
    worth a step of its own, problem counts are normalised and capped."""
    l1_fail, problems, failed = s
    return -(
        W_L1_GATE * l1_fail
        + W_L1_PROBLEMS * min(problems, CAP) / CAP
        + W_BEHAVIOUR * min(failed, CAP) / CAP
    )


def reward(t: Trajectory) -> tuple[float, dict[str, float]]:
    """(reward, parts) for one trajectory; ``parts`` is for logging and analysis."""
    parts: dict[str, float] = {}

    # 1 outcome
    if t.solved:
        parts["outcome"] = R_SOLVED if t.heldout_passed in (True, None) else R_SOLVED_VISIBLE_ONLY
        parts["progress"] = 0.0
    else:
        parts["outcome"] = 0.0
        # potential-based shaping: only the net change counts, so going worse and back
        # again earns nothing; a worse sheet is negative
        parts["progress"] = max(-0.3, _potential(t.score_end) - _potential(t.score_start))

    # 2 process
    bypass = sum(any(m in s.result for m in BYPASS_MARKS) for s in t.steps)
    invalid = sum(
        any(m in s.result for m in INVALID_MARKS) and not any(m in s.result for m in BYPASS_MARKS)
        for s in t.steps
    )
    parts["bypass"] = -min(P_BYPASS_CAP, P_BYPASS * bypass)
    parts["guard"] = -P_GUARD * sum(s.guard_warning for s in t.steps)
    parts["expectation"] = -P_EXPECT * sum(
        s.expectation_changed and not s.because_cites_clause for s in t.steps
    )
    parts["invalid"] = -min(P_INVALID_CAP, P_INVALID * invalid)
    no_edit = not t.solved and not any(s.accepted_change for s in t.steps)
    parts["no_edit"] = -P_NO_EDIT if no_edit else 0.0

    # 3 cost: only for solved runs. On an unsolved run a step penalty would make giving
    # up early the best strategy whenever no rollout of the group solves the sheet.
    parts["steps"] = -P_STEP * len(t.steps) if t.solved else 0.0

    r = sum(parts.values())
    # 4 gate: a sheet that no longer says what the requirement says earns nothing positive
    if not t.faithful:
        parts["faithfulness_gate"] = -max(0.0, r)
        r = min(0.0, r)
    return max(-1.0, min(1.2, r)), parts


def group_advantages(rewards: list[float], eps: float = 1e-6) -> list[float]:
    """GRPO advantages for one prompt's group: (r - mean) / std. A group whose rewards
    are all equal gives no gradient; such prompts are filtered (dynamic sampling)."""
    n = len(rewards)
    mean = sum(rewards) / n
    std = (sum((r - mean) ** 2 for r in rewards) / n) ** 0.5
    if std < eps:
        return [0.0] * n
    return [(r - mean) / std for r in rewards]
