"""Self-checking repair loop for tactic sheets v3 (``falcon draft3`` after drafting).

The model works like a developer with a test suite: each step it calls one tool (a JSON
action), sees a short result, and goes on until the checks pass or the budget runs out.
The deterministic checks decide; the model only proposes (design agent-loop-deepagents-v1
§4, ported to v3).

Tools: ``check``, ``show``, ``lookup`` (contracts, tactic-set subtrees and examples,
blackboard fields), ``explain`` (one problem with its context, e.g. which referenced
subtree already holds a resource), ``try_tick`` (one L2 step), ``simulate`` (one L3
scenario), ``edit`` (several blocks at once, applied together), ``revert``, ``finish``,
``ask_human``; ``diagnose`` (issue, place, cause, plan: required before editing once
L1 passes), ``run_afsim`` (re-run AFSIM on the current sheet, when AFSIM is in the loop) and
``classify`` (a failed check that is not the sheet's to fix: scenario, judge, capability).

Repair tools v2 (falcon.v3.tools, design ``repair-tools-v2``): ``explain_check`` (why one
check fails, condition terms evaluated), ``eval_condition``, ``who_writes``, ``history``;
structured edits (``set_condition``, ``add_transition`` / ``remove_transition`` /
``set_trigger``, ``declare_event``, ``set_state_tree``, ``attach_subtree``, ``rename``) and
``try_edit`` (dry run); ``revert`` by step. Once L1 passes a change carries ``because`` (it
is then its own diagnosis) or follows a ``diagnose``; ``expect`` names the checks it should
make pass. A refused change says what to do instead; the same resulting sheet twice is
refused; a time guard added to a condition is warned about, a constant condition refused.

Rule triage (falcon.v3.triage) comes first: every failed run is classified (the sheet's
model or verification block / the scenario / a false alarm of the judge / a missing
capability) and the sheet's failures come with their likely cause and change. The loop does
not start, or ends, when nothing left is the sheet's; it may not classify a failure the rules
put on the sheet; after a diagnosis it may read at most twice before editing, and finish is
refused while a diagnosis has not been followed by an edit.

Guards enforced here, not only in the prompt:

* an edit that breaks L1 when it passed before is not applied;
* tests, scenarios and scenario expectations may not be removed;
* the reference policy, state names and scenario events are the compiler's to enforce
  (an edit that violates them shows up as an L1 error and is refused);
* every applied edit is logged with the text before and after, for the review page;
* once L1 passes, an edit needs a diagnosis (``diagnose``) and may only touch the blocks it
  names; a failed AFSIM re-run voids the diagnosis (the planned change did not fix it);
* with AFSIM in the loop the ``afsim`` check stays failed until AFSIM passes on the current
  sheet; AFSIM re-runs by itself once every 2D check passes, and finish is not accepted
  before (the forge / skill pattern: a fix counts only when the real checker passes it).
"""

from __future__ import annotations

import difflib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from afsim_research_agent.falcon.llm import ModelRunner
from afsim_research_agent.falcon.sheet.blocks import Block
from afsim_research_agent.falcon.v3 import catalog as K
from afsim_research_agent.falcon.v3 import tools as T
from afsim_research_agent.falcon.v3 import triage
from afsim_research_agent.ir.v2.sim import simulate
from afsim_research_agent.progress import report

RESULT_LIMIT = 5000
MAX_READS = 4  # consecutive show calls without an edit before show is refused
STALL = 8  # steps without beating the best score: stop
READ_ALLOWANCE = 6  # reads after the last progress that do not count towards the stall
HISTORY = 5
SHOW_LINES = 120
LOOKS = 5  # reads in a row (show / lookup / explain / try_tick / simulate) before a
# diagnosis is written; more are refused once L1 passes
LOOKS_AFTER_DIAGNOSIS = 2  # reads between a diagnosis and its edit
READS = ("show", "lookup", "explain", "try_tick", "simulate", *T.READ_TOOLS, "try_edit")
WRITES = ("edit", "revert", *T.WRITE_TOOLS)
NO_GAIN = 3  # applied or refused changes in a row without a better score: explain again
OUTSIDE = (triage.SCENARIO, triage.JUDGE, triage.CAPABILITY)


class FixStep(BaseModel):
    thought: str = ""
    action: Literal[
        "check",
        "show",
        "lookup",
        "explain",
        "try_tick",
        "simulate",
        "edit",
        "revert",
        "finish",
        "ask_human",
        "diagnose",
        "run_afsim",
        "classify",
        "explain_check",
        "eval_condition",
        "who_writes",
        "history",
        "try_edit",
        "set_condition",
        "add_transition",
        "remove_transition",
        "set_trigger",
        "declare_event",
        "set_state_tree",
        "attach_subtree",
        "rename",
    ]
    args: dict[str, Any] = Field(default_factory=dict)


@dataclass
class EditRecord:
    step: int
    block: str
    before: str
    after: str
    reason: str
    score_before: tuple[int, int, int]
    score_after: tuple[int, int, int]


@dataclass
class FixOutcome:
    solved: bool
    steps: int
    edits: list[EditRecord] = field(default_factory=list)
    log: list[dict[str, Any]] = field(default_factory=list)
    question: str | None = None
    summary: str = ""
    score_start: tuple[int, int, int] = (0, 0, 0)
    score_end: tuple[int, int, int] = (0, 0, 0)
    diagnoses: list[dict[str, Any]] = field(default_factory=list)
    afsim_runs: int = 0  # AFSIM re-runs inside the loop
    # failed checks recorded as not the sheet's to fix (classify): check, category, reason
    classified: list[dict[str, Any]] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)  # triage at the end
    diagnosed_unfixed: bool = False  # diagnosed, but no edit was ever applied


def _clip(text: str, limit: int = RESULT_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 20] + "\n…（已截断）"


def _copy(blocks: list[Block]) -> list[Block]:
    return [Block(b.kind, b.role, b.text, b.line, b.info) for b in blocks]


def _counts(blocks: list[Block]) -> dict[str, int]:
    block = next((b for b in blocks if b.kind == "verification"), None)
    if block is None:
        return {"tests": 0, "scenarios": 0, "expectations": 0}
    try:
        data = yaml.safe_load(block.text) or {}
    except yaml.YAMLError:
        return {"tests": -1, "scenarios": -1, "expectations": -1}
    if not isinstance(data, dict):  # an edit left the block as a list or a scalar
        return {"tests": -1, "scenarios": -1, "expectations": -1}
    if isinstance(data.get("verification"), dict):
        data = data["verification"]
    tests = data.get("tests") or []
    scenarios = data.get("scenarios") or []
    return {
        "tests": len(tests) if isinstance(tests, list) else 0,
        "scenarios": len(scenarios) if isinstance(scenarios, list) else 0,
        "expectations": sum(len(s.get("expect") or []) for s in scenarios if isinstance(s, dict))
        if isinstance(scenarios, list)
        else 0,
    }


RULES = """你在修正一份战术稿（v3）。每一步调用一个工具，输出 JSON：
{"thought": "简短想法", "action": "<工具名>", "args": {…}}

定位（不改稿件）：
- check {}：重新检查，返回 L1 问题（带编号）和 L2 / L3 未通过项。
- explain_check {"id": L1 问题编号 / L2 测试 ID / L3 想定 ID}：这一项为什么没过——期望什么、实际怎样、
  当时每个出口条件逐项的取值（标出决定结果的项）、本轮改过哪些相关条件。不知道原因时先用它。
- eval_condition {"role", "state", "name": 条件或事件名（不写则列出全部出口）, "given": {"blackboard": {…}, "events": […], "time_in_state_s": 0, "actions": {…}}}：
  在给定情况下逐项求值。
- who_writes {"field": 黑板字段}：哪些动作写它、写成什么值，本稿件哪些状态运行这些动作，哪些条件读它。
- show {"block": "块名", "start": 行号, "end": 行号}：查看一个块（带整份稿件的行号）。
- lookup {"id": "动作 ID / 战术集 ID / 黑板字段 / 切片 ID"}：查契约、子树写法、字段类型与取值。
- explain {"problem": 问题编号}：L1 问题的上下文（资源冲突的分支等）。
- try_tick {"role", "state", "given": {…}}：用临时测试跑一拍；simulate {"scenario": 想定 ID}：跑一个 L3 想定的完整轨迹。
- history {}：本轮试过的修改和各自的得分变化。

修改（改完自动检查；L1 通过后要带 because：依据哪一步的哪个结论，可选 expect：预期哪些检查会通过）：
- set_condition {"name": 条件名, "expr": 表达式}：改写或新增 conditions 里的一个条件。
- add_transition / remove_transition {"role", "from", "to", "trigger"}：增删状态图转换（from 写 * 表示任意状态）。
- set_trigger {"role", "from", "to", "trigger": 新触发, "old_trigger": 原触发（有多条时）}。
- declare_event {"name", "source": "tree"|"timer"|"external", "result": "success"|"failure"（tree）, "after": "5 s"（timer）}：
  在 model 的 events 中声明事件（例如本状态的树结束时触发）。
- set_state_tree {"role", "state", "code": "def STATE():\n    …"}：整体替换一个状态的树；
  attach_subtree {"role", "state", "subtree": 战术集子树 ID, "position": "start"|"end"}：在状态树里引用子树。
- rename {"kind": "trigger"|"condition"|"event"|"state", "old", "new"}：一次改完所有引用处。
- edit {"changes": [{"block", "old": "原文片段", "new": "新文本"} 或 {"block", "text": "整块新内容"}]}：文本替换（兜底）；old 必须唯一。
- try_edit {"op": 上面任一修改工具名, "args": {…}}：试改，只返回得分变化，不保存。
- revert {"block": 块名} 或 {"step": 步数}：恢复一个块的初始内容，或撤销某一步及之后的修改。

流程：
- diagnose {"issue", "where": [块名], "cause", "plan"}：写下诊断（可选；修改带 because 时等同于诊断）。
- classify {"check": 失败项 ID, "category": "scenario|judge|capability", "reason"}：记录不是战术稿的问题
  （想定/绑定没给出条件、判定误报、能力集缺能力）；"定位与改法"标为稿件的问题不能这样记录。
- run_afsim {}：用真实 AFSIM 复验（只在有 AFSIM 反馈时可用）；finish {"summary"}：结束；ask_human {"question"}：需要人决定时提问。

规则：
- 块名写法：model、mermaid:<角色>、conditions、pytree:<角色>、gaps、verification。
- 状态图里的触发名与 conditions / events 的名字必须一致（rename 会一起改）。
- 状态树和任务状态机自己写；只能用 tactic("…") 引用战术集子树，不能引用战术集状态树和任务。
- 引用的战术集子树已经包含的动作，不要在并行分支里再写一遍（会争用同一独占资源）。
- 不能删测试、想定和想定期望；期望与需求不符时可以改期望（带 because 引用需求原文），不能为了通过而改成与需求不符的期望。
- 检查结果里有"定位与改法"时先照它处理；标为想定/判定误报/能力缺失的用 classify 记录，不要改稿件。
- 修改没有达到预期时，先 explain_check 弄清原因再换改法；不要重复提交同样的修改。
- 只给条件追加 time_in_state() 等待只会推迟出口：需求要求等待时才这样写，否则让条件读本状态动作写入的字段或用树事件。
- 位置和路线只用 point=<接口点> / route=<接口路线>，不要写经纬度，也不要改成 latitude_deg 等参数。
- 保持需求的意图；只修正表达错误和遗漏。检查通过后调用 finish。"""


class SheetFixer:
    """The tools of the loop over a :class:`~afsim_research_agent.falcon.v3.draft.Drafter`."""

    def __init__(self, drafter: Any, l1_only: bool = False) -> None:
        self.d = drafter
        # before the verification stage there are no tests to pass: L1 is the goal
        self.l1_only = l1_only
        self.original = {b.key: b.text for b in drafter.blocks}
        self.counts0 = _counts(drafter.blocks)
        self.edits: list[EditRecord] = []
        self.step = 0
        self.last = drafter.check(verify=True)
        self.best = (self.score(self.last), _copy(drafter.blocks), self.last)
        self.refused = 0
        self.shown: dict[tuple[Any, ...], tuple[str, int]] = {}
        self.reads = 0  # consecutive show calls
        self.diagnosis: dict[str, Any] | None = None  # the plan edits must follow
        self.diagnoses: list[dict[str, Any]] = []  # every diagnosis, for the review page
        self.afsim_runs = 0  # AFSIM re-runs started by this loop
        self.looks = 0  # reads in a row (any read tool) since the last diagnosis / edit
        self.edited_since_diagnosis = False
        self.classified: list[dict[str, Any]] = []
        self.finish_refusals = 0
        self.attempts: list[dict[str, Any]] = []  # changes that were not applied, and why
        self.tried: dict[int, tuple[int, tuple[int, int, int]]] = {}  # sheet → (step, score)
        self.snapshots: dict[int, list[Block]] = {}  # step → blocks before its change
        self.no_gain = 0

    # ------------------------------------------------------------------ scoring
    @staticmethod
    def score(check: Any) -> tuple[int, int, int]:
        failed = sum(not t["passed"] for t in check.l2) + sum(not s["passed"] for s in check.l3)
        empty = 0 if (check.l2 or check.l3) else 1
        return (0 if check.l1_ok else 1, len(check.problems), failed + empty)

    @property
    def solved(self) -> bool:
        if self.l1_only:
            return bool(self.last.l1_ok)
        return self.last.verified

    def _recheck(self) -> Any:
        self.last = self.d.check(verify=True)
        s = self.score(self.last)
        if s < self.best[0]:
            self.best = (s, _copy(self.d.blocks), self.last)
        return self.last

    # ------------------------------------------------------------------ tools
    def check(self) -> str:
        return self.summary(self._recheck())

    def summary(self, check: Any | None = None) -> str:
        check = check or self.last
        lines = [f"L1：{'通过' if check.l1_ok else '未通过'}"]
        for i, p in enumerate(check.problems, start=1):
            lines.append(f"[{i}] {p.render()[2:]}")
            lines += self._context(p.block, p.line, 2)
        if check.l1_ok:
            failed = [t for t in check.l2 if not t["passed"]]
            lines.append(f"L2：{len(check.l2) - len(failed)}/{len(check.l2)} 通过")
            lines += [f"  - {t['id']}: {t['detail']}" for t in failed[:12]]
            for s in check.l3:
                lines.append(f"L3 {s['id']}：{'通过' if s['passed'] else '未通过'}")
                if not s["passed"]:
                    keep = 24 if s.get("afsim") else 6
                    # the AFSIM feedback's own triage is shown once, below, with the 2D one
                    render = s["render"].split("  定位与改法", 1)[0]
                    lines += ["    " + x for x in render.splitlines()[1:keep]]
                    spec = self.last.compiled.spec if getattr(self.last, "compiled", None) else None
                    lines += ["    " + x for x in _never_entered(s, spec or {})]
            lines += self._triage_lines(check)
            if not check.l2 and not check.l3 and not self.l1_only:
                lines.append("还没有可运行的测试或想定（verification 块）")
        if self.solved and self.l1_only:
            lines.append("L1 已通过，可以 finish；测试和想定由之后的验证阶段编写。")
        elif self.solved:
            lines.append("全部检查通过，可以 finish。")
        return "\n".join(lines)

    # ------------------------------------------------------------------ triage
    def findings(self, check: Any | None = None) -> list[triage.Finding]:
        """Rule findings for the failed checks: the 2D scenarios', and the AFSIM run's while
        its check is failed."""
        check = check or self.last
        out = triage.from_check(check)
        if any(s.get("afsim") and not s["passed"] for s in check.l3):
            note = getattr(self.d, "afsim_note", None)
            out += list(getattr(note, "findings", None) or [])
        return out

    def _triage_lines(self, check: Any) -> list[str]:
        found = self.findings(check)
        done = {c["check"] for c in self.classified}
        found = [f for f in found if not (f.check in done and not f.sheet)]
        return triage.render(found)

    def _explained(self, check_id: str, found: list[triage.Finding]) -> bool:
        """A failed check that is not the sheet's to fix: every finding on it says so (or
        none does and the loop classified it)."""
        mine = [f for f in found if f.check == check_id]
        if any(f.sheet for f in mine):
            return False
        return bool(mine) or any(c["check"] == check_id for c in self.classified)

    def outside_left(self) -> bool:
        """L1 passes and every failed L2/L3/AFSIM check is someone else's to fix."""
        check = self.last
        if not check.l1_ok or any(not t["passed"] for t in check.l2):
            return False
        failed = [str(s["id"]) for s in check.l3 if not s["passed"]]
        if not failed:
            return False
        found = self.findings(check)
        return all(self._explained(cid, found) for cid in failed)

    def classify(self, args: dict[str, Any]) -> str:
        cid = str(args.get("check") or "").strip()
        category = str(args.get("category") or "").strip()
        reason = str(args.get("reason") or "").strip()
        failed = [str(s["id"]) for s in self.last.l3 if not s["passed"]]
        if not cid or cid not in failed:
            return f"check 写失败项的 ID；当前未通过的：{failed or '无'}"
        if category not in OUTSIDE:
            return f"category 只能是 {', '.join(OUTSIDE)}；稿件自己的问题请 diagnose 后 edit"
        if not reason:
            return "写出 reason：为什么这不是战术稿的问题"
        mine = [f for f in self.findings() if f.check == cid and f.sheet]
        if mine:
            self.refused += 1
            return "不接受：规则判定这一项是稿件的问题，要修改稿件：\n" + "\n".join(
                "  " + f.render() for f in mine[:3]
            )
        self.classified.append(
            {"step": self.step, "check": cid, "category": category, "reason": reason[:300]}
        )
        self.looks = 0
        left = [c for c in failed if not self._explained(c, self.findings())]
        return f"已记录：{cid} 归为 {triage.CATEGORY_TEXT[category]}。" + (
            f"还要处理的失败项：{left}" if left else "其余失败项都不是稿件的问题，可以 finish。"
        )

    def read_refused(self, action: str) -> str | None:
        """Reads stop once there is enough to act on (L2/L3/AFSIM stage)."""
        if not self.needs_diagnosis:
            return None
        if self.diagnosis is not None and not self.edited_since_diagnosis:
            if self.looks >= LOOKS_AFTER_DIAGNOSIS:
                self.refused += 1
                return (
                    f"{action} 被拒绝：诊断之后已经查看 {self.looks} 次。按诊断的改法修改"
                    f"（块：{', '.join(self.diagnosis['where'])}）；改法不对就重新 diagnose；"
                    "不是稿件的问题就 classify"
                )
        elif self.looks >= LOOKS:
            self.refused += 1
            return (
                f"{action} 被拒绝：已经连续查看 {self.looks} 次。照检查结果里的"
                '"定位与改法"带上 because 修改，或写 diagnose（标为稿件的问题）、classify（其余问题）'
            )
        return None

    def _conditions(self) -> dict[str, str]:
        """Condition id → its expression, compact (for diagnostics)."""
        spec = self.last.compiled.spec if getattr(self.last, "compiled", None) else None
        out = {}
        for c in (spec or {}).get("conditions") or []:
            if isinstance(c, dict) and c.get("id"):
                out[str(c["id"])] = _clip(json.dumps(c.get("expression"), ensure_ascii=False), 240)
        return out

    def _block(self, key: str) -> Block | None:
        return next((b for b in self.d.blocks if b.key == key), None)

    def _context(self, key: str, line: int | None, around: int) -> list[str]:
        """The sheet lines around a problem, so an edit needs no ``show`` first."""
        block = self._block(key)
        if block is None or not line:
            return []
        rows = block.text.splitlines()
        i = line - block.line
        if not 0 <= i < len(rows):
            return []
        lo, hi = max(0, i - around), min(len(rows), i + around + 1)
        return [
            f"     {block.line + k:4d}{'>' if k == i else ' '}| {rows[k]}" for k in range(lo, hi)
        ]

    def _line_text(self, key: str, line: int | None) -> str:
        block = self._block(key)
        if block is None or not line:
            return ""
        rows = block.text.splitlines()
        i = line - block.line
        return rows[i].strip()[:160] if 0 <= i < len(rows) else ""

    def show(self, key: str, start: Any = None, end: Any = None) -> str:
        block = self._block(key)
        if block is None:
            return f"没有块 {key}；现有：{', '.join(b.key for b in self.d.blocks)}"
        # the content is always shown again: a pointer to an earlier step ("see step 1")
        # left a model that had lost that step from its history re-reading the same lines
        # until the loop ran out (formation batch 10)
        note = ""
        seen = self.shown.get((key, start, end))
        if seen is not None and seen[0] == block.text:
            note = (
                f"\n（与第 {seen[1]} 步看到的相同，战术稿没有变化。下一步请用 edit 修改"
                "（old 抄上面的原文片段），或用 ask_human 说明为什么不该改模型）"
            )
        elif self.reads >= MAX_READS:
            note = (
                f"\n（已经连续查看 {self.reads} 次而没有修改。问题和出错行已在检查结果里给出："
                "下一步请直接 edit；认为问题出在测试或仿真本身时用 ask_human 说明）"
            )
        else:
            self.shown[(key, start, end)] = (block.text, self.step)
        rows = block.numbered().splitlines()
        first = block.line
        lo = int(start) - first if isinstance(start, int) else 0
        hi = int(end) - first + 1 if isinstance(end, int) else lo + SHOW_LINES
        part = rows[max(0, lo) : max(lo + 1, hi)]
        more = "" if hi >= len(rows) else f"\n（还有 {len(rows) - hi} 行，用 start/end 查看）"
        return f"```{block.label}\n" + "\n".join(part) + "\n```" + more + note

    def lookup(self, ref: str) -> str:
        c = self.d.c
        ref = str(ref).strip().strip('"')
        if ref in c.actions:
            a = c.actions[ref]
            cap = c.catalog.capabilities.get(ref)
            return "\n".join(
                [
                    f"动作 {ref}（切片 {a.slice}，{a.kind}）",
                    f"说明：{(cap.raw.get('description') if cap else '') or ''}",
                    "参数：" + json.dumps(a.parameters, ensure_ascii=False),
                    "独占资源：" + (", ".join(sorted(a.exclusive)) or "无"),
                    "完成判据：" + json.dumps(a.success, ensure_ascii=False),
                    "失败码：" + ", ".join(a.failure_codes),
                    *[
                        f"  {code}：{text}"
                        for code, text in ((a.result or {}).get("codes") or {}).items()
                    ],
                    *([K.result_text(a.result, a.persistent)] if a.result else []),
                    *(["读 " + ", ".join(a.reads)] if a.reads else []),
                    *([K._writes_text(c, a)] if a.writes is not None else []),
                ]
            )
        if ref in c.tactics:
            t = c.tactics[ref]
            kind = {"subtrees": "子树（可引用）", "state_trees": "状态树（只能参照）"}.get(
                t.section, "任务（只能参照）"
            )
            if t.section == "missions":
                return f"战术集任务 {ref}：" + json.dumps(t.raw, ensure_ascii=False)[:3000]
            result = K.result_text(t.raw.get("result"), t.raw.get("persistent"))
            return (
                f"战术集{kind} {ref}\n"
                + (f"# {result}\n" if result else "")
                + K.entry_python(c, t, "_" + ref.split(".")[-1])
            )
        if ref in c.blackboard:
            f = c.blackboard[ref]
            return (
                f"黑板字段 {ref}：类型 {f.type}"
                + (f"，量纲 {f.dimension}" if f.dimension else "")
                + (f"，取值 {'/'.join(f.enum)}" if f.enum else "，没有列出取值")
                + f"，切片 {f.slice}\n"
                + K.field_text(c, ref)
            )
        if ref in K.slices(c):
            return K.details(c, [ref], self.d.r.reference_policy)[:RESULT_LIMIT]
        pool = sorted(set(c.actions) | set(c.tactics) | set(c.blackboard))
        near = difflib.get_close_matches(ref, pool, n=8, cutoff=0.4)
        return f"没有找到 {ref}" + (f"；相近的有：{', '.join(near)}" if near else "")

    def explain(self, index: Any) -> str:
        try:
            p = self.last.problems[int(index) - 1]
        except (ValueError, TypeError, IndexError):
            return f"没有问题 {index}；当前共 {len(self.last.problems)} 个"
        out = [p.render()[2:]]
        block = self._block(p.block)
        if block is not None and p.line:
            rows = block.text.splitlines()
            i = p.line - block.line
            lo, hi = max(0, i - 4), min(len(rows), i + 5)
            out.append("上下文：")
            out += [f"{block.line + k:4d} | {rows[k]}" for k in range(lo, hi)]
        if "ARB001" in p.message:
            out += self._arb_context(p.message)
        if "FSM002" in p.message or "没有定义" in p.message or "不是事件" in p.message:
            spec = self.last.compiled.spec or {}
            out.append("已定义的事件：" + ", ".join(e["id"] for e in spec.get("events") or []))
            out.append("已定义的条件：" + ", ".join(c["id"] for c in spec.get("conditions") or []))
        return "\n".join(out)

    def _arb_context(self, message: str) -> list[str]:
        """Which branch of the parallel holds which exclusive resource, and through which
        referenced subtree."""
        m = re.search(r"state_trees\.(\w+)\.(\w+)", message)
        if not m:
            return []
        rid, state = m.group(1), m.group(2)
        from afsim_research_agent.ir.v2 import normalize

        spec = self.last.compiled.spec or {}
        result = normalize(spec, self.d.c)
        model = result.model or {}
        tree = (((model.get("roles") or {}).get(rid) or {}).get("states") or {}).get(state, {})
        root = tree.get("tree")
        par = _find(root, lambda n: n.get("type") == "parallel")
        if par is None:
            return []
        lines = ["并行分支占用的独占资源："]
        for i, kid in enumerate(par.get("children") or []):
            holders: dict[str, set[str]] = {}
            for leaf in _leaves(kid):
                contract = self.d.c.actions.get(str(leaf.get("action")))
                for res in sorted(contract.exclusive if contract else ()):
                    holders.setdefault(res, set()).add(
                        f"{leaf.get('action')}（来自 {leaf.get('origin')}）"
                    )
            desc = "; ".join(f"{r}: {', '.join(sorted(v))}" for r, v in holders.items())
            lines.append(f"  分支 {i}：{desc or '无'}")
        lines.append("同一资源只能由一个分支持有：去掉重复的那一份，或改成顺序执行。")
        return lines

    def try_tick(self, role: str, state: str, given: Any) -> str:
        model = self.last.model
        if model is None:
            return "L1 未通过，还不能试跑"
        given = given if isinstance(given, dict) else {}
        case = {
            "id": "try_tick",
            "given": {"role": role, "state": state, **given},
            "expect": {},
        }
        from afsim_research_agent.ir.v2.runtime import Instance
        from afsim_research_agent.ir.v2.sim.run import TestCase, _StubHost

        try:
            tc = TestCase.model_validate(case)
        except Exception as exc:  # noqa: BLE001 - shown to the model
            return f"given 写法不对：{exc}"
        g = tc.given
        if g.role not in model["roles"]:
            return f"没有角色 {g.role}"
        inst = Instance(f"{g.role}_0", g.role, 0, model, self.d.c, _StubHost(g.actions))
        if g.state not in inst.mission["states"]:
            return f"没有状态 {g.state}"
        inst.enter(g.state, -g.time_in_state_s)
        inst.tree_status = g.tree_status
        inst.blackboard.update(g.blackboard)
        for name in g.events:
            inst.raise_event(name)
        inst.step(0.0)
        path = " → ".join([g.state, *[t["to"] for t in inst.trace]])
        fired = "; ".join(f"{t['from']}→{t['to']} 当 {t['on']}" for t in inst.trace) or "无"
        return (
            f"状态：{path}\n触发的转换：{fired}\n树的结果：{inst.tree_status}\n"
            f"下达的动作：{', '.join(inst.issued) or '无'}"
        )

    def simulate(self, scenario: str) -> str:
        model = self.last.model
        if model is None:
            return "L1 未通过，还不能仿真"
        raw = next(
            (
                s
                for s in (self.last.compiled.spec or {}).get("scenarios") or []
                if s.get("id") == scenario
            ),
            None,
        )
        if raw is None:
            ids = [s.get("id") for s in (self.last.compiled.spec or {}).get("scenarios") or []]
            return f"没有想定 {scenario}；现有：{ids}"
        return simulate(model, self.d.c, raw, self.d.binding(model)).render()

    # ------------------------------------------------------------------ diagnosis / AFSIM
    @property
    def needs_diagnosis(self) -> bool:
        """Once L1 passes (L2 / L3 / AFSIM problems), edits follow a written diagnosis."""
        return not self.l1_only and bool(self.last.l1_ok)

    def diagnose(self, args: dict[str, Any]) -> str:
        issue = str(args.get("issue") or "").strip()
        cause = str(args.get("cause") or "").strip()
        plan = str(args.get("plan") or "").strip()
        where = args.get("where") or []
        if isinstance(where, str):
            where = [where]
        where = [str(w).strip() for w in where if str(w).strip()]
        missing = [
            k
            for k, v in (("issue", issue), ("where", where), ("cause", cause), ("plan", plan))
            if not v
        ]
        if missing:
            return f"诊断不完整，缺 {', '.join(missing)}：写出问题原文、所在块、原因和改法"
        unknown = [w for w in where if self._block(w) is None]
        if unknown:
            return f"where 中的块 {unknown} 不存在；现有：{', '.join(b.key for b in self.d.blocks)}"
        if self.diagnosis is not None and self.diagnosis.get("issue") == issue[:400]:
            # the same issue diagnosed again (a model that keeps planning instead of editing)
            self.diagnosis.update(where=where, cause=cause[:400], plan=plan[:600])
            return (
                f"这个问题已经诊断过（第 {self.diagnosis['step']} 步），已按新写法更新。不要再重复"
                f"诊断：下一步直接 edit {', '.join(where)}（old 抄原文片段，new 写改后的文本）。"
            )
        self.diagnosis = {
            "step": self.step,
            "issue": issue[:400],
            "where": where,
            "cause": cause[:400],
            "plan": plan[:600],
        }
        self.diagnoses.append(dict(self.diagnosis))
        self.looks = 0
        self.edited_since_diagnosis = False
        return (
            f"诊断已记录（可改的块：{', '.join(where)}）。下一步按计划 edit"
            f"（之后最多再查看 {LOOKS_AFTER_DIAGNOSIS} 次）。"
        )

    def _allowed(self, key: str) -> bool:
        """A block the current diagnosis allows editing. A trigger renamed in a state
        diagram is renamed in conditions / model events in the same step, so those go along
        with any listed mermaid block (and the other way round)."""
        where = set((self.diagnosis or {}).get("where") or [])
        if key in where:
            return True
        kind = key.split(":")[0]
        kinds = {w.split(":")[0] for w in where}
        if kind in ("conditions", "model") and "mermaid" in kinds:
            return True
        return kind == "mermaid" and bool(kinds & {"conditions", "model"})

    @property
    def afsim_pending(self) -> bool:
        """AFSIM is in the loop and has not passed the current sheet."""
        return any(s.get("afsim") and not s["passed"] for s in self.last.l3)

    def _only_afsim_left(self) -> bool:
        check = self.last
        return (
            bool(check.l1_ok)
            and self.afsim_pending
            and all(t["passed"] for t in check.l2)
            and all(s["passed"] or s.get("afsim") for s in check.l3)
        )

    async def run_afsim(self, auto: bool = False) -> str:
        probe = getattr(self.d, "afsim_probe", None)
        if probe is None:
            return "这一轮没有 AFSIM 复验"
        from afsim_research_agent.falcon.v3.afsim_loop import sheet_hash

        known = getattr(self.d, "afsim_verdicts", {}).get(sheet_hash(self.d.sheet))
        if known is not None:
            return "AFSIM 已经判定过这份稿件（之后没有改动）：" + (
                "通过" if known.passed else "未通过，见检查结果中的 afsim 项"
            )
        if not self.last.l1_ok or self.last.model is None:
            return "L1 未通过，不能运行 AFSIM"
        if getattr(self.d, "afsim_budget", 0) <= 0:
            return "AFSIM 复验次数已用完；按目前最好的版本 finish，或 ask_human 说明卡在哪里"
        self.d.afsim_budget -= 1
        self.afsim_runs += 1
        passed, _text, _infra = await probe(self.last.model)
        self._recheck()
        head = "（二维检查已全部通过，自动用 AFSIM 复验）\n" if auto else ""
        if passed:
            return head + "AFSIM 复验通过。\n" + self.summary()
        # the planned change did not fix it: a new diagnosis is needed
        self.diagnosis = None
        return (
            head
            + f"AFSIM 复验未通过（还可复验 {self.d.afsim_budget} 次）。上一份诊断作废：对照新的反馈"
            "重新 diagnose。\n" + self.summary()
        )

    def _refuse(self, what: str, why: str) -> str:
        self.refused += 1
        self.attempts.append({"step": self.step, "what": what[:300], "why": why[:300]})
        self.no_gain += 1
        return why

    def _basis(self, keys: list[str], extra: dict[str, Any], reason: str) -> str | None:
        """Once L1 passes a change needs a basis: a written diagnosis whose ``where`` holds
        the blocks, or ``because`` on the change itself (it then becomes the diagnosis)."""
        if not self.needs_diagnosis:
            return None
        because = str(extra.get("because") or "").strip()
        if because:
            self.diagnosis = {
                "step": self.step,
                "issue": because[:400],
                "where": sorted(set(keys)),
                "cause": because[:400],
                "plan": reason[:600],
            }
            self.diagnoses.append(dict(self.diagnosis))
            self.edited_since_diagnosis = False
            return None
        if self.diagnosis is None:
            first = T.first_failure(self)
            hint = (
                ("\n当前第一个未通过项的解释：\n" + T.explain_check(self, first)) if first else ""
            )
            return self._refuse(
                f"修改 {keys}",
                "修改被拒绝：L1 已通过，修改要有依据：在修改里带上 because（引用 explain_check 的"
                "结论），或先用 diagnose 写出诊断（问题原文、所在块、原因、改法）" + hint,
            )
        outside = [k for k in keys if not self._allowed(k)]
        if outside:
            return self._refuse(
                f"修改 {outside}",
                f"修改被拒绝：块 {outside} 不在诊断的 where 中（{self.diagnosis['where']}）；"
                "确实要改就带上 because 说明原因，或先重新 diagnose",
            )
        return None

    def edit(self, changes: Any, reason: str, extra: dict[str, Any] | None = None) -> str:
        extra = extra or {}
        if isinstance(changes, dict):
            changes = [changes]
        if not isinstance(changes, list) or not changes:
            return (
                'changes 写成列表：[{"block": …, "old": …, "new": …}] 或 [{"block": …, "text": …}]'
            )
        texts: dict[str, str] = {b.key: b.text for b in self.d.blocks}
        for ch in changes:
            if not isinstance(ch, dict) or "block" not in ch:
                return "每项修改要写 block"
            key = str(ch["block"])
            kind = key.split(":")[0]
            if kind not in ("model", "mermaid", "conditions", "pytree", "gaps", "verification"):
                return f"块名 {key} 不对"
            current = texts.get(key)
            if current is None and kind == "verification":
                return "验证块由验证阶段编写，纠错循环不能新建它；先修 L1 问题"
            if "text" in ch:
                texts[key] = _strip_fence(str(ch["text"]))
                continue
            if current is None:
                return f"没有块 {key}；新建块请用 text 写整块内容"
            old, new = str(ch.get("old", "")), str(ch.get("new", ""))
            if not old:
                return "old 不能为空"
            n = current.count(old)
            if n != 1:
                block = self._block(key)
                shown = _clip(block.numbered(), 2500) if block is not None else ""
                tool = {
                    "conditions": "set_condition（按条件名改，不用抄原文）",
                    "mermaid": "add_transition / remove_transition / set_trigger",
                    "model": "declare_event",
                    "pytree": "set_state_tree / attach_subtree",
                }.get(kind)
                return self._refuse(
                    f"edit {key} old={old[:120]}",
                    f"块 {key} 中 old 出现了 {n} 次，需要唯一。这个块现在的内容：\n{shown}"
                    + (f"\n也可以改用 {tool}" if tool else ""),
                )
            texts[key] = current.replace(old, new)
        return self.commit(texts, reason, extra)

    def apply(self, op: str, args: dict[str, Any], reason: str) -> str:
        """A structured edit (tools.structured) committed like an edit."""
        got = T.structured(self, op, args)
        if isinstance(got, str):
            return self._refuse(f"{op} {json.dumps(args, ensure_ascii=False)[:200]}", got)
        return self.commit(got, reason, args)

    def try_edit(self, args: dict[str, Any], reason: str) -> str:
        op = str(args.get("op") or "edit")
        inner = args.get("args") if isinstance(args.get("args"), dict) else args
        inner = {k: v for k, v in inner.items() if k != "op"}
        if op == "edit":
            texts: dict[str, str] | str = {b.key: b.text for b in self.d.blocks}
            for ch in inner.get("changes") or []:
                if not isinstance(ch, dict) or "block" not in ch or texts.get(ch["block"]) is None:
                    return "try_edit 的 changes 写法同 edit"
                cur = texts[ch["block"]]
                if "text" in ch:
                    texts[ch["block"]] = _strip_fence(str(ch["text"]))
                elif cur.count(str(ch.get("old", ""))) != 1 or not ch.get("old"):
                    return f"块 {ch['block']} 中 old 不唯一或不存在"
                else:
                    texts[ch["block"]] = cur.replace(str(ch["old"]), str(ch.get("new", "")))
        elif op in T.WRITE_TOOLS:
            texts = T.structured(self, op, inner)
            if isinstance(texts, str):
                return texts
        else:
            return f"try_edit 的 op 写 edit 或 {', '.join(T.WRITE_TOOLS)}"
        return self.commit(texts, reason, {}, dry_run=True)

    def commit(
        self, texts: dict[str, str], reason: str, extra: dict[str, Any], dry_run: bool = False
    ) -> str:
        note = texts.pop("_note", None)
        current = {b.key: b.text for b in self.d.blocks}
        keys = [k for k, v in texts.items() if current.get(k) != v]
        if not keys:
            return self._refuse("没有变化的修改", "修改没有改变任何内容")
        refusal, warnings = T.lint(self, current, texts)
        if refusal:
            return self._refuse(f"修改 {keys}", refusal)
        if not dry_run:
            basis = self._basis(keys, extra, reason)
            if basis is not None:
                return basis
        h = hash(tuple(sorted(texts.items())))
        if not dry_run and h in self.tried:
            step, score = self.tried[h]
            return self._refuse(
                f"修改 {keys}（与第 {step} 步结果相同）",
                f"修改被拒绝：第 {step} 步已经得到过完全相同的稿件，得分 {score}。换一种改法；"
                "不确定原因时先 explain_check\n" + T.tried_summary(self),
            )
        before_blocks = _copy(self.d.blocks)
        before_check = self.last
        before_score = self.score(before_check)
        self.d.blocks = _rebuild(self.d.blocks, texts)
        counts = _counts(self.d.blocks)
        lost = [k for k in counts if 0 <= counts[k] < self.counts0[k]]
        if lost:
            self.d.blocks = before_blocks
            return self._refuse(
                f"修改 {keys}",
                f"修改被拒绝：不能减少 {', '.join(lost)}（开始时 {self.counts0}，修改后 {counts}）",
            )
        if dry_run:
            check = self.d.check(verify=True)
            after = self.score(check)
            self.d.blocks = before_blocks
            self.last = before_check
            trend = "变好" if after < before_score else ("变差" if after > before_score else "不变")
            return (
                f"试改结果（未保存）：得分 {before_score} → {after}，{trend}\n"
                + "\n".join(warnings + ([note] if note else []))
                + ("\n" if warnings or note else "")
                + self.summary(check)
                + "\n（满意就用同样的参数正式提交）"
            )
        check = self._recheck()
        after = self.score(check)
        if before_check.l1_ok and not check.l1_ok:
            problems = "\n".join(p.render() for p in check.problems[:8])
            self.d.blocks = before_blocks
            self.last = before_check
            self.looks = 0
            return self._refuse(
                f"修改 {keys}",
                f"修改被拒绝：会让 L1 不通过：\n{problems}\n诊断仍然有效：照上面的报错改写再"
                "提交（位置和路线用 point=/route= 引用想定接口，不写坐标、不改参数名）",
            )
        self.snapshots[self.step] = before_blocks
        self.tried[h] = (self.step, after)
        for key, text in texts.items():
            old_text = next((b.text for b in before_blocks if b.key == key), "")
            if text != old_text:
                self.edits.append(
                    EditRecord(self.step, key, old_text, text, reason, before_score, after)
                )
        self.looks = 0
        self.edited_since_diagnosis = True
        self.no_gain = 0 if after < before_score else self.no_gain + 1
        trend = "变好" if after < before_score else ("变差" if after > before_score else "不变")
        lines = [f"已修改（得分 {before_score} → {after}，{trend}）"]
        if after > before_score:
            lines.append(f'（变差了：可以 revert {{"step": {self.step}}} 撤销这一步）')
        lines += warnings + ([note] if note else [])
        lines += self._expectation(extra.get("expect"), check)
        return "\n".join(lines) + "\n" + self.summary(check)

    def _expectation(self, expect: Any, check: Any) -> list[str]:
        """``expect`` on a change: the checks it was meant to make pass."""
        if not expect:
            return []
        ids = [expect] if isinstance(expect, str) else [str(x) for x in expect]
        status = {t["id"]: t["passed"] for t in check.l2} | {
            str(s["id"]): s["passed"] for s in check.l3
        }
        if any(x.upper() == "L1" for x in ids):
            status["L1"] = bool(check.l1_ok)
        missed = [x for x in ids if status.get(x) is False or (x not in status and x != "L1")]
        if not missed:
            return [f"预期达成：{', '.join(ids)} 通过"]
        return [
            f"预期没有达成：{', '.join(missed)} 仍未通过（或不存在）。原因多半不是你以为的那样："
            "用 explain_check 看它在这一刻的条件取值"
        ]

    def revert(self, key: str, reason: str, step: Any = None) -> str:
        if step not in (None, ""):
            try:
                n = int(step)
            except (TypeError, ValueError):
                return "step 写步数"
            if n not in self.snapshots:
                return f"第 {n} 步没有生效的修改；有修改的步：{sorted(self.snapshots)}"
            before = self.score(self.last)
            texts_now = {b.key: b.text for b in self.d.blocks}
            self.d.blocks = _copy(self.snapshots[n])
            check = self._recheck()
            for b in self.d.blocks:
                if texts_now.get(b.key) != b.text:
                    self.edits.append(
                        EditRecord(
                            self.step,
                            b.key,
                            texts_now.get(b.key, ""),
                            b.text,
                            f"revert 第 {n} 步: " + reason,
                            before,
                            self.score(check),
                        )
                    )
            return f"已撤销第 {n} 步及之后的修改\n" + self.summary(check)
        if key not in self.original:
            return f"块 {key} 在开始时不存在"
        texts = {b.key: b.text for b in self.d.blocks}
        texts[key] = self.original[key]
        before = self.score(self.last)
        self.d.blocks = _rebuild(self.d.blocks, texts)
        check = self._recheck()
        self.edits.append(
            EditRecord(
                self.step,
                key,
                "",
                self.original[key],
                "revert: " + reason,
                before,
                self.score(check),
            )
        )
        return "已恢复\n" + self.summary(check)

    def restore_best(self) -> Any:
        score, blocks, check = self.best
        if score < self.score(self.last):
            self.d.blocks = _copy(blocks)
            self.last = self.d.check(verify=True)
        return self.last


def _skipped(scenario: dict[str, Any], conditions: dict[str, str] | None = None) -> list[str]:
    """States entered and left in the same update: the leaving trigger already held."""
    out = []
    for name, moves in (scenario.get("transitions") or {}).items():
        for a, b in zip(moves, moves[1:], strict=False):
            if a["t"] == b["t"] and a["to"] == b["from"]:
                expr = (conditions or {}).get(str(b["on"]))
                out.append(
                    f"诊断：{name} 在 {a['t']:g} s 进入 {a['to']} 后同一拍就因 {b['on']} 离开，"
                    f"即进入 {a['to']} 时 {b['on']} 已成立"
                    + (f"（conditions 块里 {b['on']} = {expr}）" if expr else "")
                    + f"；{a['to']} 的树还没做完就离开了。若要等 {a['to']} 的树做完：在 model 块的"
                    f" events 中加一行 `{b['on']}_tree: {{source: tree, result: success}}`，把状态图中"
                    f" {a['to']} 的这条转换改用它（mermaid 与 events 同一步 edit，再删掉 conditions 里"
                    f"不再使用的 {b['on']}）；若这个字段确实表达到位，就改条件读本状态才会变化的字段"
                )
    return out[:4]


def _never_entered(entry: dict[str, Any], spec: dict[str, Any]) -> list[str]:
    """States a scenario expects but the run never entered: how they are entered, and what
    in the scenario could make that happen (a scenario without a fault, a removed hostile or
    an event cannot test a loss or a recovery branch)."""
    scenario = next(
        (
            x
            for x in spec.get("scenarios") or []
            if isinstance(x, dict) and x.get("id") == entry.get("id")
        ),
        None,
    )
    if scenario is None:
        return []
    out = []
    for e in scenario.get("expect") or []:
        if not isinstance(e, dict):
            continue
        role, inst = e.get("role"), int(e.get("instance") or 0)
        visited = [s for _, s in (entry.get("states") or {}).get(f"{role}_{inst}", [])]
        wanted = list(e.get("state_sequence") or e.get("visits_in_order") or [])
        wanted += [e["reaches"]] if e.get("reaches") else []
        mission = (spec.get("missions") or {}).get(role) or {}
        for state in dict.fromkeys(w for w in wanted if w not in visited):
            ways = [
                f"{t.get('from')} -({t.get('on')})-> {state}"
                for t in mission.get("transitions") or []
                if t.get("to") == state
            ] + [
                f"任意状态 -({g.get('on')})-> {state}"
                for g in mission.get("global") or []
                if g.get("to") == state
            ]
            stimuli = []
            faults = scenario.get("faults") or []
            stimuli.append(
                "faults："
                + (
                    "，".join(
                        f"{f.get('at_s')} s {', '.join(k for k in f if k not in ('at_s', 'role', 'instance'))}"
                        for f in faults
                    )
                    or "无"
                )
            )
            removed = [
                h.get("id") for h in scenario.get("hostiles") or [] if h.get("remove_s") is not None
            ]
            stimuli.append("移除的敌方：" + ("，".join(map(str, removed)) or "无"))
            events = [f"{ev.get('event')}@{ev.get('at_s')}" for ev in scenario.get("events") or []]
            stimuli.append("外部事件：" + ("，".join(events) or "无"))
            out.append(
                f"诊断：想定期望 {role} 进入 {state}，但从未进入；进入它的转换："
                f"{'；'.join(ways) or '无'}。这个想定里的外部刺激：{'；'.join(stimuli)}。"
                "若这一支需要测试，在想定中制造让该转换成立的情况（如 faults 关传感器、"
                "敌方 remove_s、事件）；若是模型漏了转换，就改模型"
            )
    return out[:3]


def _strip_fence(text: str) -> str:
    text = text.strip("\n")
    m = re.match(r"^```[^\n]*\n(.*?)\n?```\s*$", text, re.S)
    return m.group(1) if m else text


def _rebuild(blocks: list[Block], texts: dict[str, str]) -> list[Block]:
    """New block list with the given texts (and line numbers recomputed by the sheet)."""
    known = {b.key: b for b in blocks}
    out: list[Block] = []
    for key, text in texts.items():
        if key in known:
            b = known[key]
            out.append(Block(b.kind, b.role, text, b.line, b.info))
        else:
            kind, _, role = key.partition(":")
            out.append(Block(kind, role or None, text, 0))
    from afsim_research_agent.falcon.sheet.blocks import join_sheet

    join_sheet(out)  # renumbers lines in place
    return out


def _find(node: dict[str, Any] | None, pred: Any) -> dict[str, Any] | None:
    if not node:
        return None
    if pred(node):
        return node
    for kid in node.get("children") or []:
        hit = _find(kid, pred)
        if hit is not None:
            return hit
    return None


def _leaves(node: dict[str, Any]) -> list[dict[str, Any]]:
    kids = node.get("children") or []
    if node.get("type") == "action":
        return [node]
    return [x for k in kids for x in _leaves(k)]


def _history(records: list[dict[str, Any]]) -> str:
    lines = []
    for r in records[-HISTORY:]:
        latest = r is records[-1]
        lines.append(
            f"第 {r['step']} 步 {r['action']} {_clip(json.dumps(r['args'], ensure_ascii=False), 300)}\n"
            f"想法：{r['thought'][:200]}\n结果："
            # what was shown stays readable a few steps on (it was re-shown again and again
            # when only 500 characters of it remained)
            + _clip(
                r["result"], RESULT_LIMIT if latest else (2500 if r["action"] == "show" else 500)
            )
        )
    return "\n\n".join(lines)


def _prompt(fixer: SheetFixer, records: list[dict[str, Any]], remaining: int) -> str:
    outline = "\n".join(
        f"- {b.key}（第 {b.line}–{b.line + len(b.text.splitlines()) - 1} 行）"
        for b in fixer.d.blocks
    )
    clauses = "\n".join(f"- {c['id']}：{c['text'][:160]}" for c in fixer.d.clauses[:20])
    parts = [
        RULES,
        f"需求：{fixer.d.r.requirement}",
        clauses and f"需求条款：\n{clauses}",
        f"战术稿的块：\n{outline}",
        records and "最近几步：\n" + _history(records),
        (fixer.edits or fixer.attempts) and "本轮试过的修改：\n" + T.tried_summary(fixer),
        (not records or records[-1]["action"] not in ("check", "edit", "revert"))
        and "当前检查结果：\n" + fixer.summary(),
        f"还剩 {remaining} 步。输出下一步的 JSON。",
    ]
    return "\n\n".join(str(p) for p in parts if p)


async def fix_sheet(
    drafter: Any, run: ModelRunner, max_steps: int = 20, l1_only: bool = False
) -> FixOutcome:
    """Run the loop on the drafter's sheet; the drafter keeps the best version. With
    ``l1_only`` (before the verification stage) the loop ends once L1 passes."""
    fixer = SheetFixer(drafter, l1_only=l1_only)
    outcome = FixOutcome(False, 0, score_start=fixer.score(fixer.last))
    records: list[dict[str, Any]] = []
    for step in range(1, max_steps + 1):
        if fixer.solved:
            break
        if fixer.outside_left():
            # nothing left the sheet can fix (rule triage): no step is spent on it
            drafter.notes.append("纠错循环：剩下的失败项按规则判定都不是战术稿的问题，未修改")
            break
        fixer.step = step
        started = time.perf_counter()
        try:
            action = await run(
                "fix:v3:step", _prompt(fixer, records, max_steps - step + 1), FixStep
            )
        except Exception as exc:  # noqa: BLE001 - a bad answer: retry once, then stop
            records.append(
                {
                    "step": step,
                    "action": "error",
                    "args": {},
                    "thought": "",
                    "result": f"上一步的输出无法解析（{str(exc)[:200]}），请只输出一个 JSON 动作",
                }
            )
            if sum(r["action"] == "error" for r in records) >= 2:
                break
            continue
        a = action.args
        reason = str(a.get("reason") or action.thought)[:300]
        report(f"修复循环第 {step}/{max_steps} 步：{action.action}")
        if action.action in (*WRITES, "diagnose", "classify"):
            fixer.reads = 0
        edits_before = len(fixer.edits)
        finish_refused = False
        refused_read = fixer.read_refused(action.action) if action.action in READS else None
        if refused_read is not None:
            result = refused_read
        elif action.action == "finish":
            outcome.summary = str(a.get("summary") or action.thought)[:500]
            result = fixer.check()
            if fixer.outside_left():
                result += "\n（剩下的失败项都不是战术稿的问题，结束）"
            elif (
                fixer.diagnosis is not None
                and not fixer.edited_since_diagnosis
                and not fixer.solved
                and fixer.finish_refusals < 2
            ):
                fixer.finish_refusals += 1
                finish_refused = True
                result = (
                    "不接受 finish：写了诊断但还没有按它修改。按诊断 edit；改法不对就重新 "
                    "diagnose；确认不是稿件的问题就 classify。\n" + result
                )
            elif fixer.afsim_pending and getattr(drafter, "afsim_budget", 0) > 0:
                if fixer._only_afsim_left():
                    result = await fixer.run_afsim(auto=True)
                    if not fixer.solved:
                        result = (
                            "不接受 finish：AFSIM 还没有通过当前稿件，按 AFSIM 反馈 diagnose 再修改。\n"
                            + result
                        )
                else:
                    result = (
                        "不接受 finish：AFSIM 还没有通过当前稿件，二维检查也未全部通过。"
                        "先修好二维问题，再复验 AFSIM。\n" + result
                    )
                finish_refused = not fixer.solved
        elif action.action == "diagnose":
            result = fixer.diagnose(a)
        elif action.action == "classify":
            result = fixer.classify(a)
        elif action.action == "run_afsim":
            result = await fixer.run_afsim()
        elif action.action == "ask_human":
            outcome.question = str(a.get("question") or action.thought)[:500]
            result = "已记录问题"
        elif action.action == "check":
            result = fixer.check()
        elif action.action == "show":
            result = fixer.show(str(a.get("block", "")), a.get("start"), a.get("end"))
            fixer.reads += 1
        elif action.action == "lookup":
            result = fixer.lookup(str(a.get("id", "")))
        elif action.action == "explain":
            result = fixer.explain(a.get("problem"))
        elif action.action == "try_tick":
            result = fixer.try_tick(str(a.get("role", "")), str(a.get("state", "")), a.get("given"))
        elif action.action == "simulate":
            result = fixer.simulate(str(a.get("scenario", "")))
        elif action.action == "explain_check":
            result = T.explain_check(fixer, a.get("id", a.get("check", a.get("problem"))))
        elif action.action == "eval_condition":
            result = T.eval_condition(
                fixer,
                str(a.get("role", "")),
                str(a.get("state", "")),
                str(a.get("name", "")),
                a.get("given"),
            )
        elif action.action == "who_writes":
            result = T.who_writes(fixer, str(a.get("field", "")))
        elif action.action == "history":
            result = T.history(fixer)
        elif action.action == "try_edit":
            result = fixer.try_edit(a, reason)
        elif action.action in WRITES and action.action != "revert":
            if action.action == "edit":
                result = fixer.edit(a.get("changes"), reason, a)
            else:
                result = fixer.apply(action.action, a, reason)
            if (
                result.startswith("已修改")
                and fixer._only_afsim_left()
                and getattr(drafter, "afsim_budget", 0) > 0
            ):
                result += "\n" + await fixer.run_afsim(auto=True)
        else:
            result = fixer.revert(str(a.get("block", "")), reason, a.get("step"))
        if action.action in WRITES and fixer.no_gain >= NO_GAIN and not fixer.solved:
            first = T.first_failure(fixer)
            result += (
                f"\n（已经连续 {fixer.no_gain} 次修改没有让得分变好。本轮试过的：\n"
                + T.tried_summary(fixer)
                + (
                    "\n当前第一个未通过项的解释：\n" + T.explain_check(fixer, first)
                    if first
                    else ""
                )
                + "\n换一个方向：先弄清上面标出的决定项为什么是这个值）"
            )
            fixer.no_gain = 0
        if action.action in READS and refused_read is None:
            fixer.looks += 1
        if action.action == "show" and refused_read is None:
            same = [r["step"] for r in records if r["action"] == "show" and r["args"] == a]
            if same:
                result += f"\n（与第 {same[-1]} 步看过的内容相同，战术稿没有变化）"
        idle = 0
        for r in reversed(records):
            if r["action"] in (*WRITES, "diagnose", "run_afsim", "classify"):
                break
            idle += 1
        quiet = (*WRITES, "try_edit", "finish", "ask_human", "diagnose", "run_afsim", "classify")
        if action.action not in quiet and idle >= 3:
            nxt = (
                "带上 because 修改（set_condition / add_transition / edit 等），或先 diagnose"
                if fixer.needs_diagnosis and fixer.diagnosis is None
                else "修改战术稿（set_condition / add_transition / edit 等）"
            )
            result += (
                f"\n（已连续 {idle + 1} 步没有修改。信息已经够用：下一步{nxt}，"
                "改错了可以 revert；不要再重复查看同一段）"
            )
        records.append(
            {
                "step": step,
                "action": action.action,
                "args": a,
                "thought": action.thought,
                "result": _clip(result),
                "seconds": round(time.perf_counter() - started, 2),
            }
        )
        best_step = getattr(fixer, "best_step", 0)
        if (
            fixer.score(fixer.last) <= fixer.best[0]
            and fixer.score(fixer.last) < outcome.score_start
        ):
            fixer.best_step = step
            best_step = step
        # progress also counts an applied edit or an AFSIM re-run (an edit that leaves the
        # 2D score unchanged may still fix what AFSIM showed)
        applied = action.action in WRITES and len(fixer.edits) > edits_before
        if applied or (
            action.action in ("run_afsim", "classify")
            and not str(result).startswith(("不接受", "check 写", "category", "写出"))
        ):
            fixer.best_step = step
            best_step = step
        # looking things up (show / lookup / explain) is how a diagnosis is reached: it
        # counts towards the stall only beyond READ_ALLOWANCE reads since the last progress
        reads = sum(
            1
            for r in records
            if r["step"] > max(best_step, 0)
            and r["action"] in ("show", "lookup", "explain", *T.READ_TOOLS, "try_edit")
        )
        idle = step - max(best_step, 0) - min(reads, READ_ALLOWANCE)
        if idle >= STALL and action.action not in ("finish", "ask_human"):
            records[-1]["result"] += f"\n（连续 {STALL} 步没有改进，结束）"
            break
        if action.action == "finish" and finish_refused:
            continue
        if fixer.outside_left() and action.action != "finish":
            records[-1]["result"] += "\n（剩下的失败项都不是战术稿的问题，结束）"
            break
        if action.action in ("finish", "ask_human"):
            if action.action == "finish" and not fixer.solved:
                records[-1]["result"] += "\n（检查尚未全部通过；按最好的版本结束）"
            break
    fixer.restore_best()
    drafter.notes.append(
        f"纠错循环 {len(records)} 步，修改 {len(fixer.edits)} 处，拒绝 {fixer.refused} 次；"
        f"得分 {outcome.score_start} → {fixer.score(fixer.last)}"
        + (
            f"；诊断 {len(fixer.diagnoses)} 次，AFSIM 复验 {fixer.afsim_runs} 次"
            + (f"，记录非稿件问题 {len(fixer.classified)} 项" if fixer.classified else "")
            if fixer.diagnoses or fixer.afsim_runs or fixer.classified
            else ""
        )
    )
    outcome.solved = fixer.solved
    outcome.diagnoses = list(fixer.diagnoses)
    outcome.afsim_runs = fixer.afsim_runs
    outcome.classified = list(fixer.classified)
    outcome.findings = [f.to_dict() for f in fixer.findings()]
    outcome.diagnosed_unfixed = bool(fixer.diagnoses) and not fixer.edits
    outcome.steps = len(records)
    outcome.edits = fixer.edits
    outcome.log = records
    outcome.score_end = fixer.score(fixer.last)
    return outcome


def render_fix(outcome: FixOutcome) -> list[str]:
    lines = [
        "## 自检纠错记录",
        "",
        f"{outcome.steps} 步，修改 {len(outcome.edits)} 处；得分 {outcome.score_start} → "
        f"{outcome.score_end}（L1 是否未过、L1 问题数、L2/L3 未通过数，越小越好）；"
        f"{'检查全部通过' if outcome.solved else '检查未全部通过'}。",
    ]
    if outcome.question:
        lines.append(f"需要人决定：{outcome.question}")
    if outcome.afsim_runs:
        lines.append(f"循环内 AFSIM 复验 {outcome.afsim_runs} 次。")
    if outcome.diagnosed_unfixed:
        lines.append("写了诊断但没有一处修改被采用。")
    for c in outcome.classified:
        lines.append(
            f"- 第 {c['step']} 步记录 {c['check']} 不是稿件问题"
            f"（{triage.CATEGORY_TEXT.get(c['category'], c['category'])}）：{c['reason']}"
        )
    left = [f for f in outcome.findings if f.get("text")]
    if left:
        lines.append("")
        lines.append("结束时的规则判定：")
        lines += ["- " + f["text"] for f in left[:8]]
    for dg in outcome.diagnoses:
        lines.append("")
        lines.append(
            f"- 第 {dg['step']} 步诊断（块 {', '.join(dg['where'])}）：问题：{dg['issue']}；"
            f"原因：{dg['cause']}；改法：{dg['plan']}"
        )
    for e in outcome.edits:
        lines.append("")
        lines.append(f"- 第 {e.step} 步修改 `{e.block}`：{e.reason}")
        diff = list(
            difflib.unified_diff(e.before.splitlines(), e.after.splitlines(), lineterm="", n=1)
        )[2:]
        if diff:
            lines.append("  ```diff")
            lines += ["  " + d for d in diff[:40]]
            lines.append("  ```")
    return lines
