"""Replay JSON (format ``replay/1``) from an AFSIM recording or a 2D simulation result.

One format for both sources, so the browser has one player::

    {"format": "replay/1", "source": "afsim" | "2d", "title": str,
     "duration_s": float, "step_s": float,
     "frame": {"units": "m", "x": "east", "y": "north", "origin": [lat, lon] | null},
     "platforms": [{"id", "label", "side", "icon"}],
     "samples": {id: [[t, x, y, speed, heading_deg], ...]},     # every step_s
     "states": {id: [[t, state], ...]},
     "actions": [[t, id, "start" | "done" | "fail", action, code?], ...],
     "events": [[t, kind, id, other?], ...],  # detect lost fire hit miss killed <event>
     "routes": [{"id", "platform"?, "points": [[x, y], ...]}],
     "points": [{"id", "x", "y"}],
     "sensors": {id: {"range_m": float}},
     "verdicts": [{"id", "passed", "detail"}]}

AFSIM positions come from the recording's entity states, dead-reckoned with their velocity
between updates (as Mystic does); states come from the ``ir_state`` auxiliary data the
compiled behaviour writes on every state entry, or from the run console (``IR_STATE``) of
older runs; action starts and results from the console (``IR_ACT`` / ``IR_DONE`` /
``IR_FAIL``) when it is given.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from afsim_research_agent.replay.aer import Recording
from afsim_research_agent.replay.geo import Local, ecef_to_geodetic

FORMAT = "replay/1"
_STATE = re.compile(r"^IR_STATE\|([^|]+)\|([^|]+)\|([^\r\n|]+)", re.M)
_ACT = re.compile(r"^IR_(ACT|DONE|FAIL)\|([^|]+)\|([^|]+)\|([^\r\n|]+)(?:\|([^\r\n]+))?", re.M)
_KIND = {"ACT": "start", "DONE": "done", "FAIL": "fail"}


def _heading(vx: float, vy: float) -> float:
    """Compass degrees (0 north, 90 east) of an east/north vector."""
    return math.degrees(math.atan2(vx, vy)) % 360.0


def _r(v: float, n: int = 2) -> float:
    return round(float(v), n)


def from_aer(
    recording: Recording | Path | str,
    *,
    console: str | None = None,
    labels: dict[str, str] | None = None,
    step_s: float = 1.0,
    sensor_range_m: float | None = None,
    title: str = "",
) -> dict[str, Any]:
    rec = recording if isinstance(recording, Recording) else Recording.load(recording)
    info: dict[int, dict[str, Any]] = {}
    track: dict[int, list[tuple[float, tuple, tuple | None]]] = {}
    removed: dict[int, float] = {}
    route_heading: dict[int, float] = {}
    routes: list[tuple[float, int, list[tuple[float, float]]]] = []
    aux_states: dict[int, list[list[Any]]] = {}
    events: list[list[Any]] = []
    end = 0.0
    for name, msg in rec:
        t = float(msg.get("simTime") or 0.0)
        end = max(end, t)
        if name == "MsgPlatformInfo":
            info.setdefault(msg["platformIndex"], {}).update(
                {k: msg[k] for k in ("name", "side", "icon") if k in msg}
            )
        elif name == "MsgEntityState":
            s = msg["state"]
            loc = s["locationWCS"]
            vel = s.get("velocityWCS")
            track.setdefault(s["platformIndex"], []).append(
                (
                    t,
                    (loc["x"], loc["y"], loc["z"]),
                    (vel["x"], vel["y"], vel["z"]) if vel else None,
                )
            )
        elif name == "MsgPlatformStatus":
            if msg.get("broken"):
                events.append([t, "killed", msg["platformIndex"]])
            if msg.get("removed"):
                removed.setdefault(msg["platformIndex"], t)
        elif name == "MsgRouteChanged":
            pts = [
                (w["locationX"], w["locationY"])
                for w in msg.get("route") or []
                if w.get("locationType", "").startswith("Lati")
            ]
            routes.append((t, msg["platformIndex"], pts))
            route_heading.setdefault(msg["platformIndex"], math.degrees(msg.get("heading", 0.0)))
        elif name == "MsgAuxData":
            for a in msg.get("auxData") or []:
                if a.get("name") == "ir_state" and a.get("text"):
                    seq = aux_states.setdefault(msg["platformIndex"], [])
                    if not seq or seq[-1][1] != a["text"]:
                        seq.append([_r(t), a["text"]])
        elif name == "MsgSensorTrackCreated":
            events.append([t, "detect", msg["ownerIndex"], msg.get("targetIndex")])
        elif name == "MsgSensorDetectionChange" and not msg.get("detected", True):
            events.append([t, "lost", msg["ownerIndex"], msg.get("targetIndex")])
        elif name == "MsgWeaponFired":
            events.append(
                [t, "fire", msg.get("firingPlatformIndex"), msg.get("targetPlatformIndex")]
            )
        elif name == "MsgWeaponTerminated":
            result = str(msg.get("geometryResult") or msg.get("extendedResult") or "")
            kind = "hit" if "hit" in result.lower() or "kill" in result.lower() else "miss"
            events.append([t, kind, msg.get("firingPlatformIndex"), msg.get("targetPlatformIndex")])

    # ids and the local frame: east/north metres about the first own (blue) start
    names = {i: str(v.get("name") or f"p{i}") for i, v in info.items()}
    for i in track:
        names.setdefault(i, f"p{i}")
    blue = [i for i in sorted(track) if str(info.get(i, {}).get("side", "")).lower() == "blue"]
    first = (blue or sorted(track) or [None])[0]
    if first is None:
        raise ValueError("the recording has no entity states")
    lat0, lon0, _ = ecef_to_geodetic(*track[first][0][1])
    frame = Local(lat0, lon0)
    labels = labels or {}
    pid = {i: labels.get(n, n) for i, n in names.items()}

    samples: dict[str, list[list[float]]] = {}
    steps = int(end // step_s) + 1
    for i, seq in track.items():
        seq.sort(key=lambda r: r[0])
        out, k, heading = [], 0, route_heading.get(i, 0.0) % 360.0
        stop = removed.get(i, end)
        for n in range(steps):
            t = n * step_s
            if t < seq[0][0] - 1e-6 or t > stop + 1e-6:
                continue
            while k + 1 < len(seq) and seq[k + 1][0] <= t + 1e-6:
                k += 1
            t0, loc, vel = seq[k]
            x, y = frame.from_ecef(*loc)
            speed = 0.0
            if vel is not None:
                ve, vn = frame.vec(*vel)
                dt = t - t0
                x, y = x + ve * dt, y + vn * dt
                speed = math.hypot(ve, vn)
                if speed > 0.2:
                    heading = _heading(ve, vn)
            out.append([_r(t), _r(x), _r(y), _r(speed), _r(heading, 1)])
        samples[pid[i]] = out

    states: dict[str, list[list[Any]]] = {pid[i]: v for i, v in aux_states.items()}
    actions: list[list[Any]] = []
    if console:
        by_name = {n: pid[i] for i, n in names.items()}
        if not states:
            for t, plat, state in _STATE.findall(console):
                states.setdefault(by_name.get(plat, plat), []).append([_r(float(t)), state])
        for kind, t, plat, action, code in _ACT.findall(console):
            row = [_r(float(t)), by_name.get(plat, plat), _KIND[kind], action]
            if code:
                row.append(code.strip())
            actions.append(row)

    def who(i: Any) -> Any:
        return pid.get(i, i) if isinstance(i, int) else i

    return {
        "format": FORMAT,
        "source": "afsim",
        "title": title,
        "duration_s": _r(end),
        "step_s": step_s,
        "frame": {"units": "m", "x": "east", "y": "north", "origin": [lat0, lon0]},
        "platforms": [
            {
                "id": pid[i],
                "label": pid[i],
                "side": str(info.get(i, {}).get("side") or "unknown").lower(),
                "icon": info.get(i, {}).get("icon") or "",
            }
            for i in sorted(track)
        ],
        "samples": samples,
        "states": states,
        "actions": actions,
        "events": [[_r(e[0]), e[1], *[who(x) for x in e[2:] if x is not None]] for e in events],
        "routes": [
            {
                "id": f"{pid.get(i, i)}@{_r(t)}",
                "platform": pid.get(i, i),
                "points": [[_r(v) for v in frame.from_geodetic(la, lo)] for la, lo in pts],
            }
            for t, i, pts in routes
            if pts
        ],
        "points": [],
        "sensors": {pid[i]: {"range_m": sensor_range_m} for i in blue} if sensor_range_m else {},
        "verdicts": [],
    }


def labels_from_run(platforms: list[Any]) -> dict[str, str]:
    """Run platforms (``RunPlatform`` or dicts with name, role, instance) → ``role #n``."""
    out = {}
    for p in platforms:
        get = p.get if isinstance(p, dict) else lambda k, p=p: getattr(p, k, None)
        if get("name") and get("role"):
            out[str(get("name"))] = f"{get('role')} #{int(get('instance') or 0) + 1}"
    return out


def from_sim(result: Any, binding: Any = None, *, title: str = "") -> dict[str, Any]:
    """A 2D simulation result (``ir.v2.sim.SimResult``; positions in ``tracks``)."""
    tracks = getattr(result, "tracks", None)
    by_platform = dict(getattr(tracks, "by_platform", {}) or {})
    sides = dict(getattr(tracks, "side", {}) or {})
    samples: dict[str, list[list[float]]] = {}
    for name, seq in by_platform.items():
        out, heading = [], 90.0
        for a, b in zip(seq, [*seq[1:], None], strict=True):
            if b is not None and math.hypot(b.x - a.x, b.y - a.y) > 1e-3:
                heading = _heading(b.x - a.x, b.y - a.y)
            out.append([_r(a.t), _r(a.x), _r(a.y), _r(a.speed), _r(heading, 1)])
        samples[name] = out
    states = {k: [[_r(t), s] for t, s in v] for k, v in (result.states or {}).items()}
    platforms = sorted(set(samples) | set(states))
    routes, points = [], []
    if binding is not None:
        routes = [
            {"id": rid, "points": [[_r(x), _r(y)] for x, y in r.plane]}
            for rid, r in (binding.routes or {}).items()
        ]
        points = [
            {"id": pid, "x": _r(x), "y": _r(y)}
            for pid, (x, y) in (binding.points_plane or {}).items()
        ]
    return {
        "format": FORMAT,
        "source": "2d",
        "title": title or str(getattr(result, "scenario", "")),
        "duration_s": _r(getattr(result, "duration_s", 0.0) or 0.0),
        "step_s": float(getattr(result, "step_s", 1.0) or 1.0),
        "frame": {"units": "m", "x": "east", "y": "north", "origin": None},
        "platforms": [
            {
                "id": p,
                "label": p,
                "side": sides.get(p, "blue" if p in states else "red"),
                "icon": "",
            }
            for p in platforms
        ],
        "samples": samples,
        "states": states,
        "actions": [[_r(t), inst, "start", action] for t, inst, action in result.activations],
        "events": [[_r(t), ev, ""] for t, ev in result.events],
        "routes": routes,
        "points": points,
        "sensors": {},
        "verdicts": [
            {"id": c.id, "passed": bool(c.passed), "detail": str(c.detail)}
            for c in getattr(result, "checks", []) or []
        ],
    }
