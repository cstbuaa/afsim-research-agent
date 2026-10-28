"""Simplified 2D world: point-mass kinematics, rectangular synthetic terrain,
range sensors, latency/loss message delivery and scripted events.

Coordinates are metres; +x east, +y north; heading is degrees clockwise from north.
The model is deliberately simple: it checks coordination logic and timing,
not vehicle dynamics or weapon effects.
"""

from __future__ import annotations

import math
import random
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from afsim_research_agent.ir.runtime import Delivery, RoleController
from afsim_research_agent.ir.units import to_canonical

ARRIVAL_RADIUS_M = 5.0
STATION_LOOKAHEAD_M = 30.0
STATION_GAIN = 0.3


def _q(raw: Any, dimension: str = "time", default: float = 0.0) -> float:
    if raw is None:
        return default
    if isinstance(raw, Mapping):
        return to_canonical(float(raw["value"]), str(raw["unit"]), dimension)
    return float(raw)


def _wrap(angle: float) -> float:
    return (angle + 180.0) % 360.0 - 180.0


@dataclass
class Region:
    cls: str
    x_min: float
    x_max: float
    y_min: float
    y_max: float

    def distance(self, x: float, y: float) -> float:
        dx = max(self.x_min - x, 0.0, x - self.x_max)
        dy = max(self.y_min - y, 0.0, y - self.y_max)
        return math.hypot(dx, dy)


@dataclass
class Terrain:
    default_class: str = "open"
    regions: list[Region] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> Terrain:
        if not data:
            return cls()
        regions = [
            Region(
                r["class"],
                float(r["x_min"]),
                float(r["x_max"]),
                float(r["y_min"]),
                float(r["y_max"]),
            )
            for r in data.get("regions", [])
        ]
        return cls(data.get("default_class", "open"), regions)

    def class_at(self, x: float, y: float) -> str:
        for region in self.regions:
            if region.distance(x, y) == 0.0:
                return region.cls
        return self.default_class

    def distance_to_class(self, x: float, y: float, cls: str) -> float:
        if self.class_at(x, y) == cls:
            return 0.0
        distances = [r.distance(x, y) for r in self.regions if r.cls == cls]
        return min(distances) if distances else math.inf


def load_terrain(env: Mapping[str, Any], package_dir: Path | None) -> Terrain:
    if env.get("terrain"):
        return Terrain.from_mapping(env["terrain"])
    rel = env.get("terrain_map")
    if rel and package_dir is not None:
        path = PurePosixPath(rel)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("terrain_map must stay inside the model package")
        data = yaml.safe_load((package_dir / Path(*path.parts)).read_text(encoding="utf-8"))
        return Terrain.from_mapping(data)
    return Terrain()


@dataclass
class Hostile:
    id: str
    x: float
    y: float
    alive: bool = True


@dataclass
class Entity:
    role: str
    instance: int
    controller: RoleController
    x: float = 0.0
    y: float = 0.0
    heading: float = 90.0
    speed: float = 0.0
    max_speed: float = 15.0
    max_accel: float = 2.0
    turn_rate: float = 45.0
    target_speed: float = 0.0
    target_heading: float = 90.0
    command: dict[str, Any] = field(default_factory=dict)
    at_destination: bool = False
    route_complete: bool = False
    formation_id: str = ""
    sensor_ranges: dict[str, float] = field(default_factory=dict)
    sensors_on: dict[str, bool] = field(default_factory=dict)
    weapons: dict[str, int] = field(default_factory=dict)
    has_comms: bool = True
    fuel: float = 1.0
    fuel_burn: float = 0.0
    reserve: float = 0.0
    alive: bool = True

    @property
    def key(self) -> str:
        return f"{self.role}[{self.instance}]"


@dataclass
class Pending:
    deliver_at: float
    sent_at: float
    recipient: str
    delivery: Delivery


class SimWorld:
    def __init__(
        self,
        ir: dict[str, Any],
        context: dict[str, Any],
        scenario: Mapping[str, Any],
        *,
        package_dir: Path | None = None,
        withheld: Collection[str] | None = None,
    ) -> None:
        """``withheld``: faithful mode (capability-catalog-integration-v1 ⑦). IR elements the
        platform cannot provide (unresolved capability gaps): reads return the catalog's
        "nothing known" default and actions have no effect, so L3 shows what the tactic
        relies on."""
        self.ir = ir
        self.withheld = frozenset(withheld or ())
        self.withheld_hits: dict[str, int] = {}
        self.context = context
        self.scenario = scenario
        env = scenario["environment"]
        self.env = env
        self.terrain = load_terrain(env, package_dir)
        self.routes = {
            k: [tuple(map(float, p)) for p in v] for k, v in (env.get("routes") or {}).items()
        }
        self.waypoints = {k: tuple(map(float, v)) for k, v in (env.get("waypoints") or {}).items()}
        self.duration = _q(env["duration"])
        self.dt = _q(env.get("time_step"), default=ir["execution"]["update_interval_s"])
        self.rng = random.Random(int(env.get("random_seed", 0)))
        self.now = 0.0
        self.tick_index = 0
        self.params = {name: p["value"] for name, p in ir["parameters"].items()}
        self.messages = {m["id"]: m for m in ir["messages"]}
        self.formations = {f["id"]: f for f in ir["formations"]}
        self.roles = {r["id"]: r for r in ir["roles"]}
        self.hostiles: list[Hostile] = []
        self.queue: list[Pending] = []
        self.entities: list[Entity] = []
        self.event_times: dict[str, float] = {}
        self.comm_loss: list[tuple[float, float, float, str | None]] = []
        self.sent_log: list[tuple[float, str, str]] = []
        self.action_log: list[tuple[float, str, str]] = []
        self.detect_time: dict[str, float] = {}
        self._build_entities()

    # ------------------------------------------------------------------ setup
    def _build_entities(self) -> None:
        initial = self.scenario.get("initial") or {}
        for role in self.ir["roles"]:
            binding = self.context["roles"][role["id"]]["components"]
            for instance in range(role["count"]):
                ctl = RoleController(
                    self.ir, role["id"], instance, seed=int(self.env.get("random_seed", 0))
                )
                ent = Entity(role["id"], instance, ctl)
                mover = binding.get("mover")
                if mover:
                    s = mover["settings"]
                    ent.max_speed = float(s.get("max_speed_mps", 15.0))
                    ent.max_accel = float(s.get("max_accel_mps2", 2.0))
                    ent.turn_rate = float(s.get("turn_rate_dps", 45.0))
                for comp in role["dependencies"]["sensors"]:
                    b = binding.get(comp["id"])
                    ent.sensor_ranges[comp["id"]] = float(
                        (b or {}).get("settings", {}).get("range_m", 0.0)
                    )
                    ent.sensors_on[comp["id"]] = b is not None
                for comp in role["dependencies"]["weapons"]:
                    b = binding.get(comp["id"])
                    ent.weapons[comp["id"]] = int((b or {}).get("settings", {}).get("quantity", 0))
                ent.has_comms = any(binding.get(c["id"]) for c in role["dependencies"]["comms"])
                energy = binding.get("energy")
                if energy:
                    ent.fuel_burn = float(energy["settings"].get("burn_per_s", 0.0))
                init = initial.get(role["id"], {})
                if init.get("state"):
                    ctl.state = init["state"]
                ent.heading = ent.target_heading = float(init.get("heading_deg", 90.0))
                if "position" in init:
                    ent.x, ent.y = (float(v) for v in init["position"])
                    ent.x += 0.0
                ent.speed = ent.target_speed = _q(init.get("speed"), "speed")
                self.entities.append(ent)
        for role in self.ir["roles"]:
            init = initial.get(role["id"], {})
            if "position" in init and role["count"] == 1:
                continue
            formation = init.get("formation")
            leader = self._leader_of(role["id"])
            for ent in self.by_role(role["id"]):
                if formation and leader is not None:
                    ent.formation_id = formation
                    pos = self.slot_position(formation, ent.role, ent.instance, leader)
                    if pos is not None:
                        ent.x, ent.y = pos
                        ent.heading = ent.target_heading = leader.heading
                elif "position" not in init:
                    ent.x = ent.instance * 20.0
                    ent.y = 0.0

    def by_role(self, role: str) -> list[Entity]:
        return [e for e in self.entities if e.role == role]

    def entity(self, key: str) -> Entity | None:
        return next((e for e in self.entities if e.key == key), None)

    def _leader_of(self, role_id: str) -> Entity | None:
        for coord in self.ir["coordination"]:
            if coord["pattern"] == "leader_follower" and coord.get("followers") == role_id:
                found = self.by_role(coord["leader"])
                return found[0] if found else None
        superior = self.roles[role_id]["command"].get("superior")
        if superior:
            found = self.by_role(superior)
            return found[0] if found else None
        return None

    def slot_position(
        self, formation_id: str, role: str, instance: int, leader: Entity
    ) -> tuple[float, float] | None:
        form = self.formations.get(formation_id)
        if form is None:
            return None
        for slot in form["slots"]:
            if slot["role"] == role and slot["instance"] == instance:
                fwd, right = slot["forward"]["value_m"], slot["right"]["value_m"]
                h = math.radians(leader.heading)
                x = leader.x + fwd * math.sin(h) + right * math.cos(h)
                y = leader.y + fwd * math.cos(h) - right * math.sin(h)
                return x, y
        return None

    # ------------------------------------------------------------------ events
    def _process_events(self) -> None:
        for event in self.scenario.get("events") or []:
            eid = event["id"]
            if eid in self.event_times:
                continue
            fired = False
            if event.get("at") is not None:
                fired = self.now >= _q(event["at"]) - 1e-9
            elif event.get("when"):
                fired = self._trigger(event["when"])
            if not fired:
                continue
            self.event_times[eid] = self.now
            kind = event["type"]
            if kind == "spawn_hostile":
                x, y = event["position"]
                self.hostiles.append(Hostile(event.get("target_id") or eid, float(x), float(y)))
            elif kind == "remove_hostile":
                for h in self.hostiles:
                    if h.id == event.get("target_id"):
                        h.alive = False
            elif kind in ("comm_degradation", "message_loss"):
                self.comm_loss.append(
                    (
                        self.now,
                        self.now + _q(event.get("duration")),
                        float(event.get("loss", 1.0)),
                        event.get("message") if kind == "message_loss" else None,
                    )
                )

    def _trigger(self, when: Mapping[str, Any]) -> bool:
        subjects = self.by_role(when["role"])
        if "instance" in when:
            subjects = [e for e in subjects if e.instance == when["instance"]]
        for ent in subjects:
            ok = True
            if "terrain_class" in when:
                ok &= self.terrain.class_at(ent.x, ent.y) == when["terrain_class"]
            if "x_gte" in when:
                ok &= ent.x >= float(when["x_gte"])
            if "x_lte" in when:
                ok &= ent.x <= float(when["x_lte"])
            if "y_gte" in when:
                ok &= ent.y >= float(when["y_gte"])
            if "y_lte" in when:
                ok &= ent.y <= float(when["y_lte"])
            if "state" in when:
                ok &= ent.controller.state == when["state"]
            if ok:
                return True
        return False

    def _loss(self, message_id: str) -> float:
        loss = 0.0
        for start, end, value, only in self.comm_loss:
            if start <= self.now < end and (only is None or only == message_id):
                loss = max(loss, value)
        return loss

    # ------------------------------------------------------------------ views
    def property(self, ent: Entity, source: str, prop: str, spec: Mapping[str, Any]) -> Any:
        name = f"{source}.{prop}"
        if name in self.withheld:
            self.withheld_hits[name] = self.withheld_hits.get(name, 0) + 1
            return _unknown_value(source, prop)
        if source == "self_value":
            return {
                "position_x": ent.x,
                "position_y": ent.y,
                "speed": ent.speed,
                "heading": ent.heading % 360.0,
                "altitude": 0.0,
                "fuel_remaining": ent.fuel,
                "fuel_reserve": ent.reserve,
                "instance_index": ent.instance,
                "alive": ent.alive,
                "damaged": False,
            }[prop]
        if source == "mover_value":
            return {
                "available": ent.alive,
                "at_destination": ent.at_destination,
                "route_complete": ent.route_complete,
                "moving": ent.speed > 0.1,
            }[prop]
        if source == "sensor_value":
            comp = spec.get("component")
            return {
                "available": comp in ent.sensor_ranges,
                "on": ent.sensors_on.get(comp, False),
                "track_count": len(self._detected(ent)),
            }[prop]
        if source == "weapon_value":
            qty = ent.weapons.get(spec.get("component"), 0)
            return {"available": qty > 0, "ready": qty > 0, "quantity_remaining": qty}[prop]
        if source == "comm_value":
            return ent.has_comms and self._loss("") < 1.0
        if source == "track_value":
            detected = self._detected(ent)
            nearest = min(detected, key=lambda pair: pair[1]) if detected else None
            return {
                "nearest_hostile_range": nearest[1] if nearest else math.inf,
                "hostile_count": len(detected),
                "current_target_range": nearest[1] if nearest else math.inf,
                "current_target_valid": nearest is not None,
                "current_target_id": nearest[0].id if nearest else "",
            }[prop]
        if source == "terrain_value":
            if prop == "class":
                return self.terrain.class_at(ent.x, ent.y)
            if prop == "distance_to_class":
                return self.terrain.distance_to_class(ent.x, ent.y, spec.get("argument") or "open")
            if prop == "slope":
                return 0.0
            if prop == "line_of_sight_to_nearest_hostile":
                return bool(self._detected(ent))
        if source == "formation_value":
            error = self.slot_error(ent, spec.get("formation") or ent.formation_id)
            leader = self._leader_of(ent.role)
            return {
                "current_id": ent.formation_id,
                "slot_error": error if error is not None else 0.0,
                "in_position": error is None or error <= self._tolerance(ent.formation_id),
                "leader_distance": math.hypot(leader.x - ent.x, leader.y - ent.y)
                if leader
                else 0.0,
            }[prop]
        if source == "team_value":
            others = [e for e in self.entities if e is not ent and e.alive]
            nearest = min((math.hypot(e.x - ent.x, e.y - ent.y) for e in others), default=math.inf)
            team = self.by_role(ent.role)
            errors = [self.slot_error(e, e.formation_id) for e in team]
            errors = [x for x in errors if x is not None]
            return {
                "nearest_friend_distance": nearest,
                "formation_error_rms": math.sqrt(sum(x * x for x in errors) / len(errors))
                if errors
                else 0.0,
                "members_alive": sum(1 for e in team if e.alive),
                "members_in_position": sum(
                    1 for x in errors if x <= self._tolerance(ent.formation_id)
                ),
            }[prop]
        if source == "simulation_value":
            return {"time": self.now, "tick": self.tick_index}[prop]
        raise KeyError(f"{source}.{prop}")

    def _detected(self, ent: Entity) -> list[tuple[Hostile, float]]:
        reach = max((r for s, r in ent.sensor_ranges.items() if ent.sensors_on.get(s)), default=0.0)
        out = []
        for h in self.hostiles:
            if not h.alive:
                continue
            dist = math.hypot(h.x - ent.x, h.y - ent.y)
            if dist <= reach:
                out.append((h, dist))
        if out and ent.key not in self.detect_time:
            self.detect_time[ent.key] = self.now
        return out

    def _tolerance(self, formation_id: str) -> float:
        form = self.formations.get(formation_id)
        if not form or not form.get("tolerance"):
            return math.inf
        return form["tolerance"]["value_m"]

    def slot_error(self, ent: Entity, formation_id: str) -> float | None:
        leader = self._leader_of(ent.role)
        if leader is None or not formation_id:
            return None
        pos = self.slot_position(formation_id, ent.role, ent.instance, leader)
        if pos is None:
            return None
        return math.hypot(pos[0] - ent.x, pos[1] - ent.y)

    # ------------------------------------------------------------------ step
    def step(self) -> None:
        self._process_events()
        for ent in self.entities:
            inbox = [
                p.delivery
                for p in self.queue
                if p.recipient == ent.key
                and p.deliver_at <= self.now + 1e-9
                and p.sent_at < self.now
            ]
            self.queue = [
                p
                for p in self.queue
                if not (
                    p.recipient == ent.key
                    and p.deliver_at <= self.now + 1e-9
                    and p.sent_at < self.now
                )
            ]
            view = _EntityView(self, ent)
            result = ent.controller.tick(self.now, view, inbox)
            for out in result.sent_messages:
                self._send(ent, out.message, out.fields)
        for ent in self.entities:
            self._move(ent)
            ent.fuel = max(0.0, ent.fuel - ent.fuel_burn * self.dt)
        self.now += self.dt
        self.tick_index += 1

    def _send(self, sender: Entity, message_id: str, fields: dict[str, Any]) -> None:
        msg = self.messages[message_id]
        self.sent_log.append((self.now, sender.key, message_id))
        if not sender.has_comms:
            return
        loss = max(self._loss(message_id), 1.0 - msg["reliability"])
        for ent in self.entities:
            if ent is sender or ent.role not in msg["to"]:
                continue
            if loss > 0 and self.rng.random() < loss:
                continue
            self.queue.append(
                Pending(
                    self.now + msg["latency_s"],
                    self.now,
                    ent.key,
                    Delivery(message_id, dict(fields), sender.role, sender.instance),
                )
            )

    def _apply(self, ent: Entity, command: str, args: dict[str, Any]) -> None:
        if command in self.withheld:
            self.withheld_hits[command] = self.withheld_hits.get(command, 0) + 1
            return
        speed = args.get("speed")
        if command == "follow_route":
            points = self.routes.get(args["route"], [])
            ent.command = {
                "type": "route",
                "points": points,
                "index": _resume_index(points, ent.x, ent.y),
            }
            ent.target_speed = (
                min(float(speed), ent.max_speed) if speed is not None else ent.max_speed
            )
            ent.route_complete = ent.at_destination = False
        elif command == "follow_route_with_offset":
            pts = _offset(self.routes.get(args["route"], []), float(args["offset_right"]))
            ent.command = {
                "type": "route",
                "points": pts,
                "index": _resume_index(pts, ent.x, ent.y),
            }
            ent.target_speed = (
                min(float(speed), ent.max_speed) if speed is not None else ent.max_speed
            )
            ent.route_complete = ent.at_destination = False
        elif command == "move_to_position":
            point = self.waypoints.get(args["position"])
            ent.command = {"type": "route", "points": [point] if point else [], "index": 0}
            ent.target_speed = (
                min(float(speed), ent.max_speed) if speed is not None else ent.max_speed
            )
            ent.at_destination = False
        elif command == "move_to_platform":
            ent.command = {"type": "chase", "target": args["platform"]}
            ent.target_speed = (
                min(float(speed), ent.max_speed) if speed is not None else ent.max_speed
            )
        elif command == "move_to_track":
            ent.command = {"type": "track", "target": args["track"]}
            ent.target_speed = (
                min(float(speed), ent.max_speed) if speed is not None else ent.max_speed
            )
        elif command == "set_speed":
            ent.target_speed = min(float(args["speed"]), ent.max_speed)
        elif command == "set_heading":
            ent.command = {"type": "heading"}
            ent.target_heading = float(args["heading"])
        elif command in ("halt", "hold_overwatch_position"):
            ent.command = {"type": "halt"}
            ent.target_speed = 0.0
        elif command == "keep_station":
            formation = args.get("formation") or ""
            ent.formation_id = formation
            ent.command = {"type": "station", "leader": args["leader"], "formation": formation}
            if args.get("max_speed") is not None:
                ent.command["max_speed"] = float(args["max_speed"])
        elif command == "assume_formation":
            ent.formation_id = args["formation"]
        elif command == "bound_to_position":
            toward = args.get("toward") or "heading"
            point = None
            if toward == "nearest_hostile":
                detected = self._detected(ent)
                if detected:
                    hostile = min(detected, key=lambda pair: pair[1])[0]
                    point = (hostile.x, hostile.y)
            elif toward != "heading":
                point = self.waypoints.get(toward)
            target = bound_target(
                ent.x,
                ent.y,
                ent.heading,
                float(args["distance"]),
                point,
                float(args.get("stop_short") or 0.0),
            )
            ent.command = {"type": "route", "points": [target], "index": 0}
            ent.target_speed = (
                min(float(speed), ent.max_speed) if speed is not None else ent.max_speed * 0.6
            )
            ent.at_destination = False
        elif command in ("turn_sensor_on", "turn_sensor_off"):
            ent.sensors_on[args["sensor"]] = command == "turn_sensor_on"
        elif command == "fire_weapon":
            weapon = args["weapon"]
            if ent.weapons.get(weapon, 0) > 0:
                ent.weapons[weapon] -= 1
        elif command == "set_reserve_threshold":
            ent.reserve = float(args["threshold"])

    def _move(self, ent: Entity) -> None:
        cmd = ent.command
        kind = cmd.get("type")
        target_speed = ent.target_speed
        if kind == "route":
            points = cmd["points"]
            while cmd["index"] < len(points):
                px, py = points[cmd["index"]]
                if math.hypot(px - ent.x, py - ent.y) <= ARRIVAL_RADIUS_M:
                    cmd["index"] += 1
                else:
                    break
            if cmd["index"] >= len(points):
                ent.at_destination = True
                ent.route_complete = True
                target_speed = 0.0
            else:
                px, py = points[cmd["index"]]
                ent.target_heading = math.degrees(math.atan2(px - ent.x, py - ent.y))
                remaining = math.hypot(px - ent.x, py - ent.y)
                if cmd["index"] == len(points) - 1:
                    target_speed = min(
                        target_speed, max(1.0, math.sqrt(2 * ent.max_accel * remaining))
                    )
        elif kind == "station":
            leader = next((e for e in self.by_role(cmd["leader"])), None)
            pos = (
                self.slot_position(cmd["formation"], ent.role, ent.instance, leader)
                if leader
                else None
            )
            if leader is None or pos is None:
                target_speed = 0.0
            else:
                h = math.radians(leader.heading)
                dx, dy = pos[0] - ent.x, pos[1] - ent.y
                along = dx * math.sin(h) + dy * math.cos(h)
                cross = dx * math.cos(h) - dy * math.sin(h)
                ent.target_heading = leader.heading + math.degrees(
                    math.atan2(cross, STATION_LOOKAHEAD_M)
                )
                cap = min(ent.max_speed, cmd.get("max_speed", ent.max_speed))
                target_speed = max(0.0, min(cap, leader.speed + STATION_GAIN * along))
        elif kind in ("chase", "track"):
            target = self.entity(cmd["target"]) if kind == "chase" else None
            if kind == "track":
                hostile = next(
                    (h for h in self.hostiles if h.id == cmd["target"] and h.alive), None
                )
                tx, ty = (hostile.x, hostile.y) if hostile else (ent.x, ent.y)
            else:
                tx, ty = (target.x, target.y) if target else (ent.x, ent.y)
            ent.target_heading = math.degrees(math.atan2(tx - ent.x, ty - ent.y))
        elif kind == "halt":
            target_speed = 0.0
        # heading and speed limits
        diff = _wrap(ent.target_heading - ent.heading)
        max_turn = ent.turn_rate * self.dt
        ent.heading = _wrap(ent.heading + max(-max_turn, min(max_turn, diff)))
        dv = target_speed - ent.speed
        max_dv = ent.max_accel * self.dt
        ent.speed = max(0.0, ent.speed + max(-max_dv, min(max_dv, dv)))
        h = math.radians(ent.heading)
        ent.x += ent.speed * self.dt * math.sin(h)
        ent.y += ent.speed * self.dt * math.cos(h)


def bound_target(
    x: float,
    y: float,
    heading_deg: float,
    distance: float,
    toward: tuple[float, float] | None,
    stop_short: float = 0.0,
) -> tuple[float, float]:
    """End point of a bound: ``distance`` along the heading, or toward ``toward``
    without ending closer than ``stop_short`` to it (never moving away)."""
    if toward is None:
        h = math.radians(heading_deg)
        return x + distance * math.sin(h), y + distance * math.cos(h)
    dx, dy = toward[0] - x, toward[1] - y
    gap = math.hypot(dx, dy)
    step = min(distance, gap - stop_short)
    if gap < 1e-9 or step <= 0:
        return x, y
    return x + dx / gap * step, y + dy / gap * step


def _resume_index(points: list[tuple[float, float]], x: float, y: float) -> int:
    """Index of the next waypoint after the route segment closest to (x, y)."""
    if len(points) < 2:
        return 0
    best, best_index = math.inf, 1
    for i in range(len(points) - 1):
        (ax, ay), (bx, by) = points[i], points[i + 1]
        vx, vy = bx - ax, by - ay
        length2 = vx * vx + vy * vy or 1.0
        t = max(0.0, min(1.0, ((x - ax) * vx + (y - ay) * vy) / length2))
        dist = math.hypot(ax + t * vx - x, ay + t * vy - y)
        if dist < best - 1e-9:
            best, best_index = dist, i + 1
    return best_index


def _offset(points: list[tuple[float, float]], right: float) -> list[tuple[float, float]]:
    if len(points) < 2:
        return list(points)
    out = []
    for i, (x, y) in enumerate(points):
        a = points[max(0, i - 1)] if i else points[0]
        b = points[i + 1] if i + 1 < len(points) else points[i]
        if i == 0:
            a, b = points[0], points[1]
        hx, hy = b[0] - a[0], b[1] - a[1]
        norm = math.hypot(hx, hy) or 1.0
        out.append((x + right * hy / norm, y - right * hx / norm))
    return out


def _unknown_value(source: str, prop: str) -> Any:
    """What a platform without the capability knows: the operand catalog default
    (``inf`` = nothing there, ``false``, the default terrain class)."""
    from afsim_research_agent.ir.catalog import load_catalog

    return load_catalog().operands[source].properties[prop].default


class _EntityView:
    def __init__(self, world: SimWorld, ent: Entity) -> None:
        self.world = world
        self.ent = ent

    def property(self, source: str, prop: str, spec: Mapping[str, Any]) -> Any:
        return self.world.property(self.ent, source, prop, spec)

    def apply(self, command: str, arguments: dict[str, Any]) -> None:
        self.world.action_log.append((self.world.now, self.ent.key, command))
        self.world._apply(self.ent, command, arguments)
