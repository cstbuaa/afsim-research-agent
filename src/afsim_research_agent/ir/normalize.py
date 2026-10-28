"""Normalization: confirmation gate + canonical, backend-neutral compiler input (design §23).

The normalized IR uses canonical units (m, m/s, s, deg, ratio), canonical
action/operand names, a fully expanded expression AST, expanded coordination
patterns (each element tagged ``generated_from``) and a content hash.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from afsim_research_agent.ir.catalog import DEFAULT_RESOURCES, Catalog, load_catalog
from afsim_research_agent.ir.coordination import as_list, parse_instance
from afsim_research_agent.ir.diagnostics import Collector, Diagnostic, sort_diagnostics
from afsim_research_agent.ir.models import (
    Basis,
    Behavior,
    BehaviorSpec,
    DistanceRef,
    QuantitySpec,
    TreeNode,
)
from afsim_research_agent.ir.units import (
    CANONICAL,
    PHYSICAL_TYPES,
    infer_dimension,
    to_canonical,
)
from afsim_research_agent.ir.validate import ValidationResult

IR_VERSION = "1.0"
NORMALIZER_VERSION = "normalizer-2026.10.1"


@dataclass
class NormalizeResult:
    ir: dict[str, Any] | None
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.ir is not None


# --------------------------------------------------------------------------- gate


def _confirmed(basis: Basis | None) -> bool:
    if basis is None:
        return True
    if basis.status == "user_specified":
        return basis.confirmation != "rejected"
    return basis.confirmation == "confirmed"


def confirmation_gate(
    spec: BehaviorSpec, diag: Collector, *, strict_parameters: bool = False
) -> None:
    """Every pending/rejected basis or assumption blocks normalization (design §7.2, §23).

    Parameters are the exception: an unconfirmed parameter value is a *reference value*
    (a reasonable default, with or without evidence). It passes the gate with an info
    diagnostic ``PAR002`` and is listed in review.md for later tuning; only a rejected
    parameter, or any unconfirmed one under ``strict_parameters``, blocks."""
    for index, assumption in enumerate(spec.assumptions):
        if assumption.confirmation == "rejected":
            diag.error(
                "ASM001", f"assumptions[{index}]", f"assumption '{assumption.id}' was rejected"
            )
        elif assumption.requires_confirmation and assumption.confirmation != "confirmed":
            diag.error(
                "ASM001",
                f"assumptions[{index}]",
                f"assumption '{assumption.id}' is not confirmed",
                suggestion="confirm or reject it in behavior_spec.yaml",
            )
    for name, param in spec.parameters.items():
        basis = param.basis
        if param.value is None:
            diag.error("PAR001", f"parameters.{name}.value", f"parameter '{name}' has no value")
        elif basis is not None and not _confirmed(basis):
            if basis.confirmation == "rejected" or strict_parameters:
                diag.error(
                    "PAR001",
                    f"parameters.{name}.basis.confirmation",
                    f"parameter '{name}' is {basis.confirmation} ({basis.status})",
                    evidence_ids=basis.evidence,
                )
            else:
                grounded = "有依据" if basis.evidence else "无依据"
                diag.info(
                    "PAR002",
                    f"parameters.{name}",
                    f"参考值 {param.value} {param.unit or ''}".rstrip()
                    + f"（{basis.status}，{grounded}），按参考值构建，待人工复核调参",
                    evidence_ids=basis.evidence,
                )
    items: list[tuple[str, Basis | None]] = []
    items += [(f"formations[{i}]", f.basis) for i, f in enumerate(spec.formations)]
    items += [(f"coordination[{i}]", c.basis) for i, c in enumerate(spec.coordination)]
    items += [(f"behaviors[{i}]", b.basis) for i, b in enumerate(spec.behaviors)]
    for r_index, role in enumerate(spec.roles):
        if role.state_machine is None:
            continue
        for s_index, state in enumerate(role.state_machine.states):
            for t_index, tr in enumerate(state.transitions):
                items.append(
                    (
                        f"roles[{r_index}].state_machine.states[{s_index}].transitions[{t_index}]",
                        tr.basis,
                    )
                )
    for path, basis in items:
        if basis is None or _confirmed(basis):
            continue
        diag.error(
            "EVD002",
            f"{path}.basis.confirmation",
            f"basis is {basis.confirmation} ({basis.status}); confirm before compiling",
            evidence_ids=basis.evidence,
        )


# --------------------------------------------------------------------------- normalizer


class _Normalizer:
    def __init__(self, result: ValidationResult, catalog: Catalog) -> None:
        assert result.effective is not None and result.expansion is not None
        self.spec = result.effective
        self.source = result.spec
        self.generated = result.expansion.generated_from
        self.members = result.expansion.members
        self.cat = catalog
        self.params = self.spec.parameters

    # -- primitives ------------------------------------------------------------
    @staticmethod
    def basis(basis: Basis | None) -> dict[str, Any] | None:
        if basis is None:
            return None
        data = basis.model_dump()
        if basis.status == "user_specified" and basis.confirmation == "pending":
            data["confirmation"] = "confirmed"
        return data

    @staticmethod
    def seconds(q: QuantitySpec | Any) -> float:
        return to_canonical(q.value, q.unit, "time")

    def distance(self, ref: DistanceRef | None) -> dict[str, Any] | None:
        if ref is None:
            return None
        if ref.parameter_ref is not None:
            value = self.param_value(ref.parameter_ref)
            return {
                "parameter_ref": ref.parameter_ref,
                "scale": ref.scale,
                "value_m": value * ref.scale,
            }
        assert ref.literal_distance is not None
        value = to_canonical(ref.literal_distance.value, ref.literal_distance.unit, "distance")
        return {"parameter_ref": None, "scale": ref.scale, "value_m": value * ref.scale}

    def param_value(self, name: str) -> Any:
        param = self.params[name]
        if param.type in PHYSICAL_TYPES and param.value is not None:
            return to_canonical(float(param.value), param.unit or "", param.type)
        return param.value

    def _tag(self, item_id: str, data: dict[str, Any]) -> dict[str, Any]:
        origin = self.generated.get(item_id)
        if origin is not None:
            data["generated_from"] = origin
        return data

    # -- expressions -------------------------------------------------------
    def expr(self, expr: Any) -> dict[str, Any]:
        key, value = next(iter(expr.items()))
        if key in ("all", "any"):
            return {"op": key, "args": [self.expr(item) for item in value]}
        if key == "not":
            return {"op": "not", "arg": self.expr(value)}
        if key == "condition_ref":
            return {"op": "condition_ref", "id": value}
        if key == "literal" and isinstance(value, bool):
            return {"op": "literal", "value": value}
        if key == "compare":
            return {
                "op": "compare",
                "operator": value["operator"],
                "left": self.operand(value["left"]),
                "right": self.operand(value["right"]),
            }
        return {"op": "operand", "operand": self.operand(expr)}

    def operand(self, op: Any) -> dict[str, Any]:
        if not isinstance(op, dict):
            return {"kind": "literal", "value": op}
        if set(op) == {"value", "unit"}:
            dim = infer_dimension(str(op["unit"])) or "distance"
            return {
                "kind": "quantity",
                "value": to_canonical(float(op["value"]), str(op["unit"]), dim),
                "dimension": dim,
            }
        key, value = next(iter(op.items()))
        if key == "literal":
            return {"kind": "literal", "value": value}
        if key == "parameter_ref":
            return {"kind": "parameter", "name": value}
        if key == "blackboard_value":
            parts = value.split(".")
            board, var = parts[-2], parts[-1]
            return {"kind": "blackboard", "board": board, "var": var}
        if key == "memory_value":
            return {"kind": "memory", "name": value}
        if key == "message_value":
            return {"kind": "message_value", "message": value["message"], "field": value["field"]}
        if key == "function_call":
            fn = self.cat.functions[value["name"]]
            args: dict[str, Any] = {}
            for name, kind in fn.arguments.items():
                raw = (value.get("arguments") or {}).get(name)
                if kind in ("message", "coordination", "formation", "string"):
                    args[name] = {"kind": "id", "value": raw}
                else:
                    args[name] = self.operand(raw)
            return {"kind": "function", "name": fn.name, "arguments": args}
        source = self.cat.canonical_operand(key)
        definition = self.cat.operands[source]
        spec = {"property": value} if isinstance(value, str) else dict(value)
        prop = definition.properties[spec["property"]]
        out: dict[str, Any] = {"kind": "property", "source": source, "property": prop.name}
        if definition.component in ("sensor", "weapon", "comm"):
            out["component"] = spec.get(definition.component)
        if prop.argument:
            out["argument"] = spec.get(prop.argument)
        if source == "formation_value" and "formation" in spec:
            out["formation"] = spec["formation"]
        return out

    # -- sections ------------------------------------------------------------
    def parameters(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name in sorted(self.params):
            param = self.params[name]
            physical = param.type in PHYSICAL_TYPES
            entry: dict[str, Any] = {
                "type": param.type,
                "value": self.param_value(name),
                "unit": CANONICAL[param.type] if physical else None,
                "original": {"value": param.value, "unit": param.unit},
                "description": param.description,
                "basis": self.basis(param.basis),
            }
            if physical and param.minimum is not None:
                entry["minimum"] = to_canonical(param.minimum, param.unit or "", param.type)
            elif param.minimum is not None:
                entry["minimum"] = param.minimum
            if physical and param.maximum is not None:
                entry["maximum"] = to_canonical(param.maximum, param.unit or "", param.type)
            elif param.maximum is not None:
                entry["maximum"] = param.maximum
            if param.vocabulary:
                entry["vocabulary"] = list(param.vocabulary)
            out[name] = entry
        return out

    def resources(self) -> list[dict[str, Any]]:
        declared = {r.id: r.policy for r in self.spec.resources}
        for name in DEFAULT_RESOURCES:
            declared.setdefault(name, "exclusive")
        return [{"id": rid, "policy": declared[rid]} for rid in sorted(declared)]

    def roles(self) -> list[dict[str, Any]]:
        out = []
        for role in self.spec.roles:
            deps = role.dependencies
            data: dict[str, Any] = {
                "id": role.id,
                "description": role.description,
                "count": role.count,
                "capability_class": role.capability_class,
                "command": role.command.model_dump(),
                "dependencies": {
                    "mover": deps.mover.model_dump() if deps.mover else None,
                    "energy": deps.energy.model_dump() if deps.energy else None,
                    **{
                        kind: [
                            c.model_dump() for c in sorted(getattr(deps, kind), key=lambda c: c.id)
                        ]
                        for kind in ("sensors", "comms", "weapons", "processors")
                    },
                },
                "blackboards": [
                    {
                        "id": board.id,
                        "scope": board.scope,
                        "variables": [
                            {
                                "name": v.name,
                                "type": v.type,
                                "default": float(v.default) if v.type == "double" else v.default,
                            }
                            for v in sorted(board.variables, key=lambda v: v.name)
                        ],
                    }
                    for board in sorted(role.blackboards, key=lambda b: b.id)
                ],
                "root_tree": role.root_tree,
                "state_machine": None,
            }
            machine = role.state_machine
            if machine is not None:
                states = []
                for state in machine.states:
                    ordered = sorted(
                        enumerate(state.transitions), key=lambda pair: (-pair[1].priority, pair[0])
                    )
                    states.append(
                        {
                            "id": state.id,
                            "description": state.description,
                            "tree": state.tree,
                            "terminal": state.terminal,
                            "transitions": [
                                self._tag(
                                    tr.transition_id,
                                    {
                                        "transition_id": tr.transition_id,
                                        "to": tr.to,
                                        "priority": tr.priority,
                                        "when": self.expr(tr.when),
                                        "fallback": tr.when == {"literal": True},
                                        "synchronize_with": tr.synchronize_with,
                                        "basis": self.basis(tr.basis),
                                    },
                                )
                                for _, tr in ordered
                            ],
                        }
                    )
                data["state_machine"] = {
                    "id": machine.id,
                    "initial_state": machine.initial_state,
                    "states": states,
                }
            out.append(data)
        return out

    def formations(self) -> list[dict[str, Any]]:
        out = []
        for form in sorted(self.spec.formations, key=lambda f: f.id):
            slots = []
            for slot in form.slots:
                parsed = parse_instance(slot.role_instance)
                assert parsed is not None
                slots.append(
                    {
                        "role": parsed[0],
                        "instance": parsed[1] if parsed[1] is not None else 0,
                        "forward": self.distance(slot.forward),
                        "right": self.distance(slot.right),
                    }
                )
            out.append(
                {
                    "id": form.id,
                    "description": form.description,
                    "reference": form.reference,
                    "slots": slots,
                    "tolerance": self.distance(form.tolerance),
                    "basis": self.basis(form.basis),
                }
            )
        return out

    def coordination(self) -> list[dict[str, Any]]:
        out = []
        for coord in sorted(self.spec.coordination, key=lambda c: c.id):
            data = coord.model_dump(exclude_none=True, by_alias=True)
            data["basis"] = self.basis(coord.basis)
            if coord.bound_distance is not None:
                data["bound_distance"] = self.distance(coord.bound_distance)
            if coord.active_when is not None:
                data["active_when"] = self.expr(coord.active_when)
            if coord.formation_ref is not None:
                data["formation_ref"] = self.operand(coord.formation_ref)
            if coord.ack_timeout is not None:
                data["ack_timeout_s"] = self.seconds(coord.ack_timeout)
                del data["ack_timeout"]
            pattern = self.cat.patterns.get(coord.pattern)
            if pattern is not None and pattern.supports_sync:
                data.pop("resync_interval", None)
                data["resync_interval_s"] = (
                    self.seconds(coord.resync_interval)
                    if coord.resync_interval is not None
                    else self.cat.settings["default_resync_s"]
                )
            if coord.standoff_distance is not None:
                data["standoff_distance"] = self.distance(coord.standoff_distance)
                data["standoff_margin_m"] = self.cat.settings["standoff_margin_m"]
            data["members"] = {
                group: [{"role": r, "instance": i} for r, i in items]
                for group, items in sorted(self.members.get(coord.id, {}).items())
            }
            data["generated"] = sorted(k for k, v in self.generated.items() if v == coord.id)
            out.append(data)
        return out

    def messages(self) -> list[dict[str, Any]]:
        out = []
        for msg in sorted(self.spec.messages, key=lambda m: m.id):
            out.append(
                self._tag(
                    msg.id,
                    {
                        "id": msg.id,
                        "description": msg.description,
                        "from": as_list(msg.sender),
                        "to": as_list(msg.to),
                        "fields": [
                            {
                                "name": f.name,
                                "type": f.type,
                                "default": _field_default(f.type, f.default),
                            }
                            for f in msg.fields
                        ],
                        "latency_s": self.seconds(msg.delivery.latency),
                        "reliability": msg.delivery.reliability,
                    },
                )
            )
        return out

    def conditions(self) -> list[dict[str, Any]]:
        return [
            self._tag(
                cond.id,
                {
                    "id": cond.id,
                    "description": cond.description,
                    "expression": self.expr(cond.expression),
                },
            )
            for cond in sorted(self.spec.conditions, key=lambda c: c.id)
        ]

    def behavior(self, behavior: Behavior) -> dict[str, Any]:
        actions = []
        claims = set(behavior.claims)
        for action in behavior.actions:
            definition = self.cat.action(action.command)
            assert definition is not None
            claims |= set(definition.resources)
            args: dict[str, Any] = {}
            for name, value in (action.arguments or {}).items():
                kind = definition.arguments[name].type
                if kind in (
                    "role",
                    "message",
                    "route",
                    "position",
                    "sensor",
                    "weapon",
                    "comm",
                    "processor",
                    "target",
                ):
                    args[name] = {"kind": "id", "value": value}
                elif kind == "blackboard_key":
                    parts = value.split(".")
                    args[name] = {"kind": "blackboard_key", "board": parts[-2], "var": parts[-1]}
                elif kind == "formation" and isinstance(value, str):
                    args[name] = {"kind": "literal", "value": value}
                elif kind == "fields":
                    args[name] = {
                        "kind": "fields",
                        "values": {k: self.operand(v) for k, v in sorted(value.items())},
                    }
                else:
                    args[name] = self.operand(value)
            policy = action.issue_policy or (
                "every_tick"
                if definition.every_tick and not definition.external
                else "once_per_activation"
            )
            actions.append(
                {
                    "command": definition.name,
                    "category": definition.category,
                    "external": definition.external,
                    "arguments": dict(sorted(args.items())),
                    "issue_policy": policy,
                    "allow_every_tick": action.allow_every_tick,
                    "resources": sorted(definition.resources),
                }
            )
        return self._tag(
            behavior.id,
            {
                "id": behavior.id,
                "description": behavior.description,
                "kind": behavior.kind,
                "memory": [
                    {"name": m.name, "type": m.type, "default": m.default} for m in behavior.memory
                ],
                "precondition": self.expr(behavior.precondition) if behavior.precondition else None,
                "claims": sorted(claims),
                "actions": actions,
                "success": self.expr(behavior.success.expression) if behavior.success else None,
                "failure": self.expr(behavior.failure.expression) if behavior.failure else None,
                "timeout": (
                    {"seconds": self.seconds(behavior.timeout), "result": behavior.timeout.result}
                    if behavior.timeout
                    else None
                ),
                "continuous": behavior.continuous,
                "messages": behavior.messages.model_dump(),
                "basis": self.basis(behavior.basis),
            },
        )

    def node(self, node: TreeNode) -> dict[str, Any]:
        data: dict[str, Any] = {"node_id": node.node_id, "type": node.type}
        if node.description:
            data["description"] = node.description
        for name in ("behavior", "condition", "tree", "decorator", "count"):
            value = getattr(node, name)
            if value is not None:
                data[name] = value
        if node.duration is not None:
            data["duration_s"] = self.seconds(node.duration)
        if node.until is not None:
            data["until"] = self.expr(node.until)
        if node.success_policy is not None:
            data["success_policy"] = node.success_policy.model_dump(exclude_none=True)
        if node.failure_policy is not None:
            data["failure_policy"] = node.failure_policy.model_dump(exclude_none=True)
        if node.fitness is not None:
            data["fitness"] = self.operand(node.fitness)
        if node.weight is not None:
            data["weight"] = self.operand(node.weight)
        if node.children:
            data["children"] = [self.node(child) for child in node.children]
        return data

    def trees(self) -> list[dict[str, Any]]:
        return [
            self._tag(
                tree.id,
                {"id": tree.id, "description": tree.description, "root": self.node(tree.root)},
            )
            for tree in sorted(self.spec.trees, key=lambda t: t.id)
        ]

    def run(self, *, draft: bool) -> dict[str, Any]:
        src = self.source
        assert src is not None
        ex = self.spec.execution
        ir: dict[str, Any] = {
            "ir_version": IR_VERSION,
            "draft": draft,
            "normalizer": NORMALIZER_VERSION,
            "catalog_versions": dict(sorted(self.cat.versions.items())),
            "model": src.model.model_dump(),
            "provenance": src.provenance.model_dump(),
            "assumptions": [a.model_dump() for a in sorted(src.assumptions, key=lambda a: a.id)],
            "execution": {
                "architecture": ex.architecture,
                "controller_name": ex.controller_name,
                "update_interval_s": self.seconds(ex.update_interval),
                "deterministic": ex.deterministic,
                "random_seed_parameter": ex.random_seed_parameter,
                "debug": ex.debug.model_dump(),
            },
            "parameters": self.parameters(),
            "resources": self.resources(),
            "roles": self.roles(),
            "formations": self.formations(),
            "coordination": self.coordination(),
            "messages": self.messages(),
            "conditions": self.conditions(),
            "behaviors": [
                self.behavior(b) for b in sorted(self.spec.behaviors, key=lambda b: b.id)
            ],
            "trees": self.trees(),
            "tests": [t.model_dump(by_alias=True) for t in src.tests],
            "scenarios": [s.model_dump(by_alias=True, exclude_none=True) for s in src.scenarios],
        }
        ir["ir_hash"] = ir_hash(ir)
        return ir


def _field_default(kind: str, value: Any) -> Any:
    if value is not None:
        return float(value) if kind == "double" else value
    return {"bool": False, "int": 0, "double": 0.0, "string": ""}[kind]


def canonical_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def ir_hash(ir: dict[str, Any]) -> str:
    body = {k: v for k, v in ir.items() if k != "ir_hash"}
    return "sha256:" + hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def verify_hash(ir: dict[str, Any]) -> bool:
    return ir.get("ir_hash") == ir_hash(ir)


def normalize(
    result: ValidationResult,
    *,
    draft: bool = False,
    catalog: Catalog | None = None,
    strict_parameters: bool = False,
) -> NormalizeResult:
    """Normalize a validated spec. ``draft=True`` skips the confirmation gate and
    marks the output as a draft, which backends refuse to compile."""
    diag = Collector()
    diag.extend(result.diagnostics)
    if not result.ok or result.spec is None:
        diag.error("IR002", "$", "validation failed; fix errors before normalizing")
        return NormalizeResult(None, sort_diagnostics(diag.items))
    gate = Collector()
    confirmation_gate(result.spec, gate, strict_parameters=strict_parameters)
    if gate.items and draft:
        for item in gate.items:
            if item.severity.value != "error":
                diag.extend([item])
                continue
            diag.warning(
                item.code, item.path, f"[draft] {item.message}", evidence_ids=item.evidence_ids
            )
    else:
        diag.extend(gate.items)
    if diag.has_errors:
        return NormalizeResult(None, sort_diagnostics(diag.items))
    ir = _Normalizer(result, catalog or load_catalog()).run(draft=draft)
    return NormalizeResult(ir, sort_diagnostics(diag.items))
