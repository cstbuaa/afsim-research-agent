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
    cmd_speed: float = 0.0
    cmd_heading: float | None = None
    sensor: Sensor | None = field(default_factory=Sensor)

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

    def present(self, now: float) -> bool:
        return now >= self.appear_s and (self.remove_s is None or now < self.remove_s)

    @property
    def speed(self) -> float:
        return math.hypot(self.vx, self.vy)

    @property
    def heading(self) -> float:
        return bearing_deg(self.vx, self.vy) if self.speed > 0 else 0.0


@dataclass
class World:
    geo: Geo = field(default_factory=Geo)
    vehicles: dict[str, Vehicle] = field(default_factory=dict)
    hostiles: list[Hostile] = field(default_factory=list)
    routes: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    now: float = 0.0

    def step(self, dt: float) -> None:
        for v in self.vehicles.values():
            v.step(dt)
        for h in self.hostiles:
            if h.present(self.now):
                h.x += h.vx * dt
                h.y += h.vy * dt
        self.now += dt

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
                        "speed_mps": h.speed,
                        "heading_deg_true": h.heading,
                    }
                )
        return sorted(tracks, key=lambda t: t["id"])
