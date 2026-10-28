"""Scenario timing check: a deadline the platoon cannot physically meet.

A model writing an L3 expectation such as "in ``urban_approach`` within 120 s of start"
guesses the time; it does not divide the route distance by the speed. The 2-D
simulation then fails the expectation (and every expectation anchored to it) although
the behavior is right. This module estimates, before any simulation, when a route
follower can first reach the state such an expectation names, for the common case of
a terrain-distance guard (``terrain.distance_to_class("urban") <= d``) leaving the
initial state, and reports a deadline that is clearly too short.

The estimate is deliberately simple: straight route segments, the speed of the first
``FollowRoute`` of the initial state's tree, terrain regions as rectangles.
"""

from __future__ import annotations

import math
from typing import Any

from afsim_research_agent.ir.units import UnitError, to_canonical

SLACK = 1.15  # a deadline below estimate / SLACK is reported
STEP_M = 5.0


def _quantity(raw: Any, params: dict[str, Any], dimension: str) -> float | None:
    """{value, unit} or {parameter_ref} → canonical units (m, m/s, s)."""
    if isinstance(raw, dict) and "parameter_ref" in raw:
        p = params.get(raw["parameter_ref"])
        if p is None or p.unit is None:
            return None
        raw = {"value": p.value, "unit": p.unit}
    if isinstance(raw, dict) and {"value", "unit"} <= set(raw):
        try:
            return to_canonical(float(raw["value"]), str(raw["unit"]), dimension)
        except (UnitError, TypeError, ValueError):
            return None
    return None


def _route_points(route: list[list[float]]) -> list[tuple[float, float, float]]:
    """(x, y, distance along the route) every STEP_M metres."""
    out: list[tuple[float, float, float]] = []
    travelled = 0.0
    for (x0, y0), (x1, y1) in zip(route, route[1:]):
        length = math.hypot(x1 - x0, y1 - y0)
        steps = max(1, int(length // STEP_M))
        for k in range(steps):
            f = k / steps
            out.append((x0 + (x1 - x0) * f, y0 + (y1 - y0) * f, travelled + length * f))
        travelled += length
    if route:
        out.append((route[-1][0], route[-1][1], travelled))
    return out


def _rect_distance(x: float, y: float, r: dict[str, Any]) -> float:
    dx = max(r["x_min"] - x, 0.0, x - r["x_max"])
    dy = max(r["y_min"] - y, 0.0, y - r["y_max"])
    return math.hypot(dx, dy)


def _terrain_threshold(expression: Any, conditions: dict[str, Any], params: dict[str, Any]):
    """(terrain class, distance m) of a `distance_to_class(c) <= d` guard, else None."""
    seen: set[str] = set()

    def walk(node: Any):
        if not isinstance(node, dict):
            return None
        if "condition_ref" in node:
            ref = node["condition_ref"]
            if ref in seen or ref not in conditions:
                return None
            seen.add(ref)
            return walk(conditions[ref]["expression"])
        compare = node.get("compare")
        if isinstance(compare, dict):
            left = compare.get("left") or {}
            terrain = left.get("terrain_value") if isinstance(left, dict) else None
            if (
                isinstance(terrain, dict)
                and terrain.get("property") == "distance_to_class"
                and compare.get("operator") in ("lte", "lt")
            ):
                d = _quantity(compare.get("right"), params, "distance")
                if d is not None:
                    return str(terrain.get("class")), d
            return None
        for key in ("all",):  # a conjunction is reached no earlier than its terrain part
            for item in node.get(key) or []:
                found = walk(item)
                if found:
                    return found
        return None

    return walk(expression)


def _speed_of_state(compiler: Any, role: str, state: str) -> float | None:
    tree_id = compiler.state_trees.get((role, state))
    tree = next((t for t in compiler.trees if t.id == tree_id), None)
    if tree is None:
        return None
    for node in tree.nodes:
        behavior = compiler.behaviors.get(node.behavior or "")
        for action in (behavior or {}).get("actions") or []:
            if action.get("command") == "follow_route":
                return _quantity(action["arguments"].get("speed"), compiler.parameters, "speed")
    return None


def scenario_timing(compiler: Any) -> list[tuple[str, str]]:
    """(scenario id, message) for expectations whose start-anchored deadline is too short."""
    problems: list[tuple[str, str]] = []
    for scenario in compiler.scenarios:
        env = scenario.environment
        regions = list((env.terrain or {}).get("regions") or [])
        routes = env.routes or {}
        if not routes:
            continue
        points = _route_points(next(iter(routes.values())))
        for item in scenario.expect:
            window = item.within
            if (
                item.observable != "role_state"
                or window is None
                or (window.after_event or window.after or "start") != "start"
                or item.role not in compiler.diagrams
            ):
                continue
            role, target = item.role, str(item.equals)
            initial = compiler.diagrams[role].initial
            if not initial or target == initial:
                continue
            speed = _speed_of_state(compiler, role, initial)
            if not speed:
                continue
            for tr in compiler.transitions:
                if tr["role"] != role or tr["state"] != initial or tr["to"] != target:
                    continue
                found = _terrain_threshold(tr["when"], compiler.conditions, compiler.parameters)
                if not found:
                    continue
                cls, distance = found
                boxes = [r for r in regions if r.get("class") == cls and "x_min" in r]
                hit = next(
                    (
                        s
                        for x, y, s in points
                        if any(_rect_distance(x, y, b) <= distance for b in boxes)
                    ),
                    None,
                )
                if hit is None:
                    continue
                needed = hit / speed
                try:
                    deadline = to_canonical(float(window.value), str(window.unit), "time")
                except (UnitError, TypeError, ValueError):
                    continue
                if deadline * SLACK < needed:
                    problems.append(
                        (
                            scenario.id,
                            f"想定 {scenario.id} 的期望 {item.id}：{role} 要先沿路线行进约 {hit:.0f} m"
                            f"（到{cls}类地形 {distance:.0f} m 以内）才会进入 {target}，按 {speed * 3.6:.0f} km/h"
                            f" 约需 {needed:.0f} s，期限只有 {deadline:.0f} s。"
                            f"改用位置标记作锚点（events 加 {{type: marker, when: {{role: {role}, x_gte: …}}}}，"
                            "within.after 写该事件），或把期限放宽到估算值以上",
                        )
                    )
                break
    return problems
