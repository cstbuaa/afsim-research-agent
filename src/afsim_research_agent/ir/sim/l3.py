"""L3 closed-loop scenario runner and observable evaluation (design §21).

Expectation semantics
---------------------
* ``within: {after_event|after, value, unit}`` – the condition must first become
  true no later than anchor + window (``role_state``, ``formation_id``,
  ``time_to_transition`` …). The time it first holds is the expectation's
  satisfaction time and can anchor later ``during``/``within`` windows.
* ``during: {from, duration}`` – state-like observables must hold at every
  sample in the window; numeric series are aggregated over the window
  (mean for ``formation_error_rms`` and ``speed``, min for ``min_interval``,
  max for ``max_interval``, count for ``messages_sent``/``actions_issued``).
* neither – state-like observables are checked at the end of the run; numeric
  series are aggregated over the whole run.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from afsim_research_agent.ir.sim.world import SimWorld, _q
from afsim_research_agent.ir.units import infer_dimension, to_canonical

_CMP = {
    "equals": lambda a, b: a == b if not isinstance(a, float) else math.isclose(a, b),
    "lte": lambda a, b: a <= b,
    "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "gt": lambda a, b: a > b,
}


@dataclass
class Sample:
    t: float
    states: dict[str, str | None]
    positions: dict[str, tuple[float, float]]
    speeds: dict[str, float]
    formations: dict[str, str]
    formation_rms: dict[str, float | None]
    hostile_ranges: dict[str, float] = field(default_factory=dict)


@dataclass
class ExpectationResult:
    id: str
    observable: str
    passed: bool
    measured: Any
    threshold: Any
    detail: str = ""
    satisfied_at: float | None = None


@dataclass
class ScenarioResult:
    id: str
    passed: bool
    expectations: list[ExpectationResult]
    event_times: dict[str, float]
    traces_to: list[str]
    duration_s: float
    messages_sent: dict[str, int] = field(default_factory=dict)
    final_states: dict[str, str | None] = field(default_factory=dict)
    # faithful mode: withheld IR element → how often the run needed it
    withheld: dict[str, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        extra = {"withheld": self.withheld} if self.withheld is not None else {}
        return {
            **extra,
            "id": self.id,
            "passed": self.passed,
            "duration_s": self.duration_s,
            "event_times": self.event_times,
            "traces_to": self.traces_to,
            "messages_sent": self.messages_sent,
            "final_states": self.final_states,
            "expectations": [
                {
                    "id": e.id,
                    "observable": e.observable,
                    "passed": e.passed,
                    "measured": _jsonable(e.measured),
                    "threshold": _jsonable(e.threshold),
                    "satisfied_at": e.satisfied_at,
                    "detail": e.detail,
                }
                for e in self.expectations
            ],
        }


def _jsonable(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None if math.isnan(value) else ("inf" if value > 0 else "-inf")
    if isinstance(value, float):
        return round(value, 3)
    return value


def run_scenario(
    ir: dict[str, Any],
    context: dict[str, Any],
    scenario: Mapping[str, Any],
    *,
    package_dir: Path | None = None,
    withheld: Collection[str] | None = None,
) -> ScenarioResult:
    world = SimWorld(ir, context, scenario, package_dir=package_dir, withheld=withheld)
    samples: list[Sample] = []
    formation_ids = [f["id"] for f in ir["formations"]]
    while world.now <= world.duration + 1e-9:
        world.step()
        samples.append(
            Sample(
                t=world.now,
                states={e.key: e.controller.state for e in world.entities},
                positions={e.key: (e.x, e.y) for e in world.entities},
                speeds={e.key: e.speed for e in world.entities},
                formations={e.key: e.formation_id for e in world.entities},
                formation_rms={fid: _formation_rms(world, fid) for fid in formation_ids},
                hostile_ranges={e.key: _nearest_hostile(world, e) for e in world.entities},
            )
        )
    result = evaluate_trace(ir, scenario, world, samples, duration_s=world.duration)
    if withheld is not None:
        result.withheld = dict(sorted(world.withheld_hits.items()))
    return result


def evaluate_trace(
    ir: dict[str, Any],
    scenario: Mapping[str, Any],
    trace: Any,
    samples: list[Sample],
    *,
    duration_s: float,
) -> ScenarioResult:
    """Judge a recorded run against the scenario expectations.

    ``trace`` needs ``entities`` (objects with ``key`` and ``role``),
    ``event_times``, ``sent_log``, ``action_log`` and ``detect_time``; both the
    built-in simulation and the co-simulation runner provide them, so the same
    expectations judge runs in either environment.
    """
    evaluator = _Evaluator(ir, trace, samples)
    results = [evaluator.evaluate(item) for item in scenario.get("expect") or []]
    sent: dict[str, int] = {}
    for _, _, msg in trace.sent_log:
        sent[msg] = sent.get(msg, 0) + 1
    return ScenarioResult(
        id=scenario["id"],
        passed=all(r.passed for r in results),
        expectations=results,
        event_times=dict(trace.event_times),
        traces_to=list(scenario.get("traces_to") or []),
        duration_s=duration_s,
        messages_sent=dict(sorted(sent.items())),
        final_states=samples[-1].states if samples else {},
    )


def run_scenarios(
    ir: dict[str, Any],
    context: dict[str, Any],
    *,
    package_dir: Path | None = None,
    withheld: Collection[str] | None = None,
) -> list[ScenarioResult]:
    return [
        run_scenario(ir, context, s, package_dir=package_dir, withheld=withheld)
        for s in ir["scenarios"]
    ]


def _nearest_hostile(world: SimWorld, ent: Any) -> float:
    ranges = [math.hypot(h.x - ent.x, h.y - ent.y) for h in world.hostiles if h.alive]
    return min(ranges) if ranges else math.inf


def _formation_rms(world: SimWorld, formation_id: str) -> float | None:
    form = world.formations[formation_id]
    errors = []
    for slot in form["slots"]:
        ent = world.entity(f"{slot['role']}[{slot['instance']}]")
        if ent is None:
            continue
        err = world.slot_error(ent, formation_id)
        if err is not None:
            errors.append(err)
    if not errors:
        return None
    return math.sqrt(sum(e * e for e in errors) / len(errors))


class _Evaluator:
    def __init__(self, ir: dict[str, Any], world: SimWorld, samples: list[Sample]) -> None:
        self.ir = ir
        self.world = world
        self.samples = samples
        self.satisfied: dict[str, float | None] = {}
        self.params = {name: p["value"] for name, p in ir["parameters"].items()}

    # -- helpers ------------------------------------------------------------
    def threshold(self, raw: Any) -> Any:
        if isinstance(raw, Mapping):
            if "parameter_ref" in raw:
                return self.params[raw["parameter_ref"]]
            if set(raw) == {"value", "unit"}:
                dim = infer_dimension(str(raw["unit"])) or "distance"
                return to_canonical(float(raw["value"]), str(raw["unit"]), dim)
        return raw

    def anchor(self, name: str | None) -> float | None:
        if name in (None, "start"):
            return 0.0
        if name in self.world.event_times:
            return self.world.event_times[name]
        if name in self.satisfied:
            return self.satisfied[name]
        return None

    def subjects(self, item: Mapping[str, Any]) -> list[str]:
        role = item.get("role")
        keys = [e.key for e in self.world.entities if role is None or e.role == role]
        if item.get("instance") is not None:
            keys = [k for k in keys if k.endswith(f"[{item['instance']}]")]
        return keys

    def window(self, item: Mapping[str, Any]) -> tuple[float, float] | None:
        during = item.get("during")
        if not during:
            return 0.0, math.inf
        start = self.anchor(during.get("from", "start"))
        if start is None:
            return None
        duration = (
            _q(during.get("duration"), default=math.inf) if during.get("duration") else math.inf
        )
        return start, start + duration

    # -- evaluation ---------------------------------------------------------
    def evaluate(self, item: Mapping[str, Any]) -> ExpectationResult:
        comparator = next(
            n for n in ("equals", "lte", "gte", "lt", "gt") if item.get(n) is not None
        )
        threshold = self.threshold(item[comparator])
        obs = item["observable"]
        handler = getattr(self, f"_obs_{obs}")
        result: ExpectationResult = handler(item, comparator, threshold)
        self.satisfied[item["id"]] = result.satisfied_at if result.passed else None
        return result

    def _state_like(self, item, comparator, threshold, value_of) -> ExpectationResult:
        keys = self.subjects(item)
        cmp = _CMP[comparator]

        def holds(sample: Sample) -> bool:
            return bool(keys) and all(cmp(value_of(sample, k), threshold) for k in keys)

        within = item.get("within")
        if within:
            anchor = self.anchor(within.get("after_event") or within.get("after"))
            if anchor is None:
                return ExpectationResult(
                    item["id"], item["observable"], False, None, threshold, "anchor never occurred"
                )
            deadline = anchor + to_canonical(within["value"], within["unit"], "time")
            first = next((s.t for s in self.samples if holds(s)), None)
            ok = first is not None and first <= deadline + 1e-9
            detail = (
                f"first true at {first}s, deadline {deadline:g}s"
                if first is not None
                else "never true"
            )
            return ExpectationResult(
                item["id"], item["observable"], ok, first, threshold, detail, first
            )
        win = self.window(item)
        if win is None:
            return ExpectationResult(
                item["id"],
                item["observable"],
                False,
                None,
                threshold,
                "window anchor not satisfied",
            )
        if item.get("during"):
            inside = [s for s in self.samples if win[0] <= s.t <= win[1]]
            bad = next((s for s in inside if not holds(s)), None)
            ok = bool(inside) and bad is None
            measured = None if bad is None else {k: value_of(bad, k) for k in keys}
            detail = "held for the whole window" if ok else f"violated at {bad.t if bad else '-'}s"
            return ExpectationResult(
                item["id"],
                item["observable"],
                ok,
                measured,
                threshold,
                detail,
                win[0] if ok else None,
            )
        last = self.samples[-1]
        ok = holds(last)
        return ExpectationResult(
            item["id"],
            item["observable"],
            ok,
            {k: value_of(last, k) for k in keys},
            threshold,
            "at end of run",
            last.t if ok else None,
        )

    def _obs_role_state(self, item, comparator, threshold):
        return self._state_like(item, comparator, threshold, lambda s, k: s.states[k])

    def _obs_formation_id(self, item, comparator, threshold):
        return self._state_like(item, comparator, threshold, lambda s, k: s.formations[k])

    def _series(self, item, comparator, threshold, values, aggregate: str) -> ExpectationResult:
        cmp = _CMP[comparator]
        within = item.get("within")
        if within:
            anchor = self.anchor(within.get("after_event") or within.get("after"))
            if anchor is None:
                return ExpectationResult(
                    item["id"], item["observable"], False, None, threshold, "anchor never occurred"
                )
            deadline = anchor + to_canonical(within["value"], within["unit"], "time")
            first = next(
                (t for t, v in values if v is not None and t >= anchor and cmp(v, threshold)), None
            )
            ok = first is not None and first <= deadline + 1e-9
            return ExpectationResult(
                item["id"],
                item["observable"],
                ok,
                first,
                threshold,
                f"deadline {deadline:g}s",
                first,
            )
        win = self.window(item)
        if win is None:
            return ExpectationResult(
                item["id"],
                item["observable"],
                False,
                None,
                threshold,
                "window anchor not satisfied",
            )
        inside = [v for t, v in values if win[0] <= t <= win[1] and v is not None]
        if not inside:
            return ExpectationResult(
                item["id"], item["observable"], False, None, threshold, "no samples in window"
            )
        measured = {
            "mean": sum(inside) / len(inside),
            "min": min(inside),
            "max": max(inside),
        }[aggregate]
        ok = cmp(measured, threshold)
        detail = f"{aggregate} over [{win[0]:g}, {min(win[1], self.samples[-1].t):g}]s"
        return ExpectationResult(
            item["id"], item["observable"], ok, measured, threshold, detail, win[0] if ok else None
        )

    def _obs_formation_error_rms(self, item, comparator, threshold):
        fid = item["formation"]
        values = [(s.t, s.formation_rms.get(fid)) for s in self.samples]
        return self._series(item, comparator, threshold, values, "mean")

    def _intervals(self, item) -> list[tuple[float, float, float]]:
        keys = self.subjects(item)
        out = []
        for s in self.samples:
            nearest = []
            for k in keys:
                x, y = s.positions[k]
                others = [
                    math.hypot(x - ox, y - oy) for ok_, (ox, oy) in s.positions.items() if ok_ != k
                ]
                if others:
                    nearest.append(min(others))
            if nearest:
                out.append((s.t, min(nearest), max(nearest)))
        return out

    def _obs_min_interval(self, item, comparator, threshold):
        values = [(t, lo) for t, lo, _ in self._intervals(item)]
        return self._series(item, comparator, threshold, values, "min")

    def _obs_max_interval(self, item, comparator, threshold):
        values = [(t, hi) for t, _, hi in self._intervals(item)]
        return self._series(item, comparator, threshold, values, "max")

    def _obs_hostile_range_min(self, item, comparator, threshold):
        keys = self.subjects(item)
        values = []
        for s in self.samples:
            ranges = [s.hostile_ranges.get(k, math.inf) for k in keys]
            finite = [r for r in ranges if math.isfinite(r)]
            values.append((s.t, min(finite) if finite else None))
        return self._series(item, comparator, threshold, values, "min")

    def _obs_speed(self, item, comparator, threshold):
        keys = self.subjects(item)
        values = (
            [(s.t, sum(s.speeds[k] for k in keys) / len(keys)) for s in self.samples]
            if keys
            else []
        )
        return self._series(item, comparator, threshold, values, "mean")

    def _count(self, item, comparator, threshold, log) -> ExpectationResult:
        win = self.window(item) or (0.0, math.inf)
        count = sum(1 for t in log if win[0] <= t <= win[1])
        ok = _CMP[comparator](count, threshold)
        return ExpectationResult(
            item["id"], item["observable"], ok, count, threshold, "count", win[0] if ok else None
        )

    def _obs_messages_sent(self, item, comparator, threshold):
        log = [t for t, _, msg in self.world.sent_log if msg == item["message"]]
        return self._count(item, comparator, threshold, log)

    def _obs_actions_issued(self, item, comparator, threshold):
        keys = set(self.subjects(item))
        log = [t for t, key, cmd in self.world.action_log if cmd == item["command"] and key in keys]
        return self._count(item, comparator, threshold, log)

    def _time_until_state(self, item, start: float | None) -> float | None:
        if start is None:
            return None
        keys = self.subjects(item)
        for s in self.samples:
            if s.t >= start and keys and all(s.states[k] == item["state"] for k in keys):
                return s.t - start
        return None

    def _obs_time_to_transition(self, item, comparator, threshold):
        within = item.get("within") or {}
        start = self.anchor(within.get("after_event") or within.get("after") or "start")
        measured = self._time_until_state(item, start)
        ok = measured is not None and _CMP[comparator](measured, threshold)
        return ExpectationResult(
            item["id"],
            item["observable"],
            ok,
            measured,
            threshold,
            "seconds from anchor",
            (start or 0) + (measured or 0) if ok else None,
        )

    def _obs_contact_reaction_time(self, item, comparator, threshold):
        keys = self.subjects(item)
        detections = [self.world.detect_time[k] for k in keys if k in self.world.detect_time]
        start = min(detections) if detections else None
        measured = self._time_until_state(item, start)
        detail = "seconds from first detection" if start is not None else "no detection"
        ok = measured is not None and _CMP[comparator](measured, threshold)
        return ExpectationResult(
            item["id"],
            item["observable"],
            ok,
            measured,
            threshold,
            detail,
            (start or 0) + (measured or 0) if ok else None,
        )
