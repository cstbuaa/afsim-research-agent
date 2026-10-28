"""Physical quantity types, units and conversion to canonical internal units."""

from __future__ import annotations

import math
from dataclasses import dataclass

# Canonical internal unit per dimension.
CANONICAL: dict[str, str] = {
    "distance": "m",
    "altitude": "m",
    "speed": "m/s",
    "time": "s",
    "angle": "deg",
    "mass": "kg",
    "frequency": "hz",
    "ratio": "ratio",
}

_FACTORS: dict[str, dict[str, float]] = {
    "distance": {"m": 1.0, "km": 1000.0, "nm": 1852.0, "ft": 0.3048, "mi": 1609.344},
    "speed": {
        "m/s": 1.0,
        "mps": 1.0,
        "km/h": 1000.0 / 3600.0,
        "kph": 1000.0 / 3600.0,
        "knots": 1852.0 / 3600.0,
        "kt": 1852.0 / 3600.0,
        "kts": 1852.0 / 3600.0,
        "mph": 1609.344 / 3600.0,
    },
    "time": {"s": 1.0, "sec": 1.0, "min": 60.0, "h": 3600.0, "hr": 3600.0},
    "angle": {"deg": 1.0, "rad": 180.0 / math.pi},
    "mass": {"kg": 1.0, "t": 1000.0, "lb": 0.45359237},
    "frequency": {"hz": 1.0, "khz": 1e3, "mhz": 1e6, "ghz": 1e9},
    "ratio": {"ratio": 1.0, "percent": 0.01, "%": 0.01},
}
_FACTORS["altitude"] = _FACTORS["distance"]

PHYSICAL_TYPES = frozenset(CANONICAL)
SCALAR_PARAMETER_TYPES = frozenset({"integer", "boolean", "string"})
PARAMETER_TYPES = PHYSICAL_TYPES | SCALAR_PARAMETER_TYPES


class UnitError(ValueError):
    """Unknown unit or dimension mismatch."""


@dataclass(frozen=True)
class Quantity:
    value: float
    dimension: str

    def __str__(self) -> str:
        return f"{self.value:g} {CANONICAL[self.dimension]}"


def known_units(dimension: str) -> list[str]:
    return sorted(_FACTORS.get(dimension, {}))


def dimension_of_unit(unit: str) -> list[str]:
    """Return every dimension accepting ``unit`` (distance and altitude share units)."""
    key = unit.strip().lower()
    return [dim for dim, table in _FACTORS.items() if key in table]


def to_canonical(value: float, unit: str, dimension: str) -> float:
    table = _FACTORS.get(dimension)
    if table is None:
        raise UnitError(f"'{dimension}' is not a physical dimension")
    factor = table.get(unit.strip().lower())
    if factor is None:
        raise UnitError(
            f"unit '{unit}' is not valid for {dimension}; expected one of {known_units(dimension)}"
        )
    return float(value) * factor


def compatible(a: str, b: str) -> bool:
    """Distance and altitude are the same physical dimension."""
    group = {"altitude": "distance"}
    return group.get(a, a) == group.get(b, b)


def quantity_from_mapping(raw: object, dimension: str) -> float:
    """Convert ``{value, unit}`` to canonical units; raises UnitError."""
    if not isinstance(raw, dict) or "value" not in raw or "unit" not in raw:
        raise UnitError(f"expected {{value, unit}} for a {dimension} quantity")
    value = raw["value"]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise UnitError("quantity value must be a number")
    return to_canonical(float(value), str(raw["unit"]), dimension)


def infer_dimension(unit: str) -> str | None:
    dims = dimension_of_unit(unit)
    if not dims:
        return None
    return "distance" if "distance" in dims else dims[0]
