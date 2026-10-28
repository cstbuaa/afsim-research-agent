"""Static types of IR expressions and operands."""

from __future__ import annotations

from afsim_research_agent.ir.units import PHYSICAL_TYPES, compatible

BOOL = "bool"
INT = "int"
DOUBLE = "double"
STRING = "string"
NUMBER = "number"  # untyped numeric literal
ANY = "any"

PARAMETER_TYPE_MAP = {"integer": INT, "boolean": BOOL, "string": STRING}


def parameter_value_type(parameter_type: str) -> str:
    return PARAMETER_TYPE_MAP.get(parameter_type, parameter_type)


def is_quantity(kind: str | None) -> bool:
    return kind in PHYSICAL_TYPES


def is_numeric(kind: str | None) -> bool:
    return kind in (INT, DOUBLE, NUMBER) or (kind is not None and is_quantity(kind))


def comparable(left: str, right: str) -> tuple[bool, str]:
    """Return (ok, reason) for comparing two typed operands with eq/ne/lt/..."""
    if ANY in (left, right):
        return True, ""
    if is_quantity(left) and is_quantity(right):
        return (compatible(left, right), "quantity dimensions differ")
    if is_quantity(left) or is_quantity(right):
        other = right if is_quantity(left) else left
        if other == NUMBER:
            return True, ""
        if other in (INT, DOUBLE):
            return True, "unitless"
        return False, f"cannot compare a quantity with {other}"
    if left in (INT, DOUBLE, NUMBER) and right in (INT, DOUBLE, NUMBER):
        return True, ""
    if left == right:
        return True, ""
    return False, f"cannot compare {left} with {right}"


def literal_type(value: object) -> str | None:
    if isinstance(value, bool):
        return BOOL
    if isinstance(value, int | float):
        return NUMBER
    if isinstance(value, str):
        return STRING
    return None


def assignable(target: str, source: str) -> bool:
    """Can a value of ``source`` type be stored in a variable/argument of ``target`` type?"""
    if source == ANY or target == ANY:
        return True
    if target == source:
        return True
    if target == DOUBLE and source in (INT, NUMBER):
        return True
    if target == INT and source == NUMBER:
        return True
    if is_quantity(target) and is_quantity(source):
        return compatible(target, source)
    return False
