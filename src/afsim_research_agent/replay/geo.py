"""WGS-84 earth-centred (WCS) ↔ geodetic and local east/north metres."""

from __future__ import annotations

import math

A = 6378137.0
F = 1 / 298.257223563
E2 = F * (2 - F)


def ecef_to_geodetic(x: float, y: float, z: float) -> tuple[float, float, float]:
    """(lat deg, lon deg, alt m); Bowring's method, millimetre accuracy at the surface."""
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    b = A * (1 - F)
    ep2 = (A * A - b * b) / (b * b)
    th = math.atan2(z * A, p * b)
    lat = math.atan2(z + ep2 * b * math.sin(th) ** 3, p - E2 * A * math.cos(th) ** 3)
    n = A / math.sqrt(1 - E2 * math.sin(lat) ** 2)
    alt = p / math.cos(lat) - n if abs(math.cos(lat)) > 1e-12 else abs(z) - b
    return math.degrees(lat), math.degrees(lon), alt


def geodetic_to_ecef(lat: float, lon: float, alt: float = 0.0) -> tuple[float, float, float]:
    la, lo = math.radians(lat), math.radians(lon)
    n = A / math.sqrt(1 - E2 * math.sin(la) ** 2)
    return (
        (n + alt) * math.cos(la) * math.cos(lo),
        (n + alt) * math.cos(la) * math.sin(lo),
        (n * (1 - E2) + alt) * math.sin(la),
    )


class Local:
    """East/north metres about an origin (lat, lon): ENU rotation of ECEF offsets."""

    def __init__(self, lat: float, lon: float) -> None:
        self.lat, self.lon = lat, lon
        self.o = geodetic_to_ecef(lat, lon)
        la, lo = math.radians(lat), math.radians(lon)
        self.e = (-math.sin(lo), math.cos(lo), 0.0)
        self.n = (-math.sin(la) * math.cos(lo), -math.sin(la) * math.sin(lo), math.cos(la))

    def from_ecef(self, x: float, y: float, z: float) -> tuple[float, float]:
        d = (x - self.o[0], y - self.o[1], z - self.o[2])
        return (
            sum(a * b for a, b in zip(d, self.e, strict=True)),
            sum(a * b for a, b in zip(d, self.n, strict=True)),
        )

    def vec(self, vx: float, vy: float, vz: float) -> tuple[float, float]:
        v = (vx, vy, vz)
        return (
            sum(a * b for a, b in zip(v, self.e, strict=True)),
            sum(a * b for a, b in zip(v, self.n, strict=True)),
        )

    def from_geodetic(self, lat: float, lon: float) -> tuple[float, float]:
        return self.from_ecef(*geodetic_to_ecef(lat, lon))
