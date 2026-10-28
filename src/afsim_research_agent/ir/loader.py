"""Strict YAML loading for ``behavior_spec.yaml`` (design §4).

Rejects tabs, anchors, aliases, custom tags, duplicate keys, interpolation
syntax and anything that looks like a credential. Uses a YAML 1.2-like
resolver: only ``true``/``false`` are booleans and timestamps stay strings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from afsim_research_agent.ir.diagnostics import Collector, Diagnostic

_INTERPOLATION = re.compile(r"\$\{|\$\(|\{\{|\{%|`")
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\btvly-[A-Za-z0-9]{16,}"),
)
_SECRET_KEYS = re.compile(r"(password|passwd|secret|api_key|apikey|token|private_key)$", re.I)


class _Resolver(yaml.resolver.Resolver):
    pass


_Resolver.yaml_implicit_resolvers = {}
for _first, _resolvers in yaml.resolver.Resolver.yaml_implicit_resolvers.items():
    kept = [
        (tag, regexp)
        for tag, regexp in _resolvers
        if tag not in ("tag:yaml.org,2002:bool", "tag:yaml.org,2002:timestamp")
    ]
    if kept:
        _Resolver.yaml_implicit_resolvers[_first] = kept
_Resolver.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$"), list("tf")
)


class _DuplicateKey(Exception):
    def __init__(self, key: str, line: int) -> None:
        super().__init__(key)
        self.key = key
        self.line = line


class _StrictLoader(
    yaml.reader.Reader,
    yaml.scanner.Scanner,
    yaml.parser.Parser,
    yaml.composer.Composer,
    yaml.constructor.SafeConstructor,
    _Resolver,
):
    def __init__(self, stream: str) -> None:
        yaml.reader.Reader.__init__(self, stream)
        yaml.scanner.Scanner.__init__(self)
        yaml.parser.Parser.__init__(self)
        yaml.composer.Composer.__init__(self)
        yaml.constructor.SafeConstructor.__init__(self)
        _Resolver.__init__(self)


def _construct_mapping(loader: _StrictLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in result:
            raise _DuplicateKey(str(key), key_node.start_mark.line + 1)
        result[key] = loader.construct_object(value_node, deep=True)
    return result


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


@dataclass
class LoadResult:
    data: dict[str, Any] | None
    diagnostics: list[Diagnostic] = field(default_factory=list)
    source_text: str = ""


def load_spec(path: str | Path) -> LoadResult:
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        diag = Collector()
        diag.error("IR002", "$", f"file is not valid UTF-8: {exc}")
        return LoadResult(None, diag.items)
    return load_spec_text(text)


def load_spec_text(text: str) -> LoadResult:
    diag = Collector()
    if text.startswith("﻿"):
        text = text[1:]
    for number, line in enumerate(text.splitlines(), start=1):
        indent = line[: len(line) - len(line.lstrip())]
        if "\t" in indent:
            diag.error("IR002", f"line {number}", "tab characters are not allowed in indentation")
    try:
        for token in yaml.scan(text, Loader=_StrictLoader):
            line = token.start_mark.line + 1
            if isinstance(token, yaml.AnchorToken):
                diag.error("IR002", f"line {line}", "YAML anchors are not allowed")
            elif isinstance(token, yaml.AliasToken):
                diag.error("IR002", f"line {line}", "YAML aliases are not allowed")
            elif isinstance(token, yaml.TagToken):
                diag.error("IR002", f"line {line}", "YAML tags are not allowed")
    except yaml.YAMLError as exc:
        diag.error("IR002", "$", f"YAML syntax error: {_short(exc)}")
        return LoadResult(None, diag.items, text)
    if diag.has_errors:
        return LoadResult(None, diag.items, text)
    try:
        data = yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - strict safe loader
    except _DuplicateKey as exc:
        diag.error("ID001", f"line {exc.line}", f"duplicate key '{exc.key}'")
        return LoadResult(None, diag.items, text)
    except yaml.YAMLError as exc:
        diag.error("IR002", "$", f"YAML syntax error: {_short(exc)}")
        return LoadResult(None, diag.items, text)
    if not isinstance(data, dict):
        diag.error("IR002", "$", "top level of behavior_spec.yaml must be a mapping")
        return LoadResult(None, diag.items, text)
    _scan_values(data, "$", diag)
    return LoadResult(None if diag.has_errors else data, diag.items, text)


def _scan_values(node: Any, path: str, diag: Collector) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path != "$" else str(key)
            if not isinstance(key, str):
                diag.error("IR002", child, "mapping keys must be strings")
            elif _SECRET_KEYS.search(key) and isinstance(value, str) and value:
                diag.error("IR002", child, "field name suggests a credential; remove it")
            _scan_values(value, child, diag)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _scan_values(value, f"{path}[{index}]", diag)
    elif isinstance(node, str):
        if _INTERPOLATION.search(node):
            diag.error("IR002", path, "interpolation or template syntax is not allowed in values")
        if any(pattern.search(node) for pattern in _SECRET_PATTERNS):
            diag.error("IR002", path, "value looks like a credential or private key; remove it")
    elif isinstance(node, float) and (node != node or node in (float("inf"), float("-inf"))):
        diag.error("IR002", path, "NaN and infinity are not allowed")


def _short(exc: Exception) -> str:
    return " ".join(str(exc).split())[:300]
