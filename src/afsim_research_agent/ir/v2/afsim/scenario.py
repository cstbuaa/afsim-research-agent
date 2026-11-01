"""A minimal AFSIM run for a model that has no handwritten one (e.g. a tactic from Crow).

A handwritten run configuration (see :mod:`.binding`) fits one tactic: its platforms,
auxiliary data and checks are written for that tactic's roles. A model drafted from a
newly selected tactic has none, and borrowing another tactic's run judges it against the
wrong roles and data. This module lays out a run from the model itself:

* one platform per role instance, in a line abreast at the kit's origin, facing the
  enemy; roles are told apart as *leader* (a single instance, or an id naming a leader)
  and *member*;
* the platform body is the kit's own text plus, for every capability slice the model
  uses, the slice's auxiliary data and lines for leaders / members (``{name}``,
  ``{leader}``, ``{index}``, ``{role}`` are filled in);
* stationary enemies at the kit's range; the model's external events at fixed times
  (``cancelled`` near the end);
* checks: the run's generic rules and the model's own obligations (judged by the run
  anyway); when a role's trees fire a weapon, at least one shot by that role.

The kit is the platform project's file and stays next to it (it names native platform
types, auxiliary-data keys and observer files); this module only fills it in::

    [meta]
    start_time = "18:00:00"
    end_time_s = 80
    random_seed = 1
    origin = [45.0, -121.0]          # latitude, longitude (degrees) of the first platform
    heading_deg = 90.0               # own platforms face this way; the enemy is ahead
    spacing_m = 100.0                # between own platforms (abreast)
    includes = ["<observer>", ...]
    preamble = "<raw input>"
    body = "<lines every own platform gets>"     # {name} {role} {leader} {index}
    ahead_m = 400.0                  # a point this far ahead: {ahead_lat} {ahead_lon} (DMS),
                                     # {ahead_lat_deg} {ahead_lon_deg}; the origin is
                                     # {origin_lat} {origin_lon} (every template)
    [enemy]
    count = 2
    range_m = 650.0
    spacing_m = 200.0
    platform = "platform {name} <type> side red position {lat} {lon} ... end_platform"
    [events]
    cancelled = -20                  # seconds; negative: before the end of the run
    [expect]
    fire_event = "WEAPON_FIRED"      # or WEAPON_HIT
    fire_actions = ["<action id>"]   # actions that fire a weapon
    [slices.<slice id>]              # added when the model uses the slice
    includes = ["<file>"]
    preamble = "<raw input before the platforms>"   # e.g. a processor type the bodies add
    aux = "<aux fields for every platform>"
    leader_aux = "<aux fields>"
    member_aux = "<aux fields>"      # or a list: member n takes item (n - 1) mod length
    leader_body = "<lines>"
    member_body = "<lines>"
    per_platform = "<raw input per own platform, after the platforms>"
    extra = "<raw input after the platforms>"
    parameters = {<model parameter> = "<value>"}    # supplied by the run
"""

from __future__ import annotations

import math
import re
import tomllib
from pathlib import Path
from typing import Any

from afsim_research_agent.ir.v2.afsim.binding import RunConfig

_LEADER = re.compile(r"lead|command|hq|chief")
_REF = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")


def load_kit(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8-sig"))


def _refs(node: Any, out: set[str]) -> None:
    """Capability references (``slice.name``) anywhere in a role or subtree."""
    if isinstance(node, dict):
        for value in node.values():
            _refs(value, out)
    elif isinstance(node, list):
        for value in node:
            _refs(value, out)
    elif isinstance(node, str) and _REF.match(node):
        out.add(node)


def model_refs(model: dict[str, Any]) -> dict[str, set[str]]:
    """Role → capability references its states use (subtrees of the model included)."""
    shared: set[str] = set()
    _refs(model.get("subtrees") or {}, shared)
    out: dict[str, set[str]] = {}
    for rid, role in (model.get("roles") or {}).items():
        refs: set[str] = set()
        _refs(role, refs)
        out[rid] = refs | shared
    return out


def role_kinds(model: dict[str, Any]) -> dict[str, str]:
    """Role → ``leader`` / ``member``."""
    roles = model.get("roles") or {}
    counts = {rid: int((r.get("role") or {}).get("count", 1)) for rid, r in roles.items()}
    named = {rid for rid in roles if _LEADER.search(rid)}
    out = {}
    for rid in roles:
        if named:
            out[rid] = "leader" if rid in named else "member"
        else:
            single = counts[rid] == 1 and any(c > 1 for c in counts.values())
            out[rid] = "leader" if single or len(roles) == 1 else "member"
    return out


def _offset(lat: float, lon: float, north_m: float, east_m: float) -> tuple[float, float]:
    dlat = north_m / 111_320.0
    dlon = east_m / (111_320.0 * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


def _pos(lat: float, lon: float) -> tuple[str, str]:
    def dms(value: float, pos: str, neg: str) -> str:
        hemi = pos if value >= 0 else neg
        value = abs(value)
        d = int(value)
        m = int((value - d) * 60)
        s = (value - d - m / 60) * 3600
        return f"{d}:{m:02d}:{s:05.2f}{hemi}"

    return dms(lat, "n", "s"), dms(lon, "e", "w")


def _fill(text: str, values: dict[str, Any]) -> str:
    out = text or ""
    for key, value in values.items():
        out = out.replace("{" + key + "}", str(value))
    return out


def generate(
    model: dict[str, Any],
    kit: dict[str, Any],
    contracts: Any,
    *,
    run_id: str | None = None,
) -> RunConfig:
    meta = kit.get("meta") or {}
    slices_kit = kit.get("slices") or {}
    refs = model_refs(model)

    def slice_of(ref: str) -> str | None:
        action = contracts.actions.get(ref)
        if action is not None:
            return action.slice
        tactic = contracts.tactics.get(ref)
        return getattr(tactic, "slice", None) or ref.split(".")[0]

    role_slices = {rid: {s for s in map(slice_of, r) if s} for rid, r in refs.items()}
    used = set().union(*role_slices.values()) if role_slices else set()
    kinds = role_kinds(model)

    lat0, lon0 = (float(x) for x in meta.get("origin", [45.0, -121.0]))
    heading = float(meta.get("heading_deg", 90.0))
    spacing = float(meta.get("spacing_m", 100.0))
    ahead = math.radians(heading)
    across = math.radians(heading + 90.0)
    enemy = kit.get("enemy") or {}
    rng = float(enemy.get("range_m", 650.0))
    ahead_m = float(meta.get("ahead_m", rng / 2))
    a_lat, a_lon = _offset(lat0, lon0, ahead_m * math.cos(ahead), ahead_m * math.sin(ahead))
    common = {
        "origin_lat": _pos(lat0, lon0)[0],
        "origin_lon": _pos(lat0, lon0)[1],
        "ahead_lat": _pos(a_lat, a_lon)[0],
        "ahead_lon": _pos(a_lat, a_lon)[1],
        "ahead_lat_deg": f"{a_lat:.7f}",
        "ahead_lon_deg": f"{a_lon:.7f}",
    }

    def pick(value: Any, index: int) -> str:
        if isinstance(value, list):
            return str(value[(index - 1) % len(value)]) if value else ""
        return str(value or "")

    # instances in order: leaders first, then members, role by role
    order = sorted(model.get("roles") or {}, key=lambda r: (kinds[r] != "leader", r))
    instances: list[tuple[str, str, int]] = []  # (name, role, instance)
    for rid in order:
        count = int((model["roles"][rid].get("role") or {}).get("count", 1))
        for i in range(count):
            instances.append((f"blue_{rid}_{i + 1}", rid, i))
    leader = next((n for n, r, _ in instances if kinds[r] == "leader"), instances[0][0])

    # abreast across the heading, centred on the origin
    platforms, per_platform = [], []
    member_index = 0
    for k, (name, rid, inst) in enumerate(instances):
        off = (k - (len(instances) - 1) / 2) * spacing
        lat, lon = _offset(lat0, lon0, off * math.cos(across), off * math.sin(across))
        kind = kinds[rid]
        if kind == "member":
            member_index += 1
        values = {
            **common,
            "name": name,
            "role": rid,
            "leader": leader,
            "index": member_index if kind == "member" else 0,
            "lat": _pos(lat, lon)[0],
            "lon": _pos(lat, lon)[1],
        }
        aux: list[str] = []
        body: list[str] = [_fill(meta.get("body", ""), values)]
        for sid in sorted(used):
            sk = slices_kit.get(sid) or {}
            n = values["index"]
            aux += [
                _fill(sk.get("aux", ""), values),
                _fill(pick(sk.get(f"{kind}_aux"), n), values),
            ]
            body.append(_fill(pick(sk.get(f"{kind}_body"), n), values))
            per_platform.append(_fill(sk.get("per_platform", ""), values))
        aux = [a for a in aux if a.strip()]
        lines = [
            "side blue",
            f"position {values['lat']} {values['lon']}",
            f"heading {heading:g} deg",
            *(["aux_data", *aux, "end_aux_data"] if aux else []),
            *[b for b in body if b.strip()],
        ]
        platforms.append({"name": name, "role": rid, "instance": inst, "body": "\n".join(lines)})

    gap = float(enemy.get("spacing_m", 200.0))
    n_enemy = int(enemy.get("count", 2))
    enemies, enemy_names = [], []
    for i in range(n_enemy):
        side_off = (i - (n_enemy - 1) / 2) * gap
        north = rng * math.cos(ahead) + side_off * math.cos(across)
        east = rng * math.sin(ahead) + side_off * math.sin(across)
        lat, lon = _pos(*_offset(lat0, lon0, north, east))
        name = f"red_{i + 1}"
        enemy_names.append(name)
        enemies.append(_fill(enemy.get("platform", ""), {"name": name, "lat": lat, "lon": lon}))

    end_s = float(meta.get("end_time_s", 80))
    events = []
    model_events = model.get("events") or {}
    for ev, at in (kit.get("events") or {}).items():
        if ev in model_events:
            t = float(at)
            events.append({"at_s": end_s + t if t < 0 else t, "event": ev})

    includes = list(meta.get("includes") or [])
    preamble = [_fill(meta.get("preamble", ""), common)]
    parameters: dict[str, Any] = {}
    shared = {**common, "leader": leader}
    extra = [*enemies, *(p for p in per_platform if p.strip())]
    for sid in sorted(used):
        sk = slices_kit.get(sid) or {}
        includes += [i for i in sk.get("includes") or [] if i not in includes]
        if (sk.get("preamble") or "").strip():
            preamble.append(_fill(sk["preamble"], shared))
        if (sk.get("extra") or "").strip():
            extra.append(_fill(sk["extra"], shared))
        for key, value in (sk.get("parameters") or {}).items():
            # only parameters the model declares (a run may not add new ones)
            if key in (model.get("parameters") or {}):
                parameters[key] = _fill(value, shared) if isinstance(value, str) else value

    expect: dict[str, Any] = {"events": []}
    exp = kit.get("expect") or {}
    fire = set(exp.get("fire_actions") or [])
    for rid in order:
        if refs.get(rid, set()) & fire:
            expect["events"].append(
                {
                    "kind": exp.get("fire_event", "WEAPON_FIRED"),
                    "by": rid,
                    "min": 1,
                    "note": f"{rid} 角色至少开火一次",
                }
            )
    return RunConfig.model_validate(
        {
            "id": run_id or f"generated_{model['model']['id']}",
            "start_time": meta.get("start_time", "00:00:00"),
            "end_time_s": end_s,
            "random_seed": int(meta.get("random_seed", 1)),
            "includes": includes,
            "preamble": "\n".join(x for x in preamble if x.strip()),
            "extra": "\n".join(extra),
            "platforms": platforms,
            "events": events,
            "parameters": parameters,
            "expect": expect,
        }
    )
