"""Requirement obligations: what a requirement says about *when* things happen, written so a
program can check it on any run — the 2D simulation or AFSIM — independently of the
tests and scenarios the drafting model wrote for itself.

An obligation names actions of the capability set (observable: every run records when each
action starts) and external events, never the model's own state names, so it holds for any
structure the model chooses:

``not_before``  ``action`` starts no earlier than ``min_s`` after ``after`` first started
                ("search starts about 10 s after the advance starts")
``precedes``    ``then`` does not start before ``first`` has started
                ("stop first, then deploy")
``response``    after the external event ``on_event``, ``action`` starts within ``within_s``
                ("on a stop request, switch the sensor off within 10 s")

An obligation whose anchor never happens in a run is not judged there (vacuous). Times are
compared with a tolerance of one update or 10 %, whichever is larger.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Kind = Literal["not_before", "precedes", "response"]


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

    def actions(self) -> list[str]:
        return [a for a in (self.action, self.after, self.first, self.then) if a]

    def problems(self, known_actions: set[str], known_events: set[str]) -> list[str]:
        out = []
        need = {
            "not_before": ("action", "after", "min_s"),
            "precedes": ("first", "then"),
            "response": ("on_event", "action", "within_s"),
        }[self.kind]
        for key in need:
            if getattr(self, key) is None:
                out.append(f"{self.id}: {self.kind} 需要 {key}")
        for a in self.actions():
            if known_actions and a not in known_actions:
                out.append(f"{self.id}: {a} 不是能力集的动作")
        if self.on_event and known_events and self.on_event not in known_events:
            out.append(f"{self.id}: {self.on_event} 不是外部事件")
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
) -> list[Verdict]:
    """With ``capable``, an obligation is judged on a platform only when the platform's model
    runs the constrained action. An ordering between actions of different platforms (e.g. the
    leader's broadcast before a member's adoption) is not judged: action starts carry no
    causality across vehicles (a member polls for its task from the start). With
    ``after_event``, a response is judged on a platform only when the states the event
    leads to (and those after them) run the action."""
    by_platform: dict[str, dict[str, list[float]]] = {}
    for t, platform, action in activations:
        by_platform.setdefault(platform, {}).setdefault(action, []).append(float(t))
    for platform in capable or {}:
        by_platform.setdefault(platform, {})
    out = []
    for ob in obligations:
        verdicts = []
        for platform, own in sorted(by_platform.items()):
            caps = None if capable is None else capable.get(platform)
            if ob.kind == "response" and after_event and platform in after_event:
                caps = after_event[platform].get(str(ob.on_event), set())
            subject = ob.then if ob.kind == "precedes" else ob.action
            if caps is not None and subject not in caps:
                continue
            prereq = ob.first if ob.kind == "precedes" else ob.after
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
            verdicts.append(_one(ob, platform, own, events, step_s, end_s))
        judged = [v for v in verdicts if v.judged]
        if not judged:
            out.append(Verdict(ob.id, True, "本次运行没有出现约束的前提，未判定", judged=False))
        else:
            bad = [v for v in judged if not v.passed]
            out.append(bad[0] if bad else judged[0])
    return out


def _first(starts: dict[str, list[float]], action: str | None, since: float = float("-inf")):
    times = [t for t in starts.get(action or "", []) if t >= since]
    return min(times) if times else None


def _one(ob, platform, starts, events, step_s, end_s) -> Verdict:
    if ob.kind == "not_before":
        tb = _first(starts, ob.after)
        if tb is None:
            return Verdict(ob.id, True, f"{platform}: {ob.after} 没有执行", judged=False)
        ta = _first(starts, ob.action)
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
        tf = _first(starts, ob.first)
        ok = tf is not None and tf <= tt + step_s
        return Verdict(
            ob.id,
            ok,
            f"{platform}: {ob.then} 在 {tt:g} s 开始，"
            + (f"{ob.first} 在 {tf:g} s" if tf is not None else f"此前 {ob.first} 没有执行"),
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
