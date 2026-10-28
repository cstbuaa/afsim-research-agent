"""Versioned catalogs: actions, operands/functions and coordination patterns."""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from typing import Any


@dataclass(frozen=True)
class ArgumentDef:
    name: str
    type: str
    required: bool = False


@dataclass(frozen=True)
class ActionDef:
    name: str
    category: str
    aliases: tuple[str, ...]
    requires: tuple[str, ...]
    resources: tuple[str, ...]
    external: bool
    every_tick: bool
    every_tick_forbidden: bool
    capability_classes: tuple[str, ...]
    arguments: dict[str, ArgumentDef]


@dataclass(frozen=True)
class PropertyDef:
    name: str
    type: str
    default: Any
    argument: str | None = None
    vocabulary: str | None = None


@dataclass(frozen=True)
class OperandDef:
    name: str
    component: str
    aliases: tuple[str, ...]
    properties: dict[str, PropertyDef]


@dataclass(frozen=True)
class FunctionDef:
    name: str
    returns: str
    arguments: dict[str, str]


@dataclass(frozen=True)
class PatternDef:
    name: str
    description: str
    required: tuple[str, ...]
    optional: tuple[str, ...]
    supports_sync: bool
    groups: tuple[str, ...]
    generated: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class Catalog:
    actions: dict[str, ActionDef]
    action_aliases: dict[str, str]
    operands: dict[str, OperandDef]
    operand_aliases: dict[str, str]
    functions: dict[str, FunctionDef]
    patterns: dict[str, PatternDef]
    vocabularies: dict[str, tuple[str, ...]]
    versions: dict[str, str] = field(default_factory=dict)
    settings: dict[str, float] = field(default_factory=dict)

    def action(self, name: str) -> ActionDef | None:
        return self.actions.get(self.action_aliases.get(name, name))

    def canonical_action(self, name: str) -> str:
        return self.action_aliases.get(name, name)

    def operand(self, name: str) -> OperandDef | None:
        return self.operands.get(self.operand_aliases.get(name, name))

    def canonical_operand(self, name: str) -> str:
        return self.operand_aliases.get(name, name)


DEFAULT_RESOURCES = ("comm_control", "movement_control", "sensor_control", "weapon_control")


def _read(name: str) -> dict[str, Any]:
    text = resources.files(__package__).joinpath(name).read_text(encoding="utf-8")
    return tomllib.loads(text)


def _default(raw: Any) -> Any:
    if raw == "inf":
        return math.inf
    return raw


@lru_cache(maxsize=1)
def load_catalog() -> Catalog:
    actions_raw = _read("actions.toml")
    operands_raw = _read("operands.toml")
    coord_raw = _read("coordination.toml")

    actions: dict[str, ActionDef] = {}
    aliases: dict[str, str] = {}
    for item in actions_raw["action"]:
        args = {
            arg_name: ArgumentDef(arg_name, spec["type"], bool(spec.get("required", False)))
            for arg_name, spec in item.get("arguments", {}).items()
        }
        definition = ActionDef(
            name=item["name"],
            category=item["category"],
            aliases=tuple(item.get("aliases", [])),
            requires=tuple(item.get("requires", [])),
            resources=tuple(item.get("resources", [])),
            external=bool(item.get("external", True)),
            every_tick=bool(item.get("every_tick", False)),
            every_tick_forbidden=bool(item.get("every_tick_forbidden", False)),
            capability_classes=tuple(item.get("capability_classes", [])),
            arguments=args,
        )
        actions[definition.name] = definition
        for alias in definition.aliases:
            aliases[alias] = definition.name

    operands: dict[str, OperandDef] = {}
    operand_aliases: dict[str, str] = {}
    for name, spec in operands_raw["operand"].items():
        props = {
            prop: PropertyDef(
                prop,
                p["type"],
                _default(p.get("default")),
                p.get("argument"),
                p.get("vocabulary"),
            )
            for prop, p in spec.get("properties", {}).items()
        }
        operands[name] = OperandDef(
            name, spec.get("component", ""), tuple(spec.get("aliases", [])), props
        )
        for alias in spec.get("aliases", []):
            operand_aliases[alias] = name

    functions = {
        name: FunctionDef(name, spec["returns"], dict(spec.get("arguments", {})))
        for name, spec in operands_raw["function"].items()
    }
    patterns = {
        name: PatternDef(
            name=name,
            description=spec.get("description", ""),
            required=tuple(spec.get("required", [])),
            optional=tuple(spec.get("optional", [])),
            supports_sync=bool(spec.get("supports_sync", False)),
            groups=tuple(spec.get("groups", [])),
            generated={k: tuple(v) for k, v in spec.get("generated", {}).items()},
        )
        for name, spec in coord_raw["pattern"].items()
    }
    intents_raw = _read("intents.toml")
    vocab = {
        "terrain_classes": tuple(operands_raw["meta"]["terrain_classes"]),
        "intents": tuple(intents_raw["meta"]["intents"]),
    }
    versions = {
        "actions": actions_raw["meta"]["version"],
        "operands": operands_raw["meta"]["version"],
        "coordination": coord_raw["meta"]["version"],
        "intents": intents_raw["meta"]["version"],
    }
    return Catalog(
        actions=actions,
        action_aliases=aliases,
        operands=operands,
        operand_aliases=operand_aliases,
        functions=functions,
        patterns=patterns,
        vocabularies=vocab,
        versions=versions,
        settings={
            "default_resync_s": float(coord_raw["meta"].get("default_resync_s", 5.0)),
            "standoff_margin_m": float(coord_raw["meta"].get("standoff_margin_m", 10.0)),
        },
    )
