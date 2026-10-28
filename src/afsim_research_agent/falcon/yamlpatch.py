"""In-place YAML edits that keep comments and formatting.

PyYAML nodes carry exact source positions, so a value can be replaced by
rewriting only its text span, and a missing key can be inserted next to its
siblings. Nothing else in the file is touched. (No extra dependency is needed;
the document is re-composed after every edit.)
"""

from __future__ import annotations

import json
import re
from typing import Any

import yaml

Segment = str | int
_SEGMENT = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


class PatchError(ValueError):
    pass


def parse_path(path: str) -> list[Segment]:
    out: list[Segment] = []
    for name, index in _SEGMENT.findall(path):
        out.append(int(index) if index else name)
    return out


def _compose(text: str) -> yaml.Node:
    node = yaml.compose(text, Loader=yaml.SafeLoader)
    if node is None:
        raise PatchError("empty document")
    return node


def find(node: yaml.Node, segments: list[Segment]) -> yaml.Node | None:
    for segment in segments:
        if isinstance(segment, int):
            if not isinstance(node, yaml.SequenceNode) or segment >= len(node.value):
                return None
            node = node.value[segment]
        else:
            if not isinstance(node, yaml.MappingNode):
                return None
            match = next((v for k, v in node.value if k.value == segment), None)
            if match is None:
                return None
            node = match
    return node


def render(value: Any) -> str:
    """Flow-safe YAML for a value (strings double-quoted, valid inside {...})."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(render(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {render(v)}" for k, v in value.items()) + "}"
    raise PatchError(f"cannot render {type(value).__name__}")


def set_key(text: str, mapping_path: str, key: str, value: Any) -> str:
    """Set ``key`` inside the mapping at ``mapping_path`` to ``value``."""
    root = _compose(text)
    node = find(root, parse_path(mapping_path))
    if not isinstance(node, yaml.MappingNode):
        raise PatchError(f"{mapping_path} is not a mapping")
    for k, v in node.value:
        if k.value == key:
            start, end = v.start_mark.index, v.end_mark.index
            old = text[start:end]
            new = render(value)
            if old.endswith("\n") and not node.flow_style:
                new += "\n"
            return text[:start] + new + text[end:]
    if node.flow_style or not node.value:
        start, end = node.start_mark.index, node.end_mark.index
        current = yaml.safe_load(text[start:end]) or {}
        current[key] = value
        return text[:start] + render(current) + text[end:]
    indent = node.value[0][0].start_mark.column
    pos = node.value[-1][1].end_mark.index
    while pos > 0 and text[pos - 1] in " \t\n\r":
        pos -= 1
    line_end = text.find("\n", pos)
    if line_end == -1:
        line_end = len(text)
    insertion = "\n" + " " * indent + f"{key}: {render(value)}"
    return text[:line_end] + insertion + text[line_end:]


def remove_key(text: str, mapping_path: str, key: str) -> str:
    """Set a key to null if present (removal would disturb neighbouring comments)."""
    root = _compose(text)
    node = find(root, parse_path(mapping_path))
    if isinstance(node, yaml.MappingNode) and any(k.value == key for k, _ in node.value):
        return set_key(text, mapping_path, key, None)
    return text
