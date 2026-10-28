"""Assemble skeleton + sections into a ``behavior_spec.yaml`` mapping and write it.

The model never writes YAML. Assembly problems (unknown role/state in a
transition, a tree whose children are missing ...) are returned with the
section that should fix them, and fed into the repair loop like validator
diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml
from pydantic import BaseModel

from afsim_research_agent.falcon.models import (
    BehaviorsSection,
    ConditionsSection,
    ControlSection,
    EvidencePackage,
    FalconRequest,
    FlatTree,
    ParametersSection,
    SkeletonDraft,
    StructureSection,
    VerificationSection,
)
from afsim_research_agent.ir.catalog import Catalog, load_catalog
from afsim_research_agent.ir.coordination import expand
from afsim_research_agent.ir.diagnostics import Collector
from afsim_research_agent.ir.validate import parse_spec

SCHEMA_VERSION = "1.0"


@dataclass
class AssemblyProblem:
    section: str
    path: str
    message: str


@dataclass
class Assembled:
    data: dict[str, Any]
    problems: list[AssemblyProblem] = field(default_factory=list)
    implements: dict[str, list[str]] = field(default_factory=dict)


def dump_model(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_defaults=True)


def _section(sections: dict[str, BaseModel], name: str, cls: type[BaseModel]) -> Any:
    value = sections.get(name)
    return value if isinstance(value, cls) else cls()


# --------------------------------------------------------------------------- trees


def nest_tree(tree: FlatTree, path: str, problems: list[AssemblyProblem]) -> dict[str, Any] | None:
    nodes = {}
    for node in tree.nodes:
        if node.node_id in nodes:
            problems.append(AssemblyProblem("control", path, f"duplicate node_id '{node.node_id}'"))
        nodes.setdefault(node.node_id, node)
    if tree.root not in nodes:
        problems.append(
            AssemblyProblem("control", path, f"root node '{tree.root}' is not in nodes")
        )
        return None
    used: set[str] = set()

    def build(node_id: str, stack: tuple[str, ...]) -> dict[str, Any] | None:
        if node_id in stack:
            problems.append(AssemblyProblem("control", path, f"cycle through node '{node_id}'"))
            return None
        node = nodes.get(node_id)
        if node is None:
            problems.append(
                AssemblyProblem("control", path, f"child node '{node_id}' is not defined")
            )
            return None
        used.add(node_id)
        out = node.model_dump(
            mode="json", exclude_none=True, exclude_defaults=True, exclude={"children"}
        )
        out["node_id"] = node.node_id
        out["type"] = node.type
        children = [c for c in (build(child, (*stack, node_id)) for child in node.children) if c]
        if children:
            out["children"] = children
        return out

    root = build(tree.root, ())
    unused = sorted(set(nodes) - used)
    if unused:
        problems.append(
            AssemblyProblem("control", path, f"nodes not reachable from root: {unused}")
        )
    return root


# --------------------------------------------------------------------------- assemble


def assemble(
    request: FalconRequest,
    skeleton: SkeletonDraft,
    sections: dict[str, BaseModel],
    package: EvidencePackage,
) -> Assembled:
    problems: list[AssemblyProblem] = []
    implements: dict[str, list[str]] = {}
    params_s: ParametersSection = _section(sections, "parameters", ParametersSection)
    struct_s: StructureSection = _section(sections, "structure", StructureSection)
    cond_s: ConditionsSection = _section(sections, "conditions", ConditionsSection)
    beh_s: BehaviorsSection = _section(sections, "behaviors", BehaviorsSection)
    ctl_s: ControlSection = _section(sections, "control", ControlSection)
    ver_s: VerificationSection = _section(sections, "verification", VerificationSection)

    # blackboard variables: skeleton + behaviors section additions
    boards: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    for role in skeleton.roles:
        for var in role.blackboard_variables:
            boards.setdefault(role.id, {}).setdefault("mission", {})[var.name] = var.model_dump()
    role_ids = {r.id for r in skeleton.roles}
    for index, var in enumerate(beh_s.blackboard_variables):
        if var.role not in role_ids:
            problems.append(
                AssemblyProblem(
                    "behaviors", f"blackboard_variables[{index}]", f"unknown role '{var.role}'"
                )
            )
            continue
        entry = {"name": var.name, "type": var.type, "default": var.default}
        boards.setdefault(var.role, {}).setdefault(var.blackboard, {}).setdefault(var.name, entry)

    transitions: dict[tuple[str, str], list[dict[str, Any]]] = {}
    states_by_role = {r.id: {s.id for s in r.states} for r in skeleton.roles}
    for index, tr in enumerate(ctl_s.transitions):
        if tr.role not in states_by_role or tr.state not in states_by_role[tr.role]:
            problems.append(
                AssemblyProblem(
                    "control",
                    f"transitions[{index}]",
                    f"transition '{tr.transition_id}' targets unknown role/state '{tr.role}.{tr.state}'",
                )
            )
            continue
        item: dict[str, Any] = {
            "transition_id": tr.transition_id,
            "to": tr.to,
            "priority": tr.priority,
            "when": tr.when,
        }
        if tr.synchronize_with:
            item["synchronize_with"] = tr.synchronize_with
        if tr.basis is not None and tr.when != {"literal": True}:
            item["basis"] = tr.basis.model_dump(exclude_none=True)
        transitions.setdefault((tr.role, tr.state), []).append(item)
        implements[tr.transition_id] = list(tr.implements)

    roles = []
    for role in skeleton.roles:
        implements[role.id] = list(role.implements)
        deps: dict[str, Any] = {}
        if role.mover_capability:
            deps["mover"] = {"required": True, "capability": role.mover_capability}
        for kind in ("sensors", "comms", "weapons"):
            items = getattr(role, kind)
            if items:
                deps[kind] = [
                    {"id": c.id, "capability": c.capability, "required": True} for c in items
                ]
        item: dict[str, Any] = {
            "id": role.id,
            "description": role.description,
            "count": role.count,
            "capability_class": role.capability_class,
            "command": {
                "superior": role.superior,
                "subordinates": [r.id for r in skeleton.roles if r.superior == role.id],
            },
            "dependencies": deps,
        }
        if role.id in boards:
            item["blackboards"] = [
                {"id": board, "scope": "platform", "variables": list(variables.values())}
                for board, variables in boards[role.id].items()
            ]
        if skeleton.architecture == "behavior_tree":
            item["root_tree"] = role.root_tree
        else:
            states = []
            for state in role.states:
                implements[f"{role.id}.{state.id}"] = list(state.implements)
                entry: dict[str, Any] = {"id": state.id}
                if state.terminal:
                    entry["terminal"] = True
                if state.tree and not state.terminal:  # an end state runs no tree
                    entry["tree"] = state.tree
                if state.description:
                    entry["description"] = state.description
                entry["transitions"] = sorted(
                    transitions.get((role.id, state.id), []), key=lambda t: -int(t["priority"])
                )
                states.append(entry)
            item["state_machine"] = {
                "id": f"{role.id}_mission",
                "initial_state": role.initial_state or (role.states[0].id if role.states else ""),
                "states": states,
            }
        roles.append(item)

    for group in (skeleton.formations, skeleton.coordination, skeleton.messages):
        for plan in group:
            implements[plan.id] = list(plan.implements)

    parameters: dict[str, Any] = {}
    candidates = [("parameters", i, p) for i, p in enumerate(params_s.parameters)]
    # later sections may add the thresholds/speeds they need; the first definition wins
    for name, section in (("structure", struct_s), ("conditions", cond_s), ("behaviors", beh_s)):
        candidates += [(name, i, p) for i, p in enumerate(section.new_parameters)]
    for origin, index, param in candidates:
        if param.name in parameters:
            if origin == "parameters":
                problems.append(
                    AssemblyProblem(
                        "parameters", f"parameters[{index}]", f"duplicate parameter '{param.name}'"
                    )
                )
            continue
        entry = {"type": param.type, "value": param.value}
        for key in ("unit", "minimum", "maximum"):
            if getattr(param, key) is not None:
                entry[key] = getattr(param, key)
        if param.description:
            entry["description"] = param.description
        entry["basis"] = param.basis.model_dump(exclude_none=True)
        parameters[param.name] = entry
        implements[param.name] = list(param.implements)

    assumptions = []
    for a in params_s.assumptions:
        entry = {"id": a.id, "statement": a.statement}
        if a.impact:
            entry["impact"] = a.impact
        entry["affects"] = list(a.affects)
        entry["requires_confirmation"] = a.requires_confirmation
        entry["confirmation"] = "pending"
        assumptions.append(entry)

    trees = []
    for index, tree in enumerate(ctl_s.trees):
        root = nest_tree(tree, f"trees[{index}]", problems)
        if root is None:
            continue
        entry = {"id": tree.id}
        if tree.description:
            entry["description"] = tree.description
        entry["root"] = root
        trees.append(entry)

    data: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "model": {
            "id": request.model_id,
            "display_name": request.display_name or skeleton.display_name,
            "version": "0.1.0",
            "description": skeleton.description,
            "intent": skeleton.intent,
            "authoring_mode": "agent_generated",
            "domain_concepts": list(skeleton.domain_concepts),
        },
        "provenance": {
            "requirements": [
                {
                    "source_id": "user_req_001",
                    "kind": "user_input",
                    "locator": "falcon_request.yaml",
                    "summary": request.requirement.strip()[:300],
                }
            ],
            "evidence_package": {
                "package_id": package.package_id,
                "content_hash": package.content_hash,
                "readiness": "passed",
                "path": "evidence_package.json",
            },
            "references": [],
        },
        "assumptions": assumptions,
        "execution": {
            "architecture": skeleton.architecture,
            "controller_name": "tactical_controller",
            "update_interval": {"value": skeleton.update_interval_s, "unit": "sec"},
            "deterministic": True,
            "random_seed_parameter": None,
            "debug": {
                "behavior_tree_trace": True,
                "state_transitions": True,
                "script_writes": False,
            },
        },
        "roles": roles,
        "formations": [dump_model(f) for f in struct_s.formations],
        "coordination": [dump_model(c) for c in struct_s.coordination],
        "messages": [dump_model(m) for m in struct_s.messages],
        "parameters": parameters,
        "resources": [],
        "conditions": [dump_model(c) for c in cond_s.conditions],
        "behaviors": [dump_model(b) for b in beh_s.behaviors],
        "trees": trees,
        "tests": [dump_model(t) for t in ver_s.tests],
        "scenarios": [dump_model(s) for s in ver_s.scenarios],
    }
    # basis blocks must survive exclude_defaults (confirmation is filled by enforcement)
    for key in ("formations", "coordination"):
        source = struct_s.formations if key == "formations" else struct_s.coordination
        for item, model in zip(data[key], source, strict=True):
            if model.basis is not None:
                item["basis"] = model.basis.model_dump(exclude_none=True)
    for item, model in zip(data["behaviors"], beh_s.behaviors, strict=True):
        if model.basis is not None:
            item["basis"] = model.basis.model_dump(exclude_none=True)
    return Assembled(data, problems, implements)


def add_references(data: dict[str, Any], package: EvidencePackage) -> None:
    """List every source cited by a basis as a crow_source reference."""
    cited: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            basis = node.get("basis")
            if isinstance(basis, dict):
                cited.update(basis.get("evidence", []))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    sources = {e.source_id for e in package.evidence if e.evidence_id in cited}
    refs = []
    for source in package.sources:
        if source.source_id not in sources:
            continue
        ref: dict[str, Any] = {
            "source_id": source.source_id,
            "kind": "crow_source",
            "locator": source.title[:120],
            "classification": source.classification,
        }
        if source.quality_tier:
            ref["quality_tier"] = source.quality_tier
        refs.append(ref)
    data["provenance"]["references"] = refs


def generated_ids(data: dict[str, Any], catalog: Catalog | None = None) -> dict[str, Any] | None:
    """IDs the coordination patterns will generate, so later sections can use them."""
    probe = dict(data)
    for key in ("conditions", "behaviors", "trees", "tests"):
        probe.setdefault(key, [])
    probe.setdefault("parameters", {})
    spec, _ = parse_spec(probe)
    if spec is None:
        return None
    expansion = expand(spec, catalog or load_catalog(), Collector())
    out = {
        "messages": [m.id for m in expansion.messages],
        "conditions": [c.id for c in expansion.conditions],
        "behaviors": [b.id for b in expansion.behaviors],
        "trees": [t.id for t in expansion.trees],
        "transitions": sorted(
            {tr.transition_id for items in expansion.transitions.values() for tr in items}
        ),
        "usage": generated_usage(expansion),
    }
    return {k: v for k, v in out.items() if v}


def _roles(value: Any) -> list[str]:
    return [value] if isinstance(value, str) else list(value or [])


def _messages_in(node: Any, found: set[str]) -> None:
    if isinstance(node, BaseModel):
        node = node.model_dump(by_alias=True)
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "message" and isinstance(value, str):
                found.add(value)
            else:
                _messages_in(value, found)
    elif isinstance(node, list):
        for item in node:
            _messages_in(item, found)


def generated_usage(expansion: Any) -> dict[str, str]:
    """Which role may use each generated behavior/condition: the sender of the
    messages it sends, or the recipient of the messages it waits for."""
    messages = {m.id: m for m in expansion.messages}
    receives: dict[str, list[str]] = {}
    for cond in expansion.conditions:
        found: set[str] = set()
        _messages_in(cond.expression, found)
        roles = sorted({r for mid in found if mid in messages for r in _roles(messages[mid].to)})
        if roles:
            receives[cond.id] = roles
    usage: dict[str, str] = {}
    for cond_id, roles in receives.items():
        usage[cond_id] = "、".join(roles)
    for beh in expansion.behaviors:
        sent: set[str] = set()
        _messages_in([a.arguments for a in beh.actions], sent)
        roles = {r for mid in sent if mid in messages for r in _roles(messages[mid].sender)}
        waited: set[str] = set()
        _refs(beh.model_dump(by_alias=True, exclude={"actions"}), waited)
        roles |= {r for cid in waited for r in receives.get(cid, [])}
        if roles:
            usage[beh.id] = "、".join(sorted(roles))
    return usage


def _refs(node: Any, found: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "condition_ref" and isinstance(value, str):
                found.add(value)
            else:
                _refs(value, found)
    elif isinstance(node, list):
        for item in node:
            _refs(item, found)


# --------------------------------------------------------------------------- YAML


class _Dumper(yaml.SafeDumper):
    pass


def _str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _str)

HEADER = """\
# Tactical Behavior IR 1.0 — drafted by Tactical Falcon.
# Values derived from sources are pending until confirmed (see confirmations.yaml);
# run `python -m afsim_research_agent.falcon confirm <package> --by <name>` after review.
"""


def dump_spec(data: dict[str, Any]) -> str:
    blocks = []
    options = {"Dumper": _Dumper, "sort_keys": False, "allow_unicode": True, "width": 110}
    for key, value in data.items():
        if not isinstance(value, dict | list) or not value:
            text = yaml.dump({key: value}, default_flow_style=False, **options)
        else:
            body = yaml.dump(value, default_flow_style=None, **options)
            if body.startswith(("{", "[")):
                text = f"{key}: {body}"
            else:
                text = f"{key}:\n" + "".join(
                    f"  {line}" if line.strip() else line for line in body.splitlines(keepends=True)
                )
        blocks.append(text.rstrip("\n"))
    return HEADER + "\n" + "\n\n".join(blocks) + "\n"
