"""IR diagnostic path → tactic sheet block and line (design §3.3).

The assembled ``behavior_spec`` mapping translates list indices into element IDs;
the compiler's source map gives each element's block and line.
"""

from __future__ import annotations

import re
from typing import Any

_INDEX = re.compile(r"^(?P<key>[A-Za-z_]+)\[(?P<i>\d+)\](?P<rest>.*)$")
_ROLE_TR = re.compile(r"^roles\.(?P<role>[\w]+)\.transitions\.(?P<tid>[\w]+)")
_GENERATED = re.compile(r"^coordination\.(?P<cid>[\w]+)\(generated \w+ (?P<gid>[\w]+)\)")
_ROLE_IN_MESSAGE = re.compile(r"role '(?P<role>[\w]+)'")
_STATE = re.compile(r"^\.state_machine\.states\[(?P<s>\d+)\](?:\.transitions\[(?P<t>\d+)\])?")

SECTION_BLOCK = {
    "parameters": "model",
    "structure": "model",
    "conditions": "conditions",
    "verification": "verification",
}


def _node(root: dict[str, Any], rest: str) -> str | None:
    """trees[i].root.children[1].children[0] → that node's id."""
    node = root
    for index in re.findall(r"children\[(\d+)\]", rest):
        children = node.get("children") or []
        if int(index) >= len(children):
            break
        node = children[int(index)]
    return node.get("node_id")


def element_key(path: str, data: dict[str, Any]) -> str | None:
    if path.startswith("parameters."):
        return f"parameter:{path.split('.')[1].split('[')[0]}"
    match = _ROLE_TR.match(path)
    if match:
        return f"transition:{match.group('tid')}"
    match = _GENERATED.match(path)
    if match:
        return f"coordination:{match.group('cid')}"
    match = _INDEX.match(path)
    if not match:
        return None
    key, i, rest = match.group("key"), int(match.group("i")), match.group("rest")
    items = data.get(key)
    if not isinstance(items, list) or i >= len(items):
        return None
    item = items[i]
    if key == "roles":
        sm = _STATE.match(rest)
        machine = item.get("state_machine") or {}
        if sm:
            states = machine.get("states") or []
            s = int(sm.group("s"))
            if s < len(states):
                state = states[s]
                if sm.group("t") is not None:
                    transitions = state.get("transitions") or []
                    t = int(sm.group("t"))
                    if t < len(transitions):
                        return f"transition:{transitions[t]['transition_id']}"
                return f"state:{item['id']}.{state['id']}"
        return f"role:{item['id']}"
    if key == "trees":
        node = _node(item.get("root") or {}, rest)
        return f"node:{node}" if node and "children" in rest else f"tree:{item['id']}"
    kind = {
        "conditions": "condition",
        "behaviors": "behavior",
        "formations": "formation",
        "coordination": "coordination",
        "tests": "test",
        "scenarios": "scenario",
        "assumptions": "assumption",
        "messages": "message",
    }.get(key)
    return f"{kind}:{item.get('id')}" if kind else None


def tree_block(role: str, block_keys: list[str]) -> str | None:
    """The block holding a role's trees (XML or Python form)."""
    for kind in ("xml", "pytree"):
        if f"{kind}:{role}" in block_keys:
            return f"{kind}:{role}"
    return None


def locate(
    path: str,
    section: str | None,
    data: dict[str, Any],
    source_map: dict[str, dict[str, Any]],
    block_keys: list[str],
    message: str = "",
) -> tuple[str, int | None]:
    """(block key, line) to show and repair; falls back to the section's block."""
    if "not exercised by any L2 test" in message:
        return "verification", None  # a transition without a test: the tests are missing
    generated = _GENERATED.match(path)
    role = _ROLE_IN_MESSAGE.search(message)
    if generated and role:
        # a generated element used by the wrong role: the fix is in that role's tree
        use = source_map.get(f"use:{generated.group('gid')}:{role.group('role')}")
        if use:
            return use["block"], use.get("line")
        uses = [
            v
            for k, v in source_map.items()
            if k.endswith(f":{role.group('role')}") and k.startswith("use:")
        ]
        tree = tree_block(role.group("role"), block_keys)
        if tree:
            return tree, uses[0].get("line") if uses else None
    key = element_key(path, data)
    if key and key.startswith("state:") and "tree '" in message:
        # a state without its tree: the tree belongs in that role's XML
        role_id = key.split(":", 1)[1].split(".")[0]
        tree = tree_block(role_id, block_keys)
        if tree:
            return tree, None
    if key and key in source_map:
        where = source_map[key]
        return where["block"], where.get("line")
    if key and key.startswith("state:"):
        role = key.split(":", 1)[1].split(".")[0]
        return f"mermaid:{role}", None
    if path.startswith("properties."):
        # a requirement property names the role whose behavior misses it
        role = re.search(r"角色 (?P<role>[\w]+) ", message)
        if role and f"mermaid:{role.group('role')}" in block_keys:
            return f"mermaid:{role.group('role')}", None
        return "model", None
    named = re.match(r"^(tests|scenarios)\.(?P<id>[\w-]+)", path)
    if named:
        kind = "test" if path.startswith("tests") else "scenario"
        where = source_map.get(f"{kind}:{named.group('id')}")
        if where:
            return where["block"], where.get("line")
    if path.startswith("scenarios") or path.startswith("tests"):
        return "verification", None
    block = SECTION_BLOCK.get(section or "")
    if block is None and section in ("behaviors", "control"):
        xml = [k for k in block_keys if k.startswith(("xml:", "pytree:"))]
        block = xml[0] if xml else "model"
    return block or "model", None
