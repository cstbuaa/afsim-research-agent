"""Unit and frame conversions an engine adapter typically needs.

Canonical frame: local east-north metres (+x east, +y north), heading in degrees
clockwise from north, speeds in m/s.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

EARTH_RADIUS_M = 6_371_008.8

HEADING_CONVENTIONS = ("north_cw_deg", "north_cw_rad", "east_ccw_deg", "east_ccw_rad")
SPEED_UNITS = {
    "m/s": 1.0,
    "km/h": 1000.0 / 3600.0,
    "knots": 1852.0 / 3600.0,
    "mph": 1609.344 / 3600.0,
}


def heading_from_engine(value: float, convention: str) -> float:
    """Engine heading -> degrees clockwise from north in [0, 360)."""
    if convention == "north_cw_deg":
        deg = value
    elif convention == "north_cw_rad":
        deg = math.degrees(value)
    elif convention == "east_ccw_deg":
        deg = 90.0 - value
    elif convention == "east_ccw_rad":
        deg = 90.0 - math.degrees(value)
    else:
        raise ValueError(
            f"unknown heading convention '{convention}'; use one of {HEADING_CONVENTIONS}"
        )
    return deg % 360.0


def heading_to_engine(deg_north_cw: float, convention: str) -> float:
    if convention == "north_cw_deg":
        return deg_north_cw % 360.0
    if convention == "north_cw_rad":
        return math.radians(deg_north_cw % 360.0)
    if convention == "east_ccw_deg":
        return (90.0 - deg_north_cw) % 360.0
    if convention == "east_ccw_rad":
        return math.radians((90.0 - deg_north_cw) % 360.0)
    raise ValueError(f"unknown heading convention '{convention}'; use one of {HEADING_CONVENTIONS}")


def speed_from_engine(value: float, unit: str) -> float:
    return value * SPEED_UNITS[unit]


def speed_to_engine(mps: float, unit: str) -> float:
    return mps / SPEED_UNITS[unit]


@dataclass(frozen=True)
class LocalFrame:
    """Small-area equirectangular projection around (lat0, lon0); adequate for
    tactical areas of a few tens of kilometres."""

    lat0_deg: float
    lon0_deg: float

    def to_local(self, lat_deg: float, lon_deg: float) -> tuple[float, float]:
        x = (
            math.radians(lon_deg - self.lon0_deg)
            * EARTH_RADIUS_M
            * math.cos(math.radians(self.lat0_deg))
        )
        y = math.radians(lat_deg - self.lat0_deg) * EARTH_RADIUS_M
        return x, y

    def to_geodetic(self, x: float, y: float) -> tuple[float, float]:
        lat = self.lat0_deg + math.degrees(y / EARTH_RADIUS_M)
        lon = self.lon0_deg + math.degrees(
            x / (EARTH_RADIUS_M * math.cos(math.radians(self.lat0_deg)))
        )
        return lat, lon
