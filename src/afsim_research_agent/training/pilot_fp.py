"""Pilot generator, formation + perception (design
``design/training-data-pilot-formation-perception.md``).

A model is built from a skeleton (K1–K6) and optional modules, with parameters drawn from
the reviewed ranges, out of the capability set's subtrees and steps. Each model comes with
L2 tests, 2D scenarios that drive every transition, and requirement obligations; the sheet is
then compiled and checked offline (``falcon.v3.draft.compile_package``). Only models that
pass every check are kept.

Roles: the formation skeletons have a leader (1, runs the formation steps that command the
followers) and members (2, their own perception only: their positions are commanded by the
leader's formation steps, so their trees never claim locomotion). K6 has one observer.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

SKELETONS = ("K1", "K2", "K3", "K4", "K5", "K6")
MODULE_DEFAULTS: dict[str, Any] = {
    "search_delay_s": None,
    "share_target": False,
    "stop_events": [],
    "names": {},
    "event_text": {},
}
ROUTE = "approach"  # the scenario interface's advance route (post → ahead)


@dataclass
class Spec:
    """One generated model: skeleton, modules and parameter values."""

    skeleton: str
    seed: int
    assemble: bool = False  # K3: form the column first
    lost_hold: bool = False  # hold and try to reacquire when the target is lost
    on_timeout: str = "resume"  # after the reacquire timeout: resume | shutdown
    # None | "keep" (back off while the enemy closes, holding the standoff range; the
    # standoff step does it) | "stop" / "back" (a threshold trigger: unused since the
    # standoff step keeps the range above any threshold below its band in AFSIM)
    withdraw: str | None = None
    search_limit_s: float | None = None  # K5: give up the search after this long
    wait_start: bool = False  # wait for a start order first
    # parameters (design §3)
    desired_range_m: float = 500.0
    range_tolerance_m: float = 50.0
    withdraw_threshold_m: float | None = None
    withdraw_back_m: float | None = None
    reacquire_timeout_s: float = 30.0
    wing_offset_m: float = 60.0
    column_spacing_m: float = 60.0
    ready_tolerance_m: float = 20.0
    stated: list[str] = field(default_factory=list)  # parameters the requirement will state
    # modules for drafting from a skeleton (falcon.v3.skeleton); off in the pilot sampling
    search_delay_s: float | None = None  # advance this long before the search starts
    share_target: bool = False  # the leader publishes the selected target to the members
    stop_events: list[str] = field(default_factory=list)  # other external requests → SHUTDOWN
    names: dict[str, str] = field(default_factory=dict)  # leader / member / observer → role
    event_text: dict[str, str] = field(default_factory=dict)  # external event → description

    @property
    def model_id(self) -> str:
        d = asdict(self)
        for k, v in MODULE_DEFAULTS.items():  # ids of the pilot data stay as they were
            if d.get(k) == v:
                d.pop(k)
        h = hashlib.sha1(json.dumps(d, sort_keys=True).encode()).hexdigest()[:8]
        return f"fp_{self.skeleton.lower()}_{h}"

    def name(self, role: str) -> str:
        return self.names.get(role, role)

    @property
    def formation(self) -> bool:
        return self.skeleton != "K6"


# ---------------------------------------------------------------------------- sampling
def sample(skeleton: str, seed: int) -> Spec:
    """Draw modules and parameters for a skeleton (ranges reviewed in design §2–§3).

    A parameter the requirement will state gets a drawn value; every other one keeps the
    capability default, so the requirement text alone determines the model (no hidden
    numbers a drafter could not know)."""
    r = random.Random(f"{skeleton}:{seed}")
    s = Spec(skeleton=skeleton, seed=seed)
    if skeleton == "K1":
        s.lost_hold = False
    elif skeleton == "K2":
        s.lost_hold = True
        s.on_timeout = r.choice(["resume", "shutdown"])
    elif skeleton == "K3":
        s.assemble = True
        s.lost_hold = r.random() < 0.5
        s.on_timeout = r.choice(["resume", "shutdown"])
    elif skeleton == "K4":
        s.withdraw = "keep"
        s.lost_hold = r.random() < 0.5
        s.on_timeout = r.choice(["resume", "shutdown"])
    elif skeleton == "K5":
        s.lost_hold = r.random() < 0.5
        s.on_timeout = r.choice(["resume", "shutdown"])
    elif skeleton == "K6":
        s.lost_hold = True
        s.on_timeout = "resume"
    s.wait_start = r.random() < (0.5 if skeleton == "K6" else 0.3)
    # which numbers the requirement states: the module's own number always, the standoff
    # range usually, the rest now and then
    stated: list[str] = []
    if s.formation and (s.withdraw == "keep" or r.random() < 0.8):
        stated.append("desired_range_m")
    if s.lost_hold:  # a timeout the text leaves open has no default to fall back on
        stated.append("reacquire_timeout_s")
    if s.withdraw in ("stop", "back"):
        stated.append("withdraw_threshold_m")
    if s.withdraw == "back":
        stated.append("withdraw_back_m")
    if skeleton == "K5":
        stated.append("search_limit_s")
    if s.formation:
        for extra in ("wing_offset_m", "column_spacing_m"):
            if r.random() < 0.25:
                stated.append(extra)
    s.stated = sorted(stated)
    # values: drawn when stated, else the defaults of the Spec
    if "desired_range_m" in stated:
        # backing off holds the range in AFSIM only from about 800 m out (closer, the
        # leader stops backing early; handoff 2026-11-09 §4.1)
        low = 8 if s.withdraw == "keep" else 3
        s.desired_range_m = float(r.randrange(low, 31) * 100)
    if "reacquire_timeout_s" in stated:
        s.reacquire_timeout_s = float(r.choice([10, 15, 20, 30, 45, 60]))
    if "wing_offset_m" in stated:
        s.wing_offset_m = float(r.randrange(4, 11) * 10)
    if "column_spacing_m" in stated:
        s.column_spacing_m = float(r.randrange(4, 11) * 10)
    if skeleton == "K5":
        s.search_limit_s = float(r.randrange(4, 31) * 30)
    if s.withdraw in ("stop", "back"):
        thr = s.desired_range_m * r.choice([0.5, 0.6, 0.7, 0.8])
        thr = max(200.0, round(thr / 100) * 100)
        s.withdraw_threshold_m = min(thr, s.desired_range_m - s.range_tolerance_m - 100)
        if s.withdraw_threshold_m < 150:
            s.withdraw_threshold_m = 150.0
        if s.withdraw == "back":
            s.withdraw_back_m = s.desired_range_m + float(r.randrange(5, 21) * 100)
    return s


# ---------------------------------------------------------------------------- the model
@dataclass
class Machine:
    states: list[str]
    initial: str
    transitions: list[tuple[str, str, str]]  # (from, to, condition or event)
    trees: dict[str, str]  # state -> python body (indented 4)


def _fmt(x: float) -> str:
    return f"{x:g}"


def leader_machine(s: Spec) -> Machine:
    m = s.ready_tolerance_m
    col = (
        f"formation.column_slots(spacing_m={_fmt(s.column_spacing_m)}, ready_tolerance_m={_fmt(m)})"
    )
    trees: dict[str, str] = {}
    T: list[tuple[str, str, str]] = []
    states: list[str] = []
    if s.wait_start:
        states.append("WAIT_START")
        trees["WAIT_START"] = "    movement.stop()"
    if s.assemble:
        states.append("ASSEMBLE")
        trees["ASSEMBLE"] = f"    {col}"
    if s.search_delay_s:
        # advance first, the search starts after the delay (a timer on the advance state:
        # the search's first start is then the delay after the route's first start)
        states.append("ADVANCE")
        trees["ADVANCE"] = (
            '    with parallel("all"):\n'
            "        with repeat(until_done=True):\n"
            f"            movement.follow_route(route={ROUTE})\n"
            f"        {col}"
        )
    states += ["SEARCH_ADVANCE", "CONTACT_DEPLOY", "STANDOFF_TRACK"]
    trees["SEARCH_ADVANCE"] = (
        '    with parallel("all"):\n'
        "        with repeat(until_done=True):\n"
        f"            movement.follow_route(route={ROUTE})\n"
        f"        {col}\n"
        '        tactic("perception.search_for_target")'
    )
    trees["CONTACT_DEPLOY"] = (
        '    tactic("formation_standoff.formation_contact_deploy", '
        f"wing_offset_m={_fmt(s.wing_offset_m)}, ready_tolerance_m={_fmt(m)})"
    )
    trees["STANDOFF_TRACK"] = (
        '    tactic("formation_standoff.formation_standoff_track", '
        f"desired_range_m={_fmt(s.desired_range_m)}, "
        f"range_tolerance_m={_fmt(s.range_tolerance_m)}, "
        f"wing_offset_m={_fmt(s.wing_offset_m)}, ready_tolerance_m={_fmt(m)})"
    )
    if s.share_target:  # publish the selected target while deploying and tracking
        for st in ("CONTACT_DEPLOY", "STANDOFF_TRACK"):
            trees[st] = (
                '    with parallel("all"):\n'
                + "\n".join("    " + ln for ln in trees[st].splitlines())
                + '\n        tactic("target_sharing.leader_publish_shared_target")'
            )
    if s.lost_hold:
        states.append("TARGET_LOST_HOLD")
        trees["TARGET_LOST_HOLD"] = '    tactic("formation_standoff.formation_target_lost_hold")'
        if s.on_timeout == "resume":
            states.append("RESUME_SEARCH")
            trees["RESUME_SEARCH"] = (
                '    tactic("formation_standoff.formation_resume_search", '
                f"column_spacing_m={_fmt(s.column_spacing_m)}, ready_tolerance_m={_fmt(m)})"
            )
    if s.withdraw == "back":
        states.append("WITHDRAW")
        trees["WITHDRAW"] = (
            '    tactic("formation_standoff.formation_standoff_track", '
            f"desired_range_m={_fmt(s.withdraw_back_m or 0)}, "
            f"range_tolerance_m={_fmt(s.range_tolerance_m)}, "
            f"wing_offset_m={_fmt(s.wing_offset_m)}, ready_tolerance_m={_fmt(m)})"
        )
    states += ["SHUTDOWN", "COMPLETE"]
    trees["SHUTDOWN"] = '    tactic("formation_standoff.formation_safe_shutdown")'

    go = "ADVANCE" if s.search_delay_s else "SEARCH_ADVANCE"
    if s.wait_start:
        T.append(("WAIT_START", "ASSEMBLE" if s.assemble else go, "start_order"))
    if s.assemble:
        T.append(("ASSEMBLE", go, "column_ready"))
    if s.search_delay_s:
        T.append(("ADVANCE", "SEARCH_ADVANCE", "search_delay_up"))
    T.append(("SEARCH_ADVANCE", "CONTACT_DEPLOY", "target_detected"))
    if s.search_limit_s:
        T.append(("SEARCH_ADVANCE", "SHUTDOWN", "search_time_up"))
    T.append(("CONTACT_DEPLOY", "STANDOFF_TRACK", "line_ready"))
    if s.withdraw == "stop":
        T.append(("STANDOFF_TRACK", "SHUTDOWN", "too_close"))
    elif s.withdraw == "back":
        T.append(("STANDOFF_TRACK", "WITHDRAW", "too_close"))
        T.append(("WITHDRAW", "SHUTDOWN", "target_lost"))
    if s.lost_hold:
        T.append(("STANDOFF_TRACK", "TARGET_LOST_HOLD", "target_lost"))
        T.append(("TARGET_LOST_HOLD", "STANDOFF_TRACK", "target_detected"))
        if s.on_timeout == "resume":
            T.append(("TARGET_LOST_HOLD", "RESUME_SEARCH", "reacquire_time_up"))
            T.append(("RESUME_SEARCH", "SEARCH_ADVANCE", "column_ready"))
            T.append(("RESUME_SEARCH", "CONTACT_DEPLOY", "target_detected"))
        else:
            T.append(("TARGET_LOST_HOLD", "SHUTDOWN", "reacquire_time_up"))
    else:
        T.append(("STANDOFF_TRACK", "SHUTDOWN", "target_lost"))
    T.append(("SHUTDOWN", "COMPLETE", "shutdown_done"))
    return Machine(states, states[0], T, trees)


def member_machine(s: Spec) -> Machine:
    """Members: their own perception only; their positions are commanded by the leader."""
    states = (["WAIT_START"] if s.wait_start else []) + ["SEARCH", "TRACK"]
    trees = {
        "SEARCH": '    tactic("perception.search_for_target")',
        "TRACK": '    tactic("perception.track_selected_target")',
    }
    if s.wait_start:
        trees["WAIT_START"] = '    tactic("perception.safe_sensor_shutdown")'
    T: list[tuple[str, str, str]] = []
    if s.wait_start:
        T.append(("WAIT_START", "SEARCH", "start_order"))
    T.append(("SEARCH", "TRACK", "target_detected"))
    if s.lost_hold or s.skeleton == "K6":
        states.append("REACQUIRE")
        trees["REACQUIRE"] = '    tactic("perception.reacquire_target")'
        T.append(("TRACK", "REACQUIRE", "target_lost"))
        T.append(("REACQUIRE", "TRACK", "target_detected"))
        if s.on_timeout == "resume":
            T.append(("REACQUIRE", "SEARCH", "reacquire_time_up"))
        else:
            T.append(("REACQUIRE", "SHUTDOWN", "reacquire_time_up"))
    else:
        T.append(("TRACK", "SHUTDOWN", "target_lost"))
    if s.withdraw == "stop":
        T.append(("TRACK", "SHUTDOWN", "too_close"))
    if s.search_limit_s:
        T.append(("SEARCH", "SHUTDOWN", "search_time_up"))
    states += ["SHUTDOWN", "COMPLETE"]
    trees["SHUTDOWN"] = '    tactic("perception.safe_sensor_shutdown")'
    T.append(("SHUTDOWN", "COMPLETE", "shutdown_done"))
    return Machine(states, states[0], T, trees)


def observer_machine(s: Spec) -> Machine:
    """K6: one vehicle holding an observation post."""
    states = (["WAIT_START"] if s.wait_start else []) + ["SEARCH", "TRACK", "REACQUIRE"]
    trees = {
        "SEARCH": (
            '    with parallel("all"):\n'
            "        movement.stop()\n"
            '        tactic("perception.search_for_target")'
        ),
        "TRACK": '    tactic("perception.track_selected_target")',
        "REACQUIRE": '    tactic("perception.reacquire_target")',
    }
    T: list[tuple[str, str, str]] = []
    if s.wait_start:
        trees["WAIT_START"] = "    movement.stop()"
        T.append(("WAIT_START", "SEARCH", "start_order"))
    T += [
        ("SEARCH", "TRACK", "target_detected"),
        ("TRACK", "REACQUIRE", "target_lost"),
        ("REACQUIRE", "TRACK", "target_detected"),
        ("REACQUIRE", "SEARCH", "reacquire_time_up"),
    ]
    states += ["SHUTDOWN", "COMPLETE"]
    trees["SHUTDOWN"] = (
        "    with sequence():\n"
        "        movement.stop()\n"
        '        tactic("perception.safe_sensor_shutdown")'
    )
    T.append(("SHUTDOWN", "COMPLETE", "shutdown_done"))
    return Machine(states, states[0], T, trees)


def roles(s: Spec) -> dict[str, tuple[int, Machine, str]]:
    if not s.formation:
        return {s.name("observer"): (1, observer_machine(s), "观察车")}
    return {
        s.name("leader"): (1, leader_machine(s), "长车；执行编队指挥动作，指挥两辆僚车"),
        s.name("member"): (2, member_machine(s), "僚车；只负责本车搜索与跟踪，站位由长车指挥"),
    }


TERMINAL = ("COMPLETE",)


def conditions(s: Spec) -> dict[str, str]:
    m = _fmt(s.ready_tolerance_m)
    c = {
        "target_detected": "perception.selected_target.valid",
        # the field starts false: wait for the track tree to have run (it declares a loss
        # only after 2.5 s without updates)
        "target_lost": "not perception.selected_target.valid and time_in_state() >= 3 * s",
    }
    if s.formation:
        c["column_ready"] = f"formation.geometry.column_slot_error_m <= {m} * m"
        c["line_ready"] = (
            f"formation.geometry.left_slot_error_m <= {m} * m and "
            f"formation.geometry.right_slot_error_m <= {m} * m"
        )
    if s.withdraw in ("stop", "back"):
        c["too_close"] = (
            "perception.selected_target.valid and perception.selected_target.range_m > 0 * m "
            "and perception.selected_target.range_m < withdraw_threshold_m"
        )
    return c


def events(s: Spec) -> dict[str, dict[str, Any]]:
    e: dict[str, dict[str, Any]] = {
        "cancelled": {"source": "external", "description": "上级取消任务"},
        "shutdown_done": {"source": "tree", "result": "success", "description": "收尾完成"},
    }
    if s.wait_start:
        e["start_order"] = {"source": "external", "description": "上级下达开始命令"}
    if s.lost_hold or s.skeleton == "K6":
        e["reacquire_time_up"] = {
            "after": f"{_fmt(s.reacquire_timeout_s)} s",
            "description": "失联后重新获取时限到",
        }
    if s.search_delay_s:
        e["search_delay_up"] = {
            "after": f"{_fmt(s.search_delay_s)} s",
            "description": "前出到时，开始搜索",
        }
    for ev in s.stop_events:
        e[ev] = {"source": "external", "description": s.event_text.get(ev, "上级请求结束")}
    if s.search_limit_s:
        e["search_time_up"] = {
            "after": f"{_fmt(s.search_limit_s)} s",
            "description": "限时搜索到时",
        }
    return e


def parameters(s: Spec) -> dict[str, dict[str, Any]]:
    p: dict[str, dict[str, Any]] = {}
    if s.withdraw in ("stop", "back"):
        p["withdraw_threshold_m"] = {
            "value": f"{_fmt(s.withdraw_threshold_m or 0)} m",
            "basis": "user_specified" if "withdraw_threshold_m" in s.stated else "default",
            "rationale": "距敌小于该距离即撤离",
        }
    return p


def _mermaid(role: str, mac: Machine, stops: tuple[str, ...] = ()) -> str:
    lines = ["stateDiagram-v2", f"  [*] --> {mac.initial}"]
    for a, b, c in mac.transitions:
        lines.append(f"  {a} --> {b} : {c}")
    for ev in ("cancelled", *stops):
        lines.append(f"  * --> SHUTDOWN : {ev}")
    lines.append("  COMPLETE --> [*]")
    return f"```mermaid {role}\n" + "\n".join(lines) + "\n```"


def _python(role: str, mac: Machine) -> str:
    body = []
    for st in mac.states:
        if st in TERMINAL:
            continue
        body.append(f"def {st}():\n{mac.trees[st]}\n")
    return f"```python {role}\n" + "\n".join(body).rstrip() + "\n```"


# ---------------------------------------------------------------------------- checks
BB_TRUE = {
    "target_detected": {"perception.selected_target.valid": True},
    "target_lost": {"perception.selected_target.valid": False},
    "column_ready": {"formation.geometry.column_slot_error_m": 1.0},
    "line_ready": {
        "formation.geometry.left_slot_error_m": 1.0,
        "formation.geometry.right_slot_error_m": 1.0,
    },
}


def l2_tests(s: Spec, rl: dict[str, tuple[int, Machine, str]]) -> list[dict[str, Any]]:
    ev = events(s)
    out = []
    for role, (_, mac, _) in rl.items():
        for i, (a, b, c) in enumerate(mac.transitions):
            given: dict[str, Any] = {"role": role, "state": a, "time_in_state_s": 1}
            bb: dict[str, Any] = {}
            evs: list[str] = []
            if c in BB_TRUE:
                bb.update(BB_TRUE[c])
                if c == "target_lost":
                    given["time_in_state_s"] = 5
            elif c == "too_close":
                bb.update(
                    {
                        "perception.selected_target.valid": True,
                        "perception.selected_target.range_m": max(
                            1.0, (s.withdraw_threshold_m or 0) - 50
                        ),
                    }
                )
            elif c in ev and ev[c].get("source") == "tree":
                given["tree_status"] = "success"
            elif c in ev and ev[c].get("source") == "external":
                evs.append(c)
            elif c in ev and "after" in ev[c]:
                given["time_in_state_s"] = float(ev[c]["after"].split()[0]) + 1
                evs.append(c)
            # keep competing exits quiet: a valid target while waiting for a timer, etc.
            if c in ("reacquire_time_up",):
                bb.setdefault("perception.selected_target.valid", False)
            if c == "search_time_up":
                bb.setdefault("perception.selected_target.valid", False)
            if c == "target_detected" and a in ("STANDOFF_TRACK", "TRACK"):
                continue
            if c == "target_lost" and s.withdraw in ("stop", "back"):
                bb.setdefault("perception.selected_target.range_m", 0.0)
            given["blackboard"] = bb
            given["events"] = evs
            out.append(
                {
                    "id": f"t_{role}_{i:02d}_{a.lower()}_{b.lower()}",
                    "given": given,
                    "expect": {"next_state": b},
                }
            )
        first = mac.states[0]
        for stop in s.stop_events:  # (``ev`` is the event table above)
            out.append(
                {
                    "id": f"t_{role}_{stop}",
                    "given": {"role": role, "state": first, "time_in_state_s": 1,
                              "blackboard": {}, "events": [stop]},
                    "expect": {"next_state": "SHUTDOWN"},
                }
            )  # fmt: skip
        out.append(
            {
                "id": f"t_{role}_cancel",
                "given": {
                    "role": role,
                    "state": first,
                    "time_in_state_s": 1,
                    "blackboard": {},
                    "events": ["cancelled"],
                },
                "expect": {"next_state": "SHUTDOWN"},
            }
        )
    return out


SENSOR_M = 6000.0  # the 2D sensor reaches as far as the lab radar
SPEED = 8.0  # route speed (m/s)


def l3_scenarios(s: Spec, rl: dict[str, tuple[int, Machine, str]]) -> list[dict[str, Any]]:
    """Scenarios that drive every module (design §4.2), with feasible timings (§4.4)."""
    lead = s.name("leader" if s.formation else "observer")
    mac = rl[lead][1]
    enemy_x = float(SENSOR_M - 1500) if s.formation else 2500.0
    t_start = 10.0 if s.wait_start else 0.0
    # contact: the enemy is inside the sensor range from the start (or as soon as the
    # search begins); time for the column, contact and line ≈ 60 s
    env = {
        "duration_s": 300,
        "step_s": 1,
    }
    vehicles: dict[str, Any] = {}
    if s.formation:
        vehicles[s.name("leader")] = [
            {"position_m": [0, 0], "heading_deg": 90, "max_speed_mps": 10}
        ]
        vehicles[s.name("member")] = [
            {"position_m": [-60, 0], "heading_deg": 90, "max_speed_mps": 10},
            {"position_m": [-120, 0], "heading_deg": 90, "max_speed_mps": 10},
        ]
    else:
        vehicles[s.name("observer")] = [
            {"position_m": [0, 0], "heading_deg": 90, "max_speed_mps": 10}
        ]
    start_ev = [{"at_s": t_start, "event": "start_order"}] if s.wait_start else []
    head = [st for st in mac.states if st in ("WAIT_START", "ASSEMBLE", "ADVANCE")]
    track = (
        ["SEARCH_ADVANCE", "CONTACT_DEPLOY", "STANDOFF_TRACK"]
        if s.formation
        else ["SEARCH", "TRACK"]
    )
    out: list[dict[str, Any]] = []

    def scen(sid: str, hostiles, evs, faults, path, desc, duration=300):
        e = dict(env, duration_s=duration)
        return {
            "id": sid,
            "description": desc,
            "environment": e,
            "vehicles": vehicles,
            "hostiles": hostiles,
            "events": start_ev + evs,
            "faults": faults,
            "expect": [{"id": f"{sid}_path", "role": lead, "visits_in_order": head + path}],
        }

    enemy = {"id": "e1", "position_m": [enemy_x, 0], "velocity_mps": [0, 0], "appear_s": 0}
    # main path: contact, track, cancel
    out.append(
        scen(
            "main_cancel",
            # the enemy shows up after a minute: the column advances first
            [dict(enemy, appear_s=t_start + 60.0)],
            [{"at_s": 200, "event": "cancelled"}],
            [],
            track + ["SHUTDOWN", "COMPLETE"],
            "发现目标、跟踪，随后上级取消",
        )
    )
    # no enemy: never leaves the search (or gives up after the limit)
    if s.search_limit_s:
        dur = int(s.search_limit_s + t_start + 120)
        first = "SEARCH_ADVANCE" if s.formation else "SEARCH"
        out.append(
            scen(
                "search_time_up",
                # an enemy far behind, outside the sensor range all along (an empty list
                # would get the binding's default enemy)
                [
                    {
                        "id": "far",
                        "position_m": [-20000.0, 0.0],
                        "velocity_mps": [0, 0],
                        "appear_s": 0,
                    }
                ],
                [],
                [],
                [first, "SHUTDOWN", "COMPLETE"],
                "无敌情，限时搜索到时后收队",
                duration=dur,
            )
        )
    # target lost
    lost_at = 150.0
    gone = dict(enemy, remove_s=lost_at)
    if s.lost_hold or not s.formation:
        hold = "TARGET_LOST_HOLD" if s.formation else "REACQUIRE"
        back = dict(enemy, id="e2", appear_s=lost_at + max(5.0, s.reacquire_timeout_s / 3))
        out.append(
            scen(
                "lost_reacquired",
                [gone, back],
                [],
                [],
                track + [hold, track[-1]],
                "目标丢失后在时限内重新出现",
            )
        )
        after = (
            ["RESUME_SEARCH" if s.formation else "SEARCH"]
            if s.on_timeout == "resume" or not s.formation
            else ["SHUTDOWN", "COMPLETE"]
        )
        out.append(
            scen(
                "lost_timeout",
                [gone],
                [],
                [],
                track + [hold] + after,
                "目标丢失后超时仍未重新获取",
                duration=int(lost_at + s.reacquire_timeout_s + 120),
            )
        )
    elif s.formation:
        out.append(
            scen("lost_end", [gone], [], [], track + ["SHUTDOWN", "COMPLETE"], "目标丢失后结束任务")
        )
    # the enemy advances into the band after we track it; in AFSIM the standoff step backs
    # off and keep_off holds. The 2D model does not move a standoff leader (tool gap E5), so
    # in 2D the enemy stops at the band's lower edge plus a margin and the tracking holds
    if s.withdraw == "keep":
        speed = 6.0
        start = 120.0
        stop_x = s.desired_range_m - s.range_tolerance_m + 150
        travel = max(0.0, (enemy_x - stop_x) / speed)
        adv = dict(
            enemy,
            velocity_mps=[-speed, 0],
            move_start_s=start,
            move_stop_s=round(start + travel, 1),
        )
        out.append(
            scen(
                "enemy_advances",
                [adv],
                [{"at_s": int(start + travel + 120), "event": "cancelled"}],
                [],
                track + ["SHUTDOWN", "COMPLETE"],
                "跟踪中敌方推进，长车后撤保持距离，随后上级取消",
                duration=int(start + travel + 300),
            )
        )
    if s.withdraw in ("stop", "back"):
        thr = s.withdraw_threshold_m or 0
        speed = 15.0  # faster than the leader backs off (2D leader 10 m/s at most)
        start = 120.0
        stop_x = thr - 200 if s.withdraw == "stop" else thr - 100
        travel = (enemy_x - stop_x) / speed
        adv = dict(
            enemy,
            velocity_mps=[-speed, 0],
            move_start_s=start,
            move_stop_s=round(start + travel, 1),
        )
        path = track + (["SHUTDOWN", "COMPLETE"] if s.withdraw == "stop" else ["WITHDRAW"])
        out.append(
            scen(
                "enemy_advances",
                [adv],
                [],
                [],
                path,
                "跟踪中敌方推进到撤离阈值以内",
                duration=int(start + travel + 200),
            )
        )
    return out


def obligations(s: Spec) -> list[dict[str, Any]]:
    out = [
        {
            "id": "cancel_shutdown",
            "kind": "response",
            "description": "收到上级取消后 30 s 内关闭搜索",
            "on_event": "cancelled",
            "action": "perception.stop_search",
            "within_s": 30.0,
        },
    ]
    if s.formation:
        # the column must really advance before contact (judged on positions: in 2D only the
        # leader moves, the followers' slots are not simulated vehicles)
        out.append(
            {
                "id": "leader_advances",
                "kind": "advances",
                "description": "长车沿前出路线实际前出",
                "roles": ["leader"],
                "min_m": 150.0,
            }
        )
        out.append(
            {
                "id": "stop_before_sensor_off",
                "kind": "precedes",
                "description": "先停车，再关闭搜索",
                "first": "movement.stop",
                "then": "perception.stop_search",
                "roles": ["leader"],
            }
        )
        # stop-and-shut-down promises no distance; backing off may dip below the threshold
        # for a moment when it triggers; holding the band keeps its lower edge
        if s.withdraw != "stop":
            floor = (
                (s.withdraw_threshold_m or 0) * 0.7
                if s.withdraw == "back"
                else (s.desired_range_m - s.range_tolerance_m) * 0.9
            )
            out.append(
                {
                    "id": "keep_off",
                    "kind": "keeps_distance",
                    "description": "长车与敌保持距离，不进入保持距离带下沿（撤离时为撤离阈值）以内",
                    "roles": ["leader"],
                    "min_m": float(round(floor)),
                }
            )
    return out


def sheet(s: Spec, extra: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    """The tactic sheet and its request; ``extra`` replaces / adds model-block fields
    (display name, implements, parameters: drafting from a skeleton)."""
    rl = roles(s)
    slices = ["formation_standoff", "perception", "movement"] if s.formation else [
        "perception", "movement"
    ]  # fmt: skip
    if s.share_target:
        slices.append("communication_target_sharing")
    model = {
        "model": {"display_name": f"试点模型 {s.model_id}", "intent": "生成的编队/感知战术模型"},
        "slices": slices,
        "roles": {
            r: {"capability_class": "ground_vehicle", "count": n, "description": d}
            for r, (n, _, d) in rl.items()
        },
        "events": events(s),
    }
    params = parameters(s)
    if params:
        model["parameters"] = params
    for k, v in (extra or {}).items():
        if k == "parameters":
            model.setdefault("parameters", {}).update(v)
        elif v is not None:
            model[k] = v
    ver = {"tests": l2_tests(s, rl), "scenarios": l3_scenarios(s, rl)}
    parts = [
        f"# 战术稿：{s.model_id}",
        "```yaml model\n" + yaml.safe_dump(model, allow_unicode=True, sort_keys=False) + "```",
    ]
    for r, (_, mac, _) in rl.items():
        parts.append(_mermaid(r, mac, tuple(s.stop_events)))
    parts.append(
        "```python conditions\n"
        + "\n".join(f"{k} = {v}" for k, v in conditions(s).items())
        + "\n```"
    )
    for r, (_, mac, _) in rl.items():
        parts.append(_python(r, mac))
    parts.append(
        "```yaml verification\n" + yaml.safe_dump(ver, allow_unicode=True, sort_keys=False) + "```"
    )
    request = {
        "requirement": "（逆向配文前的占位）",
        "model_id": s.model_id,
        "roles": {
            r: {"capability_class": "ground_vehicle", "count": n} for r, (n, _, _) in rl.items()
        },
        # the interface a requirement with an advance implies: post, ahead, route approach
        "scenario_interface": _interface(s),
        "scenario_events": {
            k: v["description"] for k, v in events(s).items() if v.get("source") == "external"
        },
        "obligations": obligations(s),
        "extract_obligations": False,
    }
    return "\n\n".join(parts) + "\n", request


def _interface(s: Spec) -> dict[str, Any]:
    from afsim_research_agent.ir.v2.scenario_interface import derive

    text = "沿路线搜索前进" if s.formation else "原地观察警戒"
    return derive(text).model_dump(mode="json")


def build(s: Spec, out_dir: Path) -> dict[str, Any]:
    """Write the sheet and request, compile and check offline; returns the result row."""
    from afsim_research_agent.falcon.v3.draft import compile_package

    work = out_dir / s.model_id
    work.mkdir(parents=True, exist_ok=True)
    text, req = sheet(s)
    (work / "tactic_sheet.md").write_text(text, encoding="utf-8")
    (work / "falcon_request.yaml").write_text(
        yaml.safe_dump(req, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    (work / "gen.json").write_text(
        json.dumps(asdict(s), ensure_ascii=False, indent=1), encoding="utf-8"
    )
    pkg = work / "pkg"
    if pkg.exists():
        import shutil

        shutil.rmtree(pkg)
    res = compile_package(work / "tactic_sheet.md", work / "falcon_request.yaml", pkg)
    c = res.check
    row = {
        "id": s.model_id,
        "skeleton": s.skeleton,
        "status": res.status,
        "l1": c.l1_ok,
        "problems": [f"{p.code} {p.message}"[:240] for p in c.problems][:8]
        if hasattr(c.problems[0] if c.problems else None, "code")
        else [str(p)[:240] for p in c.problems][:8],
        "l2": [sum(1 for t in c.l2 if t["passed"]), len(c.l2)],
        "l3": [[x["id"], x["passed"], (x.get("detail") or x.get("message") or "")] for x in c.l3],
        "l2_fail": [
            [t["id"], t.get("detail") or t.get("message") or t.get("actual")]
            for t in c.l2
            if not t["passed"]
        ][:6],
    }
    (work / "result.json").write_text(
        json.dumps(row, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )
    return row
