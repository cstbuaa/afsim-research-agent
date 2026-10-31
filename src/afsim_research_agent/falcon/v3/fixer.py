"""Self-checking repair loop for tactic sheets v3 (``falcon draft3`` after drafting).

The model works like a developer with a test suite: each step it calls one tool (a JSON
action), sees a short result, and goes on until the checks pass or the budget runs out.
The deterministic checks decide; the model only proposes (design agent-loop-deepagents-v1
§4, ported to v3).

Tools: ``check``, ``show``, ``lookup`` (contracts, tactic-set subtrees and examples,
blackboard fields), ``explain`` (one problem with its context, e.g. which referenced
subtree already holds a resource), ``try_tick`` (one L2 step), ``simulate`` (one L3
scenario), ``edit`` (several blocks at once, applied together), ``revert``, ``finish``,
``ask_human``.

Guards enforced here, not only in the prompt:

* an edit that breaks L1 when it passed before is not applied;
* tests, scenarios and scenario expectations may not be removed;
* the reference policy, state names and scenario events are the compiler's to enforce
  (an edit that violates them shows up as an L1 error and is refused);
* every applied edit is logged with the text before and after, for the review page.
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
from afsim_research_agent.ir.v2.sim import simulate

RESULT_LIMIT = 5000
MAX_READS = 4  # consecutive show calls without an edit before show is refused
STALL = 8  # steps without beating the best score: stop
HISTORY = 5
SHOW_LINES = 120


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

工具：
- check {}：重新检查，返回各块的问题（L1）和 L2 / L3 未通过项。
- show {"block": "块名", "start": 行号, "end": 行号}：查看一个块（带整份稿件的行号）。
- lookup {"id": "动作 ID / 战术集 ID / 黑板字段 / 切片 ID"}：查契约、子树写法、字段类型与取值。
- explain {"problem": 问题编号}：某个问题的详细上下文（如资源冲突发生在哪两个分支、各自含哪些动作）。
- try_tick {"role": 角色, "state": 状态, "given": {"blackboard": {…}, "events": […], "time_in_state_s": 0, "actions": {…}}}：
  用临时测试跑一拍，返回下一状态、树的结果、下达的动作。
- simulate {"scenario": 想定 ID}：跑一个 L3 想定，返回状态轨迹。
- edit {"changes": [{"block": "块名", "old": "原文片段", "new": "新文本"} 或 {"block": "块名", "text": "整块新内容"}], "reason": "…"}：
  一次修改一个或多个块（同时生效）；old 必须在块中唯一出现。改完自动检查。
- revert {"block": "块名"}：把一个块恢复为修正开始前的内容。
- finish {"summary": "…"}：结束。
- ask_human {"question": "…"}：问题需要人决定时结束并提问。

规则：
- 块名写法：model、mermaid:<角色>、conditions、pytree:<角色>、gaps、verification。
- 状态图里的触发名与 conditions / events 的名字必须一致；一处改名，另一处同一步一起改。
- 状态树和任务状态机自己写；只能用 tactic("…") 引用战术集子树，不能引用战术集状态树和任务。
- 引用的战术集子树已经包含的动作，不要在并行分支里再写一遍（会争用同一独占资源）。
- 不能删测试、想定和想定期望；期望与需求不符时可以改，但不能为了通过而改成与需求不符的期望。
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
                    lines += ["    " + x for x in s["render"].splitlines()[1:keep]]
                    lines += ["    " + x for x in _skipped(s)]
            for key, line in self.d._stuck(check)[:4]:
                lines.append(f"提示（{key}）：{line[2:]}")
            if not check.l2 and not check.l3 and not self.l1_only:
                lines.append("还没有可运行的测试或想定（verification 块）")
        if self.solved and self.l1_only:
            lines.append("L1 已通过，可以 finish；测试和想定由之后的验证阶段编写。")
        elif self.solved:
            lines.append("全部检查通过，可以 finish。")
        return "\n".join(lines)

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
        if self.reads >= MAX_READS:
            return (
                f"已经连续查看 {self.reads} 次而没有修改。问题和出错行已在检查结果里给出：请直接 edit；"
                "认为问题出在测试或仿真本身、而不是模型时，用 ask_human 说明"
            )
        seen = self.shown.get((key, start, end))
        if seen is not None and seen[0] == block.text:
            return (
                f"块 {key} 自第 {seen[1]} 步查看以来没有改动，内容见那一步的结果；"
                "需要改就直接 edit（old 写原文片段），或 explain 某个问题"
            )
        self.shown[(key, start, end)] = (block.text, self.step)
        rows = block.numbered().splitlines()
        first = block.line
        lo = int(start) - first if isinstance(start, int) else 0
        hi = int(end) - first + 1 if isinstance(end, int) else lo + SHOW_LINES
        part = rows[max(0, lo) : max(lo + 1, hi)]
        more = "" if hi >= len(rows) else f"\n（还有 {len(rows) - hi} 行，用 start/end 查看）"
        return f"```{block.label}\n" + "\n".join(part) + "\n```" + more

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
        return simulate(model, self.d.c, raw).render()

    def edit(self, changes: Any, reason: str) -> str:
        if isinstance(changes, dict):
            changes = [changes]
        if not isinstance(changes, list) or not changes:
            return (
                'changes 写成列表：[{"block": …, "old": …, "new": …}] 或 [{"block": …, "text": …}]'
            )
        before_blocks = _copy(self.d.blocks)
        before_check = self.last
        before_score = self.score(before_check)
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
                return f"块 {key} 中 old 出现了 {n} 次，需要唯一；可多带一点上下文，或先 show"
            texts[key] = current.replace(old, new)
        self.d.blocks = _rebuild(self.d.blocks, texts)
        counts = _counts(self.d.blocks)
        lost = [k for k in counts if 0 <= counts[k] < self.counts0[k]]
        if lost:
            self.d.blocks = before_blocks
            self.refused += 1
            return (
                f"修改被拒绝：不能减少 {', '.join(lost)}（开始时 {self.counts0}，修改后 {counts}）"
            )
        check = self._recheck()
        after = self.score(check)
        if before_check.l1_ok and not check.l1_ok:
            problems = "\n".join(p.render() for p in check.problems[:8])
            self.d.blocks = before_blocks
            self.last = before_check
            self.refused += 1
            return f"修改被拒绝：会让 L1 不通过：\n{problems}"
        for key, text in texts.items():
            old_text = next((b.text for b in before_blocks if b.key == key), "")
            if text != old_text:
                self.edits.append(
                    EditRecord(self.step, key, old_text, text, reason, before_score, after)
                )
        trend = "变好" if after < before_score else ("变差" if after > before_score else "不变")
        return f"已修改（得分 {before_score} → {after}，{trend}）\n" + self.summary(check)

    def revert(self, key: str, reason: str) -> str:
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


def _skipped(scenario: dict[str, Any]) -> list[str]:
    """States entered and left in the same update: the leaving trigger already held."""
    out = []
    for name, moves in (scenario.get("transitions") or {}).items():
        for a, b in zip(moves, moves[1:], strict=False):
            if a["t"] == b["t"] and a["to"] == b["from"]:
                out.append(
                    f"诊断：{name} 在 {a['t']:g} s 进入 {a['to']} 后同一拍就因 {b['on']} 离开，"
                    f"即进入 {a['to']} 时 {b['on']} 已成立；检查它读的字段在这一刻的值"
                )
    return out[:4]


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
        if action.action in ("edit", "revert"):
            fixer.reads = 0
        if action.action == "finish":
            outcome.summary = str(a.get("summary") or action.thought)[:500]
            result = fixer.check()
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
        elif action.action == "edit":
            result = fixer.edit(a.get("changes"), reason)
        else:
            result = fixer.revert(str(a.get("block", "")), reason)
        if action.action == "show":
            same = [r["step"] for r in records if r["action"] == "show" and r["args"] == a]
            if same:
                result += f"\n（与第 {same[-1]} 步看过的内容相同，战术稿没有变化）"
        idle = 0
        for r in reversed(records):
            if r["action"] in ("edit", "revert"):
                break
            idle += 1
        if action.action not in ("edit", "revert", "finish", "ask_human") and idle >= 3:
            result += (
                f"\n（已连续 {idle + 1} 步没有修改。信息已经够用：下一步用 edit 改战术稿，"
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
        if step - max(best_step, 0) >= STALL and action.action not in ("finish", "ask_human"):
            records[-1]["result"] += f"\n（连续 {STALL} 步没有改进，结束）"
            break
        if action.action in ("finish", "ask_human"):
            if action.action == "finish" and not fixer.solved:
                records[-1]["result"] += "\n（检查尚未全部通过；按最好的版本结束）"
            break
    fixer.restore_best()
    drafter.notes.append(
        f"纠错循环 {len(records)} 步，修改 {len(fixer.edits)} 处，拒绝 {fixer.refused} 次；"
        f"得分 {outcome.score_start} → {fixer.score(fixer.last)}"
    )
    outcome.solved = fixer.solved
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
