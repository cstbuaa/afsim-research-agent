"""Lock-step co-simulation: our controllers decide, an external engine simulates.

Per step: scenario events -> message delivery -> every controller ticks against
the latest observation -> commands and messages go to the engine -> engine steps
-> the new observation is recorded. The record is judged with the same L3
expectations as the built-in simulation.
"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from afsim_research_agent.ir.catalog import load_catalog
from afsim_research_agent.ir.cosim.engine import (
    Command,
    EngineAdapter,
    EntityObservation,
    EntitySetup,
    Observation,
    OutgoingMessage,
    ScenarioSetup,
)
from afsim_research_agent.ir.l2 import defaults_from_catalog
from afsim_research_agent.ir.runtime import Delivery, RoleController
from afsim_research_agent.ir.sim.l3 import Sample, ScenarioResult, evaluate_trace
from afsim_research_agent.ir.sim.world import _q, load_terrain

MOVEMENT_COMMANDS = {
    "move_to_position",
    "move_to_platform",
    "move_to_track",
    "follow_route",
    "follow_route_with_offset",
    "bound_to_position",
}


@dataclass
class EngineMapping:
    """``bindings/<engine>.json``: entity ids and engine bindings for capabilities."""

    entities: dict[str, str] = field(default_factory=dict)  # "role[i]" -> engine id
    capabilities: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None) -> EngineMapping:
        if path is None or not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(dict(data.get("entities", {})), dict(data.get("capabilities", {})))

    def engine_id(self, role: str, instance: int) -> str:
        return self.entities.get(f"{role}[{instance}]", f"{role}_{instance}")


@dataclass
class _Ent:
    key: str
    role: str
    instance: int
    engine_id: str
    controller: RoleController
    formation_id: str = ""
    reserve: float = 0.0
    mover_override: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Pending:
    deliver_at: float
    sent_at: float
    recipient: str
    delivery: Delivery


class CoSimRunner:
    def __init__(
        self,
        ir: dict[str, Any],
        engine: EngineAdapter,
        scenario: Mapping[str, Any],
        *,
        mapping: EngineMapping | None = None,
        bindings: dict[str, Any] | None = None,
        package_dir: Path | None = None,
    ) -> None:
        self.ir = ir
        self.engine = engine
        self.scenario = dict(scenario)
        self.mapping = mapping or EngineMapping()
        self.bindings = bindings or {"roles": {r["id"]: {"components": {}} for r in ir["roles"]}}
        self.package_dir = package_dir
        env = self.scenario["environment"]
        self.env = env
        self.duration = _q(env["duration"])
        self.dt = _q(env.get("time_step"), default=ir["execution"]["update_interval_s"])
        self.seed = int(env.get("random_seed", 0))
        self.rng = random.Random(self.seed)
        self.roles = {r["id"]: r for r in ir["roles"]}
        self.formations = {f["id"]: f for f in ir["formations"]}
        self.messages = {m["id"]: m for m in ir["messages"]}
        self.routes = {
            k: [tuple(map(float, p)) for p in v] for k, v in (env.get("routes") or {}).items()
        }
        self.waypoints = {k: tuple(map(float, v)) for k, v in (env.get("waypoints") or {}).items()}
        self.defaults = defaults_from_catalog(load_catalog())
        self.entities: list[_Ent] = []
        for role in ir["roles"]:
            for i in range(role["count"]):
                ctl = RoleController(ir, role["id"], i, seed=self.seed)
                self.entities.append(
                    _Ent(
                        f"{role['id']}[{i}]",
                        role["id"],
                        i,
                        self.mapping.engine_id(role["id"], i),
                        ctl,
                    )
                )
        self.by_engine = {e.engine_id: e for e in self.entities}
        self.obs: Observation | None = None
        self.queue: list[_Pending] = []
        self.comm_loss: list[tuple[float, float, float, str | None]] = []
        self.event_times: dict[str, float] = {}
        self.sent_log: list[tuple[float, str, str]] = []
        self.action_log: list[tuple[float, str, str]] = []
        self.detect_time: dict[str, float] = {}
        self.warnings: list[str] = []
        self.tick_index = 0
        self.trace_lines: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ setup
    def _leader_role(self, role_id: str) -> str | None:
        for coord in self.ir["coordination"]:
            if coord["pattern"] == "leader_follower" and coord.get("followers") == role_id:
                return coord["leader"]
        return self.roles[role_id]["command"].get("superior")

    def _setup(self) -> ScenarioSetup:
        initial = self.scenario.get("initial") or {}
        placed: dict[str, tuple[float, float, float, float]] = {}
        for role in self.ir["roles"]:
            init = initial.get(role["id"], {})
            for i in range(role["count"]):
                if "position" in init:
                    x, y = (float(v) for v in init["position"])
                else:
                    x, y = i * 20.0, 0.0
                placed[f"{role['id']}[{i}]"] = (
                    x,
                    y,
                    float(init.get("heading_deg", 90.0)),
                    _q(init.get("speed"), "speed"),
                )
        for role in self.ir["roles"]:
            init = initial.get(role["id"], {})
            formation = init.get("formation")
            leader_role = self._leader_role(role["id"])
            if formation and leader_role and not ("position" in init and role["count"] == 1):
                lx, ly, lh, _ = placed[f"{leader_role}[0]"]
                for i in range(role["count"]):
                    pos = slot_position(self.formations.get(formation), role["id"], i, lx, ly, lh)
                    if pos is not None:
                        placed[f"{role['id']}[{i}]"] = (
                            pos[0],
                            pos[1],
                            lh,
                            placed[f"{role['id']}[{i}]"][3],
                        )
                for ent in self.entities:
                    if ent.role == role["id"]:
                        ent.formation_id = formation
            for ent in self.entities:
                if ent.role == role["id"] and init.get("state"):
                    ent.controller.state = init["state"]
        setups = []
        for ent in self.entities:
            x, y, h, v = placed[ent.key]
            comps = self.bindings["roles"].get(ent.role, {}).get("components", {})
            setups.append(
                EntitySetup(
                    ent.engine_id,
                    ent.role,
                    ent.instance,
                    self.roles[ent.role]["capability_class"],
                    comps,
                    (x, y),
                    h,
                    v,
                )
            )
        terrain = self.env.get("terrain")
        if terrain is None and self.env.get("terrain_map"):
            loaded = load_terrain(self.env, self.package_dir)
            terrain = {
                "default_class": loaded.default_class,
                "regions": [
                    {
                        "class": r.cls,
                        "x_min": r.x_min,
                        "x_max": r.x_max,
                        "y_min": r.y_min,
                        "y_max": r.y_max,
                    }
                    for r in loaded.regions
                ],
            }
        return ScenarioSetup(
            self.scenario,
            setups,
            self.routes,
            self.waypoints,
            terrain,
            self.dt,
            self.duration,
            self.seed,
        )

    # ------------------------------------------------------------------ run
    def run(self, trace_path: Path | None = None) -> ScenarioResult:
        """Run the scenario; optionally write the run trace (JSON Lines) for later judging."""
        self.trace_lines = [
            {
                "type": "header",
                "model": self.ir["model"]["id"],
                "ir_hash": self.ir["ir_hash"],
                "scenario": self.scenario["id"],
                "entities": {e.engine_id: e.key for e in self.entities},
            }
        ]
        self.obs = self.engine.reset(self._setup())
        samples: list[Sample] = []
        try:
            while self.obs.time <= self.duration + 1e-9:
                self._tick()
                self.obs = self.engine.step(self.dt)
                self.tick_index += 1
                samples.append(self._sample())
                self.trace_lines.append(self._step_line())
        finally:
            self.engine.close()
        if trace_path is not None:
            from afsim_research_agent.ir.cosim.trace import write_trace

            write_trace(trace_path, self.trace_lines)
        return evaluate_trace(self.ir, self.scenario, self, samples, duration_s=self.duration)

    @staticmethod
    def _sample_hostile(ob: EntityObservation) -> float:
        return min(
            (
                t.range_m if t.range_m is not None else math.hypot(t.x - ob.x, t.y - ob.y)
                for t in ob.tracks
                if t.hostile
            ),
            default=math.inf,
        )

    def _step_line(self) -> dict[str, Any]:
        assert self.obs is not None
        entities = {}
        for ent in self.entities:
            ob = self.obs.entities.get(ent.engine_id)
            if ob is None:
                continue
            entities[ent.engine_id] = {
                "x": ob.x,
                "y": ob.y,
                "speed": ob.speed_mps,
                "heading": ob.heading_deg,
                "state": ent.controller.state,
                "formation": ent.formation_id,
                "nearest_hostile_m": next(
                    (v for v in [self._sample_hostile(ob)] if math.isfinite(v)),
                    None,
                ),
            }
        return {"type": "step", "time": self.obs.time, "entities": entities}

    def _tick(self) -> None:
        assert self.obs is not None
        now = self.obs.time
        self._events(now)
        commands: list[Command] = []
        outgoing: list[OutgoingMessage] = []
        for ent in self.entities:
            ob = self.obs.entities.get(ent.engine_id)
            if ob is None:
                continue
            inbox = self._inbox(ent, ob, now)
            view = ObservationWorld(self, ent, ob)
            result = ent.controller.tick(now, view, inbox)
            commands += view.commands
            for msg in result.sent_messages:
                self.sent_log.append((now, ent.key, msg.message))
                self.trace_lines.append(
                    {
                        "type": "message",
                        "time": now,
                        "sender": ent.engine_id,
                        "message": msg.message,
                    }
                )
                definition = self.messages[msg.message]
                recipients = [
                    e.engine_id
                    for e in self.entities
                    if e.role in definition["to"] and e is not ent
                ]
                outgoing.append(
                    OutgoingMessage(
                        ent.engine_id,
                        recipients,
                        msg.message,
                        dict(msg.fields),
                        definition["latency_s"],
                        definition["reliability"],
                    )
                )
            if any(t.hostile for t in ob.tracks) and ent.key not in self.detect_time:
                self.detect_time[ent.key] = now
                self.trace_lines.append({"type": "detection", "time": now, "entity": ent.engine_id})
        self.engine.apply_commands(commands)
        if self.engine.messaging == "engine":
            if outgoing:
                self.engine.send_messages(outgoing)
        else:
            for msg in outgoing:
                self._queue(msg, now)

    # ------------------------------------------------------------------ events
    def _events(self, now: float) -> None:
        assert self.obs is not None
        for event in self.scenario.get("events") or []:
            eid = event["id"]
            if eid in self.event_times:
                continue
            if event.get("at") is not None:
                fired = now >= _q(event["at"]) - 1e-9
            elif event.get("when"):
                fired = self._trigger(event["when"])
            else:
                fired = False
            if not fired:
                continue
            self.event_times[eid] = now
            self.trace_lines.append({"type": "event", "time": now, "id": eid})
            kind = event["type"]
            if kind in ("comm_degradation", "message_loss") and self.engine.messaging == "adapter":
                only = event.get("message") if kind == "message_loss" else None
                self.comm_loss.append(
                    (now, now + _q(event.get("duration")), float(event.get("loss", 1.0)), only)
                )
            elif kind != "marker":
                if not self.engine.apply_event(dict(event)):
                    self.warnings.append(f"engine does not support event '{eid}' ({kind})")

    def _trigger(self, when: Mapping[str, Any]) -> bool:
        assert self.obs is not None
        for ent in self.entities:
            if ent.role != when["role"] or (
                "instance" in when and ent.instance != when["instance"]
            ):
                continue
            ob = self.obs.entities.get(ent.engine_id)
            if ob is None:
                continue
            ok = True
            if "terrain_class" in when:
                ok &= ob.terrain.get("class") == when["terrain_class"]
            for key, attr, sign in (
                ("x_gte", "x", 1),
                ("x_lte", "x", -1),
                ("y_gte", "y", 1),
                ("y_lte", "y", -1),
            ):
                if key in when:
                    ok &= sign * getattr(ob, attr) >= sign * float(when[key])
            if "state" in when:
                ok &= ent.controller.state == when["state"]
            if ok:
                return True
        return False

    # ------------------------------------------------------------------ messages
    def _loss(self, message_id: str, now: float) -> float:
        loss = 0.0
        for start, end, value, only in self.comm_loss:
            if start <= now < end and (only is None or only == message_id):
                loss = max(loss, value)
        return loss

    def _queue(self, msg: OutgoingMessage, now: float) -> None:
        sender = self.by_engine[msg.sender]
        loss = max(self._loss(msg.message, now), 1.0 - msg.reliability)
        for rid in msg.recipients:
            if loss > 0 and self.rng.random() < loss:
                continue
            self.queue.append(
                _Pending(
                    now + msg.latency_s,
                    now,
                    rid,
                    Delivery(msg.message, dict(msg.fields), sender.role, sender.instance),
                )
            )

    def _inbox(self, ent: _Ent, ob: EntityObservation, now: float) -> list[Delivery]:
        if self.engine.messaging == "engine":
            out = []
            for item in ob.inbox:
                sender = self.by_engine.get(item.get("sender", ""))
                out.append(
                    Delivery(
                        item["message"],
                        dict(item.get("fields") or {}),
                        sender.role if sender else "",
                        sender.instance if sender else 0,
                    )
                )
            return out
        due = [
            p
            for p in self.queue
            if p.recipient == ent.engine_id and p.deliver_at <= now + 1e-9 and p.sent_at < now
        ]
        self.queue = [p for p in self.queue if p not in due]
        return [p.delivery for p in due]

    # ------------------------------------------------------------------ geometry
    def leader_obs(self, role_id: str) -> EntityObservation | None:
        leader_role = self._leader_role(role_id)
        if leader_role is None or self.obs is None:
            return None
        ent = next((e for e in self.entities if e.role == leader_role), None)
        return self.obs.entities.get(ent.engine_id) if ent else None

    def slot_error(self, ent: _Ent, formation_id: str, ob: EntityObservation) -> float | None:
        leader = self.leader_obs(ent.role)
        if leader is None or not formation_id:
            return None
        pos = slot_position(
            self.formations.get(formation_id),
            ent.role,
            ent.instance,
            leader.x,
            leader.y,
            leader.heading_deg,
        )
        return None if pos is None else math.hypot(pos[0] - ob.x, pos[1] - ob.y)

    def tolerance(self, formation_id: str) -> float:
        form = self.formations.get(formation_id)
        return form["tolerance"]["value_m"] if form and form.get("tolerance") else math.inf

    def _sample(self) -> Sample:
        assert self.obs is not None
        obs = self.obs.entities
        rms: dict[str, float | None] = {}
        for fid, form in self.formations.items():
            errors = []
            for slot in form["slots"]:
                ent = next(
                    (
                        e
                        for e in self.entities
                        if e.role == slot["role"] and e.instance == slot["instance"]
                    ),
                    None,
                )
                if ent and ent.engine_id in obs:
                    err = self.slot_error(ent, fid, obs[ent.engine_id])
                    if err is not None:
                        errors.append(err)
            rms[fid] = math.sqrt(sum(e * e for e in errors) / len(errors)) if errors else None
        present = [e for e in self.entities if e.engine_id in obs]
        return Sample(
            t=self.obs.time,
            states={e.key: e.controller.state for e in present},
            positions={e.key: (obs[e.engine_id].x, obs[e.engine_id].y) for e in present},
            speeds={e.key: obs[e.engine_id].speed_mps for e in present},
            formations={e.key: e.formation_id for e in present},
            formation_rms=rms,
            hostile_ranges={
                e.key: min(
                    (
                        t.range_m
                        if t.range_m is not None
                        else math.hypot(t.x - obs[e.engine_id].x, t.y - obs[e.engine_id].y)
                        for t in obs[e.engine_id].tracks
                        if t.hostile
                    ),
                    default=math.inf,
                )
                for e in present
            },
        )

    # ------------------------------------------------------------------ commands
    def resolve(self, ent: _Ent, command: str, args: dict[str, Any], behavior: str) -> Command:
        extra: dict[str, Any] = {}
        if command == "keep_station":
            leader = next((e for e in self.entities if e.role == args.get("leader")), None)
            form = self.formations.get(args.get("formation") or "")
            extra["leader_entity"] = leader.engine_id if leader else None
            for slot in (form or {}).get("slots", []):
                if slot["role"] == ent.role and slot["instance"] == ent.instance:
                    extra["forward_m"] = slot["forward"]["value_m"]
                    extra["right_m"] = slot["right"]["value_m"]
            ent.formation_id = args.get("formation") or ""
        elif command == "assume_formation":
            ent.formation_id = args.get("formation") or ""
        elif command in ("follow_route", "follow_route_with_offset"):
            extra["points"] = [list(p) for p in self.routes.get(args.get("route", ""), [])]
        elif command == "move_to_position":
            point = self.waypoints.get(args.get("position", ""))
            extra["point"] = list(point) if point else None
        elif command == "bound_to_position":
            toward = args.get("toward") or "heading"
            if toward not in ("heading", "nearest_hostile"):
                point = self.waypoints.get(toward)
                extra["toward_point"] = list(point) if point else None
        elif command == "move_to_platform":
            target = self.by_engine.get(args.get("platform", "")) or next(
                (e for e in self.entities if e.key == args.get("platform")), None
            )
            extra["target_entity"] = target.engine_id if target else args.get("platform")
        elif command == "set_reserve_threshold":
            ent.reserve = float(args["threshold"])
        if command in MOVEMENT_COMMANDS:
            ent.mover_override = {"at_destination": False, "route_complete": False}
        return Command(ent.engine_id, command, {**args, **extra}, ent.role, ent.instance, behavior)


class ObservationWorld:
    """World view for one controller, backed by the engine's latest observation."""

    def __init__(self, runner: CoSimRunner, ent: _Ent, ob: EntityObservation) -> None:
        self.runner = runner
        self.ent = ent
        self.ob = ob
        self.commands: list[Command] = []
        # A fresh observation supersedes overrides from the previous tick.
        ent.mover_override = {}

    def apply(self, command: str, arguments: dict[str, Any]) -> None:
        now = self.runner.obs.time if self.runner.obs else 0.0
        self.runner.action_log.append((now, self.ent.key, command))
        self.runner.trace_lines.append(
            {"type": "action", "time": now, "entity": self.ent.engine_id, "command": command}
        )
        self.commands.append(self.runner.resolve(self.ent, command, dict(arguments), ""))

    def _default(self, source: str, prop: str) -> Any:
        return self.runner.defaults.get(source, {}).get(prop)

    def property(self, source: str, prop: str, spec: Mapping[str, Any]) -> Any:
        ob, ent, r = self.ob, self.ent, self.runner
        if source == "self_value":
            values = {
                "position_x": ob.x,
                "position_y": ob.y,
                "speed": ob.speed_mps,
                "heading": ob.heading_deg % 360.0,
                "altitude": ob.altitude_m,
                "fuel_remaining": ob.fuel_remaining,
                "fuel_reserve": ent.reserve,
                "instance_index": ent.instance,
                "alive": ob.alive,
                "damaged": ob.damaged,
            }
            return values[prop]
        if source == "mover_value":
            if prop in ent.mover_override:
                return ent.mover_override[prop]
            if prop == "moving" and "moving" not in ob.mover:
                return ob.speed_mps > 0.1
            return ob.mover.get(prop, self._default(source, prop))
        if source in ("sensor_value", "weapon_value", "comm_value"):
            comp = spec.get("component")
            values = ob.components.get(comp or "", {})
            if source == "sensor_value" and prop == "track_count" and prop not in values:
                return sum(1 for t in ob.tracks if t.hostile)
            if source == "comm_value" and r.engine.messaging == "adapter":
                if r._loss("", r.obs.time if r.obs else 0.0) >= 1.0:
                    return False
            return values.get(prop, self._default(source, prop))
        if source == "track_value":
            hostile = [
                (t, t.range_m if t.range_m is not None else math.hypot(t.x - ob.x, t.y - ob.y))
                for t in ob.tracks
                if t.hostile
            ]
            nearest = min(hostile, key=lambda pair: pair[1]) if hostile else None
            return {
                "nearest_hostile_range": nearest[1] if nearest else math.inf,
                "hostile_count": len(hostile),
                "current_target_range": nearest[1] if nearest else math.inf,
                "current_target_valid": nearest is not None,
                "current_target_id": nearest[0].id if nearest else "",
            }[prop]
        if source == "terrain_value":
            terrain = ob.terrain
            if prop == "class":
                return terrain.get("class", self._default(source, prop))
            if prop == "distance_to_class":
                cls = spec.get("argument") or "open"
                if terrain.get("class") == cls:
                    return 0.0
                return float(terrain.get("distance_to", {}).get(cls, math.inf))
            if prop == "slope":
                return float(terrain.get("slope_deg", 0.0))
            if prop == "line_of_sight_to_nearest_hostile":
                return bool(
                    terrain.get(
                        "line_of_sight_to_nearest_hostile", any(t.hostile for t in ob.tracks)
                    )
                )
        if source == "formation_value":
            formation = spec.get("formation") or ent.formation_id
            error = r.slot_error(ent, formation, ob)
            leader = r.leader_obs(ent.role)
            return {
                "current_id": ent.formation_id,
                "slot_error": error if error is not None else 0.0,
                "in_position": error is None or error <= r.tolerance(ent.formation_id),
                "leader_distance": math.hypot(leader.x - ob.x, leader.y - ob.y) if leader else 0.0,
            }[prop]
        if source == "team_value":
            obs = r.obs.entities if r.obs else {}
            others = [o for eid, o in obs.items() if eid != ent.engine_id and o.alive]
            team = [e for e in r.entities if e.role == ent.role and e.engine_id in obs]
            errors = [r.slot_error(e, e.formation_id, obs[e.engine_id]) for e in team]
            errors = [x for x in errors if x is not None]
            return {
                "nearest_friend_distance": min(
                    (math.hypot(o.x - ob.x, o.y - ob.y) for o in others), default=math.inf
                ),
                "formation_error_rms": math.sqrt(sum(x * x for x in errors) / len(errors))
                if errors
                else 0.0,
                "members_alive": sum(1 for e in team if obs[e.engine_id].alive),
                "members_in_position": sum(1 for x in errors if x <= r.tolerance(ent.formation_id)),
            }[prop]
        if source == "simulation_value":
            return {"time": r.obs.time if r.obs else 0.0, "tick": r.tick_index}[prop]
        raise KeyError(f"{source}.{prop}")


def slot_position(
    form: Mapping[str, Any] | None,
    role: str,
    instance: int,
    lx: float,
    ly: float,
    heading_deg: float,
) -> tuple[float, float] | None:
    if not form:
        return None
    for slot in form["slots"]:
        if slot["role"] == role and slot["instance"] == instance:
            fwd, right = slot["forward"]["value_m"], slot["right"]["value_m"]
            h = math.radians(heading_deg)
            return (
                lx + fwd * math.sin(h) + right * math.cos(h),
                ly + fwd * math.cos(h) - right * math.sin(h),
            )
    return None
