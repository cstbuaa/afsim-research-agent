"""Convert a tactic-set mission (战术集任务状态机) into IR 2.0 without a model.

The tactic set writes missions in the lab's own shape: one state list for every role,
transitions guarded by ``role == X``, triggers as free text, and state trees mapped
explicitly, per role, or by name. This module turns one mission into one IR 2.0 mission
per role, with the tactic-set trees referenced (not copied) and every trigger declared
as a tactic event.
"""

from __future__ import annotations

import re
from typing import Any

from afsim_research_agent.ir.v2.contracts import Contracts, TacticEntry

ROLE_GUARD = re.compile(r"^\s*role\s*==\s*([A-Za-z_][A-Za-z0-9_]*)\s*$")
_OPS = (
    (">=", " gte "),
    ("<=", " lte "),
    ("==", " eq "),
    ("!=", " ne "),
    (">", " gt "),
    ("<", " lt "),
)
DEFAULT_ROLE = "VEHICLE"
_SUFFIX_TYPES = (
    ("_mps", "speed", "m/s"),
    ("_m", "distance", "m"),
    ("_s", "time", "s"),
    ("_deg", "angle", "deg"),
)


class TacticConversionError(ValueError):
    """The tactic-set mission cannot be expressed as an IR 2.0 mission."""


def event_id(text: str) -> str:
    slug = str(text)
    for op, word in _OPS:
        slug = slug.replace(op, word)
    slug = re.sub(r"[^a-z0-9]+", "_", slug.lower()).strip("_")
    if not slug or not slug[0].isalpha():
        slug = f"e_{slug}"
    return slug[:64]


def mission_roles(contracts: Contracts, entry: TacticEntry) -> list[str]:
    """Lab role names that have their own state trees for this mission (or one default)."""
    roles: list[str] = []
    for t in contracts.catalog.tactics.get(entry.slice, {}).get("state_trees") or []:
        role = t.get("role")
        if role and role not in roles:
            roles.append(str(role))
    for t in entry.raw.get("transitions") or []:
        m = ROLE_GUARD.match(str(t.get("when", "")))
        if m and m.group(1) not in roles:
            roles.append(m.group(1))
    return roles or [DEFAULT_ROLE]


def role_mission(
    contracts: Contracts, entry: TacticEntry, role: str | None
) -> tuple[dict[str, Any], dict[str, str], list[dict[str, Any]]]:
    """(mission, state → tree id, events) of one lab role of a tactic-set mission."""
    raw = entry.raw
    roles = mission_roles(contracts, entry)
    if role is None:
        if len(roles) > 1:
            raise TacticConversionError(f"'{entry.id}' has roles {roles}; name one with role")
        role = roles[0]
    if role not in roles:
        raise TacticConversionError(f"'{entry.id}' has no role '{role}' (roles: {roles})")
    states = [str(s) for s in raw.get("states") or []]
    initial = str(raw.get("initial_state"))
    transitions = []
    for t in raw.get("transitions") or []:
        when = str(t.get("when", ""))
        guard = ROLE_GUARD.match(when)
        if guard and guard.group(1) != role:
            continue
        transitions.append({"from": str(t["from"]), "to": str(t["to"]), "on": when})
    globals_ = [
        {"to": str(g["to"]), "on": str(g.get("when", ""))}
        for g in raw.get("global_transitions") or []
    ]
    edges: dict[str, set[str]] = {}
    for t in transitions:
        edges.setdefault(t["from"], set()).add(t["to"])
    reach = {initial}
    todo = [initial]
    while todo:
        cur = todo.pop()
        nxt = set(edges.get(cur, ())) | {g["to"] for g in globals_}
        for s in nxt:
            if s not in reach:
                reach.add(s)
                todo.append(s)
    kept = [s for s in states if s in reach]
    transitions = [t for t in transitions if t["from"] in reach]
    terminal = [s for s in raw.get("terminal_states") or [] if s in reach]
    trees = _state_trees(contracts, entry, role, kept, terminal)
    events: dict[str, dict[str, Any]] = {}
    for t in transitions + globals_:
        text = t["on"]
        eid = event_id(text)
        t["on"] = eid
        events.setdefault(eid, {"id": eid, "source": "tactic", "tactic": entry.id, "text": text})
    mission = {
        "initial_state": initial,
        "states": kept,
        "terminal": terminal,
        "transitions": transitions,
        "global": globals_,
    }
    return mission, trees, list(events.values())


def _state_trees(
    contracts: Contracts, entry: TacticEntry, role: str, states: list[str], terminal: list[str]
) -> dict[str, str]:
    explicit = entry.raw.get("state_trees")
    if isinstance(explicit, dict):
        return {s: str(t) for s, t in explicit.items() if s in states}
    trees = contracts.catalog.tactics.get(entry.slice, {}).get("state_trees") or []
    for t in trees:
        if t.get("role") == role and isinstance(t.get("states"), dict):
            return {s: str(ref) for s, ref in t["states"].items() if s in states}
    by_name: dict[str, str] = {}
    for t in trees:
        if t.get("role") not in (None, role) or "root" not in t:
            continue
        local = str(t["id"]).split(".", 1)[-1]
        by_name[local] = str(t["id"])
    out: dict[str, str] = {}
    for state in states:
        if state in terminal:
            continue
        low = state.lower()
        for name in (f"{role.lower()}_{low}", low):
            if name in by_name:
                out[state] = by_name[name]
                break
    return out


def _parameter(name: str, value: Any, mission_id: str) -> dict[str, Any]:
    for suffix, kind, unit in _SUFFIX_TYPES:
        if name.endswith(suffix) and isinstance(value, int | float) and not isinstance(value, bool):
            return {"type": kind, "value": value, "unit": unit, "description": f"取自 {mission_id}"}
    if isinstance(value, bool):
        kind = "boolean"
    elif isinstance(value, int):
        kind = "integer"
    elif isinstance(value, float):
        kind = "ratio"
    else:
        kind = "string"
    return {"type": kind, "value": value, "description": f"取自 {mission_id}"}


def mission_to_ir(contracts: Contracts, mission_id: str) -> dict[str, Any]:
    """A complete IR 2.0 behavior spec equivalent to one tactic-set mission."""
    entry = contracts.tactics.get(mission_id)
    if entry is None or entry.section != "missions":
        raise TacticConversionError(f"'{mission_id}' is not a tactic-set mission")
    classes = (
        list(contracts.catalog.capabilities.get(entry.slice).platform_classes or ())
        if entry.slice in contracts.catalog.capabilities
        else []
    )
    capability_class = classes[0] if classes else "ground_vehicle"
    roles, missions, state_trees = [], {}, {}
    events: dict[str, dict[str, Any]] = {}
    for lab_role in mission_roles(contracts, entry):
        rid = lab_role.lower()
        mission, trees, evs = role_mission(contracts, entry, lab_role)
        roles.append({"id": rid, "capability_class": capability_class})
        missions[rid] = mission
        state_trees[rid] = trees
        for e in evs:
            events.setdefault(e["id"], e)
    model_id = event_id(mission_id.replace(".", "_"))
    return {
        "schema_version": "2.0",
        "model": {
            "id": model_id,
            "display_name": mission_id,
            "version": "0.1.0",
            "intent": str(entry.raw.get("description") or f"战术集任务 {mission_id}"),
            "authoring_mode": "user_authored",
        },
        "roles": roles,
        "parameters": {
            k: _parameter(k, v, mission_id) for k, v in (entry.raw.get("parameters") or {}).items()
        },
        "events": list(events.values()),
        "state_trees": state_trees,
        "missions": missions,
    }
