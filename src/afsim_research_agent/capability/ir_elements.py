"""List the IR elements a normalized model uses, with where they are used.

Input is ``normalized_ir.json`` (coordination patterns already expanded), so generated
behaviors, conditions and messages are included.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

ElementKind = Literal["action", "operand", "function", "blackboard", "dependency"]


@dataclass
class UsedElement:
    name: str
    kind: ElementKind
    locations: list[str] = field(default_factory=list)
    platform_classes: set[str] = field(default_factory=set)


def _walk(node: Any, path: str) -> Iterator[tuple[str, dict[str, Any]]]:
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from _walk(value, f"{path}.{key}" if path else key)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _walk(value, f"{path}[{i}]")


def used_elements(ir: dict[str, Any]) -> dict[str, UsedElement]:
    """Element name → usage. Platform classes come from the roles whose behaviors or
    conditions use the element (conditions are shared, so they get every role's class)."""
    classes = {r["id"]: r["capability_class"] for r in ir.get("roles", [])}
    all_classes = set(classes.values())
    found: dict[str, UsedElement] = {}

    def add(name: str, kind: ElementKind, where: str, platform: set[str]) -> None:
        item = found.setdefault(name, UsedElement(name, kind))
        if where not in item.locations:
            item.locations.append(where)
        item.platform_classes |= platform

    for role in ir.get("roles", []):
        deps = role.get("dependencies") or {}
        comps = []
        if deps.get("mover"):
            comps.append(("mover", deps["mover"]))
        if deps.get("energy"):
            comps.append(("energy", deps["energy"]))
        for kind in ("sensors", "comms", "weapons", "processors"):
            comps += [(c.get("id", kind), c) for c in deps.get(kind) or []]
        for comp_id, comp in comps:
            add(
                f"dependency:{comp['capability']}",
                "dependency",
                f"roles.{role['id']}.dependencies.{comp_id}",
                {role["capability_class"]},
            )

    for i, role in enumerate(ir.get("roles", [])):
        # transition guards are evaluated by this role only
        for path, node in _walk(role.get("state_machine") or {}, f"roles[{i}].state_machine"):
            _add_node(add, node, path, {role["capability_class"]})

    for section in ("behaviors", "conditions", "trees", "coordination"):
        for path, node in _walk(ir.get(section, []), section):
            if section == "behaviors" and isinstance(node.get("command"), str):
                add(node["command"], "action", path, all_classes)
            _add_node(add, node, path, all_classes)
    return found


def _add_node(add: Any, node: dict[str, Any], path: str, classes: set[str]) -> None:
    # operand nodes appear wrapped ({"op": "operand", ...}) and bare (compare sides)
    kind = node.get("kind")
    if kind == "property" and "source" in node and "property" in node:
        add(f"{node['source']}.{node['property']}", "operand", path, classes)
    elif kind == "function" and "name" in node:
        add(node["name"], "function", path, classes)
    elif kind == "blackboard" and "var" in node:
        add("blackboard", "blackboard", path, classes)
