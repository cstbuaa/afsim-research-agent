"""Flat 2D world for the IR 2.0 simulation: vehicles, their sensors, and hostiles.

Positions are metres east / north of the scenario origin; geodetic values written to the
blackboard use a local tangent plane around the origin (adequate for tactical ranges).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

EARTH_RADIUS_M = 6_371_000.0


@dataclass
class Geo:
    lat0_deg: float = 0.0
    lon0_deg: float = 0.0

    def to_latlon(self, x: float, y: float) -> tuple[float, float]:
        lat = self.lat0_deg + math.degrees(y / EARTH_RADIUS_M)
        lon = self.lon0_deg + math.degrees(
            x / (EARTH_RADIUS_M * math.cos(math.radians(self.lat0_deg)))
        )
        return lat, lon

    def to_xy(self, lat: float, lon: float) -> tuple[float, float]:
        y = math.radians(lat - self.lat0_deg) * EARTH_RADIUS_M
        x = (
            math.radians(lon - self.lon0_deg)
            * EARTH_RADIUS_M
            * math.cos(math.radians(self.lat0_deg))
        )
        return x, y


def bearing_deg(dx: float, dy: float) -> float:
    """True bearing (0 = north, clockwise) of the vector (dx east, dy north)."""
    return math.degrees(math.atan2(dx, dy)) % 360.0


def heading_error(target: float, current: float) -> float:
    """Smallest signed angle from current to target, degrees in (-180, 180]."""
    err = (target - current + 180.0) % 360.0 - 180.0
    return 180.0 if err == -180.0 else err


@dataclass
class Sensor:
    name: str = "search_sensor"
    range_m: float = 5000.0
    exists: bool = True
    operational: bool = True
    on: bool = False
    commanded_on: bool = False


@dataclass
class Vehicle:
    name: str
    x: float = 0.0
    y: float = 0.0
    heading: float = 0.0  # degrees true
    speed: float = 0.0  # m/s
    max_speed: float = 20.0
    accel: float = 2.0  # m/s²
    turn_rate: float = 30.0  # deg/s
    mobility_available: bool = True
    radio_operational: bool = True
    cmd_speed: float = 0.0
    cmd_heading: float | None = None
    sensor: Sensor | None = field(default_factory=Sensor)
    # a follower of a formation: the vehicle whose formation steps move it (its station
    # command, refreshed every update the step runs; see ``World.step``)
    commanded_by: str | None = None
    command: dict[str, Any] | None = None

    def steer(self, leader: Vehicle | None, now: float) -> None:
        """Drive towards the commanded station: at it, keep the leader's speed and heading;
        away from it, head for it at up to the step's speed (faster the further behind)."""
        cmd = self.command or {}
        sx, sy = cmd["station"]
        dx, dy = sx - self.x, sy - self.y
        dist = math.hypot(dx, dy)
        lead_speed = leader.speed if leader is not None else 0.0
        if dist <= float(cmd.get("stop_m", 5.0)):
            self.cmd_speed = lead_speed
            self.cmd_heading = leader.heading if leader is not None and lead_speed > 0.1 else None
        else:
            self.cmd_heading = bearing_deg(dx, dy)
            self.cmd_speed = min(float(cmd.get("speed_mps", 10.0)), lead_speed + 0.5 * dist)

    def step(self, dt: float) -> None:
        if not self.mobility_available:
            self.cmd_speed = 0.0
        target = max(0.0, min(self.cmd_speed, self.max_speed))
        dv = target - self.speed
        self.speed += max(-self.accel * dt, min(self.accel * dt, dv))
        if self.cmd_heading is not None:
            err = heading_error(self.cmd_heading, self.heading)
            turn = max(-self.turn_rate * dt, min(self.turn_rate * dt, err))
            self.heading = (self.heading + turn) % 360.0
        rad = math.radians(self.heading)
        self.x += self.speed * math.sin(rad) * dt
        self.y += self.speed * math.cos(rad) * dt
        if self.sensor is not None:
            # a commanded sensor state is observed one update later
            self.sensor.on = self.sensor.commanded_on and self.sensor.operational


@dataclass
class Hostile:
    id: str
    x: float
    y: float
    affiliation: str = "ENEMY"
    vx: float = 0.0
    vy: float = 0.0
    appear_s: float = 0.0
    remove_s: float | None = None
    # the velocity applies from move_start_s until move_stop_s (None: no end)
    move_start_s: float = 0.0
    move_stop_s: float | None = None
    hits: int = 0
    destroyed_at: float | None = None

    def present(self, now: float) -> bool:
        return (
            now >= self.appear_s
            and (self.remove_s is None or now < self.remove_s)
            and self.destroyed_at is None
        )

    def moving(self, now: float) -> bool:
        return now >= self.move_start_s and (self.move_stop_s is None or now < self.move_stop_s)

    def speed_at(self, now: float) -> float:
        return self.speed if self.moving(now) else 0.0

    @property
    def speed(self) -> float:
        return math.hypot(self.vx, self.vy)

    @property
    def heading(self) -> float:
        return bearing_deg(self.vx, self.vy) if self.speed > 0 else 0.0


@dataclass
class Message:
    """A radio message between own vehicles (task assignment, shared target, track report)."""

    sender: str
    target_id: str
    epoch: int
    seq: int
    sent_at: float


@dataclass
class Shot:
    shooter: str
    weapon: str
    target: str
    fired_at: float
    impact_at: float
    result: str | None = None  # DAMAGED / DESTROYED once the round arrived


@dataclass
class Team:
    """What the vehicles exchange by radio, and the shots in flight (sim.team models).

    Messages are delivered when the sender's and the recipient's radios work; the
    recipients' radio callbacks (``sim.team.sync_team``) see them at their next update."""

    leader: str | None = None
    seq: int = 0
    assignments: dict[str, Message] = field(default_factory=dict)  # member → last task
    shared: dict[str, Message] = field(default_factory=dict)  # follower → last broadcast
    reports: dict[str, float] = field(default_factory=dict)  # target id → last report time
    shots: list[Shot] = field(default_factory=list)
    seen: dict[str, dict[str, int]] = field(default_factory=dict)  # vehicle → message seqs

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq


@dataclass
class World:
    geo: Geo = field(default_factory=Geo)
    vehicles: dict[str, Vehicle] = field(default_factory=dict)
    hostiles: list[Hostile] = field(default_factory=list)
    routes: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    now: float = 0.0
    team: Team = field(default_factory=Team)
    hits_to_kill: int = 2

    def followers(self, leader: str) -> list[Vehicle]:
        """The vehicles a leader's formation steps command, in their order (the first takes
        the first column slot and the left line slot)."""
        return sorted(
            (v for v in self.vehicles.values() if v.commanded_by == leader), key=lambda v: v.name
        )

    def step(self, dt: float) -> None:
        for v in self.vehicles.values():
            if v.command is not None:
                # the station command of this update wins over the follower's own driving
                # (the step claims the followers' locomotion); a step that stopped running
                # leaves its followers standing
                if float(v.command.get("at", -1.0)) >= self.now - 1e-9:
                    v.steer(self.vehicles.get(str(v.command.get("leader"))), self.now)
                else:
                    v.command = None
                    v.cmd_speed = 0.0
        for v in self.vehicles.values():
            v.step(dt)
        for h in self.hostiles:
            if h.present(self.now) and h.moving(self.now):
                h.x += h.vx * dt
                h.y += h.vy * dt
        self.now += dt
        for shot in self.team.shots:
            if shot.result is None and shot.impact_at <= self.now + 1e-9:
                h = self.hostile(shot.target)
                if h is None or h.destroyed_at is not None:
                    shot.result = "NO_EFFECT"
                    continue
                h.hits += 1
                if h.hits >= self.hits_to_kill:
                    h.destroyed_at = self.now
                    shot.result = "DESTROYED"
                else:
                    shot.result = "DAMAGED"

    def hostile(self, hid: str | None) -> Hostile | None:
        return next((h for h in self.hostiles if h.id == hid), None)

    def visible(self, vehicle: Vehicle) -> list[dict[str, Any]]:
        """Tracks the vehicle's sensor holds now (ideal detection within range)."""
        sensor = vehicle.sensor
        if sensor is None or not sensor.on:
            return []
        tracks = []
        for h in self.hostiles:
            if not h.present(self.now):
                continue
            dx, dy = h.x - vehicle.x, h.y - vehicle.y
            rng = math.hypot(dx, dy)
            if rng <= sensor.range_m:
                lat, lon = self.geo.to_latlon(h.x, h.y)
                tracks.append(
                    {
                        "id": h.id,
                        "affiliation": h.affiliation,
                        "range_m": rng,
                        "bearing_deg_true": bearing_deg(dx, dy),
                        "latitude_deg": lat,
                        "longitude_deg": lon,
                        "speed_mps": h.speed_at(self.now),
                        "heading_deg_true": h.heading,
                    }
                )
        return sorted(tracks, key=lambda t: t["id"])
