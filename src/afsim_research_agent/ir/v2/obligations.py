"""Requirement obligations: what a requirement says about *when* things happen, written so a
program can check it on any run — the 2D simulation or AFSIM — independently of the
tests and scenarios the drafting model wrote for itself.

An obligation names actions of the capability set (observable: every run records when each
action starts) and external events, never the model's own state names, so it holds for any
structure the model chooses:

``not_before``  ``action`` starts no earlier than ``min_s`` after ``after`` first started
                ("search starts about 10 s after the advance starts"; the first start of
                ``action`` from the anchor on); with ``after`` equal
                to ``action``: consecutive starts are at least ``min_s`` apart
                ("re-evaluate every few seconds")
``precedes``    ``then`` does not start before ``first`` has started
                ("stop first, then deploy")
``response``    after the external event ``on_event``, ``action`` starts within ``within_s``
                ("on a stop request, switch the sensor off within 10 s")

Effect obligations say what the vehicles must *do*, judged on sampled positions (the AFSIM
run's IR_POS lines, :mod:`afsim_research_agent.ir.v2.effects`); runs without positions
(the 2D simulation for now) do not judge them:

``keeps_distance``  the vehicles (of ``roles``, default all own) never come closer than
                    ``min_m`` to an enemy vehicle
``stays_still``     after ``action`` starts on a vehicle (or after ``on_event``), the vehicle
                    moves no more than ``tol_m`` within ``duration_s`` (default: to the end)
``not_all_moving``  at no time are all the vehicles (of ``roles``) moving at once
``moves_in_turn``   the vehicles (of ``roles``) move in at least ``min_bounds`` separate
                    bounds of at least ``min_m`` each, and consecutive bounds are made by
                    different vehicles (bounding overwatch)
``advances``        the vehicles (of ``roles``) together close at least ``min_m`` on the
                    enemy
``destroys``        the vehicles (of ``roles``) destroy at least ``min_count`` enemy vehicles
                    (default: every enemy vehicle of the run), within ``within_s`` of the
                    start when given; judged on the run's weapon events (AFSIM's event
                    output, the 2D team models' shots)
``formation_shape`` the vehicles (of ``roles``) take up ``shape`` around the ``leader_role``
                    vehicle (slots as the platform lays them out: ``spacing_m`` along the
                    leader's heading, ``lateral_m`` across), every slot within ``tol_m``,
                    held ``hold_s``, within ``within_s`` after ``action`` starts on the
                    leader (or ``on_event``; else from the start)

An obligation whose anchor never happens in a run is not judged there (vacuous). Times are
compared with a tolerance of one update or 10 %, whichever is larger. ``precedes`` between
actions of different vehicles is judged on completions when the run reports them
(AFSIM: IR_DONE), else on starts of the prerequisite and completions of the follower.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Kind = Literal[
    "not_before",
    "precedes",
    "response",
    "keeps_distance",
    "stays_still",
    "not_all_moving",
    "moves_in_turn",
    "advances",
    "destroys",
    "formation_shape",
]
SPATIAL = frozenset(
    {
        "keeps_distance",
        "stays_still",
        "not_all_moving",
        "moves_in_turn",
        "advances",
        "destroys",
        "formation_shape",
    }
)
SHAPES = (
    "column",
    "line_abreast",
    "wedge_forward",
    "wedge_rear",
    "echelon_left",
    "echelon_right",
)
_COMMON = {"id", "kind", "description", "clause", "roles"}
_USES = {
    "not_before": {"action", "after", "min_s"},
    "precedes": {"first", "then"},
    "response": {"on_event", "action", "within_s"},
    "keeps_distance": {"min_m"},
    "stays_still": {"action", "on_event", "duration_s", "tol_m"},
    "not_all_moving": {"speed_mps"},
    "moves_in_turn": {"min_bounds", "min_m"},
    "advances": {"min_m"},
    "destroys": {"min_count", "within_s"},
    "formation_shape": {
        "shape",
        "spacing_m",
        "lateral_m",
        "tol_m",
        "hold_s",
        "within_s",
        "leader_role",
        "action",
        "on_event",
    },
}
_NEED = {
    "not_before": ("action", "after", "min_s"),
    "precedes": ("first", "then"),
    "response": ("on_event", "action", "within_s"),
    "keeps_distance": ("min_m",),
    "stays_still": (),
    "not_all_moving": (),
    "moves_in_turn": (),
    "advances": ("min_m",),
    "destroys": (),
    "formation_shape": ("shape", "leader_role"),
}


class Obligation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    kind: Kind
    description: str = ""
    clause: str | None = None  # requirement clause id (rq_xx)
    action: str | None = None  # not_before / response
    after: str | None = None  # not_before: anchor action
    min_s: float | None = Field(default=None, ge=0)
    first: str | None = None  # precedes
    then: str | None = None  # precedes
    on_event: str | None = None  # response
    within_s: float | None = Field(default=None, gt=0)
    roles: list[str] = Field(default_factory=list)  # effect kinds: whose vehicles
    min_m: float | None = Field(default=None, ge=0)
    tol_m: float | None = Field(default=None, ge=0)
    duration_s: float | None = Field(default=None, gt=0)
    speed_mps: float | None = Field(default=None, ge=0)
    min_bounds: int | None = Field(default=None, ge=1)
    min_count: int | None = Field(default=None, ge=1)  # destroys
    shape: Literal[SHAPES] | None = None  # formation_shape  # type: ignore[valid-type]
    spacing_m: float | None = Field(default=None, gt=0)
    lateral_m: float | None = Field(default=None, gt=0)
    hold_s: float | None = Field(default=None, ge=0)
    leader_role: str | None = None

    @property
    def spatial(self) -> bool:
        return self.kind in SPATIAL

    def clauses(self) -> list[str]:
        """The clause ids this obligation checks (``clause`` may list several)."""
        return [c.strip() for c in re.split(r"[,，;；\s]+", self.clause or "") if c.strip()]

    @model_validator(mode="before")
    @classmethod
    def _drop_unused(cls, data: Any) -> Any:
        """Fields another kind uses are ignored (drafts often fill them with 0 or null)."""
        if not isinstance(data, dict) or data.get("kind") not in _USES:
            return data
        used = _USES[data["kind"]]
        return {k: v for k, v in data.items() if k in _COMMON or k in used}

    def actions(self) -> list[str]:
        return [a for a in (self.action, self.after, self.first, self.then) if a]

    def problems(self, known_actions: set[str], known_events: set[str]) -> list[str]:
        out = []
        for key in _NEED[self.kind]:
            if getattr(self, key) is None:
                out.append(f"{self.id}: {self.kind} 需要 {key}")
        for a in self.actions():
            if known_actions and a not in known_actions:
                out.append(f"{self.id}: {a} 不是能力集的动作")
        if self.on_event and known_events and self.on_event not in known_events:
            out.append(f"{self.id}: {self.on_event} 不是外部事件")
        if self.kind == "stays_still" and not (self.action or self.on_event):
            out.append(f"{self.id}: stays_still 需要 action 或 on_event")
        return out


@dataclass
class Verdict:
    id: str
    passed: bool
    detail: str
    judged: bool = True  # False: the anchor did not happen in this run


def _slack(span: float, step_s: float) -> float:
    return max(step_s, 0.1 * span)


def state_actions(spec: dict[str, Any], tactics: dict[str, Any]) -> dict[str, dict[str, set[str]]]:
    """The actions each state's tree can run, per role (through subtrees and tactic-set
    trees). ``tactics``: tactic-set id → its raw entry (with ``root``)."""
    subs = {s["id"]: s.get("root") for s in spec.get("subtrees") or [] if "id" in s}

    def walk(node: Any, acc: set[str], seen: set[str]) -> None:
        if isinstance(node, list):
            for n in node:
                walk(n, acc, seen)
            return
        if not isinstance(node, dict):
            return
        if isinstance(node.get("action"), str):
            acc.add(node["action"])
        for key in ("tactic", "subtree"):
            ref = node.get(key)
            if isinstance(ref, str) and ref not in seen:
                seen.add(ref)
                root = subs.get(ref)
                if root is None and ref in tactics:
                    root = (tactics[ref] or {}).get("root")
                walk(root, acc, seen)
        for value in node.values():
            if isinstance(value, dict | list):
                walk(value, acc, seen)

    out: dict[str, dict[str, set[str]]] = {}
    for rid, trees in (spec.get("state_trees") or {}).items():
        per: dict[str, set[str]] = {}
        for state, ref in (trees or {}).items():
            acc: set[str] = set()
            walk({"tactic": ref} if isinstance(ref, str) else ref, acc, set())
            per[state] = acc
        out[rid] = per
    return out


def role_actions(spec: dict[str, Any], tactics: dict[str, Any]) -> dict[str, set[str]]:
    """The actions each role's state trees can run."""
    return {
        rid: set().union(*per.values()) if per else set()
        for rid, per in state_actions(spec, tactics).items()
    }


def event_actions(
    missions: dict[str, Any], per_state: dict[str, dict[str, set[str]]]
) -> dict[str, dict[str, set[str]]]:
    """Per role and event: the actions of the states the event leads to and every state
    reachable from them — what a response to that event can run on this role."""
    out: dict[str, dict[str, set[str]]] = {}
    for rid, mission in (missions or {}).items():
        if not isinstance(mission, dict) or "transitions" not in mission:
            continue
        trans = list(mission.get("transitions") or [])
        glob = list(mission.get("global") or [])
        nxt: dict[str, set[str]] = {}
        for t in trans:
            nxt.setdefault(t["from"], set()).add(t["to"])
        for g in glob:
            for s in mission.get("states") or []:
                nxt.setdefault(s, set()).add(g["to"])
        starts: dict[str, set[str]] = {}
        for t in [*trans, *glob]:
            starts.setdefault(str(t["on"]), set()).add(t["to"])
        acts = per_state.get(rid, {})
        per: dict[str, set[str]] = {}
        for event, targets in starts.items():
            seen, todo = set(targets), list(targets)
            while todo:
                for n in nxt.get(todo.pop(), set()):
                    if n not in seen:
                        seen.add(n)
                        todo.append(n)
            per[event] = set().union(*(acts.get(s, set()) for s in seen)) if seen else set()
        out[rid] = per
    return out


def check(
    obligations: list[Obligation],
    activations: list[tuple[float, str, str]],  # (time, platform / instance, action)
    events: list[tuple[float, str]],  # external events raised: (time, event)
    *,
    step_s: float = 1.0,
    end_s: float | None = None,
    capable: dict[str, set[str]] | None = None,  # platform → actions its model can run
    after_event: dict[str, dict[str, set[str]]] | None = None,  # platform → event → actions
    completions: list[tuple[float, str, str]] | None = None,  # (time, platform, action) done
    completions_only: bool = False,
) -> list[Verdict]:
    """With ``capable``, an obligation is judged on a platform only when the platform's model
    runs the constrained action. An ordering between actions of different platforms (e.g. the
    leader's broadcast before a member's adoption) is not judged: action starts carry no
    causality across vehicles (a member polls for its task from the start). With
    ``after_event``, a response is judged on a platform only when the states the event
    leads to (and those after them) run the action.

    With ``completions`` (the run reports when actions succeed), an ordering across vehicles
    is judged: the prerequisite's first completion (else start) on any other vehicle must
    come no later than the follower's first completion (else start) on this one. With
    ``completions_only`` (the 2D run, whose completions cover the modelled actions only) an
    ordering across vehicles is judged only when both actions completed. Effect
    kinds (:data:`SPATIAL`) are judged by :mod:`.effects`, not here."""
    done: dict[str, dict[str, list[float]]] = {}
    for t, platform, action in completions or []:
        done.setdefault(platform, {}).setdefault(action, []).append(float(t))
    by_platform: dict[str, dict[str, list[float]]] = {}
    for t, platform, action in activations:
        by_platform.setdefault(platform, {}).setdefault(action, []).append(float(t))
    for platform in capable or {}:
        by_platform.setdefault(platform, {})
    out = []
    for ob in obligations:
        if ob.spatial:
            continue
        verdicts = []
        for platform, own in sorted(by_platform.items()):
            caps = None if capable is None else capable.get(platform)
            if ob.kind == "response" and after_event and platform in after_event:
                caps = after_event[platform].get(str(ob.on_event), set())
            subject = ob.then if ob.kind == "precedes" else ob.action
            if caps is not None and subject not in caps:
                continue
            prereq = ob.first if ob.kind == "precedes" else ob.after
            others = {n: o for n, o in by_platform.items() if n != platform}
            if (
                ob.kind == "precedes"
                and completions is not None
                and ob.first not in own
                and any(ob.first in o for o in others.values())
            ):
                verdicts.append(_across(ob, platform, own, others, done, step_s, completions_only))
                continue
            if caps is not None and prereq and prereq not in caps:
                verdicts.append(
                    Verdict(
                        ob.id,
                        True,
                        f"{platform}: {prereq} 由其他车辆执行，跨车先后不按开始时刻判定",
                        judged=False,
                    )
                )
                continue
            if (
                ob.kind == "precedes"
                and ob.first not in own
                and any(
                    ob.first in other for name, other in by_platform.items() if name != platform
                )
            ):
                # the prerequisite ran on another vehicle only (its capable set includes the
                # action through a subtree, e.g. a shutdown that also stops broadcasting):
                # ordering across vehicles is not judged
                verdicts.append(
                    Verdict(
                        ob.id,
                        True,
                        f"{platform}: {ob.first} 只在其他车辆上执行，跨车先后不按开始时刻判定",
                        judged=False,
                    )
                )
                continue
            verdicts.append(_one(ob, platform, own, events, step_s, end_s))
        judged = [v for v in verdicts if v.judged]
        if not judged:
            out.append(Verdict(ob.id, True, "本次运行没有出现约束的前提，未判定", judged=False))
        else:
            bad = [v for v in judged if not v.passed]
            out.append(bad[0] if bad else judged[0])
    return out


def _across(ob, platform, own, others, done, step_s, only_done: bool = False) -> Verdict:
    """``precedes`` with the prerequisite on other vehicles (see :func:`check`)."""
    then_done = _first(done.get(platform, {}), ob.then)
    if only_done:
        firsts_done = [t for n in others if (t := _first(done.get(n, {}), ob.first)) is not None]
        if then_done is None or not firsts_done:
            return Verdict(
                ob.id, True, f"{platform}: {ob.then} 或 {ob.first} 没有完成", judged=False
            )
        own, others = {}, {n: {} for n in others}  # judge on the completions alone
    tt = then_done if then_done is not None else _first(own, ob.then)
    if tt is None:
        return Verdict(ob.id, True, f"{platform}: {ob.then} 没有执行", judged=False)
    firsts = []
    for name, starts in others.items():
        t = _first(done.get(name, {}), ob.first)
        t = t if t is not None else _first(starts, ob.first)
        if t is not None:
            firsts.append((t, name))
    tf, who = min(firsts) if firsts else (None, None)
    ok = tf is not None and tf <= tt + step_s
    what = "完成" if then_done is not None else "开始"
    return Verdict(
        ob.id,
        ok,
        f"{platform}: {ob.then} 在 {tt:g} s {what}，"
        + (f"{who} 的 {ob.first} 在 {tf:g} s" if tf is not None else f"其他车辆没有执行 {ob.first}")
        + ("" if ok else "（跨车先后不满足）"),
    )


def _first(starts: dict[str, list[float]], action: str | None, since: float = float("-inf")):
    times = [t for t in starts.get(action or "", []) if t >= since]
    return min(times) if times else None


def _one(ob, platform, starts, events, step_s, end_s) -> Verdict:
    if ob.kind == "not_before" and ob.after == ob.action:
        # "repeat no more often than every min_s": consecutive starts of the same action
        # starts in the same update (e.g. two states entered back to back both run it) are
        # one round
        times = sorted(set(starts.get(ob.action or "", [])))
        if len(times) < 2:
            return Verdict(ob.id, True, f"{platform}: {ob.action} 只执行了一次", judged=False)
        need = float(ob.min_s or 0.0)
        gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
        gap = min(gaps)
        ok = gap >= need - _slack(need, step_s)
        return Verdict(
            ob.id,
            ok,
            f"{platform}: {ob.action} 相邻两次开始最短间隔 {gap:g} s"
            + ("" if ok else f"，要求不短于 {need:g} s"),
        )
    if ob.kind == "not_before":
        tb = _first(starts, ob.after)
        if tb is None:
            return Verdict(ob.id, True, f"{platform}: {ob.after} 没有执行", judged=False)
        # the first start of the action from the anchor on (an earlier start, e.g. the first
        # round of a repeated pipeline, is not the one the requirement times)
        ta = _first(starts, ob.action, tb)
        if ta is None:
            return Verdict(ob.id, True, f"{platform}: {ob.action} 没有执行", judged=False)
        need = float(ob.min_s or 0.0)
        gap = ta - tb
        ok = gap >= need - _slack(need, step_s)
        return Verdict(
            ob.id,
            ok,
            f"{platform}: {ob.action} 在 {ob.after} 开始后 {gap:g} s 开始"
            + ("" if ok else f"，要求不早于 {need:g} s"),
        )
    if ob.kind == "precedes":
        tt = _first(starts, ob.then)
        if tt is None:
            return Verdict(ob.id, True, f"{platform}: {ob.then} 没有执行", judged=False)
        before = [t for t in starts.get(ob.first or "", []) if t <= tt + step_s]
        tf = max(before) if before else None
        ok = tf is not None
        moved: list[tuple[float, str]] = []
        if ok and ob.first == "movement.stop":
            # "stop, then switch the sensor off": the vehicle must still be stopped. A stop
            # earlier in the run (on contact) followed by motion (the standoff manoeuvre)
            # does not satisfy it (S5 injection E7, 11-10)
            from afsim_research_agent.ir.v2.effects import RESUME_ACTIONS

            moved = sorted(
                (t, a)
                for a in RESUME_ACTIONS
                for t in starts.get(a, [])
                if tf is not None and tf + step_s < t <= tt
            )
            ok = not moved
        return Verdict(
            ob.id,
            ok,
            f"{platform}: {ob.then} 在 {tt:g} s 开始，"
            + (f"{ob.first} 在 {tf:g} s" if tf is not None else f"此前 {ob.first} 没有执行")
            + (f"，但之后 {moved[0][1]} 在 {moved[0][0]:g} s 又开始移动" if moved else ""),
        )
    # response
    raised = [t for t, e in events if e == ob.on_event]
    if not raised:
        return Verdict(ob.id, True, f"事件 {ob.on_event} 没有发生", judged=False)
    te = min(raised)
    limit = float(ob.within_s or 0.0)
    if end_s is not None and end_s < te + limit:
        return Verdict(ob.id, True, f"事件 {ob.on_event} 之后运行时间不足", judged=False)
    ta = _first(starts, ob.action, te)
    ok = ta is not None and ta - te <= limit + _slack(limit, step_s)
    return Verdict(
        ob.id,
        ok,
        f"{platform}: 事件 {ob.on_event} 在 {te:g} s，"
        + (
            f"{ob.action} 在其后 {ta - te:g} s 开始"
            if ta is not None
            else f"之后 {ob.action} 没有执行"
        )
        + ("" if ok else f"，要求 {limit:g} s 内"),
    )


def render(verdicts: list[Verdict], obligations: list[Obligation]) -> list[str]:
    desc = {o.id: o for o in obligations}
    lines = []
    for v in verdicts:
        if not v.judged:
            continue
        o = desc.get(v.id)
        head = f"需求约束 {v.id}" + (f"（{o.clause}）" if o and o.clause else "")
        text = f"{head} {o.description}：" if o and o.description else f"{head}："
        lines.append(f"  [{'ok' if v.passed else 'FAIL'}] {text}{v.detail}")
    return lines
