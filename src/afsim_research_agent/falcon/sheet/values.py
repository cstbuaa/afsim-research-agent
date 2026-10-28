"""Small value helpers: "50 m" quantities, basis shorthands, ``-2 * column_interval``."""

from __future__ import annotations

import difflib
import re
from typing import Any

from afsim_research_agent.ir.units import PHYSICAL_TYPES, infer_dimension

_QUANTITY = re.compile(
    r"^\s*(?P<num>[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*(?P<unit>[A-Za-z/%]+)\s*$"
)
_ID = re.compile(r"^[a-z][a-z0-9_]*$")
BASIS_STATUSES = ("explicit", "inferred", "conflicting", "unknown", "user_specified")


def parse_quantity(value: Any) -> tuple[float, str, str] | None:
    """(number, unit, dimension) for "50 m", "1.5 km", "30 s"; None otherwise."""
    if not isinstance(value, str):
        return None
    match = _QUANTITY.match(value)
    if not match:
        return None
    unit = match.group("unit")
    dimension = infer_dimension(unit)
    if dimension is None:
        return None
    number = float(match.group("num"))
    return (int(number) if number.is_integer() else number), unit, dimension


def quantity(value: Any) -> dict[str, Any] | None:
    parsed = parse_quantity(value)
    if parsed is None:
        return None
    return {"value": parsed[0], "unit": parsed[1]}


def is_id(text: Any) -> bool:
    return isinstance(text, str) and bool(_ID.match(text)) and "__" not in text


def slug(text: str, fallback: str = "x") -> str:
    out = re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")
    out = re.sub(r"_+", "_", out)
    if not out or not out[0].isalpha():
        out = f"{fallback}_{out}" if out else fallback
    return out[:60].rstrip("_")


def near(name: str, choices: list[str] | set[str], n: int = 3) -> list[str]:
    return difflib.get_close_matches(str(name), sorted(choices), n=n, cutoff=0.5)


def basis_from(spec: Any, parent: dict[str, Any] | None = None) -> dict[str, Any]:
    """``user_specified`` | {status, evidence, rationale, assumptions}; the keys may also
    sit directly on the element (``basis: inferred, evidence: [...]``)."""
    out: dict[str, Any] = {}
    if isinstance(spec, str):
        out["status"] = spec.strip()
    elif isinstance(spec, dict):
        out.update(
            {
                k: v
                for k, v in spec.items()
                if k in ("status", "evidence", "rationale", "assumptions")
            }
        )
    if parent:
        for key in ("evidence", "rationale", "assumptions"):
            if key in parent and key not in out:
                out[key] = parent[key]
    if out.get("status") not in BASIS_STATUSES:
        out["status"] = "unknown"
    for key in ("evidence", "assumptions"):
        value = out.get(key)
        if isinstance(value, str):
            out[key] = [value]
        elif value is not None and not isinstance(value, list):
            out[key] = [str(value)]
    if out.get("rationale") is not None:
        out["rationale"] = str(out["rationale"])
    return out


_SCALED = re.compile(
    r"^\s*(?:(?P<pre>[-+]?\d+(?:\.\d+)?)\s*\*\s*)?(?P<sign>[-+]?)\s*(?P<name>[a-z][a-z0-9_]*)"
    r"\s*(?:\*\s*(?P<post>[-+]?\d+(?:\.\d+)?))?\s*$"
)


def distance_ref(value: Any, parameters: set[str]) -> tuple[dict[str, Any] | None, str | None]:
    """``-2 * column_interval`` → {parameter_ref, scale}; ``0 m`` → literal_distance.

    Returns (ref, error)."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return None, f'距离 {value} 缺少单位（写成 "{value} m"）'
    if not isinstance(value, str):
        return None, f"无法理解的距离 {value!r}"
    q = parse_quantity(value)
    if q is not None:
        if q[2] not in ("distance", "altitude"):
            return None, f"'{value}' 不是距离"
        return {"literal_distance": {"value": q[0], "unit": q[1]}}, None
    match = _SCALED.match(value)
    if not match:
        return None, f"无法理解的距离表达式 '{value}'（写成 参数名、k * 参数名 或 \"50 m\"）"
    name = match.group("name")
    if name not in parameters:
        return None, f"未定义的参数 '{name}'" + (
            f"（可选：{', '.join(near(name, parameters))}）" if near(name, parameters) else ""
        )
    scale = float(match.group("pre") or 1) * float(match.group("post") or 1)
    if match.group("sign") == "-":
        scale = -scale
    ref: dict[str, Any] = {"parameter_ref": name}
    if scale != 1:
        ref["scale"] = int(scale) if float(scale).is_integer() else scale
    return ref, None


def operand_value(value: Any, parameters: set[str]) -> dict[str, Any]:
    """A YAML scalar as an IR operand: parameter name, "30 s" quantity or literal."""
    if isinstance(value, str) and value in parameters:
        return {"parameter_ref": value}
    q = quantity(value)
    if q is not None:
        return q
    return {"literal": value}


def parameter_type(value: Any, explicit: str | None) -> tuple[str | None, Any, str | None]:
    """(IR type, value, unit) of a parameter value written as "50 m", 3, true, "text"."""
    q = parse_quantity(value)
    if q is not None:
        dimension = explicit if explicit in PHYSICAL_TYPES else q[2]
        return dimension, q[0], q[1]
    if isinstance(value, bool):
        return "boolean", value, None
    if isinstance(value, int):
        return explicit or "integer", value, None
    if isinstance(value, float):
        return explicit or "ratio", value, "ratio" if (explicit or "ratio") == "ratio" else None
    if isinstance(value, str):
        return "string", value, None
    return explicit, value, None
