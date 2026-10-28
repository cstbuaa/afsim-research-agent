"""Mermaid ``stateDiagram-v2`` subset: one diagram per role.

    stateDiagram-v2
      %% role: platoon_leader
      [*] --> travel_open
      travel_open --> contact : in_contact [sync]
      travel_open --> travel_urban : near_urban [sync]
      contact --> travel_open : contact_cleared
      travel_open : 开阔地楔形行进

Transitions out of one state are prioritized in writing order (first = highest).
``* --> contact : in_contact [sync]`` is a global exception: the compiler adds it to every
non-terminal state except the target (before that state's own transitions, so it takes
precedence), skipping states that already have a transition to the same target.
The label is a condition name, ``not <name>`` or a short Python expression; ``[sync]``
marks a transition the role's leader_follower / synchronized_transition coordination
propagates. The "stay in this state" fallback is added by the compiler.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from afsim_research_agent.falcon.sheet.blocks import SheetProblem

END_HINT = (
    "要在条件成立时结束（如失去战斗能力），新建一个结束状态，写 `* --> disabled : 条件`"
    "（或从具体状态转入）和 `disabled --> [*]`，测试的 next_state 写 disabled"
)

_ARROW = re.compile(
    r"^(?P<a>\[\*\]|\*|[A-Za-z_][\w]*)\s*-+>\s*(?P<b>\[\*\]|[A-Za-z_][\w]*)\s*(?::\s*(?P<label>.*))?$"
)
_DESC = re.compile(r"^(?P<s>[A-Za-z_][\w]*)\s*:\s*(?P<d>.+)$")
_STATE_AS = re.compile(r'^state\s+"(?P<d>[^"]*)"\s+as\s+(?P<s>[A-Za-z_][\w]*)\s*$')
_SYNC = re.compile(r"\[\s*(?:sync|同步)\s*\]|\(\s*(?:sync|同步)\s*\)", re.I)
_IGNORED = re.compile(
    r"^(?:stateDiagram(?:-v2)?|direction\s+\w+|classDef\b.*|class\b.*|note\b.*|end note)$"
)


@dataclass
class MermaidTransition:
    source: str
    target: str
    label: str
    sync: bool
    line: int


@dataclass
class Diagram:
    role: str
    initial: str | None = None
    states: list[str] = field(default_factory=list)  # in first-seen order
    descriptions: dict[str, str] = field(default_factory=dict)
    terminal: set[str] = field(default_factory=set)
    transitions: list[MermaidTransition] = field(default_factory=list)
    lines: dict[str, int] = field(default_factory=dict)  # state -> first line
    wildcards: list[MermaidTransition] = field(default_factory=list)  # `* --> x`

    def _see(self, state: str, line: int) -> None:
        if state not in self.lines:
            self.lines[state] = line
            self.states.append(state)


def parse_diagram(
    text: str, role: str, block: str, line: int
) -> tuple[Diagram, list[SheetProblem]]:
    diagram = Diagram(role)
    problems: list[SheetProblem] = []
    for offset, raw in enumerate(text.splitlines()):
        here = line + offset
        stripped = raw.strip()
        if not stripped or stripped.startswith("%%") or _IGNORED.match(stripped):
            continue
        if stripped.startswith("state ") and stripped.endswith("{"):
            problems.append(SheetProblem(block, here, "不支持复合状态（state x { … }），请展平"))
            continue
        if stripped == "}":
            continue
        match = _ARROW.match(stripped)
        if match:
            a, b, label = match.group("a"), match.group("b"), (match.group("label") or "").strip()
            if a == "[*]" and b == "[*]":
                problems.append(SheetProblem(block, here, "[*] --> [*] 没有意义"))
                continue
            if a == "[*]":
                if diagram.initial and diagram.initial != b:
                    problems.append(
                        SheetProblem(block, here, f"有两个初始状态（{diagram.initial}、{b}）")
                    )
                diagram.initial = diagram.initial or b
                diagram._see(b, here)
                continue
            if a == "*":
                if b == "[*]":
                    problems.append(SheetProblem(block, here, "* --> [*] 不能这样写：" + END_HINT))
                    continue
                diagram._see(b, here)
                sync = bool(_SYNC.search(label))
                label = _SYNC.sub("", label).strip()
                if not label:
                    problems.append(
                        SheetProblem(
                            block, here, f"* --> {b} 缺少条件（写成 `* --> {b} : 条件名`）"
                        )
                    )
                    continue
                diagram.wildcards.append(MermaidTransition("*", b, label, sync, here))
                continue
            diagram._see(a, here)
            if b == "[*]":
                if label:
                    # `x --> [*]` marks x as an end state; a condition here was ignored
                    problems.append(
                        SheetProblem(
                            block,
                            here,
                            f"`{a} --> [*]` 表示 {a} 是结束状态，不能带条件（{label}）。"
                            + END_HINT,
                        )
                    )
                    continue
                diagram.terminal.add(a)
                continue
            diagram._see(b, here)
            sync = bool(_SYNC.search(label))
            label = _SYNC.sub("", label).strip()
            if not label:
                problems.append(
                    SheetProblem(
                        block, here, f"转换 {a} --> {b} 缺少条件（写成 `{a} --> {b} : 条件名`）"
                    )
                )
                continue
            diagram.transitions.append(MermaidTransition(a, b, label, sync, here))
            continue
        match = _STATE_AS.match(stripped)
        if match:
            diagram._see(match.group("s"), here)
            diagram.descriptions[match.group("s")] = match.group("d").strip()
            continue
        match = _DESC.match(stripped)
        if match:
            diagram._see(match.group("s"), here)
            diagram.descriptions[match.group("s")] = match.group("d").strip()
            continue
        if re.match(r"^[A-Za-z_]\w*$", stripped):
            diagram._see(stripped, here)
            continue
        problems.append(SheetProblem(block, here, f"无法理解的 Mermaid 行：{stripped[:80]}"))
    _expand_wildcards(diagram)
    if diagram.initial is None:
        if diagram.states:
            problems.append(
                SheetProblem(
                    block,
                    line,
                    f"没有初始状态（[*] --> 状态），按 {diagram.states[0]} 处理",
                    severity="warning",
                )
            )
            diagram.initial = diagram.states[0]
        else:
            problems.append(SheetProblem(block, line, "状态图中没有状态"))
    return diagram, problems


def _expand_wildcards(diagram: Diagram) -> None:
    """`* --> x : c` → `s --> x : c` for every other non-terminal state, ahead of the
    state's own transitions (a global exception preempts them)."""
    if not diagram.wildcards:
        return
    own = list(diagram.transitions)
    expanded: list[MermaidTransition] = []
    for state in diagram.states:
        if state in diagram.terminal:
            continue
        for w in diagram.wildcards:
            if w.target == state or any(t.source == state and t.target == w.target for t in own):
                continue
            expanded.append(MermaidTransition(state, w.target, w.label, w.sync, w.line))
    diagram.transitions = expanded + own
