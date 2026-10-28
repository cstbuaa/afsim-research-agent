"""Mermaid diagrams: one state diagram and one flowchart per tree, per role."""

from __future__ import annotations

from typing import Any

from afsim_research_agent.ir.backends.common import describe_expr, header, trees_for_role

TEMPLATE_VERSION = "mermaid-2026.10.1"

_SHAPES = {
    "sequence": ("[", "]", "→ "),
    "sequence_with_memory": ("[", "]", "→* "),
    "selector": ("{{", "}}", "? "),
    "selector_with_memory": ("{{", "}}", "?* "),
    "priority_selector": ("{{", "}}", "?prio "),
    "weighted_random": ("{{", "}}", "?rand "),
    "parallel": ("[/", "/]", "⇉ "),
    "decorator": ("[\\", "\\]", "◇ "),
    "condition": ("([", "])", ""),
    "behavior": ("[", "]", ""),
    "subtree": ("[[", "]]", ""),
}


def _text(value: str) -> str:
    return value.replace('"', "'").replace("\n", " ")


def _label(node: dict[str, Any]) -> str:
    kind = node["type"]
    _, _, prefix = _SHAPES[kind]
    detail = {
        "behavior": node.get("behavior"),
        "condition": node.get("condition"),
        "subtree": node.get("tree"),
        "decorator": node.get("decorator"),
    }.get(kind)
    text = f"{prefix}{node['node_id']}"
    if detail:
        text += f"<br/>{detail}"
    if kind == "parallel":
        text += f"<br/>ok:{node['success_policy']['type']} fail:{node['failure_policy']['type']}"
    return _text(text)


def state_diagram(ir: dict[str, Any], role: dict[str, Any]) -> str:
    machine = role["state_machine"]
    lines = ["stateDiagram-v2", header("mermaid", ir, TEMPLATE_VERSION, "    %%").rstrip("\n")]
    lines.append(f"    [*] --> {machine['initial_state']}")
    for state in machine["states"]:
        note = f"tree: {state['tree']}" if state.get("tree") else "terminal"
        lines.append(f"    {state['id']} : {_text(note)}")
        if state["terminal"]:
            lines.append(f"    {state['id']} --> [*]")
            continue
        for tr in state["transitions"]:
            if tr["fallback"] and tr["to"] == state["id"]:
                continue
            tag = " [sync]" if "generated_from" in tr else ""
            sync = f" ⇒ {tr['synchronize_with']}" if tr.get("synchronize_with") else ""
            cond = describe_expr(tr["when"])
            if len(cond) > 60:
                cond = cond[:57] + "..."
            label = _text(f"{tr['transition_id']} p{tr['priority']}{tag} / {cond}{sync}").replace(
                ":", " "
            )
            lines.append(f"    {state['id']} --> {tr['to']} : {label}")
    return "\n".join(lines) + "\n"


def flowchart(ir: dict[str, Any], tree: dict[str, Any]) -> str:
    lines = ["flowchart TD", header("mermaid", ir, TEMPLATE_VERSION, "    %%").rstrip("\n")]

    def walk(node: dict[str, Any]) -> None:
        left, right, _ = _SHAPES[node["type"]]
        lines.append(f'    n_{node["node_id"]}{left}"{_label(node)}"{right}')
        for index, child in enumerate(node.get("children", [])):
            walk(child)
            lines.append(f"    n_{node['node_id']} -->|{index + 1}| n_{child['node_id']}")

    walk(tree["root"])
    return "\n".join(lines) + "\n"


def compile(
    ir: dict[str, Any], context: dict[str, Any], catalog_versions: dict[str, str]
) -> dict[str, str]:
    files: dict[str, str] = {}
    trees = {t["id"]: t for t in ir["trees"]}
    for role in ir["roles"]:
        if role["state_machine"]:
            files[f"{role['id']}_state_machine.mmd"] = state_diagram(ir, role)
        for tree_id in trees_for_role(ir, role):
            files[f"{role['id']}_{tree_id}.mmd"] = flowchart(ir, trees[tree_id])
    return files
