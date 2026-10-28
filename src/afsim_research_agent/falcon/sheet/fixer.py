"""``falcon fix <package>``: a self-checking repair loop over a tactic sheet.

Design: ``docs/design/agent-loop-deepagents-v1.md`` §4; efficiency changes after the
first online runs: ``docs/analysis/2026-10-19-agent-loop-efficiency.md``.

The model works like a developer with a test suite: each step it picks one tool, sees a
short result, and goes on until the checks pass or the budget runs out. The
deterministic checks decide; the model only proposes.

What keeps the step count down (py9 fix2 spent 17 of 30 steps on ``show``):

* **problem cards** — every problem carries its sheet location and the lines around it
  (a failing test: the test, the transition that fired instead, where its mechanical
  expectations come from), so an edit needs no ``show`` first;
* **quick fixes** — mechanical problems (a transition without a test, a wait without a
  timeout exit, a moving state deaf to contact, a time without unit) come with a fix the
  model can apply in one step;
* **compound edits** — one ``edit`` changes several places at once; a transition and its
  test land together because tests for newly added transitions are synthesized (inputs
  searched and verified with the L2 runtime) in the same step;
* **edit guard** — an edit that makes the checks stop earlier (a parse or L1 error) is
  not applied; the model sees why and tries again;
* **progress ledger** — kept by the program, not by the model: solved, open (and how
  many edits did not solve it), approaches that failed.

Guards that keep the model from passing by weakening the checks (deleting tests or
expectations, claiming values as user-given) are enforced here, and every edit is
logged for the reviewer. The tools are plain methods with string results so they can be
wrapped as LangChain tools for a Deep Agents orchestrator later (P1).
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from afsim_research_agent.falcon.errors import FalconError
from afsim_research_agent.falcon.llm import ModelRunner
from afsim_research_agent.falcon.sheet import prompts as P
from afsim_research_agent.falcon.sheet.blocks import Block, join_sheet, split_sheet
from afsim_research_agent.falcon.sheet.fixcards import (
    Card,
    append_tests,
    build_cards,
    candidate_givens,
    render_cards,
    render_test,
    test_indent,
    test_item,
)
from afsim_research_agent.falcon.sheet.generate import (
    BLOCK_KINDS,
    SHEET_FILE,
    _problem_lines,
    _stage,
    guide_for,
    usage,
)
from afsim_research_agent.falcon.sheet.recompile import (
    RecompileReport,
    Workspace,
    open_workspace,
    recompile_package,
)

FIX_LOG = "fix_log.json"
ACTIONS = (
    "check",
    "show",
    "guide",
    "explain",
    "try_tick",
    "edit",
    "apply_fix",
    "patch",
    "replace",
    "undo",
    "revert",
    "finish",
    "ask_human",
)
RESULT_LIMIT = 6000  # characters of a tool result shown to the model
HISTORY = 5  # recent steps repeated in each prompt
SHOW_LINES = 120  # a longer block is shown in parts
SHOW_CONTEXT = 6  # lines around each `find` match
LATEST_LIMIT = 9000  # the latest result is shown (almost) whole; older ones are clipped
STUCK_EDITS = 2  # edits that left a problem open before the prompt says so
STALL = 8  # steps without beating the best version: go back to it; twice as many: stop and ask


class AgentStep(BaseModel):
    """One step of the loop: what the model thinks and the tool it calls."""

    thought: str = ""
    action: Literal[
        "check",
        "show",
        "guide",
        "explain",
        "try_tick",
        "edit",
        "apply_fix",
        "patch",
        "replace",
        "undo",
        "revert",
        "finish",
        "ask_human",
    ]
    args: dict[str, Any] = Field(default_factory=dict)


@dataclass
class Edit:
    step: int
    block: str
    action: str
    before: str
    after: str
    reason: str
    score_before: tuple[int, int, int]
    score_after: tuple[int, int, int]


@dataclass
class StepRecord:
    step: int
    thought: str
    action: str
    args: dict[str, Any]
    result: str
    seconds: float


@dataclass
class FixResult:
    package_dir: Path
    status: str
    problems: list[str]
    steps: list[StepRecord] = field(default_factory=list)
    edits: list[Edit] = field(default_factory=list)
    question: str | None = None
    summary: str = ""
    report: RecompileReport | None = None
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        return 0 if self.status == "draft_valid" else 4


@dataclass
class Evaluation:
    score: tuple[int, int, int]  # (stage reached, problems, failed L3 expectations)
    problems: list[tuple[str, str]]  # (block key, line)
    l3_failures: list[str]
    symbols: dict[str, Any]
    cards: list[Card] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems and not self.l3_failures

    @property
    def open_keys(self) -> dict[str, Card]:
        return {c.key: c for c in self.cards}


@dataclass
class LedgerItem:
    text: str
    first: int
    solved: int | None = None
    edits: int = 0  # edits made while it stayed open


def _counts(blocks: list[Block]) -> dict[str, int]:
    """What the guards protect: tests, scenarios, expectations, user_specified claims."""
    out = {"tests": 0, "scenarios": 0, "expectations": 0, "user_specified": 0, "deferred": 0}
    for block in blocks:
        out["user_specified"] += block.text.count("user_specified")
        if block.kind != "verification":
            continue
        try:
            data = yaml.safe_load(block.text) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(data, dict):
            continue
        tests = data.get("tests") or []
        scenarios = data.get("scenarios") or []
        out["tests"] += len(tests) if isinstance(tests, list) else 0
        if isinstance(tests, list):
            out["deferred"] += sum(1 for x in tests if isinstance(x, dict) and x.get("deferred"))
        if isinstance(scenarios, list):
            out["scenarios"] += len(scenarios)
            out["expectations"] += sum(
                len(s.get("expect") or []) for s in scenarios if isinstance(s, dict)
            )
    return out


def _test_intents(blocks: list[Block]) -> dict[str, str]:
    """Test id → what the test asks (role, state, given, intent fields of expect)."""
    block = next((b for b in blocks if b.kind == "verification"), None)
    try:
        data = yaml.safe_load(block.text) if block else {}
    except yaml.YAMLError:
        return {}
    out: dict[str, str] = {}
    for t in (data or {}).get("tests") or [] if isinstance(data, dict) else []:
        if not isinstance(t, dict) or "id" not in t:
            continue
        expect = t.get("expect") if isinstance(t.get("expect"), dict) else {}
        intent = {k: expect.get(k) for k in ("next_state", "transition", "blackboard")}
        key = [t.get("role"), t.get("instance"), t.get("state"), t.get("given"), intent]
        out[str(t["id"])] = json.dumps(key, ensure_ascii=False, sort_keys=True, default=str)
    return out


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses a key written twice in one mapping (PyYAML keeps the last)."""


def _no_duplicates(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False) -> Any:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"重复的键 {key!r}", key_node.start_mark
            )
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)


def _verification_issues(blocks: list[Block]) -> set[str]:
    """Structural damage in the verification block an edit can leave behind."""
    block = next((b for b in blocks if b.kind == "verification"), None)
    if block is None:
        return set()
    try:
        data = yaml.load(block.text, Loader=_StrictLoader)  # noqa: S506 - safe loader subclass
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
        where = f"（块内第 {mark.line + 1} 行）" if mark is not None else ""
        problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
        return {f"验证块 YAML 有误{where}：{problem}"}
    tests = (data or {}).get("tests") if isinstance(data, dict) else None
    ids = [str(t.get("id")) for t in tests or [] if isinstance(t, dict)]
    return {f"测试 ID 重复：{i}" for i in ids if ids.count(i) > 1}


def _clip(text: str, limit: int = RESULT_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n…（截断，共 {len(text)} 字）"


def _strip_fence(text: str) -> str:
    lines = text.strip().splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines) + "\n"


def _copy(blocks: list[Block]) -> list[Block]:
    return [Block(b.kind, b.role, b.text, b.line, b.info) for b in blocks]


class SheetFixer:
    """The tools and the state of one repair session (a copy of the package)."""

    def __init__(self, workspace: Workspace, *, with_l3: bool = False) -> None:
        self.ws = workspace
        self.with_l3 = with_l3
        text = (workspace.package_dir / SHEET_FILE).read_text(encoding="utf-8")
        first = text.splitlines()[0] if text else ""
        self.title = first.split("：", 1)[1].strip() if "：" in first else ""
        self.blocks, _ = split_sheet(text)
        self.original = {b.key: b.text for b in self.blocks}
        self.baseline = _counts(self.blocks)
        self.structure0 = _verification_issues(self.blocks)
        self.history: list[list[Block]] = []  # blocks before each applied edit (undo)
        self.best_step = 0
        # recorded mechanical results belong to the test as it was first written: a test
        # whose situation or intent the loop changes is recorded again from its next run
        self.observed0 = dict(workspace.ctx.observed)
        self.intents0 = _test_intents(self.blocks)
        self.rerecorded: set[str] = set()
        self.generated: list[str] = []  # tests the loop synthesized
        self.edits: list[Edit] = []
        self.ledger: dict[str, LedgerItem] = {}
        self.failed: list[str] = []  # approaches that made things worse or were refused
        self.counters = {"refused": 0, "worse": 0, "quick_fixes": 0}
        self.step = 0
        self.last = self.evaluate()
        self._track(self.last, edited=False)
        self.start_score = self.last.score
        self.best = (self.last.score, self.sheet())

    # ------------------------------------------------------------------ state
    def sheet(self) -> str:
        return join_sheet(self.blocks, self.title)

    def block(self, key: str) -> Block | None:
        return next((b for b in self.blocks if b.key == key), None)

    def evaluate(self) -> Evaluation:
        text = self.sheet()
        intents = _test_intents(self.blocks)
        stale = {
            t for t in self.observed0 if t in self.intents0 and intents.get(t) != self.intents0[t]
        }
        self.rerecorded = stale
        self.ws.ctx.observed = {k: v for k, v in self.observed0.items() if k not in stale}
        compiled, assembled, check = self.ws.build(text, with_l3=self.with_l3)
        grouped = _problem_lines(compiled, check, assembled.data, [b.key for b in self.blocks])
        problems = [(key, line) for key, lines in grouped.items() for line in lines]
        failures = []
        for scenario in check.l3 or []:
            for e in scenario.get("expectations") or []:
                if not e.get("passed"):
                    failures.append(
                        f"[verification] L3 想定 {scenario['id']} 的期望 {e['id']} 未通过："
                        f"实测 {e.get('measured')}（{e.get('detail')}）"
                    )
        count = len(compiled.errors) + len(check.problems)
        cards = build_cards(
            problems, self.blocks, compiled.symbols, compiled.source_map, self.ws.ctx.observed
        )
        evaluation = Evaluation(
            (_stage(check), count, len(failures)), problems, failures, compiled.symbols, cards
        )
        self._compiled = compiled
        self._ir = check.ir
        return evaluation

    def _track(self, ev: Evaluation, *, edited: bool) -> tuple[list[str], list[str]]:
        """Update the ledger; (solved, new) problem texts since the last evaluation."""
        now = ev.open_keys
        solved, new = [], []
        for key, item in self.ledger.items():
            if item.solved is None and key not in now:
                item.solved = self.step
                solved.append(item.text[:100])
            elif item.solved is None and edited:
                item.edits += 1
        for key, card in now.items():
            item = self.ledger.get(key)
            if item is None or item.solved is not None:
                self.ledger[key] = LedgerItem(card.short, self.step)
                if self.step:
                    new.append(card.short[:100])
        return solved, new

    def _keep_best(self) -> None:
        if self.last.score < self.best[0]:
            self.best = (self.last.score, self.sheet())
            self.best_step = self.step

    def _guard(self, blocks: list[Block]) -> str | None:
        damage = sorted(_verification_issues(blocks) - self.structure0)
        if damage:
            return (
                "拒绝修改：改完后验证块结构损坏——"
                + "；".join(damage[:5])
                + "。常见原因：按行号替换时范围没有对齐测试的边界，留下了半个测试；"
                "改测试时先 show 这个测试的完整行范围，或用 old/new 整段替换"
            )
        counts = _counts(blocks)
        for name, label in (
            ("tests", "单步测试"),
            ("scenarios", "想定"),
            ("expectations", "想定期望"),
        ):
            if counts[name] < self.baseline[name]:
                return (
                    f"拒绝修改：{label}从 {self.baseline[name]} 个减少到 {counts[name]} 个。"
                    "不能靠删除测试或期望来通过检查；测试意图写错时改它的 given / expect"
                )
        if counts["user_specified"] > self.baseline["user_specified"]:
            return "拒绝修改：不能新增 user_specified 依据（它只能来自需求原文），改用 inferred 或 unknown"
        if counts["deferred"] != self.baseline.get("deferred", 0):
            return (
                "拒绝修改：测试的 deferred（暂缓）标记只能由人决定，纠错循环不能新增或去掉；"
                "需要暂缓或恢复某个测试时用 ask_human 提问"
            )
        return None

    def _refuse(self, message: str, reason: str) -> str:
        self.counters["refused"] += 1
        self.failed.append(f"第 {self.step} 步（{reason[:80]}）被拒绝：{message[:160]}")
        return message

    def _commit(
        self,
        candidate: list[Block],
        action: str,
        reason: str,
        *,
        add_tests: bool = True,
        stage_guard: bool = True,
    ) -> str:
        """Apply ``candidate`` if the guards allow it; evaluate, log and report."""
        refused = self._guard(candidate)
        if refused:
            return self._refuse(refused, reason)
        before_blocks = _copy(self.blocks)
        before_text = {b.key: b.text for b in before_blocks}
        undoing = action == "undo"
        score_before = self.last.score
        untested_before = set(self.last.symbols.get("untested") or [])
        self.blocks = candidate
        ev = self.evaluate()
        if stage_guard and ev.score[0] > score_before[0]:
            # the checks now stop earlier: a parse or L1 error; do not apply
            errors = "\n".join(c.short for c in ev.cards[:6])
            self.blocks = before_blocks
            self.evaluate()
            return self._refuse(
                "修改未应用：改完后检查只能走到更早的阶段"
                f"（{score_before[0]} → {ev.score[0]}；0 = 单步测试已运行），新出现的错误：\n{errors}\n"
                "修正写法后再提交（可以在同一个 edit 里一起改）",
                reason,
            )
        if not undoing:
            self.history.append(before_blocks)
        added: list[str] = []
        missing: list[str] = []
        if add_tests:
            new_tids = [t for t in ev.symbols.get("untested") or [] if t not in untested_before]
            if new_tids:
                added, missing = self._add_tests(new_tids)
                if added:
                    ev = self.evaluate()
        self.last = ev
        solved, new = self._track(ev, edited=True)
        self._keep_best()
        for b in self.blocks:
            if before_text.get(b.key) != b.text:
                self.edits.append(
                    Edit(
                        self.step,
                        b.key,
                        action,
                        before_text.get(b.key, ""),
                        b.text,
                        reason,
                        score_before,
                        ev.score,
                    )
                )
        trend = "变好" if ev.score < score_before else "变差" if ev.score > score_before else "不变"
        if trend == "变差":
            self.counters["worse"] += 1
            self.failed.append(
                f"第 {self.step} 步（{reason[:80]}）后得分变差 {score_before} → {ev.score}"
            )
        lines = [f"已修改；得分 {score_before} → {ev.score}（{trend}）"]
        if added:
            lines.append(f"新增转换已自动补测试：{', '.join(added)}")
        if missing:
            lines.append(f"没找到能触发这些转换的输入，测试需要手写：{', '.join(missing)}")
        if solved:
            lines.append("已解决：" + "；".join(solved))
        if new:
            lines.append("新出现：" + "；".join(new))
        return "\n".join(lines) + "\n" + self.summary()

    # ------------------------------------------------------------------ test synthesis
    def _scratch_l2(self, tests: list[dict[str, Any]]) -> dict[str, dict[str, Any]] | None:
        """Run extra L2 tests against the current sheet in a scratch copy of the package."""
        blocks = _copy(self.blocks)
        verification = next((b for b in blocks if b.kind == "verification"), None)
        try:
            data = yaml.safe_load(verification.text) if verification else {}
        except yaml.YAMLError:
            return None
        data = data if isinstance(data, dict) else {}
        data["tests"] = [*(data.get("tests") or []), *tests]
        text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        if verification is None:
            blocks.append(Block("verification", None, text, 0))
        else:
            blocks[blocks.index(verification)] = Block("verification", None, text, 0)
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp) / "pkg"
            shutil.copytree(self.ws.package_dir, scratch)
            ws = open_workspace(scratch)
            ws.ctx.observed = {}
            _compiled, _assembled, check = ws.build(join_sheet(blocks, self.title))
        if check.l2 is None:
            return None
        wanted = {t["id"] for t in tests}
        return {t["id"]: t["result"] for t in check.l2.get("tests") or [] if t["id"] in wanted}

    def _add_tests(self, tids: list[str]) -> tuple[list[str], list[str]]:
        """Synthesize and append one verified test per transition; (added, not found)."""
        checklist = {t["id"]: t for t in self.last_symbols().get("transition_checklist") or []}
        candidates: list[dict[str, Any]] = []
        owner: dict[str, str] = {}
        for tid in tids:
            t = checklist.get(tid)
            if t is None:
                continue
            for k, given in enumerate(candidate_givens(t["inputs"])):
                item = test_item(t, given, 0)
                item["id"] = f"zz_cand_{len(candidates)}"
                candidates.append(item)
                owner[item["id"]] = tid
        results = self._scratch_l2(candidates) if candidates else None
        if results is None:
            return [], list(tids)
        chosen: dict[str, dict[str, Any]] = {}
        for item in candidates:
            tid = owner[item["id"]]
            if tid in chosen:
                continue
            if (results.get(item["id"]) or {}).get("fired_transition") == tid:
                chosen[tid] = item
        verification = self.block("verification")
        if not chosen or verification is None:
            return [], [t for t in tids if t not in chosen]
        existing = set(_test_intents(self.blocks))
        indent = test_indent(verification.text)
        rendered, added = [], []
        for tid, item in chosen.items():
            final = test_item(checklist[tid], item["given"], 0)
            n = 2
            while final["id"] in existing:
                final["id"] = f"auto_{tid}_{n}"
                n += 1
            existing.add(final["id"])
            rendered.append(render_test(final, indent))
            added.append(final["id"])
        verification.text = append_tests(verification.text, rendered)
        self.generated += added
        return added, [t for t in tids if t not in chosen]

    def last_symbols(self) -> dict[str, Any]:
        return self._compiled.symbols

    # ------------------------------------------------------------------ tools
    def summary(self) -> str:
        ev = self.last
        status = "全部通过" if ev.ok else f"{len(ev.cards)} 个问题"
        lines = [f"状态：{status}；得分 {ev.score}（阶段, 问题数, L3 未通过数；越小越好）"]
        if ev.cards:
            lines.append(render_cards(ev.cards))
        for line in ev.l3_failures[:8]:
            lines.append(line[:300])
        return "\n".join(lines)

    def check(self) -> str:
        self.last = self.evaluate()
        self._track(self.last, edited=False)
        self._keep_best()
        return self.summary()

    def show(self, block: str, start: Any = None, end: Any = None, find: Any = None) -> str:
        """A block with sheet line numbers: whole, a line range, or the lines around ``find``."""
        b = self.block(str(block))
        if b is None:
            return f"没有块 {block}；现有：{', '.join(x.key for x in self.blocks)}"
        rows = b.numbered().splitlines()
        head = (
            f"```{b.label}```（第 {b.line}–{b.line + len(rows) - 1} 行；行号是整份战术稿中的行号）"
        )
        if find:
            hits = [i for i, row in enumerate(rows) if str(find) in row]
            if not hits:
                return f"{head}\n块内没有找到 {find!r}"
            keep: set[int] = set()
            for i in hits[:8]:
                keep |= set(range(max(0, i - SHOW_CONTEXT), min(len(rows), i + SHOW_CONTEXT + 1)))
            out, last = [], -2
            for i in sorted(keep):
                if i != last + 1:
                    out.append("  …")
                out.append(rows[i])
                last = i
            return f"{head}\n找到 {len(hits)} 处 {find!r}：\n" + "\n".join(out)
        try:
            lo = int(start) - b.line if start is not None else 0
            hi = int(end) - b.line + 1 if end is not None else len(rows)
        except (TypeError, ValueError):
            return "start / end 要写整份战术稿中的行号（整数）"
        lo, hi = max(0, lo), min(len(rows), hi)
        if start is None and end is None and len(rows) > SHOW_LINES:
            hi = SHOW_LINES
        body = "\n".join(rows[lo:hi])
        more = ""
        if hi < len(rows):
            more = (
                f"\n…（只显示到第 {b.line + hi - 1} 行；用 start/end 看其余部分，"
                "或用 find 查找关键字）"
            )
        return f"{head}\n{body}{more}"

    def guide(self, block: str) -> str:
        kind = str(block).split(":", 1)[0]
        if kind not in BLOCK_KINDS:
            return f"没有这种块：{block}"
        return _clip(guide_for(kind, self.ws.ctx, self.last.symbols), 6000)

    def _card(self, problem: Any) -> Card | None:
        try:
            index = int(str(problem).lstrip("#")) - 1
        except ValueError:
            return None
        cards = self.last.cards
        return cards[index] if 0 <= index < len(cards) else None

    def explain(self, problem: Any) -> str:
        card = self._card(problem)
        if card is None:
            return f"没有问题 #{problem}；先调用 check 看编号"
        parts = [f"问题全文：{card.text}", f"所在块：{card.block}"]
        if card.context:
            parts.append(card.context)
        parts += card.notes
        symbols = self.last.symbols
        if card.block == "verification":
            parts.append(P.transition_checklist(symbols))
        parts.append(
            "可引用的名字：\n" + P.symbol_list(symbols, usage(self.ws.ctx, self._compiled))
        )
        return _clip("\n".join(parts))

    def try_tick(self, role: Any, state: Any, given: Any = None) -> str:
        """One L2 tick of a role in a chosen situation, in a scratch copy of the package."""
        if not isinstance(given, dict):
            given = {}
        probe = {
            "id": "zz_try_tick",
            "category": "nominal",
            "role": str(role),
            "instance": 0,
            "state": str(state),
            "given": given,
            "expect": {"next_state": str(state)},
        }
        results = self._scratch_l2([probe])
        r = (results or {}).get("zz_try_tick")
        if r is None:
            return "当前稿件还不能运行单步测试（编译或静态校验有错误），先解决这些问题再试"
        return (
            f"{role} 在 {state}，给定 {json.dumps(given, ensure_ascii=False)}：\n"
            f"下一状态 {r.get('next_state')}，触发转换 {r.get('fired_transition')}，"
            f"树状态 {r.get('tree_status')}，动作 {r.get('issued_actions')}，消息 {r.get('sent_messages')}"
        )

    def _apply_changes(self, changes: Any) -> tuple[list[Block] | None, str]:
        """Changes → candidate blocks (or None and why). Line ranges use current numbers."""
        if isinstance(changes, dict):
            changes = [changes]
        if not isinstance(changes, list) or not changes:
            return None, "changes 要写成列表：[{block, old, new}] 或 [{block, start, end, text}]"
        candidate = _copy(self.blocks)
        by_key = {b.key: b for b in candidate}
        # line ranges refer to the sheet as it is now: apply them bottom-up first
        ranged = [c for c in changes if isinstance(c, dict) and "start" in c]
        textual = [c for c in changes if isinstance(c, dict) and "start" not in c]
        if len(ranged) + len(textual) != len(changes):
            return None, "changes 中每一项都要是对象"
        for c in sorted(ranged, key=lambda c: -int(c.get("start") or 0)):
            b = by_key.get(str(c.get("block")))
            if b is None:
                return None, f"没有块 {c.get('block')}；现有：{', '.join(by_key)}"
            try:
                start = int(c["start"])
                end = int(c.get("end", start))
            except (TypeError, ValueError):
                return None, "start / end 要写整份战术稿中的行号（整数）"
            rows = b.text.splitlines()
            lo, hi = start - b.line, end - b.line + 1
            if lo < 0 or hi > len(rows) or hi < lo:
                return None, (
                    f"{b.key} 的行号范围是 {b.line}–{b.line + len(rows) - 1}，"
                    f"第 {start}–{end} 行不在块内（end = start - 1 表示在 start 之前插入）"
                )
            text = str(c.get("text") or "")
            rows[lo:hi] = text.splitlines() if text else []
            b.text = "\n".join(rows) + "\n"
        for c in textual:
            b = by_key.get(str(c.get("block")))
            if b is None:
                return None, f"没有块 {c.get('block')}；现有：{', '.join(by_key)}"
            old, new = str(c.get("old") or ""), str(c.get("new") or "")
            found = b.text.count(old) if old else 0
            if found != 1:
                return None, (
                    f"old 在 {b.key} 中出现 {found} 次，必须恰好 1 次；"
                    "可以从问题卡片里的原文复制，或改用 start/end 按行号替换"
                )
            b.text = b.text.replace(old, new, 1)
        return candidate, ""

    def edit(self, changes: Any, reason: str = "", add_tests: Any = True) -> str:
        candidate, why = self._apply_changes(changes)
        if candidate is None:
            return self._refuse(why, reason)
        return self._commit(candidate, "edit", reason, add_tests=add_tests is not False)

    def patch(self, block: Any, old: Any, new: Any, reason: str = "") -> str:
        return self.edit([{"block": block, "old": old, "new": new}], reason)

    def apply_fix(self, problem: Any, fix: Any = 1, reason: str = "") -> str:
        card = self._card(problem)
        if card is None:
            return f"没有问题 #{problem}；先调用 check 看编号"
        try:
            chosen = card.fixes[int(fix) - 1]
        except (ValueError, IndexError):
            return f"问题 #{problem} 没有快速修复 {fix}"
        self.counters["quick_fixes"] += 1
        why = reason or f"快速修复：{chosen.title}"
        if chosen.changes:
            candidate, err = self._apply_changes(chosen.changes)
            if candidate is None:
                return self._refuse(err, why)
            return self._commit(candidate, "apply_fix", why)
        if chosen.tests_for:
            before = {b.key: b.text for b in self.blocks}
            score_before = self.last.score
            added, missing = self._add_tests(chosen.tests_for)
            if not added:
                return self._refuse(
                    f"没找到能触发 {', '.join(missing)} 的输入；用 try_tick 试出输入后手写测试",
                    why,
                )
            self.last = self.evaluate()
            solved, new = self._track(self.last, edited=True)
            self._keep_best()
            v = self.block("verification")
            self.edits.append(
                Edit(
                    self.step,
                    "verification",
                    "apply_fix",
                    before.get("verification", ""),
                    v.text if v else "",
                    why,
                    score_before,
                    self.last.score,
                )
            )
            lines = [f"已补测试 {', '.join(added)}；得分 {score_before} → {self.last.score}"]
            if solved:
                lines.append("已解决：" + "；".join(solved))
            if new:
                lines.append("新出现：" + "；".join(new))
            return "\n".join(lines) + "\n" + self.summary()
        return "这个快速修复没有内容"

    def replace(self, block: Any, text: Any, reason: str = "") -> str:
        key = str(block)
        kind, _, role = key.partition(":")
        if kind not in BLOCK_KINDS:
            return f"没有这种块：{block}"
        body = _strip_fence(str(text or ""))
        candidate = [x for x in _copy(self.blocks) if x.key != key]
        candidate.append(Block(kind, role or None, body, 0))
        return self._commit(candidate, "replace", reason)

    def undo(self, reason: str = "") -> str:
        """Undo the last applied edit as a whole (every block it touched)."""
        if not self.history:
            return "没有可以撤销的修改"
        previous = self.history.pop()
        return self._commit(
            previous, "undo", reason or "撤销上一次修改", add_tests=False, stage_guard=False
        )

    def restore_best(self, reason: str) -> str:
        """Go back to the best version seen (all blocks)."""
        blocks, _ = split_sheet(self.best[1])
        self.history.append(_copy(self.blocks))
        return self._commit(blocks, "undo", reason, add_tests=False, stage_guard=False)

    def revert(self, block: Any, reason: str = "") -> str:
        key = str(block)
        if key not in self.original:
            return f"{key} 不是开始时就有的块"
        kind, _, role = key.partition(":")
        candidate = [x for x in _copy(self.blocks) if x.key != key]
        candidate.append(Block(kind, role or None, self.original[key], 0))
        return self._commit(
            candidate,
            "revert",
            reason or "撤回到开始时的文本",
            add_tests=False,
            stage_guard=False,
        )

    # ------------------------------------------------------------------ progress
    def progress(self, thoughts: list[str]) -> str:
        solved = [(k, v) for k, v in self.ledger.items() if v.solved is not None]
        lines = []
        if solved:
            lines.append(
                "已解决："
                + "；".join(f"{v.text[:70]}（第 {v.solved} 步）" for _, v in solved[-10:])
            )
        stuck = []
        for i, card in enumerate(self.last.cards, 1):
            item = self.ledger.get(card.key)
            if item and item.edits >= STUCK_EDITS:
                stuck.append(
                    f"#{i}（已出现 {self.step - item.first} 步，{item.edits} 次修改仍未解决）"
                )
        if stuck:
            lines.append(
                "⚠ 反复未解决："
                + "；".join(stuck)
                + "。先用 try_tick 或 explain 验证你的假设，换一种改法；确属意图问题就 ask_human"
            )
        if self.failed:
            lines.append(
                "失败的做法（不要重复）：\n" + "\n".join(f"- {f}" for f in self.failed[-6:])
            )
        if thoughts:
            lines.append("你最近的计划：\n" + "\n".join(f"- {t}" for t in thoughts[-3:]))
        return "\n".join(lines)


# ---------------------------------------------------------------------- the loop
RULES = """你在修正一份战术稿（战术行为模型的源文件），使它通过编译器、单步测试、反应探针{l3}检查。
你每一步只能调用一个工具，用一个 JSON 对象回答：{{"thought": "简短的推理与计划", "action": "工具名", "args": {{…}}}}。

问题卡片：check 和每次修改后都会列出带编号的问题，卡片里已附上所在的行和原文（失败的测试还附实际触发的转换），通常不需要再 show。

工具：
- apply_fix {{"problem": 编号, "fix": 1}}：应用卡片里列出的快速修复（机械问题的确定修法，检查器会验证）。有快速修复时优先用它，但先判断它是否符合战术意图。
- edit {{"changes": [{{"block": "块名", "old": "块内唯一出现的原文", "new": "替换文本"}}, {{"block": "块名", "start": 行号, "end": 行号, "text": "替换这些行的新文本"}}], "reason": "为什么"}}：一次改一处或多处（可跨块），一起检查。start/end 是整份战术稿的行号；end = start - 1 表示在 start 之前插入。新增的转换会自动补上验证过的单步测试，不必手写；改动若让检查停在更早的阶段（解析或静态校验错误）则不会应用。
- check {{}}：重新检查。
- show {{"block": "块名", "start": 行号, "end": 行号, "find": "关键字"}}：看某块（带行号）；start/end/find 可选，长块默认只显示前 {show} 行。块名如 model、mermaid:platoon_leader、conditions、pytree:wingman、basis、needs、verification。
- explain {{"problem": 编号}}：问题全文、相关转换清单与可引用的名字。
- guide {{"block": "块名"}}：该块的写法说明。
- try_tick {{"role": "角色", "state": "状态", "given": {{…与测试 given 写法相同…}}}}：让该角色在这个情况下跑一拍，看下一状态、触发的转换、动作与消息。用来验证假设，不改稿件。
- replace {{"block": "块名", "text": "整块新内容（不带 ``` 围栏）", "reason": "为什么"}}：整块重写，或新增块。
- undo {{}}：撤销上一次修改（它改动的所有块，包括同时改的测试）。改坏了先 undo，不要逐块 revert。
- revert {{"block": "块名"}}：把一个块撤回到开始时的文本。
- finish {{"summary": "做了什么"}}：全部通过后结束。
- ask_human {{"question": "需要人决定的问题"}}：遇到只能由人判断的问题（战术意图有歧义、依据不足）时结束并提问。

规则：
- 一个意图的所有改动放在同一个 edit 里（例如改转换条件和改相应测试的 expect）。
- 不能靠删除测试、想定或期望来通过检查，不能新增 user_specified 依据（工具会拒绝）。测试意图写错时改 given / expect。
- 改了行为后，原来断言旧行为的测试要一起改；测试的动作、消息、树状态期望来自首次运行记录，改了测试的 given 或 expect 后会自动重新记录。
- `* --> 状态` 全局例外会展开到所有非终止状态（包括正在处理这件事的状态，如交替跃进），只有每个状态都该这样转时才用；多条 `*` 中写在前面的优先。
- 去掉或加上 [sync] 会改变发出的协同消息。
- 不要重复"失败的做法"里的改法；同一问题两次修改没解决，就先验证假设再换思路，或 ask_human。
- 角色要结束（如失去战斗能力）时，新建结束状态：`* --> disabled : 条件` 加 `disabled --> [*]`；`X --> [*]` 不能带条件，测试的 next_state 写 disabled，不要写 [*]。
- 需求属性（PROP001）来自需求条款，不在稿件里也不能改：要让行为满足它；确信属性写错时 ask_human 并说明理由。
- 保持战术本身的意图（需求条款、选定战术）；只修正表达错误和遗漏。"""


def _history(records: list[StepRecord]) -> str:
    lines = []
    recent = records[-HISTORY:]
    for r in recent:
        args = json.dumps(r.args, ensure_ascii=False)
        latest = r is recent[-1]
        limit = LATEST_LIMIT if latest else 400
        result = r.result
        if not latest and "\n状态：" in result:
            result = result.split("\n状态：", 1)[0]  # the cards of old steps are stale
        lines.append(
            f"第 {r.step} 步 {r.action} {_clip(args, 300)}\n想法：{r.thought[:200]}\n"
            f"结果：{_clip(result, limit)}"
        )
    return "\n\n".join(lines)


def _prompt(
    fixer: SheetFixer, records: list[StepRecord], thoughts: list[str], remaining: int
) -> str:
    outline = "\n".join(
        f"- {b.key}（第 {b.line}–{b.line + len(b.text.splitlines()) - 1} 行）" for b in fixer.blocks
    )
    tactic = fixer.ws.ctx.tactic_block or ""
    latest_has_cards = bool(records) and "\n状态：" in records[-1].result
    parts = [
        RULES.format(show=SHOW_LINES, l3="和仿真想定" if fixer.with_l3 else ""),
        "需求条款：\n"
        + "\n".join(f"- {c.id}：{c.text[:160]}" for c in fixer.ws.trace.clauses[:20]),
        tactic and f"选定战术（摘要）：{tactic[:1200]}",
        f"战术稿的块：\n{outline}",
        fixer.progress(thoughts) and "进度（由程序记录）：\n" + fixer.progress(thoughts),
        records and "最近几步：\n" + _history(records),
        not latest_has_cards and f"当前问题：\n{fixer.summary()}",
        f"还剩 {remaining} 步。输出下一步的 JSON。",
    ]
    return "\n\n".join(p for p in parts if p)


def _call(fixer: SheetFixer, step: AgentStep) -> str:
    a = step.args
    reason = str(a.get("reason") or step.thought)[:300]
    if step.action == "check":
        return fixer.check()
    if step.action == "show":
        return fixer.show(a.get("block", ""), a.get("start"), a.get("end"), a.get("find"))
    if step.action == "guide":
        return fixer.guide(a.get("block", ""))
    if step.action == "explain":
        return fixer.explain(a.get("problem", ""))
    if step.action == "try_tick":
        return fixer.try_tick(a.get("role", ""), a.get("state", ""), a.get("given"))
    if step.action == "edit":
        return fixer.edit(a.get("changes"), reason, a.get("add_tests", True))
    if step.action == "apply_fix":
        return fixer.apply_fix(a.get("problem", ""), a.get("fix", 1), str(a.get("reason") or ""))
    if step.action == "patch":
        return fixer.patch(a.get("block", ""), a.get("old"), a.get("new"), reason)
    if step.action == "replace":
        return fixer.replace(a.get("block", ""), a.get("text"), reason)
    if step.action == "revert":
        return fixer.revert(a.get("block", ""), reason)
    if step.action == "undo":
        return fixer.undo(reason)
    return ""


def _metrics(records: list[StepRecord], fixer: SheetFixer, solved: bool) -> dict[str, Any]:
    actions = [r.action for r in records]
    reads = {"show", "guide", "explain"}
    edits = {"edit", "patch", "replace", "apply_fix", "revert", "undo"}
    return {
        "solved": solved,
        "steps": len(records),
        "reads": sum(a in reads for a in actions),
        "edit_steps": sum(a in edits for a in actions),
        "quick_fixes": fixer.counters["quick_fixes"],
        "refused": fixer.counters["refused"],
        "worse": fixer.counters["worse"],
        "try_tick": actions.count("try_tick"),
        "generated_tests": len(fixer.generated),
        "seconds": round(sum(r.seconds for r in records), 1),
    }


async def ensure_properties(fixer: SheetFixer, run: ModelRunner) -> list[str]:
    """Write requirement_properties.yaml when the package has none (independent step:
    the prompt carries the requirement, clauses, tactic and vocabulary, not the sheet)."""
    from afsim_research_agent.falcon.sheet.properties import (
        PROPERTIES_FILE,
        PropertySet,
        message_kinds,
        properties_document,
        property_prompt,
        validate_drafts,
    )

    package = fixer.ws.package_dir
    if (package / PROPERTIES_FILE).is_file():
        return [f"沿用已有的 {PROPERTIES_FILE}"]
    model = fixer.block("model")
    try:
        roles_raw = (yaml.safe_load(model.text) or {}).get("roles") or {} if model else {}
    except yaml.YAMLError:
        roles_raw = {}
    roles = {
        str(rid): {"platform": info.get("platform", ""), "description": info.get("description", "")}
        for rid, info in roles_raw.items()
        if isinstance(info, dict)
    }
    ctx = fixer.ws.ctx
    hidden = set(ctx.constraints.names("unavailable")) if ctx.constraints is not None else set()
    actions = sorted(
        a
        for a in ctx.catalog.actions
        if a not in hidden and a not in ("set_blackboard", "delete_blackboard_value")
    )
    kinds = message_kinds(fixer._ir or {})
    requirement, tactic = ctx.request.requirement, ctx.tactic_block or ""
    selected = package / "selected_tactics.yaml"
    if selected.is_file():
        try:
            doc = yaml.safe_load(selected.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            doc = {}
        if not requirement or requirement.startswith("（"):
            requirement = str((doc.get("selection") or {}).get("requirement") or requirement)
        if not tactic:
            cards = [c.get("card") for c in doc.get("candidates") or [] if isinstance(c, dict)]
            tactic = yaml.safe_dump(cards, allow_unicode=True, sort_keys=False)
    prompt = property_prompt(requirement, fixer.ws.trace.clauses, tactic, roles, actions, kinds)
    try:
        drafts = await run("properties:write", prompt, PropertySet)
    except Exception as exc:  # noqa: BLE001 - the loop still runs on the other checks
        return [f"未能生成需求属性（{type(exc).__name__}）；本次只按测试与探针检查"]
    kept, rejected = validate_drafts(drafts, list(roles), set(actions), kinds)
    (package / PROPERTIES_FILE).write_text(
        properties_document(
            kept,
            [s.model_dump() for s in drafts.skipped],
            rejected,
            getattr(run.model, "name", "model"),
        ),
        encoding="utf-8",
    )
    fixer.last = fixer.evaluate()
    fixer._track(fixer.last, edited=False)
    fixer.start_score = fixer.last.score
    fixer.best = (fixer.last.score, fixer.sheet())
    note = f"已生成需求属性 {len(kept)} 条（{PROPERTIES_FILE}，未经人工确认）"
    if rejected:
        note += f"；{len(rejected)} 条因词汇不符被剔除"
    return [note]


async def write_properties(
    package_dir: Path, run: ModelRunner, *, force: bool = False
) -> tuple[list[str], list[str]]:
    """`falcon properties`: (re)write the properties of a package and check its sheet."""
    from afsim_research_agent.falcon.sheet.properties import PROPERTIES_FILE

    path = Path(package_dir) / PROPERTIES_FILE
    if path.is_file():
        if not force:
            raise FalconError("FAL002", f"{PROPERTIES_FILE} exists; use --force to rewrite it")
        path.unlink()
    fixer = SheetFixer(open_workspace(Path(package_dir)))
    notes = await ensure_properties(fixer, run)
    failures = [c.short for c in fixer.last.cards if c.code == "PROP001"]
    return notes + [f"不满足的属性：{len(failures)} 条"], failures


async def fix_package(
    package_dir: Path,
    run: ModelRunner,
    *,
    out_dir: Path | None = None,
    max_steps: int = 30,
    with_l3: bool = False,
    properties: bool = True,
) -> FixResult:
    """Copy ``package_dir`` (to ``<package>_fix`` by default) and repair the copy."""
    source = Path(package_dir)
    if not (source / SHEET_FILE).is_file():
        raise FalconError("FAL002", f"no {SHEET_FILE} in {source}")
    target = Path(out_dir) if out_dir else source.with_name(source.name + "_fix")
    if target.exists():
        raise FalconError("FAL002", f"output directory exists: {target}")
    shutil.copytree(source, target)
    fixer = SheetFixer(open_workspace(target), with_l3=with_l3)
    from afsim_research_agent.falcon.sheet.properties import properties_hash

    property_notes = await ensure_properties(fixer, run) if properties else []
    property_hash = properties_hash(target)
    records: list[StepRecord] = []
    thoughts: list[str] = []
    question = None
    summary = ""
    for index in range(1, max_steps + 1):
        if fixer.last.ok:
            summary = summary or "检查全部通过"
            break
        fixer.step = index
        started = time.perf_counter()
        step = await run(
            "fix:step", _prompt(fixer, records, thoughts, max_steps - index + 1), AgentStep
        )
        if step.thought:
            thoughts.append(step.thought[:200])
        if step.action == "finish":
            summary = str(step.args.get("summary") or step.thought)
            result = fixer.check()
            records.append(StepRecord(index, step.thought, step.action, step.args, result, 0.0))
            if fixer.last.ok:
                break
            fixer.failed.append(f"第 {index} 步 finish 被拒绝：检查还没有全部通过")
            continue
        if step.action == "ask_human":
            question = str(step.args.get("question") or step.thought)
            records.append(StepRecord(index, step.thought, step.action, step.args, "", 0.0))
            break
        try:
            result = _call(fixer, step)
        except FalconError:
            raise
        except Exception as exc:  # noqa: BLE001 - a tool error is an observation, not a crash
            result = f"工具出错：{type(exc).__name__}: {str(exc)[:300]}"
        records.append(
            StepRecord(
                index,
                step.thought,
                step.action,
                step.args,
                result,
                round(time.perf_counter() - started, 3),
            )
        )
        # no progress: the model is digging; go back to the best version, then stop and ask
        stalled = index - fixer.best_step
        if stalled >= 2 * STALL and not fixer.last.ok:
            question = (
                f"自动修复连续 {stalled} 步没有超过第 {fixer.best_step} 步的版本，已停下。"
                "剩下的问题可能涉及战术意图，需要你决定：\n"
                + "\n".join(f"- {c.short}" for c in fixer.last.cards[:6])
            )
            fixer.counters["stopped"] = 1
            break
        if stalled == STALL and fixer.last.score > fixer.best[0]:
            note = fixer.restore_best(
                f"连续 {STALL} 步没有超过第 {fixer.best_step} 步的版本，自动回到那个版本"
            )
            fixer.failed.append(
                f"第 {fixer.best_step + 1}–{index} 步的改法没有带来进展，已自动回到第 {fixer.best_step} 步的版本；"
                "换一种思路，或者 ask_human"
            )
            records.append(StepRecord(index, "", "auto_restore", {}, note, 0.0))
    # keep the best version seen, rebuilt and written like `falcon compile`
    if fixer.best[0] < fixer.last.score:
        (target / SHEET_FILE).write_text(fixer.best[1], encoding="utf-8")
    else:
        (target / SHEET_FILE).write_text(fixer.sheet(), encoding="utf-8")
    # drop recorded results of tests the loop rewrote; the rebuild records them again
    from afsim_research_agent.falcon.sheet.generate import OBSERVED_FILE, observed_document

    fixer.blocks, _ = split_sheet((target / SHEET_FILE).read_text(encoding="utf-8"))
    fixer.with_l3, wanted_l3 = False, fixer.with_l3
    fixer.evaluate()
    fixer.with_l3 = wanted_l3
    if fixer.rerecorded:
        (target / OBSERVED_FILE).write_text(
            observed_document(fixer.ws.ctx.observed), encoding="utf-8"
        )
    report = recompile_package(target)
    status, problems = report.status, list(report.problems)
    if with_l3 and status == "draft_valid":
        # `falcon compile` stops at L1/L2; with --with-l3 the scenarios must pass as well
        fixer.blocks, _ = split_sheet((target / SHEET_FILE).read_text(encoding="utf-8"))
        final = fixer.evaluate()
        if final.l3_failures:
            status = "l3_failed"
            problems = final.l3_failures
    result = FixResult(
        target,
        status,
        problems,
        records,
        fixer.edits,
        question,
        summary,
        report,
        _metrics(records, fixer, status == "draft_valid"),
    )
    if properties_hash(target) != property_hash:
        raise FalconError("FAL003", "requirement_properties.yaml changed during the fix loop")
    fixer.property_notes = property_notes
    _write_log(target, result, fixer)
    return result


def _write_log(target: Path, result: FixResult, fixer: SheetFixer) -> None:
    sheet = (target / SHEET_FILE).read_text(encoding="utf-8")
    generated = [t for t in fixer.generated if f"id: {t}" in sheet]
    log = {
        "format": "FalconFixLog",
        "status": result.status,
        "start_score": list(fixer.start_score),
        "best_score": list(fixer.best[0]),
        "metrics": result.metrics,
        "question": result.question,
        "rerecorded_tests": sorted(fixer.rerecorded),
        "properties": getattr(fixer, "property_notes", []),
        "generated_tests": generated,
        "summary": result.summary,
        "steps": [asdict(s) for s in result.steps],
        "edits": [asdict(e) for e in result.edits],
    }
    (target / FIX_LOG).write_text(
        json.dumps(log, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    review = target / "falcon_review.md"
    m = result.metrics
    lines = [
        "",
        "## 自检修改记录（falcon fix）",
        "",
        f"- 结果：{result.status}；得分 {tuple(fixer.start_score)} → {tuple(fixer.best[0])}"
        f"（阶段, 问题数, L3 未通过数）；共 {len(result.steps)} 步，{len(result.edits)} 处修改"
        f"（查看 {m.get('reads', 0)} 步，快速修复 {m.get('quick_fixes', 0)} 次，"
        f"被拒绝 {m.get('refused', 0)} 次）",
    ]
    for note in getattr(fixer, "property_notes", []):
        lines.append(f"- 需求属性：{note}；请核对 requirement_properties.yaml（确认、改正或停用）")
    if result.summary:
        lines.append(f"- 模型总结：{result.summary}")
    if result.question:
        lines.append(f"- **需要你决定**：{result.question}")
    if fixer.rerecorded:
        lines.append(
            "- 这些测试的情况或意图被修改，机械结果（动作、消息、树状态）已按修改后的运行重新记录"
            f"（observed_expectations.yaml），请核对：{', '.join(sorted(fixer.rerecorded))}"
        )
    if generated:
        lines.append(
            "- 这些单步测试由程序自动生成（搜索能触发新增转换的输入并验证），只证明转换能被触发，"
            f"请核对输入是否合理：{', '.join(generated)}"
        )
    lines.append("- 下列修改由模型自行完成，请逐条核对是否改变了战术意图。")
    for e in result.edits:
        lines += [
            "",
            f"### 第 {e.step} 步 {e.action} `{e.block}`（得分 {e.score_before} → {e.score_after}）",
            "",
            f"理由：{e.reason}",
            "",
            "```diff",
            *_diff(e.before, e.after),
            "```",
        ]
    old = review.read_text(encoding="utf-8") if review.is_file() else ""
    review.write_text(old.rstrip() + "\n" + "\n".join(lines) + "\n", encoding="utf-8")


def _diff(before: str, after: str) -> list[str]:
    import difflib

    out = list(difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=1))
    return [line for line in out[2:]] if out else ["（无变化）"]


__all__ = ["ACTIONS", "AgentStep", "FixResult", "SheetFixer", "fix_package"]
