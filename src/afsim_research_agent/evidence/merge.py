"""Evidence aggregation with explicit conflict preservation.

Generic helpers shared by Falcon's evidence normalization: unit recognition
for quantities quoted in sources, conversion to canonical units, and the
deterministic interval-conflict rule (two numeric claims about the same
proposition conflict when their ranges do not overlap after conversion).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from afsim_research_agent.ir.units import UnitError, infer_dimension, to_canonical

# Spellings seen in doctrine, papers and Chinese sources -> IR unit symbols.
UNIT_ALIASES: dict[str, str] = {
    "m": "m",
    "meter": "m",
    "meters": "m",
    "metre": "m",
    "metres": "m",
    "米": "m",
    "km": "km",
    "kilometer": "km",
    "kilometers": "km",
    "kilometre": "km",
    "kilometres": "km",
    "公里": "km",
    "千米": "km",
    "ft": "ft",
    "feet": "ft",
    "foot": "ft",
    "mi": "mi",
    "mile": "mi",
    "miles": "mi",
    "nm": "nm",
    "nmi": "nm",
    "nautical mile": "nm",
    "nautical miles": "nm",
    "海里": "nm",
    "m/s": "m/s",
    "mps": "m/s",
    "米/秒": "m/s",
    "km/h": "km/h",
    "kph": "km/h",
    "kmh": "km/h",
    "km/hr": "km/h",
    "kilometers per hour": "km/h",
    "公里/小时": "km/h",
    "千米/小时": "km/h",
    "mph": "mph",
    "miles per hour": "mph",
    "kt": "kt",
    "kts": "kt",
    "knot": "kt",
    "knots": "kt",
    "节": "kt",
    "s": "s",
    "sec": "s",
    "second": "s",
    "seconds": "s",
    "秒": "s",
    "min": "min",
    "mins": "min",
    "minute": "min",
    "minutes": "min",
    "分钟": "min",
    "分": "min",
    "h": "h",
    "hr": "h",
    "hrs": "h",
    "hour": "h",
    "hours": "h",
    "小时": "h",
    "deg": "deg",
    "degree": "deg",
    "degrees": "deg",
    "°": "deg",
    "度": "deg",
    "%": "percent",
    "percent": "percent",
    "百分比": "percent",
}

_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:[.,]\d+)*")


def canonical_unit(unit: str) -> tuple[str, str] | None:
    """Return ``(ir_unit, dimension)`` for a quoted unit, or ``None``."""
    key = " ".join(unit.strip().lower().split())
    symbol = UNIT_ALIASES.get(key, key)
    dimension = infer_dimension(symbol)
    if dimension is None:
        return None
    try:
        to_canonical(1.0, symbol, dimension)
    except UnitError:
        return None
    return symbol, dimension


def numbers_in(text: str) -> list[float]:
    """Numbers written with digits ("1,000", "2.5", "50–100" -> 50, 100)."""
    out: list[float] = []
    for match in _NUMBER.finditer(text):
        raw = match.group(0)
        if raw.count(",") and all(len(p) == 3 for p in raw.split(",")[1:]):
            raw = raw.replace(",", "")
        elif "," in raw:
            raw = raw.replace(",", ".")
        try:
            out.append(abs(float(raw)))
        except ValueError:
            continue
    return out


def number_present(value: float, text: str) -> bool:
    return any(abs(n - abs(value)) <= 1e-9 * max(1.0, abs(value)) for n in numbers_in(text))


@dataclass(frozen=True)
class Interval:
    low: float
    high: float
    dimension: str

    def overlaps(self, other: Interval, tolerance: float = 1e-9) -> bool:
        if self.dimension != other.dimension:
            return True  # different quantities cannot be compared
        return self.low <= other.high + tolerance and other.low <= self.high + tolerance

    def contains(self, value: float, tolerance: float = 1e-6) -> bool:
        scale = max(1.0, abs(value))
        return self.low - tolerance * scale <= value <= self.high + tolerance * scale


def interval(minimum: float, maximum: float, unit: str, dimension: str) -> Interval:
    low = to_canonical(minimum, unit, dimension)
    high = to_canonical(maximum, unit, dimension)
    return Interval(min(low, high), max(low, high), dimension)


def disjoint_pairs(items: list[tuple[str, Interval]]) -> list[tuple[str, str]]:
    """Pairs of ids whose intervals do not overlap (same dimension only)."""
    pairs: list[tuple[str, str]] = []
    for i, (a, ia) in enumerate(items):
        for b, ib in items[i + 1 :]:
            if ia.dimension == ib.dimension and not ia.overlaps(ib):
                pairs.append((a, b))
    return pairs
