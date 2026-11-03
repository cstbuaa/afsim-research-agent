"""Mechanical repairs of a drafted sheet, applied before the model-driven repair loop.

``ENTRY_TRUE`` (see ``semantics``): a transition whose condition already holds when its
state is entered is the common way an "arrived / done" step is written wrongly (a field
compared before the state's actions have written it). The repair is always the same: the
transition fires on the state's own tree event (``{source: tree, result: success}``, the
tree has done its work), so it is applied here without a model call. The caller keeps
the result only when the sheet checks better afterwards (e.g. not when the state's tree
never ends, which the compiler reports for tree events).
"""

from __future__ import annotations

import re

import yaml

from afsim_research_agent.falcon.sheet.blocks import Block


def _event_name(state: str, taken: set[str]) -> str:
    base = f"{state.lower()}_done"
    name, n = base, 2
    while name in taken:
        name, n = f"{base}_{n}", n + 1
    return name


def entry_events(
    blocks: list[Block], items: list[tuple[str, str, str, str]]
) -> tuple[list[Block], list[str]]:
    """The blocks with each (role, from, to, condition) transition switched to a tree
    event of ``from``; notes of what changed. Blocks that cannot be edited are left as
    they are (the item is skipped)."""
    out = [Block(b.kind, b.role, b.text, b.line, b.info) for b in blocks]
    model = next((b for b in out if b.kind == "model"), None)
    if model is None or not items:
        return out, []
    try:
        doc = yaml.safe_load(model.text) or {}
    except yaml.YAMLError:
        return out, []
    if not isinstance(doc, dict):
        return out, []
    events = doc.get("events")
    if not isinstance(events, dict):
        events = {}
    notes: list[str] = []
    replaced: set[str] = set()
    for role, state, target, cid in items:
        diagram = next((b for b in out if b.kind == "mermaid" and b.role == role), None)
        if diagram is None:
            continue
        pattern = re.compile(
            rf"^(\s*{re.escape(state)}\s*-->\s*{re.escape(target)}\s*:\s*){re.escape(cid)}\s*$",
            re.M,
        )
        if not pattern.search(diagram.text):
            continue
        event = next(
            (
                k
                for k, v in events.items()
                if isinstance(v, dict)
                and v.get("source") == "tree"
                and k.startswith(f"{state.lower()}_done")
            ),
            None,
        ) or _event_name(state, set(events))
        events.setdefault(
            event, {"source": "tree", "result": "success", "description": f"{state} 的工作完成"}
        )
        diagram.text = pattern.sub(lambda m: m.group(1) + event, diagram.text)
        replaced.add(cid)
        notes.append(
            f"自动修正 ENTRY_TRUE：{role} 的 {state} → {target} 原由条件 {cid} 触发（进入即成立），"
            f"改为本状态的树完成事件 {event}"
        )
    if not notes:
        return [Block(b.kind, b.role, b.text, b.line, b.info) for b in blocks], []
    doc["events"] = events
    model.text = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=100)
    # a condition no transition uses any more is dropped from the conditions block
    cond = next((b for b in out if b.kind == "conditions"), None)
    if cond is not None:
        others = "\n".join(b.text for b in out if b is not cond)
        for cid in replaced:
            if not re.search(rf"\b{re.escape(cid)}\b", others):
                cond.text = re.sub(rf"^\s*{re.escape(cid)}\s*=.*\n?", "", cond.text, flags=re.M)
    return out, notes
