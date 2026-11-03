"""Mechanical repairs of a drafted sheet, applied before the model-driven repair loop.

``ENTRY_TRUE`` (see ``semantics``): a transition whose condition already holds when its
state is entered is the common way an "arrived / done" step is written wrongly (a field
compared before the state's actions have written it, e.g. ``range_to_target_m <= 5`` on a
field that starts at 0). The repair: the transition waits until the state's actions have
run and written their fields (``time_in_state() >= 2 s`` is added to its trigger), so the
condition is judged on real values. This keeps the drafted meaning and works whether or
not the state's tree ever ends. The caller keeps the result only when the sheet checks
better afterwards.
"""

from __future__ import annotations

import re

from afsim_research_agent.falcon.sheet.blocks import Block

GUARD = "time_in_state() >= 2 * s"


def entry_guards(
    blocks: list[Block], items: list[tuple[str, str, str, str]]
) -> tuple[list[Block], list[str]]:
    """The blocks with the trigger of each (role, from, to, condition) transition guarded
    by the time in state; notes of what changed. A transition whose line cannot be found
    in the role's state diagram is skipped."""
    out = [Block(b.kind, b.role, b.text, b.line, b.info) for b in blocks]
    notes: list[str] = []
    for role, state, target, _cid in items:
        diagram = next((b for b in out if b.kind == "mermaid" and b.role == role), None)
        if diagram is None:
            continue
        pattern = re.compile(
            rf"^(\s*{re.escape(state)}\s*-->\s*{re.escape(target)}\s*:\s*)(.+?)\s*$", re.M
        )
        m = pattern.search(diagram.text)
        if not m or "time_in_state" in m.group(2):
            continue
        label = m.group(2)
        simple = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", label)
        guarded = f"{label} and {GUARD}" if simple else f"({label}) and {GUARD}"
        diagram.text = diagram.text[: m.start(2)] + guarded + diagram.text[m.end(2) :]
        notes.append(
            f"自动修正 ENTRY_TRUE：{role} 的 {state} → {target}（{label}）在进入时就成立，"
            f"加上 {GUARD}，等本状态的动作写入字段后再判断"
        )
    return out, notes
