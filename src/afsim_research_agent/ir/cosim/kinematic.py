"""Model-independent point-mass engine (Python twin of the C++ bridge demo engine).

Needs only a ``ScenarioSetup``; used to check protocol hosts and as a stand-in
engine while a real one is being connected.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal

from afsim_research_agent.ir.cosim.engine import (
    Command,
    EntityObservation,
    Observation,
    OutgoingMessage,
    ScenarioSetup,
    TrackObservation,
)
from afsim_research_agent.ir.sim.world import bound_target

ARRIVAL_M = 5.0


@dataclass
class _Body:
    x: float
    y: float
    heading: float
    speed: float
    max_speed: float = 15.0
    accel: float = 2.0
    turn_rate: float = 45.0
    target_speed: float = 0.0
    target_heading: float = 0.0
    points: list[tuple[float, float]] = field(default_factory=list)
    index: int = 0
    mode: str = "idle"
    station: dict[str, Any] = field(default_factory=dict)
    arrived: bool = False
    sensor_range: float = 2000.0


class KinematicEngine:
    messaging: Literal["adapter", "engine"] = "adapter"

    def __init__(self) -> None:
        self.setup: ScenarioSetup | None = None
        self.time = 0.0
        self.bodies: dict[str, _Body] = {}
        self.hostiles: dict[str, tuple[float, float]] = {}
        self.regions: list[dict[str, Any]] = []
        self.default_class = "open"

    def reset(self, setup: ScenarioSetup) -> Observation:
        self.setup = setup
        self.time = 0.0
        self.hostiles = {}
        self.bodies = {}
        for e in setup.entities:
            body = _Body(e.position[0], e.position[1], e.heading_deg, e.speed_mps)
            body.target_heading, body.target_speed = e.heading_deg, e.speed_mps
            mover = (e.components or {}).get("mover") or {}
            settings = mover.get("settings", {}) if isinstance(mover, dict) else {}
            body.max_speed = float(settings.get("max_speed_mps", body.max_speed))
            ranges = [
                float(c.get("settings", {}).get("range_m"))
                for c in (e.components or {}).values()
                if isinstance(c, dict) and c.get("settings", {}).get("range_m") is not None
            ]
            if ranges:
                body.sensor_range = max(ranges)
            self.bodies[e.engine_id] = body
        terrain = setup.terrain or {}
        self.regions = list(terrain.get("regions", []))
        self.default_class = terrain.get("default_class", "open")
        return self._observe()

    # -- commands ------------------------------------------------------------------
    def apply_commands(self, commands: list[Command]) -> None:
        for cmd in commands:
            b = self.bodies[cmd.entity]
            a = cmd.args
            speed = a.get("speed")
            if cmd.command in ("follow_route", "follow_route_with_offset"):
                b.points = [tuple(p) for p in a.get("points") or []]
                b.index = _resume(b.points, b.x, b.y)
                b.mode, b.arrived = "route", False
                b.target_speed = (
                    min(float(speed), b.max_speed) if speed is not None else b.max_speed
                )
            elif cmd.command == "move_to_position":
                b.points = [tuple(a["point"])] if a.get("point") else []
                b.index, b.mode, b.arrived = 0, "route", False
                b.target_speed = (
                    min(float(speed), b.max_speed) if speed is not None else b.max_speed
                )
            elif cmd.command == "bound_to_position":
                toward = a.get("toward") or "heading"
                point = tuple(a["toward_point"]) if a.get("toward_point") else None
                if toward == "nearest_hostile":
                    seen = [
                        (math.hypot(hx - b.x, hy - b.y), (hx, hy))
                        for hx, hy in self.hostiles.values()
                        if math.hypot(hx - b.x, hy - b.y) <= b.sensor_range
                    ]
                    point = min(seen)[1] if seen else None
                b.points = [
                    bound_target(
                        b.x,
                        b.y,
                        b.heading,
                        float(a["distance"]),
                        point,
                        float(a.get("stop_short") or 0.0),
                    )
                ]
                b.index, b.mode, b.arrived = 0, "route", False
                b.target_speed = (
                    min(float(speed), b.max_speed) if speed is not None else 0.6 * b.max_speed
                )
            elif cmd.command in ("halt", "hold_overwatch_position"):
                b.mode, b.target_speed = "halt", 0.0
            elif cmd.command == "set_speed":
                b.target_speed = min(float(a["speed"]), b.max_speed)
            elif cmd.command == "set_heading":
                b.mode, b.target_heading = "heading", float(a["heading"])
            elif cmd.command == "keep_station":
                b.mode = "station"
                b.station = {
                    "leader": a.get("leader_entity"),
                    "forward": float(a.get("forward_m") or 0.0),
                    "right": float(a.get("right_m") or 0.0),
                    "max_speed": float(a["max_speed"])
                    if a.get("max_speed") is not None
                    else b.max_speed,
                }

    def send_messages(self, messages: list[OutgoingMessage]) -> None:
        raise NotImplementedError("KinematicEngine leaves messaging to the adapter")

    def apply_event(self, event: dict[str, Any]) -> bool:
        if event.get("type") == "spawn_hostile":
            x, y = event["position"]
            self.hostiles[event.get("target_id") or event["id"]] = (float(x), float(y))
            return True
        if event.get("type") == "remove_hostile":
            self.hostiles.pop(event.get("target_id", ""), None)
            return True
        return False

    def step(self, dt: float) -> Observation:
        for b in self.bodies.values():
            self._move(b, dt)
        self.time += dt
        return self._observe()

    def close(self) -> None:
        pass

    # -- physics ----------------------------------------------------------------------
    def _move(self, b: _Body, dt: float) -> None:
        target_speed = b.target_speed
        if b.mode == "route":
            while (
                b.index < len(b.points)
                and math.hypot(b.points[b.index][0] - b.x, b.points[b.index][1] - b.y) <= ARRIVAL_M
            ):
                b.index += 1
            if b.index >= len(b.points):
                b.arrived, target_speed = True, 0.0
            else:
                px, py = b.points[b.index]
                b.target_heading = math.degrees(math.atan2(px - b.x, py - b.y))
                if b.index == len(b.points) - 1:
                    remaining = math.hypot(px - b.x, py - b.y)
                    target_speed = min(target_speed, max(1.0, math.sqrt(2 * b.accel * remaining)))
        elif b.mode == "station":
            leader = self.bodies.get(b.station.get("leader") or "")
            if leader is None:
                target_speed = 0.0
            else:
                h = math.radians(leader.heading)
                sx = (
                    leader.x + b.station["forward"] * math.sin(h) + b.station["right"] * math.cos(h)
                )
                sy = (
                    leader.y + b.station["forward"] * math.cos(h) - b.station["right"] * math.sin(h)
                )
                dx, dy = sx - b.x, sy - b.y
                along = dx * math.sin(h) + dy * math.cos(h)
                cross = dx * math.cos(h) - dy * math.sin(h)
                b.target_heading = leader.heading + math.degrees(math.atan2(cross, 30.0))
                target_speed = max(
                    0.0, min(b.station["max_speed"], b.max_speed, leader.speed + 0.3 * along)
                )
        elif b.mode == "halt":
            target_speed = 0.0
        diff = (b.target_heading - b.heading + 180.0) % 360.0 - 180.0
        turn = b.turn_rate * dt
        b.heading = (b.heading + max(-turn, min(turn, diff)) + 180.0) % 360.0 - 180.0
        dv = target_speed - b.speed
        b.speed = max(0.0, b.speed + max(-b.accel * dt, min(b.accel * dt, dv)))
        h = math.radians(b.heading)
        b.x += b.speed * dt * math.sin(h)
        b.y += b.speed * dt * math.cos(h)

    # -- observation ---------------------------------------------------------------
    def _terrain(self, x: float, y: float) -> dict[str, Any]:
        def dist(r: dict[str, Any]) -> float:
            dx = max(r["x_min"] - x, 0.0, x - r["x_max"])
            dy = max(r["y_min"] - y, 0.0, y - r["y_max"])
            return math.hypot(dx, dy)

        cls = next((r["class"] for r in self.regions if dist(r) == 0.0), self.default_class)
        classes = {r["class"] for r in self.regions}
        return {
            "class": cls,
            "distance_to": {
                c: min(dist(r) for r in self.regions if r["class"] == c) for c in sorted(classes)
            },
        }

    def _observe(self) -> Observation:
        out = {}
        for eid, b in self.bodies.items():
            tracks = [
                TrackObservation(hid, hx, hy, True, math.hypot(hx - b.x, hy - b.y))
                for hid, (hx, hy) in sorted(self.hostiles.items())
                if math.hypot(hx - b.x, hy - b.y) <= b.sensor_range
            ]
            out[eid] = EntityObservation(
                x=b.x,
                y=b.y,
                heading_deg=b.heading % 360.0,
                speed_mps=b.speed,
                mover={
                    "available": True,
                    "at_destination": b.arrived,
                    "route_complete": b.arrived,
                    "moving": b.speed > 0.1,
                },
                tracks=tracks,
                terrain=self._terrain(b.x, b.y),
            )
        return Observation(self.time, out)


def _resume(points: list[tuple[float, float]], x: float, y: float) -> int:
    if len(points) < 2:
        return 0
    best, best_index = math.inf, 1
    for i in range(len(points) - 1):
        (ax, ay), (bx, by) = points[i], points[i + 1]
        vx, vy = bx - ax, by - ay
        t = max(0.0, min(1.0, ((x - ax) * vx + (y - ay) * vy) / ((vx * vx + vy * vy) or 1.0)))
        d = math.hypot(ax + t * vx - x, ay + t * vy - y)
        if d < best - 1e-9:
            best, best_index = d, i + 1
    return best_index
