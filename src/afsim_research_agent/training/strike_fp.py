"""Strike skeletons: centralized allocation by the platoon leader, engagement by the members
(capability set slices threat_allocation, engagement, perception).

The structure follows what passed in AFSIM (the hand-written reference and the r7 draft,
``handoff/2026-10-30-capability-batch2.md`` §4) and what the 2D team models enforce
(``ir/v2/sim/team.py``):

- the leader runs the whole allocation pipeline on every update (collect targets, score,
  collect members, filter, build candidates, allocate, broadcast), so a task never outlives
  its 3 s validity between two broadcasts;
- a member goes WAITING_TASK → VALIDATING → SELECTING → FIRING → OBSERVING and back, every
  step a tree whose success moves it on and whose failure (task expired, line of fire
  blocked, out of range, target destroyed, shot limit) takes it back to WAITING_TASK —
  every state has a way out;
- every role switches its search sensor on first (PREPARE) and on the end request ceases
  fire and switches it off (CEASE → COMPLETE).

Modules: ``reattack`` (observe → validate again, or one engagement per task), ``wait_start``
(the leader waits for a start order), ``engage_limit_s`` (the leader stops allocating after
this long), ``cease_order`` (an external cease-fire order ends the engagement for all).
``sheet(spec)`` writes a tactic sheet with its L2 tests, 2D scenarios and obligations.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

import yaml

ALLOCATE = (
    "threat_allocation.collect_fused_targets",
    "threat_allocation.score_threats",
    "threat_allocation.collect_member_status",
    "threat_allocation.filter_eligible_members",
    "threat_allocation.build_allocation_candidates",
    "threat_allocation.allocate_targets",
    "threat_allocation.broadcast_assignments",
)
VALIDATE = (
    "threat_allocation.adopt_assignment",
    "engagement.validate_engagement_task",
    "engagement.collect_weapon_status",
    "engagement.evaluate_fire_safety",
)
SELECT = (
    "engagement.filter_weapon_candidates",
    "engagement.score_weapon_candidates",
    "engagement.select_weapon",
)
FIRE = ("engagement.aim_selected_weapon", "engagement.fire_selected_weapon")
OBSERVE = ("engagement.observe_weapon_effect", "engagement.evaluate_reattack")
OBSERVE_ONCE = ("engagement.observe_weapon_effect",)
# the task ends: stop shooting, drop the task (and stop assigning), switch the sensor off
CEASE_TREE = (
    "    engagement.cease_fire()\n"
    "    threat_allocation.expire_assignment()\n"
    "    perception.stop_search()"
)


@dataclass
class StrikeSpec:
    skeleton: str = "S1"
    reattack: bool = True
    wait_start: bool = False
    engage_limit_s: float | None = None
    cease_order: str | None = None  # external event id of a cease-fire order
    destroy: bool = False  # the requirement asks for the enemy destroyed (effect obligation)
    names: dict[str, str] = field(default_factory=dict)  # leader / member → role
    event_text: dict[str, str] = field(default_factory=dict)
    members: int = 2

    @property
    def model_id(self) -> str:
        data = asdict(self)
        if not data["destroy"]:
            del data["destroy"]  # ids of the specs from before the module stay as they were
        h = hashlib.sha1(json.dumps(data, sort_keys=True).encode()).hexdigest()[:8]
        return f"st_{self.skeleton.lower()}_{h}"

    def name(self, role: str) -> str:
        return self.names.get(role, role)

    @property
    def stops(self) -> tuple[str, ...]:
        """Events that end the task for every role (→ CEASE → COMPLETE)."""
        return ("cancelled",)

    @property
    def holds(self) -> tuple[str, ...]:
        """Events that stop the shooting for every role but keep the task (→ HOLD, until
        the task ends): a cease-fire order."""
        return (self.cease_order,) if self.cease_order else ()

    @property
    def has_hold(self) -> bool:
        return bool(self.engage_limit_s or self.cease_order)


def _seq(actions: tuple[str, ...]) -> str:
    return "\n".join(f"    {a}()" for a in actions)


@dataclass
class Machine:
    states: list[str]
    transitions: list[tuple[str, str, str]]
    trees: dict[str, str]


def leader_machine(s: StrikeSpec) -> Machine:
    states, T, trees = [], [], {}
    if s.wait_start:
        states.append("WAIT_START")
        trees["WAIT_START"] = "    perception.start_search()"
        T.append(("WAIT_START", "PREPARE", "start_order"))
    states += ["PREPARE", "ALLOCATING"] + (["HOLD"] if s.has_hold else []) + ["CEASE", "COMPLETE"]
    trees["PREPARE"] = "    perception.start_search()"
    T.append(("PREPARE", "ALLOCATING", "leader_ready"))
    # the whole pipeline on every update: a fresh broadcast well within the task validity
    trees["ALLOCATING"] = _seq(ALLOCATE)
    if s.has_hold:
        # no more tasks, hold fire until the task ends (cancelled → CEASE → COMPLETE)
        trees["HOLD"] = "    engagement.cease_fire()\n    threat_allocation.expire_assignment()"
    if s.engage_limit_s:
        T.append(("ALLOCATING", "HOLD", "engage_time_up"))
    trees["CEASE"] = CEASE_TREE
    T.append(("CEASE", "COMPLETE", "leader_ceased"))
    return Machine(states, T, trees)


def member_machine(s: StrikeSpec) -> Machine:
    states = ["PREPARE", "WAITING_TASK", "VALIDATING", "SELECTING", "FIRING", "OBSERVING"]
    states += (["HOLD"] if s.cease_order else []) + ["CEASE", "COMPLETE"]
    trees = {
        "PREPARE": "    perception.start_search()",
        # hold fire while waiting; clears a task whose validity ran out
        "WAITING_TASK": "    engagement.cease_fire()\n    threat_allocation.expire_assignment()",
        "VALIDATING": _seq(VALIDATE),
        "SELECTING": _seq(SELECT),
        "FIRING": _seq(FIRE),
        "OBSERVING": _seq(OBSERVE if s.reattack else OBSERVE_ONCE),
        "CEASE": CEASE_TREE,
    }
    if s.cease_order:
        trees["HOLD"] = "    engagement.cease_fire()\n    threat_allocation.expire_assignment()"
    T = [
        ("PREPARE", "WAITING_TASK", "member_ready"),
        ("WAITING_TASK", "VALIDATING", "task_received"),
        ("VALIDATING", "SELECTING", "task_valid"),
        ("VALIDATING", "WAITING_TASK", "task_rejected"),
        ("SELECTING", "FIRING", "weapon_selected"),
        ("SELECTING", "WAITING_TASK", "no_weapon"),
        ("FIRING", "OBSERVING", "shot_fired"),
        ("FIRING", "WAITING_TASK", "fire_failed"),
    ]
    if s.reattack:
        T += [
            ("OBSERVING", "VALIDATING", "reattack"),
            ("OBSERVING", "WAITING_TASK", "engagement_done"),
        ]
    else:
        T += [("OBSERVING", "WAITING_TASK", "engagement_done")]
    T.append(("CEASE", "COMPLETE", "member_ceased"))
    return Machine(states, T, trees)


def roles(s: StrikeSpec) -> dict[str, tuple[int, Machine, str]]:
    return {
        s.name("leader"): (
            1,
            leader_machine(s),
            "排长车；每拍完成一次目标分配并下发任务，不直接开火",
        ),
        s.name("member"): (
            s.members,
            member_machine(s),
            "成员车；按收到的任务校验、选武器、瞄准、开火、观察毁伤",
        ),
    }


TREE_OK = {
    "leader_ready": "传感器已开机",
    "member_ready": "传感器已开机",
    "task_valid": "任务有效、武器可用、射线安全",
    "weapon_selected": "已选定武器",
    "shot_fired": "已开火",
    "reattack": "未摧毁且允许重攻",
    "leader_ceased": "停火收尾完成",
    "member_ceased": "停火收尾完成",
}
TREE_FAIL = {
    "task_rejected": "任务过期或无效、无弹药、射线上有友车",
    "no_weapon": "没有可用武器（射程外、未装填、弹在飞行中）",
    "fire_failed": "瞄准或开火失败（任务过期、开火上限、射程外）",
}


def conditions(s: StrikeSpec) -> dict[str, str]:
    # the radio callback sets the field; wait a second in the state so a rejected task is
    # not retried in the same update
    return {"task_received": "allocation.received_assignment.valid and time_in_state() >= 1 * s"}


def events(s: StrikeSpec) -> dict[str, dict[str, Any]]:
    e: dict[str, dict[str, Any]] = {
        "cancelled": {
            "source": "external",
            "description": s.event_text.get("cancelled", "上级结束任务"),
        }
    }
    if s.wait_start:
        e["start_order"] = {
            "source": "external",
            "description": s.event_text.get("start_order", "上级下达开始命令"),
        }
    if s.cease_order:
        e[s.cease_order] = {
            "source": "external",
            "description": s.event_text.get(s.cease_order, "上级停火命令"),
        }
    for k, d in TREE_OK.items():
        if k == "reattack" and not s.reattack:
            continue
        e[k] = {"source": "tree", "result": "success", "description": d}
    for k, d in TREE_FAIL.items():
        e[k] = {"source": "tree", "result": "failure", "description": d}
    e["engagement_done"] = {
        "source": "tree",
        "result": "failure" if s.reattack else "success",
        "description": "目标已摧毁、达到开火上限或任务过期"
        if s.reattack
        else "已观察毁伤，本次任务结束",
    }
    if s.engage_limit_s:
        e["engage_time_up"] = {"after": f"{s.engage_limit_s:g} s", "description": "交战时限到"}
    return e


def _mermaid(role: str, mac: Machine, s: StrikeSpec) -> str:
    lines = ["stateDiagram-v2", f"  [*] --> {mac.states[0]}"]
    lines += [f"  {a} --> {b} : {c}" for a, b, c in mac.transitions]
    lines += [f"  * --> HOLD : {ev}" for ev in s.holds]
    lines += [f"  * --> CEASE : {ev}" for ev in s.stops]
    lines.append("  COMPLETE --> [*]")
    return f"```mermaid {role}\n" + "\n".join(lines) + "\n```"


def _python(role: str, mac: Machine) -> str:
    body = [f"def {st}():\n{mac.trees[st]}\n" for st in mac.states if st != "COMPLETE"]
    return f"```python {role}\n" + "\n".join(body).rstrip() + "\n```"


def l2_tests(s: StrikeSpec, rl: dict[str, tuple[int, Machine, str]]) -> list[dict[str, Any]]:
    ev = events(s)
    out = []
    for role, (_, mac, _) in rl.items():
        for i, (a, b, c) in enumerate(mac.transitions):
            given: dict[str, Any] = {"role": role, "state": a, "time_in_state_s": 2}
            bb: dict[str, Any] = {}
            evs: list[str] = []
            spec = ev.get(c) or {}
            if c == "task_received":
                bb["allocation.received_assignment.valid"] = True
            elif spec.get("source") == "tree":
                # "the tree ended that way" for the given state only: tree_status would be
                # overwritten by the stubbed (successful) tick and the reattack cycle followed
                evs.append(c)
            elif spec.get("source") == "external":
                evs.append(c)
            elif "after" in spec:
                given["time_in_state_s"] = float(spec["after"].split()[0]) + 1
                evs.append(c)
            given["blackboard"] = bb
            given["events"] = evs
            out.append(
                {
                    "id": f"t_{role}_{i:02d}_{a.lower()}_{b.lower()}",
                    "given": given,
                    "expect": {"next_state": b},
                }
            )
        for stop, to in [(x, "CEASE") for x in s.stops] + [(x, "HOLD") for x in s.holds]:
            out.append(
                {
                    "id": f"t_{role}_{stop}",
                    "given": {"role": role, "state": mac.states[0], "time_in_state_s": 1,
                              "blackboard": {}, "events": [stop]},
                    "expect": {"next_state": to},
                }
            )  # fmt: skip
    return out


def l3_scenarios(s: StrikeSpec) -> list[dict[str, Any]]:
    lead, mem = s.name("leader"), s.name("member")
    # side by side across the direction of the enemy: no friend on a line of fire
    vehicles = {
        lead: [{"position_m": [0, 0], "heading_deg": 90, "max_speed_mps": 10}],
        mem: [
            {"position_m": [0, 150 * (1 if k % 2 == 0 else -1) * (1 + k // 2)], "heading_deg": 90,
             "max_speed_mps": 10}
            for k in range(s.members)
        ],
    }  # fmt: skip
    hostiles = [
        {"id": "e1", "position_m": [1500, 250]},
        {"id": "e2", "position_m": [1500, -250]},
    ]
    t0 = 10.0 if s.wait_start else 0.0
    start = [{"at_s": t0, "event": "start_order"}] if s.wait_start else []
    head = ["WAIT_START"] if s.wait_start else []
    engage = ["WAITING_TASK", "VALIDATING", "SELECTING", "FIRING", "OBSERVING"]

    def scen(sid, desc, hostiles_, evs, expect, faults=(), duration=90):
        return {
            "id": sid,
            "description": desc,
            "environment": {"duration_s": duration, "step_s": 1},
            "vehicles": vehicles,
            "hostiles": hostiles_,
            "events": start + evs,
            "faults": list(faults),
            "expect": expect,
        }

    end = t0 + 60
    out = [
        scen(
            "engage_two_targets",
            "两个目标在射程内：排长分配，两车各自开火，上级结束后停火收尾",
            hostiles,
            [{"at_s": end, "event": "cancelled"}],
            [
                {"id": "leader_path", "role": lead,
                 "visits_in_order": head + ["PREPARE", "ALLOCATING", "CEASE", "COMPLETE"]},
                {"id": "member0_path", "role": mem, "instance": 0,
                 "visits_in_order": ["PREPARE", *engage, "CEASE", "COMPLETE"]},
                {"id": "member1_path", "role": mem, "instance": 1,
                 "visits_in_order": ["PREPARE", *engage, "CEASE", "COMPLETE"]},
            ],
        ),
        scen(
            "no_targets",
            "没有目标：成员车一直等待任务，不开火",
            [],
            [{"at_s": t0 + 30, "event": "cancelled"}],
            [
                {"id": "member_waits", "role": mem, "visits_in_order": ["WAITING_TASK", "CEASE"]},
                {"id": "member_never_fires", "role": mem, "never": "FIRING"},
            ],
            duration=60,
        ),
        scen(
            "member_radio_lost",
            "一辆成员车电台故障：它收不到任务、不开火；另一辆照常交战",
            hostiles,
            [{"at_s": end, "event": "cancelled"}],
            [
                {"id": "lost_never_fires", "role": mem, "instance": 1, "never": "FIRING"},
                {"id": "other_fires", "role": mem, "instance": 0,
                 "visits_in_order": ["WAITING_TASK", "FIRING", "CEASE"]},
            ],
            faults=[{"at_s": 0, "role": mem, "instance": 1, "radio_operational": False}],
        ),
    ]  # fmt: skip
    if s.engage_limit_s:
        out.append(
            scen(
                "engage_time_limit",
                "交战时限到：排长停止分配、保持停火，上级结束后收尾",
                hostiles,
                [{"at_s": t0 + s.engage_limit_s + 30, "event": "cancelled"}],
                [{"id": "leader_stops", "role": lead,
                  "visits_in_order": ["ALLOCATING", "HOLD"],
                  "within_s": t0 + s.engage_limit_s + 10},
                 {"id": "leader_ends", "role": lead,
                  "visits_in_order": ["HOLD", "CEASE", "COMPLETE"]}],
                duration=int(t0 + s.engage_limit_s + 60),
            )
        )  # fmt: skip
    if s.cease_order:
        out.append(
            scen(
                "cease_order",
                "交战中收到停火命令：全部停火待命，上级结束后收尾",
                hostiles,
                [{"at_s": t0 + 25, "event": s.cease_order},
                 {"at_s": t0 + 45, "event": "cancelled"}],
                [{"id": "leader_ceases", "role": lead,
                  "visits_in_order": ["ALLOCATING", "HOLD", "CEASE", "COMPLETE"]},
                 {"id": "member_ceases", "role": mem,
                  "visits_in_order": ["FIRING", "HOLD", "CEASE", "COMPLETE"]}],
                duration=int(t0 + 60),
            )
        )  # fmt: skip
    return out


def obligations(s: StrikeSpec) -> list[dict[str, Any]]:
    out = [
        {
            "id": "allocate_before_fire",
            "kind": "precedes",
            "description": "先由排长下发任务，成员车再开火",
            "first": "threat_allocation.broadcast_assignments",
            "then": "engagement.fire_selected_weapon",
        },
        {
            "id": "cease_on_cancel",
            "kind": "response",
            "description": "收到结束请求后 10 s 内停火",
            "on_event": "cancelled",
            "action": "engagement.cease_fire",
            "within_s": 10.0,
        },
    ]
    if s.destroy:
        out.append(
            {
                "id": "enemy_destroyed",
                "kind": "destroys",
                "description": "成员车交战直至敌方车辆全部被摧毁",
                "roles": [s.name("member")],
            }
        )
    if s.cease_order:
        out.append(
            {
                "id": "cease_on_order",
                "kind": "response",
                "description": "收到停火命令后 10 s 内停火",
                "on_event": s.cease_order,
                "action": "engagement.cease_fire",
                "within_s": 10.0,
            }
        )
    return out


def sheet(s: StrikeSpec, extra: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    """The tactic sheet and its request (``extra``: model-block fields to add/replace)."""
    rl = roles(s)
    model: dict[str, Any] = {
        "model": {
            "display_name": f"打击骨架 {s.model_id}",
            "intent": "排长集中分配目标，成员车交战",
        },
        "slices": ["threat_allocation", "engagement", "perception"],
        "roles": {
            r: {"capability_class": "ground_vehicle", "count": n, "description": d}
            for r, (n, _, d) in rl.items()
        },
        "events": events(s),
    }
    for k, v in (extra or {}).items():
        if v is not None:
            model[k] = v
    ver = {"tests": l2_tests(s, rl), "scenarios": l3_scenarios(s)}
    parts = [
        f"# 战术稿：{s.model_id}",
        "```yaml model\n" + yaml.safe_dump(model, allow_unicode=True, sort_keys=False) + "```",
    ]
    parts += [_mermaid(r, mac, s) for r, (_, mac, _) in rl.items()]
    parts.append(
        "```python conditions\n"
        + "\n".join(f"{k} = {v}" for k, v in conditions(s).items())
        + "\n```"
    )
    parts += [_python(r, mac) for r, (_, mac, _) in rl.items()]
    parts.append(
        "```yaml verification\n" + yaml.safe_dump(ver, allow_unicode=True, sort_keys=False) + "```"
    )
    request = {
        "requirement": "（逆向配文前的占位）",
        "model_id": s.model_id,
        "roles": {
            r: {"capability_class": "ground_vehicle", "count": n} for r, (n, _, _) in rl.items()
        },
        "scenario_events": {
            k: v["description"] for k, v in events(s).items() if v.get("source") == "external"
        },
        "extract_obligations": False,
        "obligations": obligations(s),
    }
    return "\n\n".join(parts) + "\n", request


def catalog() -> list[StrikeSpec]:
    """The batch of strike skeletons to verify: the base and each module on its own, and
    all modules together."""
    return [
        StrikeSpec("S1"),
        StrikeSpec("S1", reattack=False),
        StrikeSpec("S1", wait_start=True),
        StrikeSpec("S1", engage_limit_s=40),
        StrikeSpec("S1", cease_order="cease_fire_order"),
        StrikeSpec("S1", wait_start=True, engage_limit_s=40, cease_order="cease_fire_order"),
        StrikeSpec("S1", destroy=True),
    ]
