"""What the vehicles did: effect obligations and motion checks on sampled positions.

A run that only reports which actions started cannot show that a tactic was carried out —
a route action may start without a route, a stopped vehicle may be pushed on by another
controller. The AFSIM run samples every platform's position and speed (``IR_POS``, see
:mod:`afsim_research_agent.ir.v2.afsim.compiler`); this module turns the samples into
tracks and judges:

* the effect obligations (:data:`afsim_research_agent.ir.v2.obligations.SPATIAL`):
  distance kept, standing still, not all moving at once, moving in turns, advancing;
* two checks every run gets: a vehicle keeps moving after its ``movement.stop`` (and no
  movement action started after it), and a movement action starts but the vehicle does
  not move.

Positions are metres east / north of the first platform's start.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from afsim_research_agent.ir.v2.obligations import Obligation, Verdict

POS = re.compile(
    r"^IR_POS\|([^|]+)\|([^|]+)\|([^|]*)\|([^|]+)\|([^|]+)\|([^|]+)\|([^|\r\n]+)", re.M
)
ORIGIN = re.compile(r"^IR_POS_ORIGIN\|([^|]+)\|([^|\r\n]+)", re.M)
MOVING_MPS = 0.5  # faster than this counts as moving
STOP_SETTLE_S = 3.0  # a vehicle may still roll this long after a stop
STOP_TOL_M = 10.0
# actions that should make a vehicle move (NO_MOTION; after a stop they end STOP_DRIFT's window)
MOVE_ACTIONS = frozenset(
    {
        "movement.move_to_point",
        "movement.follow_route",
        "hierarchical_formation.move_to_formation_slot",
    }
)


@dataclass
class Sample:
    t: float
    x: float  # east, m
    y: float  # north, m
    speed: float


@dataclass
class Tracks:
    by_platform: dict[str, list[Sample]] = field(default_factory=dict)
    side: dict[str, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.by_platform)

    def at(self, platform: str, t: float) -> Sample | None:
        """The last sample at or before ``t`` (the first one before the track starts)."""
        track = self.by_platform.get(platform) or []
        best = None
        for s in track:
            if s.t <= t + 1e-6:
                best = s
            else:
                break
        return best or (track[0] if track else None)


def parse(console: str) -> Tracks:
    origin = ORIGIN.search(console)
    lat0 = float(origin.group(1)) if origin else 0.0
    scale = math.cos(math.radians(lat0))
    out = Tracks()
    for t, name, side, north, east, speed, _heading in POS.findall(console):
        try:
            sample = Sample(float(t), float(east) * scale, float(north), float(speed))
        except ValueError:
            continue
        out.by_platform.setdefault(name, []).append(sample)
        out.side[name] = side.strip().lower()
    for track in out.by_platform.values():
        track.sort(key=lambda s: s.t)
    return out


def dist(a: Sample, b: Sample) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def _vehicles(ob: Obligation, own: dict[str, str]) -> list[str]:
    """Own platforms of the obligation's roles (all own platforms when none are named)."""
    return sorted(p for p, r in own.items() if not ob.roles or r in ob.roles)


def enemies(tracks: Tracks, own: dict[str, str]) -> list[str]:
    sides = {tracks.side.get(p) for p in own}
    return sorted(p for p in tracks.by_platform if p not in own and tracks.side.get(p) not in sides)


def judge(
    obligations: list[Obligation],
    tracks: Tracks,
    own: dict[str, str],  # platform → role
    acts: list[tuple[float, str, str]],  # (time, platform, action) starts
    events: list[tuple[float, str]],
    end_s: float | None = None,
) -> list[Verdict]:
    """Verdicts of the effect obligations (others are skipped)."""
    out = []
    for ob in obligations:
        if not ob.spatial:
            continue
        if not tracks:
            out.append(Verdict(ob.id, True, "本次运行没有位置采样，未判定", judged=False))
            continue
        vehicles = [p for p in _vehicles(ob, own) if p in tracks.by_platform]
        if not vehicles:
            out.append(Verdict(ob.id, True, "没有这些角色的车辆", judged=False))
            continue
        fn = {
            "keeps_distance": _keeps_distance,
            "stays_still": _stays_still,
            "not_all_moving": _not_all_moving,
            "moves_in_turn": _moves_in_turn,
            "advances": _advances,
        }[ob.kind]
        out.append(fn(ob, tracks, vehicles, own, acts, events, end_s))
    return out


def _keeps_distance(ob, tracks, vehicles, own, acts, events, end_s) -> Verdict:
    foes = enemies(tracks, own)
    if not foes:
        return Verdict(ob.id, True, "运行中没有敌方车辆", judged=False)
    need = float(ob.min_m or 0.0)
    worst = (math.inf, "", "", 0.0)
    for p in vehicles:
        for s in tracks.by_platform[p]:
            for f in foes:
                e = tracks.at(f, s.t)
                if e is None:
                    continue
                d = dist(s, e)
                if d < worst[0]:
                    worst = (d, p, f, s.t)
    d, p, f, t = worst
    ok = d >= need
    return Verdict(
        ob.id,
        ok,
        f"与敌最近距离 {d:.0f} m（{p} 与 {f}，{t:.1f} s）"
        + ("" if ok else f"，要求不小于 {need:g} m"),
    )


def _stays_still(ob, tracks, vehicles, own, acts, events, end_s) -> Verdict:
    tol = float(ob.tol_m if ob.tol_m is not None else STOP_TOL_M)
    anchors: list[tuple[str, float]] = []
    if ob.on_event:
        raised = [t for t, e in events if e == ob.on_event]
        if not raised:
            return Verdict(ob.id, True, f"事件 {ob.on_event} 没有发生", judged=False)
        anchors = [(p, min(raised)) for p in vehicles]
    else:
        for p in vehicles:
            starts = [t for t, q, a in acts if q == p and a == ob.action]
            if starts:
                anchors.append((p, min(starts)))
        if not anchors:
            return Verdict(ob.id, True, f"{ob.action} 没有执行", judged=False)
    worst = (0.0, "", 0.0)
    for p, t0 in anchors:
        start = t0 + STOP_SETTLE_S
        stop = start + ob.duration_s if ob.duration_s else math.inf
        ref = tracks.at(p, start)
        if ref is None:
            continue
        for s in tracks.by_platform[p]:
            if start <= s.t <= stop:
                d = dist(s, ref)
                if d > worst[0]:
                    worst = (d, p, s.t)
    d, p, t = worst
    ok = d <= tol
    anchor = f"事件 {ob.on_event}" if ob.on_event else ob.action
    return Verdict(
        ob.id,
        ok,
        f"{anchor} 之后最大位移 {d:.0f} m"
        + (f"（{p}，{t:g} s）" if p else "")
        + ("" if ok else f"，要求不超过 {tol:g} m"),
    )


def _moving(tracks: Tracks, p: str, t: float, limit: float) -> bool:
    s = tracks.at(p, t)
    return s is not None and s.speed > limit


def _not_all_moving(ob, tracks, vehicles, own, acts, events, end_s) -> Verdict:
    if len(vehicles) < 2:
        return Verdict(ob.id, True, "只有一辆车", judged=False)
    limit = float(ob.speed_mps if ob.speed_mps is not None else MOVING_MPS)
    times = sorted({s.t for p in vehicles for s in tracks.by_platform[p]})
    moved = any(_moving(tracks, p, t, limit) for p in vehicles for t in times)
    if not moved:
        return Verdict(ob.id, True, "车辆都没有机动", judged=False)
    together = [t for t in times if all(_moving(tracks, p, t, limit) for p in vehicles)]
    ok = not together
    return Verdict(
        ob.id,
        ok,
        f"{len(vehicles)} 辆车"
        + ("从未同时机动" if ok else f"在 {together[0]:.1f}–{together[-1]:.1f} s 间同时机动"),
    )


def bounds(tracks: Tracks, p: str, limit: float, min_m: float) -> list[tuple[float, float, float]]:
    """Movement episodes of a vehicle: (start, end, distance) of each run of samples faster
    than ``limit`` that covered at least ``min_m``."""
    out = []
    track = tracks.by_platform.get(p) or []
    start = None  # (first moving sample's time, the sample before it: where it set off)
    for prev, s in zip(track, track[1:], strict=False):
        if s.speed > limit and start is None:
            start = (s.t, prev)
        if start is not None and (s.speed <= limit or s is track[-1]):
            d = dist(start[1], s)
            if d >= min_m:
                out.append((start[0], s.t, d))
            start = None
    return out


def _moves_in_turn(ob, tracks, vehicles, own, acts, events, end_s) -> Verdict:
    need = int(ob.min_bounds or 2)
    min_m = float(ob.min_m if ob.min_m is not None else 20.0)
    episodes = sorted(
        (b[0], b[1], b[2], p) for p in vehicles for b in bounds(tracks, p, MOVING_MPS, min_m)
    )
    if not episodes:
        return Verdict(ob.id, False, f"没有车辆机动超过 {min_m:g} m")
    order = [p for *_x, p in episodes]
    repeats = [i for i in range(1, len(order)) if order[i] == order[i - 1]]
    # bounds that overlap in time are vehicles moving together, not in turns
    overlaps = [
        (episodes[i - 1][3], episodes[i][3], episodes[i][0])
        for i in range(1, len(episodes))
        if episodes[i][0] < episodes[i - 1][1] - 1e-6 and episodes[i][3] != episodes[i - 1][3]
    ]
    movers = sorted(set(order))
    ok = len(episodes) >= need and len(movers) >= 2 and not repeats and not overlaps
    seq = " → ".join(f"{p}@{t0:.1f}s({d:.0f}m)" for t0, _t1, d, p in episodes[:12])
    why = []
    if overlaps:
        a, b, t = overlaps[0]
        why.append(f"{a} 与 {b} 在 {t:.1f} s 同时机动")
    if len(episodes) < need:
        why.append(f"只有 {len(episodes)} 次机动，要求至少 {need} 次")
    if len(movers) < 2:
        why.append("只有一辆车机动")
    if repeats:
        why.append(f"{order[repeats[0]]} 连续两次机动，没有轮换")
    return Verdict(ob.id, ok, f"机动顺序：{seq}" + ("" if ok else "；" + "；".join(why)))


def _advances(ob, tracks, vehicles, own, acts, events, end_s) -> Verdict:
    foes = enemies(tracks, own)
    if not foes:
        return Verdict(ob.id, True, "运行中没有敌方车辆", judged=False)
    need = float(ob.min_m or 0.0)

    def gap(t: float) -> float:
        ds = []
        for p in vehicles:
            s = tracks.at(p, t)
            if s is not None:
                ds.append(min(dist(s, e) for f in foes if (e := tracks.at(f, t)) is not None))
        return sum(ds) / len(ds) if ds else math.nan

    times = sorted({s.t for p in vehicles for s in tracks.by_platform[p]})
    first, best = gap(times[0]), min(gap(t) for t in times)
    closed = first - best
    ok = closed >= need
    return Verdict(
        ob.id,
        ok,
        f"与敌平均距离由 {first:.0f} m 缩短到 {best:.0f} m（接近 {closed:.0f} m）"
        + ("" if ok else f"，要求至少 {need:g} m"),
    )


def motion_issues(
    tracks: Tracks,
    own: dict[str, str],
    acts: list[tuple[float, str, str]],
    end_s: float | None = None,
) -> list[dict[str, Any]]:
    """Checks every run gets: still moving after a stop; a movement action without motion."""
    out: list[dict[str, Any]] = []
    if not tracks:
        return out
    for p in sorted(own):
        track = tracks.by_platform.get(p) or []
        if not track:
            continue
        mine = sorted((t, a) for t, q, a in acts if q == p)
        for i, (t0, a) in enumerate(mine):
            if a != "movement.stop":
                continue
            later = [t for t, b in mine[i + 1 :] if b in MOVE_ACTIONS]
            until = min(later) if later else math.inf
            ref = tracks.at(p, t0 + STOP_SETTLE_S)
            if ref is None:
                continue
            drift = max(
                (dist(s, ref) for s in track if t0 + STOP_SETTLE_S <= s.t < until), default=0.0
            )
            if drift > STOP_TOL_M:
                out.append({"kind": "STOP_DRIFT", "platform": p, "t": t0, "drift_m": round(drift)})
            break
        for t0, a in mine:
            if a not in MOVE_ACTIONS:
                continue
            window = [s for s in track if t0 <= s.t <= t0 + 15.0]
            if len(window) < 5:
                continue
            moved = max((dist(s, window[0]) for s in window), default=0.0)
            if moved < 5.0:
                out.append({"kind": "NO_MOTION", "platform": p, "t": t0, "action": a})
                break
    return out
