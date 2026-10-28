"""Problem cards, quick fixes and test synthesis for ``falcon fix``.

Two online runs on py9 spent more than half of their steps reading: a problem named a
block and a line but not the text there, so every edit started with a ``show``; an IR
path (``conditions[7]…``) named no place in the sheet at all; and a missing test, whose
inputs the checker already knew, took a separate hand-written edit after every new
transition. Here each problem becomes a card that carries its location and the text
around it, and the problems with a mechanical fix carry that fix, ready to apply.

Design and sources: ``docs/analysis/2026-10-19-agent-loop-efficiency.md``.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from afsim_research_agent.falcon.sheet.blocks import Block

CONTEXT = 3  # lines around a problem line
TEST_LINES = 18  # lines of a test shown with its failure
MAX_CANDIDATES = 160  # inputs tried when synthesizing one test

_LINE = re.compile(r"^- \[(?P<block>\S+) (?:第 (?P<line>\d+) 行|整块)\] ?(?P<rest>.*)$", re.S)
_CODE = re.compile(r"^(?P<code>[A-Z]+\d+) (?P<path>\S+?):? (?P<msg>.*)$", re.S)
_ARROW = re.compile(r"^\s*(?P<src>\*|\w+)\s*-+>\s*(?P<dst>\w+)\s*:\s*(?P<label>.+?)\s*$")


@dataclass
class QuickFix:
    title: str
    changes: list[dict[str, Any]] = field(default_factory=list)  # edit changes
    tests_for: list[str] = field(default_factory=list)  # transition ids to synthesize tests for


@dataclass
class Card:
    key: str  # stable across steps (no line numbers)
    code: str
    block: str
    line: int | None
    text: str  # the problem as the checker states it
    context: str = ""  # numbered sheet lines
    notes: list[str] = field(default_factory=list)
    fixes: list[QuickFix] = field(default_factory=list)

    @property
    def short(self) -> str:
        where = f"{self.block} 第 {self.line} 行" if self.line else self.block
        return f"[{self.code}] {where}：{_one_line(self.text)[:120]}"


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def numbered(block: Block, lo: int, hi: int) -> str:
    """Sheet lines lo..hi (inclusive) of a block, numbered like ``show``."""
    rows = block.text.splitlines()
    out = []
    for i in range(max(lo, block.line), min(hi, block.line + len(rows) - 1) + 1):
        out.append(f"{i:4d} | {rows[i - block.line]}")
    return "\n".join(out)


def block_line(block: Block, line: int) -> str:
    rows = block.text.splitlines()
    i = line - block.line
    return rows[i] if 0 <= i < len(rows) else ""


def _test_span(block: Block, test_id: str) -> tuple[int, int] | None:
    rows = block.text.splitlines()
    start = next(
        (
            i
            for i, r in enumerate(rows)
            if re.match(rf"^\s*-\s+id:\s*['\"]?{re.escape(test_id)}['\"]?\s*$", r)
        ),
        None,
    )
    if start is None:
        return None
    end = start + 1
    while (
        end < len(rows)
        and not re.match(r"^\s*-\s+id:", rows[end])
        and not re.match(r"^\S", rows[end])
    ):
        end += 1
    end = min(end, start + TEST_LINES)
    return block.line + start, block.line + end - 1


# ---------------------------------------------------------------------- cards
def build_cards(
    problems: list[tuple[str, str]],
    blocks: list[Block],
    symbols: dict[str, Any],
    source_map: dict[str, dict[str, Any]],
    observed: dict[str, Any],
) -> list[Card]:
    by_key = {b.key: b for b in blocks}
    checklist = {t["id"]: t for t in symbols.get("transition_checklist") or []}
    untested = [t for t in symbols.get("untested") or [] if t in checklist]
    cards: list[Card] = []
    hostile_fixes: dict[str, list] = {}
    for block_key, raw in problems:
        m = _LINE.match(raw)
        block = m.group("block") if m else block_key
        line = int(m.group("line")) if m and m.group("line") else None
        rest = m.group("rest") if m else raw
        c = _CODE.match(rest)
        code, path, msg = (
            (c.group("code"), c.group("path"), c.group("msg")) if c else ("", "", rest)
        )
        if "没有单步测试" in rest and untested:
            continue  # replaced by one card per untested transition below
        if not code:
            code = "SHEET"
        key = (
            f"{code}:{path}"
            if path
            else f"{code}:{block}:{re.sub(r'[0-9]+', '#', _one_line(msg))[:80]}"
        )
        card = Card(key, code, block, line, f"{path}: {msg}" if code == "TEST001" else msg)
        b = by_key.get(block)
        if code == "PROP001":
            role = re.search(r"角色 (\w+) 从 (\w+) 出发", msg)
            if role:
                card.key = f"PROP001:{path}:{role.group(1)}:{role.group(2)}"
            diagram = by_key.get(block)
            if diagram is not None and role:
                visited = {role.group(2), *re.findall(r"(\w+)（动作", msg)}
                rows = [
                    f"{diagram.line + i:4d} | {row}"
                    for i, row in enumerate(diagram.text.splitlines())
                    if (a := _ARROW.match(row)) and a.group("src") in visited | {"*"}
                ]
                if rows:
                    card.context = "途经状态的出口转换：\n" + "\n".join(rows)
            card.notes.append(
                "需求属性由独立的核查步骤根据需求条款写成（requirement_properties.yaml），不在战术稿里，"
                "不能修改，改测试也绕不过去：要改的是行为。若认为属性本身写错了，用 ask_human 说明理由"
            )
        if code == "TEST001" and path.startswith("tests."):
            _test_card(card, path.split(".", 1)[1], by_key, source_map, observed)
        elif b is not None and line:
            card.context = numbered(b, line - CONTEXT, line + CONTEXT)
        if code == "UNIT001" and b is not None and line:
            _unit_fix(card, b, line)
        if code in ("PROBE001", "PROBE002"):
            _probe_context(card, by_key)
            _probe_fix(card, by_key, symbols, hostile_fixes)
        cards.append(card)
    _merge_hostile(hostile_fixes)
    for tid in untested:
        t = checklist[tid]
        needs = f"；条件读取 {', '.join(t['inputs'])}" if t["inputs"] else ""
        card = Card(
            f"COVER:{tid}",
            "COVER",
            "verification",
            None,
            f"{t['role']} 的转换 {t['transition']}（条件 {t['when']}）没有单步测试{needs}",
        )
        where = source_map.get(f"transition:{tid}")
        if where and where.get("block") in by_key and where.get("line"):
            src = by_key[where["block"]]
            card.notes.append("转换所在行：\n" + numbered(src, where["line"], where["line"]))
        card.fixes.append(
            QuickFix(
                "自动生成测试：搜索能触发这条转换的输入，验证通过后写入验证块", tests_for=[tid]
            )
        )
        cards.append(card)
    return cards


def _test_card(
    card: Card,
    test_id: str,
    by_key: dict[str, Block],
    source_map: dict[str, dict[str, Any]],
    observed: dict[str, Any],
) -> None:
    v = by_key.get("verification")
    span = _test_span(v, test_id) if v else None
    if v and span:
        card.line = span[0]
        card.context = numbered(v, *span)
    fired = re.search(r"fired_transition=(\w+)", card.text) or re.search(
        r"fired_transition: expected '[^']*', got '(\w+)'", card.text
    )
    if fired and fired.group(1) != "None":
        where = source_map.get(f"transition:{fired.group(1)}")
        if where and where.get("block") in by_key and where.get("line"):
            card.notes.append(
                f"实际触发的转换 {fired.group(1)} 写在：\n"
                + numbered(by_key[where["block"]], where["line"], where["line"])
            )
    if test_id in observed and re.search(r"issued_actions|sent_messages|tree_status", card.text):
        card.notes.append(
            "动作、消息、树状态的期望来自 observed_expectations.yaml（首次运行的记录），不在稿件里；"
            "只要改了这个测试的 given 或 expect（next_state/transition），记录就会按新的运行重新生成"
        )


def _probe_context(card: Card, by_key: dict[str, Block]) -> None:
    """A probe names states: show the transitions that leave them (and the `*` ones)."""
    role = re.match(r"^(\w+) ", card.text)
    diagram = by_key.get(f"mermaid:{role.group(1)}") if role else None
    if diagram is None:
        return
    cycle = re.search(r"来回切换：([\w →]+)（", card.text)
    single = re.search(r"^\w+ 在 (\w+) ", card.text)
    states = (
        {s.strip() for s in cycle.group(1).split("→")}
        if cycle
        else ({single.group(1)} if single else set())
    )
    if not states:
        return
    keep = []
    for i, row in enumerate(diagram.text.splitlines()):
        a = _ARROW.match(row)
        if a and a.group("src") in states | {"*"}:
            keep.append(f"{diagram.line + i:4d} | {row}")
    names = "、".join(sorted(states))
    card.context = (
        f"离开 {names} 的转换（含 `*` 全局例外）：\n" + "\n".join(keep)
        if keep
        else f"{names} 没有自己的出口转换"
    )


def _unit_fix(card: Card, block: Block, line: int) -> None:
    text = block_line(block, line)
    m = re.search(r"(time_in_state\(\)\s*[<>]=?\s*)(\d+(?:\.\d+)?)(?![\w.\"'\s]*[\"'a-zA-Z])", text)
    if m and "time" in card.text:
        new = text[: m.start(2)] + f'"{m.group(2)} s"' + text[m.end(2) :]
        card.fixes.append(
            QuickFix(
                f'时间加上单位：{m.group(2)} → "{m.group(2)} s"',
                [{"block": block.key, "start": line, "end": line, "text": new}],
            )
        )


def _label_reads_ack(label: str, conditions: Block | None) -> bool:
    for tok in re.findall(r"[A-Za-z_][\w.]*", label):
        if "ack" in tok:
            return True
        if conditions is not None:
            m = re.search(rf"^\s*{re.escape(tok)}\s*=\s*(.*)$", conditions.text, re.M)
            if m and "ack_received" in m.group(1):
                return True
    return False


def _timeout_parameter(symbols: dict[str, Any]) -> str | None:
    params = [str(p) for p in symbols.get("parameters") or []]
    for test in (
        lambda p: "ack" in p and ("timeout" in p or "wait" in p),
        lambda p: "timeout" in p,
    ):
        found = [p for p in params if test(p)]
        if found:
            return found[0]
    return None


def _probe_fix(
    card: Card,
    by_key: dict[str, Block],
    symbols: dict[str, Any],
    hostile_fixes: dict[str, list],
) -> None:
    ack = re.search(r"(?P<role>\w+) 在 (?P<state>\w+) 等待 (?P<coord>\w+) 的确认", card.text)
    if ack:
        diagram = by_key.get(f"mermaid:{ack.group('role')}")
        param = _timeout_parameter(symbols)
        if diagram is None or param is None:
            return
        rows = diagram.text.splitlines()
        for i, row in enumerate(rows):
            m = _ARROW.match(row)
            if not m or m.group("src") != ack.group("state"):
                continue
            label = m.group("label")
            sync = ""
            if label.endswith("[sync]"):
                label, sync = label[: -len("[sync]")].rstrip(), " [sync]"
            if not _label_reads_ack(label, by_key.get("conditions")):
                continue
            cond = label if re.fullmatch(r"[\w.]+", label) else f"({label})"
            indent = re.match(r"^\s*", row).group(0)
            new = f"{indent}{m.group('src')} --> {m.group('dst')} : {cond} or time_in_state() >= {param}{sync}"
            line = diagram.line + i
            card.fixes.append(
                QuickFix(
                    f"等待确认加超时出口：第 {line} 行改为 `{new.strip()}`（{param} 内没收到确认也离开；"
                    "原来断言超时后仍停留的测试需要随之修改）",
                    [{"block": diagram.key, "start": line, "end": line, "text": new}],
                )
            )
            break
        return
    hostile = re.search(
        r"(?P<role>\w+) 在 (?P<state>\w+) 沿路线行进时发现敌人.*?而在 (?P<example>\w+) 会转入 (?P<target>\w+)",
        card.text,
        re.S,
    )
    if not hostile:
        return
    role, state = hostile.group("role"), hostile.group("state")
    example, target = hostile.group("example"), hostile.group("target")
    diagram = by_key.get(f"mermaid:{role}")
    if diagram is None:
        return
    rows = diagram.text.splitlines()
    label = None
    own = last_plain = None
    for i, row in enumerate(rows):
        m = _ARROW.match(row)
        if not m:
            continue
        if m.group("src") != "*":
            last_plain = i
        if m.group("src") == state and own is None:
            own = i  # contact preempts the state's routine transitions: go before them
        if m.group("src") == example and m.group("dst") == target and label is None:
            label = m.group("label")
    at = own if own is not None else last_plain
    if label is None or at is None:
        return
    line = diagram.line + at
    indent = re.match(r"^\s*", rows[at]).group(0) or "  "
    added = f"{indent}{state} --> {target} : {label}"
    text = f"{added}\n{rows[at]}" if own is not None else f"{rows[at]}\n{added}"
    card.fixes.append(
        QuickFix(
            f"加转换 `{state} --> {target} : {label}`，放在 {state} 其他出口之前（接敌优先；"
            f"与 {example} 的接敌处理相同；自动补测试）",
            [{"block": diagram.key, "start": line, "end": line, "text": text}],
        )
    )
    hostile_fixes.setdefault(role, []).append(
        (card, diagram.key, line, rows[at], added, own is not None)
    )


def _merge_hostile(hostile_fixes: dict[str, list]) -> None:
    """Several moving states of one role deaf to contact: one fix for all of them."""
    for items in hostile_fixes.values():
        if len(items) < 2:
            continue
        by_line: dict[tuple[str, int], tuple[str, list[str], list[str]]] = {}
        for _card, key, line, base, added, before in items:
            entry = by_line.setdefault((key, line), (base, [], []))
            (entry[1] if before else entry[2]).append(added)
        changes = [
            {"block": key, "start": line, "end": line, "text": "\n".join([*pre, base, *post])}
            for (key, line), (base, pre, post) in by_line.items()
        ]
        states = "、".join(item[4].split("-->")[0].strip() for item in items)
        merged = QuickFix(f"一次给 {states} 都加上接敌转换（自动补测试）", changes)
        for card, *_ in items:
            card.fixes.append(merged)


def render_cards(cards: list[Card], *, detail: int = 8) -> str:
    out = []
    for i, card in enumerate(cards, 1):
        if i > detail:
            out.append(f"#{i} {card.short}")
            continue
        out.append(f"#{i} [{card.code}] {_one_line(card.text)[:600]}")
        if card.context:
            out.append(card.context)
        out += card.notes
        for j, fix in enumerate(card.fixes, 1):
            out.append(f'  快速修复 {j}：{fix.title}（apply_fix {{"problem": {i}, "fix": {j}}}）')
    return "\n".join(out)


# ---------------------------------------------------------------------- test synthesis
_RANGES = ["800 m", "300 m", "2 km", "100 m", "10 km", "0 m"]
_TIMES = ["5 min", "30 s", "2 min", "15 min", "1 h", "5 s"]
_COUNTS = [1, 3, 0]
_BOOLS = [True, False]
_CLASSES = ["urban", "open", "forest", "road", "water"]


def _values(key: str) -> list[Any]:
    name = key.rsplit(".", 1)[-1]
    if key.startswith("elapsed") or "time" in name or "duration" in name:
        return _TIMES
    if "count" in name or name.startswith("num"):
        return _COUNTS
    if "range" in name or "distance" in key or "altitude" in name:
        return _RANGES
    if (
        key.startswith(("platform.", "components."))
        or name in ("alive", "damaged", "valid")
        or name.endswith(("_valid", "available"))
    ):
        return _BOOLS
    if name == "class":
        return _CLASSES
    if "speed" in name:
        return ["20 km/h", "0 km/h", "60 km/h"]
    return [1, 0, 10, 100, True, False]


def candidate_givens(inputs: list[str]) -> list[dict[str, Any]]:
    """Inputs a transition's condition reads → given sections to try, most plausible first."""
    fixed: dict[str, Any] = {}
    axes: list[tuple[str, str, list[Any]]] = []
    for item in inputs:
        text = item.removeprefix("given.")
        if ":" in text and text.split(":", 1)[0].strip() in ("messages", "coordination"):
            section, value = text.split(":", 1)
            try:
                fixed[section.strip()] = yaml.safe_load(value)
            except yaml.YAMLError:
                continue
            continue
        if "." not in text:
            continue
        section, key = text.split(".", 1)
        axes.append((section, key, _values(f"{section}.{key}")))
    out = []
    for combo in itertools.islice(itertools.product(*(a[2] for a in axes)), MAX_CANDIDATES):
        given: dict[str, Any] = {k: v for k, v in fixed.items()}
        for (section, key, _), value in zip(axes, combo):
            given.setdefault(section, {})[key] = value
        out.append(given)
    return out or [dict(fixed)]


def test_item(t: dict[str, Any], given: dict[str, Any], index: int) -> dict[str, Any]:
    src, dst = [s.strip() for s in t["transition"].split("->")]
    category = "nominal"
    if any(i.startswith("given.elapsed") for i in t["inputs"]):
        category = "timeout_or_loss"
    return {
        "id": f"auto_{t['id']}" if index == 0 else f"auto_{t['id']}_{index}",
        "category": category,
        "role": t["role"],
        "instance": 0,
        "state": src,
        "given": given,
        "expect": {"next_state": dst, "transition": f"{src} -> {dst}"},
    }


def render_test(item: dict[str, Any], indent: str) -> str:
    def flow(value: Any) -> str:
        return yaml.safe_dump(
            value, default_flow_style=True, allow_unicode=True, width=1000
        ).strip()

    pad = indent + "  "
    lines = [f"{indent}- id: {item['id']}"]
    for k in ("category", "role", "instance", "state"):
        lines.append(f"{pad}{k}: {item[k]}")
    if item.get("given"):
        lines.append(f"{pad}given: {flow(item['given'])}")
    lines.append(f"{pad}expect: {flow(item['expect'])}")
    return "\n".join(lines)


def append_tests(text: str, rendered: list[str]) -> str:
    """Insert test items at the end of the ``tests:`` list of a verification block."""
    rows = text.rstrip("\n").splitlines()
    start = next((i for i, r in enumerate(rows) if re.match(r"^tests\s*:", r)), None)
    if start is None:
        return "tests:\n" + "\n".join(rendered) + "\n" + text
    end = start + 1
    while end < len(rows) and not re.match(r"^[A-Za-z_]", rows[end]):
        end += 1
    while end - 1 > start and not rows[end - 1].strip():
        end -= 1
    rows[end:end] = rendered
    return "\n".join(rows) + "\n"


def test_indent(text: str) -> str:
    m = re.search(r"^(\s*)-\s+id:", text, re.M)
    return m.group(1) if m else "  "


__all__ = [
    "Card",
    "QuickFix",
    "append_tests",
    "block_line",
    "build_cards",
    "candidate_givens",
    "numbered",
    "render_cards",
    "render_test",
    "test_indent",
    "test_item",
]
