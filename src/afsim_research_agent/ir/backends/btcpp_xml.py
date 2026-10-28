"""BehaviorTree.CPP v4 XML: one file per role.

Each role file holds every tree the role uses plus a ``<role>_controller`` main
tree that evaluates state transitions and dispatches to the active state's tree.
Tactical leaves are custom nodes (``TacticalBehavior``, ``CheckCondition``,
``EvaluateTransitions``) that a C++ host implements by reading the same
normalized IR; they are declared in ``TreeNodesModel``.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from afsim_research_agent.ir.backends.common import (
    describe_expr,
    describe_operand,
    header,
    trees_for_role,
)

TEMPLATE_VERSION = "btcpp_xml-2026.10.1"

CONTROL = {
    "sequence": "ReactiveSequence",
    "sequence_with_memory": "Sequence",
    "selector": "ReactiveFallback",
    "selector_with_memory": "Fallback",
    "priority_selector": "PrioritySelector",
    "weighted_random": "WeightedRandomSelector",
    "parallel": "Parallel",
}
DECORATOR = {
    "inverter": "Inverter",
    "negator": "Inverter",
    "succeeder": "ForceSuccess",
    "repeat_count": "Repeat",
    "repeat_until_done": "RepeatUntilDone",
    "repeat_for_duration": "RepeatForDuration",
}
CUSTOM_NODES = [
    ("Action", "TacticalBehavior", ["behavior"]),
    ("Condition", "CheckCondition", ["condition"]),
    ("Action", "EvaluateTransitions", ["role", "state"]),
    ("Control", "PrioritySelector", ["fitness"]),
    ("Control", "WeightedRandomSelector", ["weights"]),
    ("Decorator", "RepeatUntilDone", ["max_attempts", "until"]),
    ("Decorator", "RepeatForDuration", ["duration_s"]),
]


def _count(policy: dict[str, Any], n: int, *, success: bool) -> int:
    kind = policy["type"]
    if kind in ("all", "fail_on_all"):
        return n
    if kind in ("one", "fail_on_one"):
        return 1
    return int(policy.get("count") or n)


def _node(node: dict[str, Any]) -> ET.Element:
    kind = node["type"]
    if kind == "behavior":
        return ET.Element("TacticalBehavior", name=node["node_id"], behavior=node["behavior"])
    if kind == "condition":
        return ET.Element("CheckCondition", name=node["node_id"], condition=node["condition"])
    if kind == "subtree":
        return ET.Element("SubTree", ID=node["tree"], name=node["node_id"])
    children = node.get("children", [])
    if kind == "decorator":
        tag = DECORATOR[node["decorator"]]
        el = ET.Element(tag, name=node["node_id"])
        if node["decorator"] == "repeat_count":
            el.set("num_cycles", str(node["count"]))
        elif node["decorator"] == "repeat_until_done":
            el.set("max_attempts", str(node.get("count") or -1))
            if node.get("until"):
                el.set("until", describe_expr(node["until"]))
        elif node["decorator"] == "repeat_for_duration":
            el.set("duration_s", f"{node['duration_s']:g}")
    else:
        el = ET.Element(CONTROL[kind], name=node["node_id"])
        if kind == "parallel":
            n = len(children)
            el.set("success_count", str(_count(node["success_policy"], n, success=True)))
            el.set("failure_count", str(_count(node["failure_policy"], n, success=False)))
        elif kind == "priority_selector":
            el.set("fitness", ";".join(describe_operand(c["fitness"]) for c in children))
        elif kind == "weighted_random":
            el.set("weights", ";".join(describe_operand(c["weight"]) for c in children))
    for child in children:
        el.append(_node(child))
    return el


def role_xml(ir: dict[str, Any], role: dict[str, Any]) -> str:
    trees = {t["id"]: t for t in ir["trees"]}
    used = trees_for_role(ir, role)
    controller = f"{role['id']}_controller"
    root = ET.Element("root", BTCPP_format="4", main_tree_to_execute=controller)
    machine = role["state_machine"]
    main = ET.SubElement(root, "BehaviorTree", ID=controller)
    if machine:
        seq = ET.SubElement(main, "ReactiveSequence", name=f"{role['id']}_tick")
        ET.SubElement(
            seq,
            "EvaluateTransitions",
            name=f"{role['id']}_transitions",
            role=role["id"],
            state="{state}",
        )
        dispatch = ET.SubElement(seq, "ReactiveFallback", name=f"{role['id']}_dispatch")
        for state in machine["states"]:
            if state["terminal"] or not state.get("tree"):
                continue
            branch = ET.SubElement(dispatch, "ReactiveSequence", name=f"state_{state['id']}")
            ET.SubElement(branch, "ScriptCondition", code=f"state=='{state['id']}'")
            ET.SubElement(branch, "SubTree", ID=state["tree"], name=f"run_{state['id']}")
        ET.SubElement(dispatch, "AlwaysSuccess", name=f"{role['id']}_terminal")
    else:
        ET.SubElement(main, "SubTree", ID=role["root_tree"], name=f"{role['id']}_root")
    for tree_id in used:
        bt = ET.SubElement(root, "BehaviorTree", ID=tree_id)
        bt.append(_node(trees[tree_id]["root"]))
    model = ET.SubElement(root, "TreeNodesModel")
    for category, tag, ports in CUSTOM_NODES:
        el = ET.SubElement(model, category, ID=tag)
        for port in ports:
            direction = (
                "output_port" if port == "state" and tag == "EvaluateTransitions" else "input_port"
            )
            ET.SubElement(el, direction, name=port)
    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    comment = header("btcpp_xml", ir, TEMPLATE_VERSION, "  ").rstrip("\n")
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<!--\n{comment}\n-->\n{body}\n'


def compile(
    ir: dict[str, Any], context: dict[str, Any], catalog_versions: dict[str, str]
) -> dict[str, str]:
    return {f"{role['id']}.xml": role_xml(ir, role) for role in ir["roles"]}


# --------------------------------------------------------------------------- round trip

_REVERSE_CONTROL = {v: k for k, v in CONTROL.items()}


def _shape_from_ir(node: dict[str, Any]) -> tuple[Any, ...]:
    kind = node["type"]
    if kind == "decorator" and node["decorator"] == "negator":
        kind_key = ("decorator", "inverter")
    elif kind == "decorator":
        kind_key = ("decorator", node["decorator"])
    else:
        kind_key = (kind, node.get("behavior") or node.get("condition") or node.get("tree"))
    return (node["node_id"], kind_key, tuple(_shape_from_ir(c) for c in node.get("children", [])))


def _shape_from_xml(el: ET.Element) -> tuple[Any, ...]:
    tag = el.tag
    name = el.get("name", "")
    if tag == "TacticalBehavior":
        key: tuple[str, Any] = ("behavior", el.get("behavior"))
    elif tag == "CheckCondition":
        key = ("condition", el.get("condition"))
    elif tag == "SubTree":
        key = ("subtree", el.get("ID"))
    elif tag in _REVERSE_CONTROL:
        key = (_REVERSE_CONTROL[tag], None)
    else:
        rev = {v: k for k, v in DECORATOR.items() if k != "negator"}
        key = ("decorator", rev.get(tag, tag))
    return (name, key, tuple(_shape_from_xml(c) for c in el))


def verify_role_xml(ir: dict[str, Any], role: dict[str, Any], text: str) -> list[str]:
    """Parse generated XML back and check every tree matches the IR structure."""
    problems: list[str] = []
    root = ET.fromstring(text.split("-->", 1)[1])
    if root.get("BTCPP_format") != "4":
        problems.append("BTCPP_format must be 4")
    trees = {t["id"]: t for t in ir["trees"]}
    found = {bt.get("ID"): bt for bt in root.findall("BehaviorTree")}
    for tree_id in trees_for_role(ir, role):
        bt = found.get(tree_id)
        if bt is None or len(bt) != 1:
            problems.append(f"tree '{tree_id}' missing or malformed")
            continue
        if _shape_from_xml(bt[0]) != _shape_from_ir(trees[tree_id]["root"]):
            problems.append(f"tree '{tree_id}' structure differs from IR")
    controller = found.get(f"{role['id']}_controller")
    if controller is None:
        problems.append("controller tree missing")
    elif role["state_machine"]:
        states = [s["id"] for s in role["state_machine"]["states"] if not s["terminal"]]
        branches = [
            el.get("name")
            for el in controller.iter("ReactiveSequence")
            if el.get("name", "").startswith("state_")
        ]
        if branches != [f"state_{s}" for s in states]:
            problems.append("controller dispatch does not match the state machine")
    return problems
