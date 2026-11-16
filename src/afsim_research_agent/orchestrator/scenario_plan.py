"""Scenario variants that trigger a model's branches, from a few questions to the user.

The baseline scenario (the kit) only runs a model's main line: search, contact, track,
shut down at the end. The branches a tactic is about (the target lost and reacquired, the
reacquire timeout, withdrawing when the enemy closes, an order from above) need a scenario
that makes them happen. This module

1. finds the branches in the normalized model (``branches``): transitions on a condition
   that reads "target not valid" (lost), on a range below a threshold (close), on a timer
   out of the lost state (timeout) or out of the search (search limit), and on external
   events other than ``cancelled``;
2. writes the questions a person answers before the variants are generated
   (``questions``): which branches, when after the tracking starts, how long the target
   stays lost, how fast the enemy closes, when an order is sent — each with options and a
   recommended one (taken when nobody answers);
3. turns the answers into scenario overrides, one variant per branch case, each with the
   state sequence the leader should visit (``variants``).

Times are anchored on the first entry of the tracking state: from a baseline AFSIM run when
one is given (``phase_times``), else estimated from the scenario (the time to drive into
radar range). Questions and answers carry only relative quantities (seconds, metres,
m/s); positions are laid out by the scenario generator.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

VALID = "selected_target.valid"
RANGE = "selected_target.range_m"


@dataclass
class Branch:
    id: str
    kind: str  # lost_reacquire | lost_timeout | close_withdraw | order | search_limit
    role: str
    path: list[str]  # leader states the variant should visit, in order
    text: str
    track_state: str | None = None
    timeout_s: float | None = None
    threshold_m: float | None = None
    event: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _expr_text(expr: Any) -> str:
    return json.dumps(expr, ensure_ascii=False, sort_keys=True)


def _negated_valid(expr: Any) -> bool:
    """The expression is true when the target is not valid (``not`` over the valid field)."""
    if isinstance(expr, dict):
        for k, v in expr.items():
            if k == "not" and VALID in _expr_text(v):
                return True
            if k in ("all", "and") and isinstance(v, list):
                if any(_negated_valid(x) for x in v):
                    return True
            elif isinstance(v, (dict, list)) and k not in ("any", "or") and _negated_valid(v):
                return True
    elif isinstance(expr, list):
        return any(_negated_valid(x) for x in expr)
    return False


def _number(value: Any) -> float | None:
    if isinstance(value, dict) and "value" in value:  # {value, unit}
        n = _number(value["value"])
        return None if n is None else n * (1000.0 if value.get("unit") == "km" else 1.0)
    if isinstance(value, (int, float)):
        return float(value)
    m = re.match(r"\s*(-?\d+(?:\.\d+)?)\s*(km|m)?\b", str(value or ""))
    if not m:
        return None
    return float(m.group(1)) * (1000.0 if m.group(2) == "km" else 1.0)


def _close_threshold(expr: Any, params: dict[str, Any]) -> float | None:
    """The range below which the expression is true (``range_m < x``), or None."""
    if isinstance(expr, dict):
        cmp_ = expr.get("compare")
        if isinstance(cmp_, dict) and RANGE in _expr_text(cmp_.get("left")):
            if str(cmp_.get("operator")) in ("lt", "lte", "<", "<="):
                right = cmp_.get("right")
                if isinstance(right, dict) and "parameter_ref" in right:
                    p = params.get(right["parameter_ref"]) or {}
                    return _number(p.get("value") if isinstance(p, dict) else p)
                return _number(right)
        for v in expr.values():
            got = _close_threshold(v, params)
            if got is not None and got > 0:
                return got
    elif isinstance(expr, list):
        for v in expr:
            got = _close_threshold(v, params)
            if got is not None and got > 0:
                return got
    return None


def _detects(expr: Any) -> bool:
    return VALID in _expr_text(expr) and not _negated_valid(expr)


def branches(model: dict[str, Any]) -> list[Branch]:
    """The branches of the model's leader (or single) role a scenario has to provoke."""
    from afsim_research_agent.ir.v2.afsim.scenario import role_kinds

    kinds = role_kinds(model)
    roles = model.get("roles") or {}
    lead = next((r for r, k in kinds.items() if k == "leader"), next(iter(roles), None))
    if lead is None:
        return []
    mission = roles[lead].get("mission") or {}
    trans = [t for t in mission.get("transitions") or [] if "from" in t]
    glob = mission.get("global") or []
    conds = model.get("conditions") or {}
    events = model.get("events") or {}
    params = model.get("parameters") or {}
    out: list[Branch] = []

    def exits(state: str) -> list[dict[str, Any]]:
        return [t for t in trans if t["from"] == state]

    def is_lost(on: str) -> bool:
        return on in conds and _negated_valid(conds[on])

    def is_detect(on: str) -> bool:
        return on in conds and _detects(conds[on])

    def timer(on: str) -> float | None:
        ev = events.get(on) or {}
        return float(ev["after_s"]) if ev.get("source") == "timer" and ev.get("after_s") else None

    holds = {t["to"] for t in trans if is_lost(str(t.get("on")))}
    for t in trans:
        on = str(t.get("on"))
        if is_lost(on):
            track, hold = t["from"], t["to"]
            back = next((x for x in exits(hold) if is_detect(str(x.get("on")))), None)
            tout = next((x for x in exits(hold) if timer(str(x.get("on")))), None)
            if back is not None:
                out.append(
                    Branch(
                        f"{lead}_lost_reacquire",
                        "lost_reacquire",
                        lead,
                        [track, hold, back["to"]],
                        f"跟踪中目标丢失，在时限内重新出现：{track} → {hold} → {back['to']}",
                        track_state=track,
                        timeout_s=timer(str(tout.get("on"))) if tout else None,
                    )
                )
            if tout is not None:
                out.append(
                    Branch(
                        f"{lead}_lost_timeout",
                        "lost_timeout",
                        lead,
                        [track, hold, tout["to"]],
                        f"目标丢失超过时限（{timer(str(tout['on'])):g} s）：{track} → {hold} → {tout['to']}",
                        track_state=track,
                        timeout_s=timer(str(tout["on"])),
                    )
                )
            if back is None and tout is None and hold:
                out.append(
                    Branch(
                        f"{lead}_lost",
                        "lost_end",
                        lead,
                        [track, hold],
                        f"跟踪中目标丢失：{track} → {hold}",
                        track_state=track,
                    )
                )
        elif on in conds and (thr := _close_threshold(conds[on], params)) is not None:
            out.append(
                Branch(
                    f"{lead}_close",
                    "close_withdraw",
                    lead,
                    [t["from"], t["to"]],
                    f"敌方逼近到 {thr:g} m 以内：{t['from']} → {t['to']}",
                    track_state=t["from"],
                    threshold_m=thr,
                )
            )
        elif (
            timer(on)
            and t["from"] not in holds
            and any(is_detect(str(x.get("on"))) for x in exits(t["from"]))
        ):
            out.append(
                Branch(
                    f"{lead}_search_limit",
                    "search_limit",
                    lead,
                    [t["from"], t["to"]],
                    f"限时搜索（{timer(on):g} s）内没有发现：{t['from']} → {t['to']}",
                    timeout_s=timer(on),
                )
            )
    seen: set[str] = set()
    for t in [*trans, *glob]:
        on = str(t.get("on"))
        ev = events.get(on) or {}
        if ev.get("source") != "external" or on == "cancelled" or on in seen:
            continue
        seen.add(on)
        start = t.get("from")
        out.append(
            Branch(
                f"{lead}_order_{on}",
                "order",
                lead,
                [start, t["to"]] if start else [t["to"]],
                f"收到外部事件 {on}" + (f"：{start} → {t['to']}" if start else f" → {t['to']}"),
                event=on,
            )
        )
    uniq: dict[str, Branch] = {}
    for b in out:
        uniq.setdefault(b.id, b)
    return list(uniq.values())


# ============================================================================ questions
def _opt(label: str, value: Any, recommended: bool = False) -> dict[str, Any]:
    return {"label": label + ("（推荐）" if recommended else ""), "value": value}


def questions(found: list[Branch], baseline: dict[str, Any]) -> list[dict[str, Any]]:
    """What to ask before generating the variants. ``baseline``: the scenario as it is
    (``enemy_range_m``, ``sensor_range_m``, ``end_s``)."""
    qs: list[dict[str, Any]] = []
    if not found:
        return qs
    qs.append(
        {
            "id": "branches",
            "question": "要在 AFSIM 中验证哪些战术分支？（每个分支生成一个想定变体）",
            "multi": True,
            "options": [_opt(b.text, b.id, True) for b in found],
            "recommended": [b.id for b in found],
        }
    )
    rng = float(baseline.get("enemy_range_m") or 0)
    qs.append(
        {
            "id": "enemy_range_m",
            "question": f"敌方起始距离？（当前想定 {rng / 1000:g} km，雷达约 "
            f"{float(baseline.get('sensor_range_m') or 6000) / 1000:g} km）",
            "options": [_opt(f"保持当前 {rng / 1000:g} km", None, True)]
            + [
                _opt(text, value)
                for text, value in (
                    ("6.5 km（刚出雷达范围，较快接敌）", 6500.0),
                    ("10 km", 10000.0),
                )
                if abs(value - rng) > 1.0
            ],
            "recommended": None,
        }
    )
    kinds = {b.kind for b in found}
    if kinds & {"lost_reacquire", "lost_timeout", "lost_end"}:
        qs.append(
            {
                "id": "lost_after_s",
                "question": "跟踪开始后多久让目标消失（敌方从想定中移除，之后在原处重新出现）？",
                "options": [_opt("30 s", 30.0), _opt("60 s", 60.0, True), _opt("120 s", 120.0)],
                "recommended": 60.0,
            }
        )
        tout = next((b.timeout_s for b in found if b.timeout_s and "lost" in b.kind), None)
        if tout:
            qs.append(
                {
                    "id": "lost_for_s",
                    "question": f"目标消失多久？模型的重新获取时限是 {tout:g} s。",
                    "options": [
                        _opt(
                            f"两种都跑：{tout / 2:g} s（时限内回来）和 {tout + 20:g} s（超过时限）",
                            "both",
                            True,
                        ),
                        _opt(f"只跑时限内回来（{tout / 2:g} s）", "short"),
                        _opt(f"只跑超过时限（{tout + 20:g} s）", "long"),
                    ],
                    "recommended": "both",
                }
            )
    close = next((b for b in found if b.kind == "close_withdraw"), None)
    if close is not None:
        thr = close.threshold_m or 2000.0
        qs.append(
            {
                "id": "close_speed_mps",
                "question": f"跟踪中敌方以多快的速度逼近（要冲进 {thr:g} m 以内；"
                "长车会后退保持距离，敌方要比它快）？",
                "options": [
                    _opt("12 m/s", 12.0),
                    _opt("15 m/s", 15.0, True),
                    _opt("20 m/s", 20.0),
                ],
                "recommended": 15.0,
            }
        )
        qs.append(
            {
                "id": "close_stop_m",
                "question": "敌方逼近到多近停下？",
                "options": [
                    _opt(f"{thr * 0.6:g} m", thr * 0.6, True),
                    _opt(f"{thr * 0.3:g} m", thr * 0.3),
                ],
                "recommended": thr * 0.6,
            }
        )
    for b in found:
        if b.kind == "order":
            qs.append(
                {
                    "id": f"order_at_{b.event}",
                    "question": f"跟踪开始后多久发出 {b.event}？",
                    "options": [
                        _opt("30 s", 30.0, True),
                        _opt("90 s", 90.0),
                        _opt("跟踪开始前（搜索阶段）", -60.0),
                    ],
                    "recommended": 30.0,
                }
            )
    qs.append(
        {
            "id": "end_after_s",
            "question": "分支走完后再运行多久、然后发结束请求（cancelled）？",
            "options": [_opt("60 s", 60.0, True), _opt("120 s", 120.0)],
            "recommended": 60.0,
        }
    )
    return qs


def recommended(qs: list[dict[str, Any]]) -> dict[str, Any]:
    return {q["id"]: q.get("recommended") for q in qs}


# ============================================================================ variants
def track_start(
    found: list[Branch], baseline: dict[str, Any], phase_times: dict[str, float] | None
) -> tuple[float, str]:
    """When the leader first enters its tracking state, and where the time comes from."""
    state = next((b.track_state for b in found if b.track_state), None)
    if phase_times and state and state in phase_times:
        return float(phase_times[state]), "基准 AFSIM 运行"
    rng = float(baseline.get("enemy_range_m") or 0)
    sensor = float(baseline.get("sensor_range_m") or 6000)
    speed = float(baseline.get("route_speed_mps") or 8.0)
    est = max(0.0, (rng - sensor) / max(speed, 0.5)) + 60.0
    return round(est), "按想定估计（开进雷达范围 + 接敌展开约 60 s）"


def variants(
    found: list[Branch],
    answers: dict[str, Any],
    baseline: dict[str, Any],
    phase_times: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """One scenario variant per branch case: ``{name, branch, overrides, expect, note}``.
    ``overrides`` is in the format of ``scenario_set`` files (kit sections)."""
    chosen = set(answers.get("branches") or [b.id for b in found])
    t_track, source = track_start(found, baseline, phase_times)
    common: dict[str, Any] = {}
    if answers.get("enemy_range_m"):
        common.setdefault("enemy", {})["range_m"] = float(answers["enemy_range_m"])
        # another starting distance: the baseline run's times no longer hold
        t_track, source = track_start(
            found, baseline | {"enemy_range_m": float(answers["enemy_range_m"])}, None
        )
    after_end = float(answers.get("end_after_s") or 60.0)
    out: list[dict[str, Any]] = []

    def add(name: str, b: Branch, over: dict[str, Any], t_done: float, note: str) -> None:
        o = json.loads(json.dumps(common))
        for k, v in over.items():
            o.setdefault(k, {}).update(v)
        end = t_done + after_end + 20.0
        o.setdefault("meta", {})["end_time_s"] = round(end)
        o.setdefault("events", {})["cancelled"] = -20.0
        out.append(
            {
                "name": name,
                "branch": b.id,
                "overrides": o,
                "expect": {"role": b.role, "visits_in_order": b.path},
                "note": f"{note}；跟踪开始取 {t_track:g} s（{source}）",
            }
        )

    lost_at = t_track + float(answers.get("lost_after_s") or 60.0)
    mode = answers.get("lost_for_s") or "both"
    for b in found:
        if b.id not in chosen:
            continue
        if b.kind == "lost_reacquire" and mode in ("both", "short"):
            dur = (b.timeout_s or 30.0) / 2
            add(
                f"{b.id}",
                b,
                {"enemy": {"gone": [[round(lost_at), round(lost_at + dur)]]}},
                lost_at + dur + 30,
                f"{lost_at:g} s 目标消失，{dur:g} s 后在原处重新出现",
            )
        elif b.kind == "lost_timeout" and mode in ("both", "long"):
            dur = (b.timeout_s or 30.0) + 20.0
            add(
                f"{b.id}",
                b,
                {"enemy": {"gone": [[round(lost_at), round(lost_at + dur)]]}},
                lost_at + dur + 30,
                f"{lost_at:g} s 目标消失，{dur:g} s 后才回来（超过时限 {b.timeout_s:g} s）",
            )
        elif b.kind == "lost_end":
            add(
                b.id,
                b,
                {"enemy": {"gone": [[round(lost_at), None]]}},
                lost_at + 30,
                f"{lost_at:g} s 目标消失，不再出现",
            )
        elif b.kind == "close_withdraw":
            speed = float(answers.get("close_speed_mps") or 15.0)
            stop = float(answers.get("close_stop_m") or (b.threshold_m or 2000.0) * 0.6)
            start = t_track + 30.0
            rng = float(common.get("enemy", {}).get("range_m") or baseline.get("enemy_range_m"))
            add(
                b.id,
                b,
                {
                    "enemy": {
                        "approach_speed_mps": speed,
                        "stop_range_m": stop,
                        "approach_start_s": round(start),
                    }
                },
                start + max(0.0, rng - stop) / speed,
                f"跟踪开始 30 s 后敌方以 {speed:g} m/s 逼近，停在 {stop:g} m",
            )
        elif b.kind == "order" and b.event:
            dt = float(answers.get(f"order_at_{b.event}") or 30.0)
            at = max(5.0, t_track + dt)
            add(
                b.id,
                b,
                {"events": {b.event: round(at)}},
                at + 20,
                f"{at:g} s 发出 {b.event}",
            )
        elif b.kind == "search_limit":
            add(
                b.id,
                b,
                {"enemy": {"count": 0}},
                (b.timeout_s or 300.0) + 40,
                "全程没有敌方",
            )
    return out


def path_met(states: list[tuple[float, str]], wanted: list[str]) -> bool:
    """The visited states contain ``wanted`` in order (as the 2D checks judge it)."""
    it = iter(s for _, s in states)
    return all(any(v == w for v in it) for w in wanted)


def parse_states(text: str) -> list[tuple[float, str]]:
    """``A@0 → B@20.5`` (afsim_run's state lines) → [(0, A), (20.5, B)]."""
    out = []
    for part in str(text or "").split("→"):
        m = re.match(r"\s*([A-Za-z0-9_]+)@(-?\d+(?:\.\d+)?)", part)
        if m:
            out.append((float(m.group(2)), m.group(1)))
    return out
