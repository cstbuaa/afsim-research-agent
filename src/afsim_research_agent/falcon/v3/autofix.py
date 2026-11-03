"""Mechanical repairs of a drafted sheet, applied before the model-driven repair loop.

``ENTRY_TRUE`` (see ``semantics``): a transition whose condition already holds when its
state is entered is the common way an "arrived / done" step is written wrongly (a field
compared before the state's actions have written it, e.g. ``range_to_target_m <= 5`` on a
field that starts at 0). The repair: the transition waits until the state's actions have
run and written their fields (``time_in_state() >= 2 s`` is added to its trigger), so the
condition is judged on real values. This keeps the drafted meaning and works whether or
not the state's tree ever ends.

``completion_events``: when the state's tree ends by itself (following a route, moving to a
point, stopping — nothing in it holds or tracks for ever), a broken "arrived / done"
condition (ENTRY_TRUE, or one reading a field nothing in the role writes, such as
``range_to_target_m <= arrival_tolerance_m`` after ``follow_route``) is replaced by a tree
event: ``<state>_done: {source: tree, result: success}``, fired when the state's tree
succeeds — exactly when its own completion criterion (end of route, stopped) holds.

``fire_orders``: a transition into a firing state that time alone can take (FIRE_BY_TIME)
is triggered by an external fire order instead (``fire_order: {source: external}``) — the
vehicle does not fire unless commanded.

``held_here``: ``movement.hold_position`` given latitude / longitude holds the vehicle at
that point and drives it there first; the scenario gives no points, so drafted coordinates
are invented. Without them it holds where it stopped. The coordinate parameters no longer
used are removed.

``scenario_refs``: a route or position given as a value (a made-up route name or
coordinates) becomes the scenario interface's name when the interface leaves no doubt
which one is meant (one route; one position besides the post). A vehicle already at its
post (the interface has no other position) that was sent to a made-up point stops instead.

The caller keeps the result only when the sheet checks better afterwards.
"""

from __future__ import annotations

import re
from typing import Any

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


def _event_line(model: Block, name: str, state: str, entry: str | None = None) -> bool:
    """Declare ``name`` as an event in the model block (a tree success event of ``state``
    unless ``entry`` gives the declaration; text edit, so the rest of the block keeps its
    form)."""
    if re.search(rf"^\s+{re.escape(name)}\s*:", model.text, re.M):
        return True
    entry = entry or f"{name}: {{source: tree, result: success, description: {state} 的树完成}}"
    lines = model.text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^events\s*:\s*(\{\s*\})?\s*$", line)
        if not m:
            continue
        if m.group(1):
            lines[i] = "events:"
            lines.insert(i + 1, "  " + entry)
        else:
            indent = "  "
            if i + 1 < len(lines):
                nxt = re.match(r"^(\s+)\S", lines[i + 1])
                if nxt:
                    indent = nxt.group(1)
            lines.insert(i + 1, indent + entry)
        model.text = "\n".join(lines) + "\n"
        return True
    if re.search(r"^events\s*:", model.text, re.M):
        return False  # some other form (e.g. a flow mapping): leave it to the repair loop
    model.text = model.text.rstrip("\n") + "\nevents:\n  " + entry + "\n"
    return True


def completion_events(
    blocks: list[Block], items: list[tuple[str, str, str, str]]
) -> tuple[list[Block], list[str]]:
    """The blocks with the trigger of each (role, from, to, condition) transition replaced
    by a tree event of the source state; notes of what changed."""
    out = [Block(b.kind, b.role, b.text, b.line, b.info) for b in blocks]
    notes: list[str] = []
    model = next((b for b in out if b.kind == "model"), None)
    if model is None:
        return out, notes
    taken = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", "\n".join(b.text for b in out)))
    named: dict[tuple[str, str], str] = {}
    for role, state, target, _cid in items:
        diagram = next((b for b in out if b.kind == "mermaid" and b.role == role), None)
        if diagram is None:
            continue
        pattern = re.compile(
            rf"^(\s*{re.escape(state)}\s*-->\s*{re.escape(target)}\s*:\s*)(.+?)\s*$", re.M
        )
        m = pattern.search(diagram.text)
        if not m:
            continue
        name = named.get((role, state))
        if name is None:
            base = name = f"{state.lower()}_done"
            k = 2
            while name in taken:
                name, k = f"{base}_{k}", k + 1
            if not _event_line(model, name, state):
                continue
            taken.add(name)
            named[(role, state)] = name
        label = m.group(2)
        diagram.text = diagram.text[: m.start(2)] + name + diagram.text[m.end(2) :]
        notes.append(
            f"自动修正：{role} 的 {state} → {target}（{label}）写法不能判断到达/完成，"
            f"{state} 的树会自行结束，改用树事件 {name}（{state} 的树成功时触发）"
        )
    return out, notes


FIRE_ORDER = "fire_order"


def fire_orders(
    blocks: list[Block],
    items: list[tuple[str, str, str, str]],
    event: str = FIRE_ORDER,
    description: str = "上级下达开火命令",
) -> tuple[list[Block], list[str]]:
    """The blocks with each (role, from, to, condition) transition into a firing state
    triggered by the run's external fire-order event; notes of what changed."""
    out = [Block(b.kind, b.role, b.text, b.line, b.info) for b in blocks]
    notes: list[str] = []
    model = next((b for b in out if b.kind == "model"), None)
    if model is None:
        return out, notes
    entry = f"{event}: {{source: external, description: {description}}}"
    for role, state, target, _cid in items:
        diagram = next((b for b in out if b.kind == "mermaid" and b.role == role), None)
        if diagram is None:
            continue
        pattern = re.compile(
            rf"^(\s*{re.escape(state)}\s*-->\s*{re.escape(target)}\s*:\s*)(.+?)\s*$", re.M
        )
        m = pattern.search(diagram.text)
        if not m or not _event_line(model, event, state, entry):
            continue
        label = m.group(2)
        diagram.text = diagram.text[: m.start(2)] + event + diagram.text[m.end(2) :]
        notes.append(
            f"自动修正 FIRE_BY_TIME：{role} 的 {state} → {target}（{label}）只靠计时就会开火，"
            f"改由外部开火命令 {event} 触发"
        )
    return out, notes


_HOLD = re.compile(r"movement\.hold_position\(([^()]*)\)")
_COORD = re.compile(r"\s*(latitude_deg|longitude_deg)\s*=\s*([^,()]+?)\s*(?=,|$)")


def held_here(blocks: list[Block]) -> tuple[list[Block], list[str]]:
    """``hold_position`` without coordinates (hold where the vehicle is); coordinate
    parameters left unused are removed from the model block."""
    out = [Block(b.kind, b.role, b.text, b.line, b.info) for b in blocks]
    notes: list[str] = []
    dropped: set[str] = set()

    def strip(m: re.Match[str]) -> str:
        args = [a for a in m.group(1).split(",") if a.strip()]
        keep = []
        for a in args:
            c = _COORD.fullmatch(a)
            if c:
                value = c.group(2).strip()
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
                    dropped.add(value)
            else:
                keep.append(a.strip())
        return "movement.hold_position(" + ", ".join(keep) + ")"

    for b in out:
        if b.kind != "pytree":
            continue
        new = _HOLD.sub(strip, b.text)
        if new != b.text:
            notes.append(
                f"自动修正：{b.role} 的 movement.hold_position 去掉自编的经纬度，改为保持当前位置"
            )
            b.text = new
    if not notes:
        return out, notes
    text = "\n".join(b.text for b in out if b.kind != "model")
    model = next((b for b in out if b.kind == "model"), None)
    for name in sorted(dropped):
        if model is None or re.search(rf"\b{re.escape(name)}\b", text):
            continue
        lines = model.text.splitlines()
        kept = [ln for ln in lines if not re.match(rf"^\s+{re.escape(name)}\s*:", ln)]
        if len(kept) != len(lines):
            model.text = "\n".join(kept) + "\n"
            notes.append(f"删除不再使用的坐标参数 {name}")
    return out, notes


_VALUE = r"(\"[^\"]*\"|'[^']*'|[A-Za-z_][A-Za-z0-9_]*|-?[0-9.]+(?:\s*\*\s*[A-Za-z]+)?)"
_ROUTE_ARG = re.compile(rf"\broute_name\s*=\s*{_VALUE}")
_MOVE = re.compile(r"movement\.move_to_point\(([^()]*)\)")


def _drop_unused(out: list[Block], names: set[str], notes: list[str]) -> None:
    text = "\n".join(b.text for b in out if b.kind != "model")
    model = next((b for b in out if b.kind == "model"), None)
    for name in sorted(names):
        if model is None or re.search(rf"\b{re.escape(name)}\b", text):
            continue
        lines = model.text.splitlines()
        kept = [ln for ln in lines if not re.match(rf"^\s+{re.escape(name)}\s*:", ln)]
        if len(kept) != len(lines):
            model.text = "\n".join(kept) + "\n"
            notes.append(f"删除不再使用的参数 {name}")


def scenario_refs(blocks: list[Block], iface: Any) -> tuple[list[Block], list[str]]:
    """Routes and positions given as values replaced by the scenario interface's names."""
    out = [Block(b.kind, b.role, b.text, b.line, b.info) for b in blocks]
    notes: list[str] = []
    if iface is None:
        return out, notes
    routes = list(iface.routes)
    points = [p for p in iface.points if p != "post"]
    dropped: set[str] = set()
    for b in out:
        if b.kind != "pytree":
            continue
        text = b.text
        if len(routes) == 1:

            def route(m: re.Match[str]) -> str:
                value = m.group(1)
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
                    dropped.add(value)
                return f"route={routes[0]}"

            text = _ROUTE_ARG.sub(route, text)

        def move(m: re.Match[str]) -> str:
            args = [a.strip() for a in m.group(1).split(",") if a.strip()]
            coords = [a for a in args if re.match(r"(latitude_deg|longitude_deg)\s*=", a)]
            if not coords:
                return m.group(0)
            for a in coords:
                value = a.split("=", 1)[1].strip()
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
                    dropped.add(value)
            rest = [a for a in args if a not in coords]
            if len(points) == 1:
                return "movement.move_to_point(" + ", ".join([f"point={points[0]}", *rest]) + ")"
            if not points:
                return "movement.stop()"  # already at the post: stay
            return m.group(0)

        text = _MOVE.sub(move, text)
        if text != b.text:
            b.text = text
            notes.append(
                f"自动修正：{b.role} 中写成具体值的路线 / 位置改用想定接口名"
                + (f"（路线 {routes[0]}）" if len(routes) == 1 else "")
                + (f"（位置 {points[0]}）" if len(points) == 1 else "")
                + ("（已在阵位：移动改为停车）" if not points else "")
            )
    if notes:
        _drop_unused(out, dropped, notes)
    return out, notes
