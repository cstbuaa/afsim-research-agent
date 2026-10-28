"""L1 validation of Tactical Behavior IR: schema, references, types, units,
graphs, evidence basis, coordination, tests and scenarios (design §22)."""

from __future__ import annotations

import json
import math
import re
import tomllib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import ValidationError

from afsim_research_agent.ir import irtypes as T
from afsim_research_agent.ir.catalog import DEFAULT_RESOURCES, Catalog, load_catalog
from afsim_research_agent.ir.coordination import (
    SEP,
    Expansion,
    as_list,
    distance_operand,
    expand,
    parse_instance,
)
from afsim_research_agent.ir.diagnostics import Collector, Diagnostic, has_errors, sort_diagnostics
from afsim_research_agent.ir.loader import load_spec
from afsim_research_agent.ir.models import (
    Basis,
    Behavior,
    BehaviorSpec,
    DistanceRef,
    Role,
    TreeNode,
)
from afsim_research_agent.ir.units import (
    PHYSICAL_TYPES,
    UnitError,
    infer_dimension,
    to_canonical,
)

SUPPORTED_SCHEMA_VERSIONS = ("1.0",)
IDENT = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
OBSERVABLES = (
    "role_state",
    "formation_id",
    "formation_error_rms",
    "min_interval",
    "max_interval",
    "speed",
    "time_to_transition",
    "messages_sent",
    "actions_issued",
    "contact_reaction_time",
    "hostile_range_min",
)
# physical dimension each numeric observable is measured in (None: a count)
OBSERVABLE_DIMENSIONS = {
    "formation_error_rms": "distance",
    "min_interval": "distance",
    "max_interval": "distance",
    "hostile_range_min": "distance",
    "speed": "speed",
    "time_to_transition": "time",
    "contact_reaction_time": "time",
    "messages_sent": None,
    "actions_issued": None,
}
STRUCTURAL_KEYS = ("all", "any", "not", "condition_ref", "compare")
COMPARE_OPS = ("eq", "ne", "lt", "lte", "gt", "gte")
TEST_CATEGORIES = (
    "nominal",
    "precondition_failure",
    "priority_transition",
    "resource_unavailable",
    "timeout_or_loss",
)
_VOCAB_PATH = Path(__file__).resolve().parents[1] / "domain" / "data" / "tactical_vocabulary.toml"


@dataclass
class EvidenceIndex:
    evidence_ids: set[str] = field(default_factory=set)
    source_ids: set[str] = field(default_factory=set)
    package_id: str | None = None
    available: bool = False


def load_evidence_index(path: Path | None) -> EvidenceIndex:
    if path is None or not path.exists():
        return EvidenceIndex()
    data = json.loads(path.read_text(encoding="utf-8"))
    evidence = {
        str(item.get("evidence_id") or item.get("id"))
        for item in data.get("evidence", [])
        if item.get("evidence_id") or item.get("id")
    }
    sources = {str(item["source_id"]) for item in data.get("sources", []) if "source_id" in item}
    return EvidenceIndex(evidence, sources, data.get("package_id"), True)


@dataclass
class ValidationResult:
    spec: BehaviorSpec | None
    effective: BehaviorSpec | None
    expansion: Expansion | None
    diagnostics: list[Diagnostic]

    @property
    def ok(self) -> bool:
        return self.effective is not None and not has_errors(self.diagnostics)


# --------------------------------------------------------------------------- parse


def _loc(loc: tuple[Any, ...]) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out or "$"


def parse_spec(data: dict[str, Any]) -> tuple[BehaviorSpec | None, list[Diagnostic]]:
    diag = Collector()
    version = data.get("schema_version")
    if version is None:
        diag.error("IR001", "schema_version", "schema_version is missing")
        return None, diag.items
    if str(version) not in SUPPORTED_SCHEMA_VERSIONS:
        diag.error(
            "IR001",
            "schema_version",
            f"unsupported schema_version '{version}'",
            suggestion=f"use one of {list(SUPPORTED_SCHEMA_VERSIONS)} (quote it as a string)",
        )
        return None, diag.items
    if not isinstance(version, str):
        diag.error("IR001", "schema_version", 'schema_version must be a string, e.g. "1.0"')
        return None, diag.items
    try:
        return BehaviorSpec.model_validate(data), diag.items
    except ValidationError as exc:
        for error in exc.errors():
            kind = error["type"]
            path = _loc(tuple(error["loc"]))
            if kind == "extra_forbidden":
                message = "unknown field"
            elif kind == "missing":
                message = "required field is missing"
            else:
                message = error["msg"]
            diag.error("IR002", path, message)
        return None, diag.items


# --------------------------------------------------------------------------- helpers


def effective_spec(spec: BehaviorSpec, expansion: Expansion) -> BehaviorSpec:
    eff = spec.model_copy(deep=True)
    eff.messages = [*eff.messages, *expansion.messages]
    eff.conditions = [*eff.conditions, *expansion.conditions]
    eff.behaviors = [*eff.behaviors, *expansion.behaviors]
    eff.trees = [*eff.trees, *expansion.trees]
    for role in eff.roles:
        if role.state_machine is None:
            continue
        for state in role.state_machine.states:
            extra = expansion.transitions.get((role.id, state.id), [])
            if extra:
                state.transitions = [*state.transitions, *[t.model_copy(deep=True) for t in extra]]
    return eff


def iter_nodes(node: TreeNode, path: str):
    yield node, path
    for index, child in enumerate(node.children):
        yield from iter_nodes(child, f"{path}.children[{index}]")


def expr_condition_refs(expr: Any) -> set[str]:
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "condition_ref" in node and isinstance(node["condition_ref"], str):
                found.add(node["condition_ref"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(expr)
    return found


@dataclass
class Usage:
    trees: list[str] = field(default_factory=list)
    behaviors: list[str] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)


def role_usage(role: Role, spec: BehaviorSpec) -> Usage:
    trees = {tree.id: tree for tree in spec.trees}
    behaviors = {b.id: b for b in spec.behaviors}
    conditions = {c.id: c for c in spec.conditions}
    usage = Usage()
    roots: list[str] = []
    cond_queue: list[str] = []
    if role.state_machine is not None:
        for state in role.state_machine.states:
            if state.tree:
                roots.append(state.tree)
            for transition in state.transitions:
                cond_queue += sorted(expr_condition_refs(transition.when))
    if role.root_tree:
        roots.append(role.root_tree)
    while roots:
        tree_id = roots.pop(0)
        if tree_id in usage.trees or tree_id not in trees:
            continue
        usage.trees.append(tree_id)
        for node, _ in iter_nodes(trees[tree_id].root, ""):
            if node.type == "subtree" and node.tree:
                roots.append(node.tree)
            if node.type == "behavior" and node.behavior and node.behavior not in usage.behaviors:
                usage.behaviors.append(node.behavior)
            if node.type == "condition" and node.condition:
                cond_queue.append(node.condition)
            if node.until:
                cond_queue += sorted(expr_condition_refs(node.until))
    for behavior_id in usage.behaviors:
        behavior = behaviors.get(behavior_id)
        if behavior is None:
            continue
        for expr in _behavior_exprs(behavior):
            cond_queue += sorted(expr_condition_refs(expr))
    while cond_queue:
        cond_id = cond_queue.pop(0)
        if cond_id in usage.conditions or cond_id not in conditions:
            continue
        usage.conditions.append(cond_id)
        cond_queue += sorted(expr_condition_refs(conditions[cond_id].expression))
    return usage


def _behavior_exprs(behavior: Behavior) -> list[Any]:
    exprs: list[Any] = []
    if behavior.precondition is not None:
        exprs.append(behavior.precondition)
    if behavior.success is not None:
        exprs.append(behavior.success.expression)
    if behavior.failure is not None:
        exprs.append(behavior.failure.expression)
    return exprs


def _vocabulary_concepts() -> set[str]:
    try:
        data = tomllib.loads(_VOCAB_PATH.read_text(encoding="utf-8"))
    except OSError:
        return set()
    return {item["id"] for item in data.get("concept", [])}


# --------------------------------------------------------------------------- validator


@dataclass
class Ctx:
    role: Role | None
    behavior: Behavior | None = None
    in_condition: bool = True


class Validator:
    def __init__(
        self,
        spec: BehaviorSpec,
        effective: BehaviorSpec,
        expansion: Expansion,
        catalog: Catalog,
        evidence: EvidenceIndex,
        diag: Collector,
    ) -> None:
        self.spec = spec
        self.eff = effective
        self.exp = expansion
        self.cat = catalog
        self.evidence = evidence
        self.d = diag
        self.roles = {r.id: r for r in effective.roles}
        self.params = effective.parameters
        self.conditions = {c.id: c for c in effective.conditions}
        self.behaviors = {b.id: b for b in effective.behaviors}
        self.trees = {t.id: t for t in effective.trees}
        self.messages = {m.id: m for m in effective.messages}
        self.formations = {f.id: f for f in effective.formations}
        self.coordination = {c.id: c for c in effective.coordination}
        self.assumptions = {a.id for a in effective.assumptions}
        self.resources = {r.id: r for r in effective.resources}
        for name in DEFAULT_RESOURCES:
            self.resources.setdefault(name, None)  # type: ignore[arg-type]
        self.node_paths: dict[str, str] = {}
        self.usage: dict[str, Usage] = {}

    # -- paths for generated elements -------------------------------------
    def _where(self, kind: str, item_id: str, index: int) -> str:
        origin = self.exp.generated_from.get(item_id)
        if origin is not None:
            return f"coordination.{origin}(generated {kind} {item_id})"
        return f"{kind}[{index}]"

    # -- top-level -----------------------------------------------------------
    def run(self) -> None:
        self.check_ids()
        self.check_model()
        self.check_provenance()
        self.check_execution()
        self.check_parameters()
        self.check_roles()
        self.check_messages()
        self.check_formations()
        self.check_coordination()
        self.check_resources()
        self.check_trees()
        for role in self.eff.roles:
            self.usage[role.id] = role_usage(role, self.eff)
        self.check_conditions_and_behaviors()
        self.check_state_machines()
        self.check_parallel_resources()
        self.check_tests()
        self.check_scenarios()

    # -- identifiers -------------------------------------------------------
    def _ident(self, value: str, path: str, *, generated: bool = False) -> None:
        if generated:
            return
        if not IDENT.match(value):
            self.d.error("ID001", path, f"identifier '{value}' must match [a-z][a-z0-9_]{{0,63}}")
        elif SEP in value:
            self.d.error("ID001", path, f"'{SEP}' is reserved for generated identifiers")

    def _unique(self, items: list[tuple[str, str]], namespace: str) -> None:
        seen: dict[str, str] = {}
        for value, path in items:
            if value in seen:
                self.d.error(
                    "ID001", path, f"duplicate {namespace} id '{value}' (first at {seen[value]})"
                )
            else:
                seen[value] = path

    def check_ids(self) -> None:
        s = self.spec
        g = self.exp.generated_from
        self._ident(s.model.id, "model.id")
        groups: dict[str, list[tuple[str, str]]] = {
            "role": [(r.id, f"roles[{i}].id") for i, r in enumerate(s.roles)],
            "formation": [(f.id, f"formations[{i}].id") for i, f in enumerate(s.formations)],
            "coordination": [(c.id, f"coordination[{i}].id") for i, c in enumerate(s.coordination)],
            "message": [
                (m.id, self._where("messages", m.id, i)) for i, m in enumerate(self.eff.messages)
            ],
            "resource": [(r.id, f"resources[{i}].id") for i, r in enumerate(s.resources)],
            "condition": [
                (c.id, self._where("conditions", c.id, i))
                for i, c in enumerate(self.eff.conditions)
            ],
            "behavior": [
                (b.id, self._where("behaviors", b.id, i)) for i, b in enumerate(self.eff.behaviors)
            ],
            "tree": [(t.id, self._where("trees", t.id, i)) for i, t in enumerate(self.eff.trees)],
            "test": [(t.id, f"tests[{i}].id") for i, t in enumerate(s.tests)],
            "scenario": [(sc.id, f"scenarios[{i}].id") for i, sc in enumerate(s.scenarios)],
            "assumption": [(a.id, f"assumptions[{i}].id") for i, a in enumerate(s.assumptions)],
        }
        for namespace, items in groups.items():
            for value, path in items:
                self._ident(value, path, generated=value in g)
            self._unique(items, namespace)
        for name in s.parameters:
            self._ident(name, f"parameters.{name}")
        node_items: list[tuple[str, str]] = []
        for t_index, tree in enumerate(self.eff.trees):
            base = self._where("trees", tree.id, t_index)
            for node, sub in iter_nodes(tree.root, f"{base}.root"):
                self._ident(node.node_id, f"{sub}.node_id", generated=tree.id in g)
                node_items.append((node.node_id, sub))
                self.node_paths.setdefault(node.node_id, sub)
        self._unique(node_items, "node")
        for r_index, role in enumerate(self.eff.roles):
            rp = f"roles[{r_index}]"
            deps = role.dependencies
            comp = [
                (c.id, f"{rp}.dependencies.{kind}[{i}].id")
                for kind in ("sensors", "comms", "weapons", "processors")
                for i, c in enumerate(getattr(deps, kind))
            ]
            for value, path in comp:
                self._ident(value, path)
            self._unique(comp, f"component (role {role.id})")
            boards = [(b.id, f"{rp}.blackboards[{i}].id") for i, b in enumerate(role.blackboards)]
            for value, path in boards:
                self._ident(value, path)
            self._unique(boards, f"blackboard (role {role.id})")
            for b_index, board in enumerate(role.blackboards):
                names = [
                    (v.name, f"{rp}.blackboards[{b_index}].variables[{i}].name")
                    for i, v in enumerate(board.variables)
                ]
                for value, path in names:
                    self._ident(value, path)
                self._unique(names, f"variable ({role.id}.{board.id})")
            machine = role.state_machine
            if machine is None:
                continue
            self._ident(machine.id, f"{rp}.state_machine.id")
            states = [
                (st.id, f"{rp}.state_machine.states[{i}].id") for i, st in enumerate(machine.states)
            ]
            for value, path in states:
                self._ident(value, path)
            self._unique(states, f"state (role {role.id})")
            transitions = []
            for s_index, state in enumerate(machine.states):
                for t_index, tr in enumerate(state.transitions):
                    path = (
                        f"{rp}.state_machine.states[{s_index}].transitions[{t_index}].transition_id"
                    )
                    self._ident(tr.transition_id, path, generated=tr.transition_id in g)
                    transitions.append((tr.transition_id, path))
            self._unique(transitions, f"transition (role {role.id})")
        for b_index, behavior in enumerate(self.eff.behaviors):
            mems = [
                (m.name, f"{self._where('behaviors', behavior.id, b_index)}.memory[{i}].name")
                for i, m in enumerate(behavior.memory)
            ]
            self._unique(mems, f"memory ({behavior.id})")

    # -- model / provenance ---------------------------------------------------
    def check_model(self) -> None:
        m = self.spec.model
        if not SEMVER.match(m.version):
            self.d.error("IR002", "model.version", "version must be MAJOR.MINOR.PATCH")
        intents = self.cat.vocabularies["intents"]
        if m.intent not in intents:
            self.d.error(
                "IR002",
                "model.intent",
                f"intent '{m.intent}' is not in the controlled vocabulary",
                suggestion="use a catalog intent or 'custom'",
            )
        elif m.intent == "custom":
            self.d.warning("IR002", "model.intent", "custom intent requires human review")
        known = _vocabulary_concepts()
        for index, concept in enumerate(m.domain_concepts):
            if known and concept not in known:
                self.d.warning(
                    "REF001",
                    f"model.domain_concepts[{index}]",
                    f"'{concept}' is not a tactical vocabulary concept",
                )

    def check_provenance(self) -> None:
        p = self.spec.provenance
        self._unique(
            [(r.source_id, f"provenance.requirements[{i}]") for i, r in enumerate(p.requirements)]
            + [(r.source_id, f"provenance.references[{i}]") for i, r in enumerate(p.references)],
            "source",
        )
        for index, ref in enumerate(p.references):
            if ref.kind == "crow_source":
                if p.evidence_package is None:
                    self.d.error(
                        "REF001",
                        f"provenance.references[{index}]",
                        "crow_source references require provenance.evidence_package",
                    )
                elif self.evidence.available and ref.source_id not in self.evidence.source_ids:
                    self.d.error(
                        "REF001",
                        f"provenance.references[{index}].source_id",
                        f"source '{ref.source_id}' is not in the evidence package",
                    )
        if p.evidence_package is not None:
            if p.evidence_package.readiness == "failed":
                self.d.warning(
                    "EVD001",
                    "provenance.evidence_package.readiness",
                    "the Crow evidence package failed its readiness gate",
                )
            if (
                self.evidence.available
                and self.evidence.package_id
                and self.evidence.package_id != p.evidence_package.package_id
            ):
                self.d.error(
                    "REF001",
                    "provenance.evidence_package.package_id",
                    f"package id does not match evidence_package.json "
                    f"('{self.evidence.package_id}')",
                )
        for index, assumption in enumerate(self.spec.assumptions):
            for a_index, target in enumerate(assumption.affects):
                if not self._known_id(target):
                    self.d.warning(
                        "REF001",
                        f"assumptions[{index}].affects[{a_index}]",
                        f"'{target}' does not name a model element",
                    )

    def _known_id(self, value: str) -> bool:
        if value in self.params or value in self.conditions or value in self.behaviors:
            return True
        if value in self.coordination or value in self.formations or value in self.roles:
            return True
        if value in self.messages or value in self.trees:
            return True
        for role in self.eff.roles:
            if role.state_machine is None:
                continue
            for state in role.state_machine.states:
                if state.id == value:
                    return True
                if any(t.transition_id == value for t in state.transitions):
                    return True
        return False

    # -- basis -------------------------------------------------------------
    def check_basis(
        self, basis: Basis | None, path: str, *, required: bool, what: str, severity: str = "error"
    ) -> None:
        if basis is None:
            if required:
                emit = self.d.error if severity == "error" else self.d.warning
                emit(
                    "EVD001",
                    f"{path}.basis",
                    f"{what} needs a basis (evidence status and confirmation)",
                    suggestion="add basis: {status: ..., evidence: [...], confirmation: pending}",
                )
            return
        bp = f"{path}.basis"
        if basis.status == "explicit" and not basis.evidence:
            self.d.error("EVD001", bp, "explicit basis needs at least one evidence id")
        if basis.status == "conflicting":
            if len(basis.evidence) < 2:
                self.d.error("EVD001", bp, "conflicting basis needs at least two evidence ids")
            if not basis.rationale:
                self.d.error(
                    "EVD001", bp, "conflicting basis needs a rationale for the chosen value"
                )
        if basis.status == "inferred":
            if not basis.rationale:
                self.d.error("EVD001", bp, "inferred basis needs a rationale")
            if not basis.evidence and not basis.assumptions:
                self.d.error(
                    "EVD001", bp, "inferred basis needs at least one evidence id or assumption"
                )
        for index, evidence_id in enumerate(basis.evidence):
            if self.evidence.available and evidence_id not in self.evidence.evidence_ids:
                self.d.error(
                    "REF001",
                    f"{bp}.evidence[{index}]",
                    f"evidence '{evidence_id}' is not in evidence_package.json",
                    evidence_ids=[evidence_id],
                )
        if basis.evidence and not self.evidence.available:
            self.d.warning(
                "EVD001", bp, "evidence ids cannot be verified: evidence_package.json not available"
            )
        for index, assumption_id in enumerate(basis.assumptions):
            if assumption_id not in self.assumptions:
                self.d.error(
                    "REF001",
                    f"{bp}.assumptions[{index}]",
                    f"assumption '{assumption_id}' undefined",
                )
        if (
            basis.confirmation == "confirmed"
            and not basis.confirmed_by
            and basis.status != "user_specified"
        ):
            self.d.warning("EVD001", bp, "confirmed basis should record confirmed_by")

    # -- execution / parameters ---------------------------------------------
    def _quantity(self, raw: Any, dimension: str, path: str) -> float | None:
        try:
            return to_canonical(raw.value, raw.unit, dimension)
        except UnitError as exc:
            self.d.error("UNIT001", path, str(exc))
            return None

    def check_execution(self) -> None:
        ex = self.spec.execution
        value = self._quantity(ex.update_interval, "time", "execution.update_interval")
        if value is not None and value <= 0:
            self.d.error("UNIT001", "execution.update_interval", "update interval must be positive")
        if ex.random_seed_parameter is not None:
            param = self.params.get(ex.random_seed_parameter)
            if param is None or param.type != "integer":
                self.d.error(
                    "REF001",
                    "execution.random_seed_parameter",
                    "random_seed_parameter must name an integer parameter",
                )
        arch = ex.architecture
        if arch == "script_processor":
            self.d.warning(
                "IR002",
                "execution.architecture",
                "script_processor is not compiled by the bundled backends",
            )

    def check_parameters(self) -> None:
        for name, param in self.params.items():
            path = f"parameters.{name}"
            self.check_basis(param.basis, path, required=True, what=f"parameter '{name}'")
            status = param.basis.status if param.basis else None
            if param.value is None:
                if status != "unknown":
                    self.d.error(
                        "PAR001",
                        f"{path}.value",
                        "value may be null only when basis.status is unknown",
                    )
            if param.type in PHYSICAL_TYPES:
                if not param.unit:
                    self.d.error("UNIT001", f"{path}.unit", f"{param.type} parameter needs a unit")
                    continue
                try:
                    to_canonical(0.0, param.unit, param.type)
                except UnitError as exc:
                    self.d.error("UNIT001", f"{path}.unit", str(exc))
                    continue
                if param.value is not None and (
                    isinstance(param.value, bool) or not isinstance(param.value, int | float)
                ):
                    self.d.error("TYPE001", f"{path}.value", f"{param.type} value must be a number")
                    continue
            else:
                if param.unit:
                    self.d.error("UNIT001", f"{path}.unit", f"{param.type} parameter takes no unit")
                expected = {"integer": int, "boolean": bool, "string": str}[param.type]
                value = param.value
                if value is not None:
                    bad = not isinstance(value, expected) or (
                        expected is int and isinstance(value, bool)
                    )
                    if bad:
                        self.d.error("TYPE001", f"{path}.value", f"value must be {param.type}")
                        continue
                if param.type == "string" and param.vocabulary and value not in param.vocabulary:
                    self.d.error("TYPE001", f"{path}.value", f"'{value}' is not in the vocabulary")
                if param.type in ("boolean", "string") and (
                    param.minimum is not None or param.maximum is not None
                ):
                    self.d.error(
                        "TYPE001", path, "minimum/maximum only apply to numeric parameters"
                    )
            if param.minimum is not None and param.maximum is not None:
                if param.minimum > param.maximum:
                    self.d.error("PAR001", path, "minimum is greater than maximum")
            if isinstance(param.value, int | float) and not isinstance(param.value, bool):
                if param.minimum is not None and param.value < param.minimum:
                    self.d.error("PAR001", f"{path}.value", "value is below minimum")
                if param.maximum is not None and param.value > param.maximum:
                    self.d.error("PAR001", f"{path}.value", "value is above maximum")

    # -- roles ---------------------------------------------------------------
    def check_roles(self) -> None:
        arch = self.spec.execution.architecture
        needs_fsm = arch in ("state_machine", "state_machine_with_behavior_trees")
        for r_index, role in enumerate(self.eff.roles):
            rp = f"roles[{r_index}]"
            if needs_fsm and role.state_machine is None:
                self.d.error(
                    "FSM001", f"{rp}.state_machine", f"architecture '{arch}' needs a state machine"
                )
            if arch == "behavior_tree" and not role.root_tree:
                self.d.error(
                    "TREE001", f"{rp}.root_tree", "behavior_tree architecture needs root_tree"
                )
            if role.root_tree and role.root_tree not in self.trees:
                self.d.error("REF001", f"{rp}.root_tree", f"tree '{role.root_tree}' is not defined")
            cmd = role.command
            if cmd.superior is not None:
                if cmd.superior not in self.roles:
                    self.d.error(
                        "ROLE001", f"{rp}.command.superior", f"role '{cmd.superior}' undefined"
                    )
                elif cmd.superior == role.id:
                    self.d.error(
                        "ROLE001", f"{rp}.command.superior", "a role cannot command itself"
                    )
                elif role.id not in self.roles[cmd.superior].command.subordinates:
                    self.d.warning(
                        "ROLE001",
                        f"{rp}.command.superior",
                        f"'{cmd.superior}' does not list '{role.id}' as a subordinate",
                    )
            for s_index, sub in enumerate(cmd.subordinates):
                if sub not in self.roles:
                    self.d.error(
                        "ROLE001",
                        f"{rp}.command.subordinates[{s_index}]",
                        f"role '{sub}' undefined",
                    )
            deps = role.dependencies
            for kind, item in (("mover", deps.mover), ("energy", deps.energy)):
                if item is not None:
                    self._capability(item.capability, f"{rp}.dependencies.{kind}.capability")
            for kind in ("sensors", "comms", "weapons", "processors"):
                for c_index, comp in enumerate(getattr(deps, kind)):
                    self._capability(
                        comp.capability, f"{rp}.dependencies.{kind}[{c_index}].capability"
                    )
            for b_index, board in enumerate(role.blackboards):
                for v_index, var in enumerate(board.variables):
                    if not T.assignable(var.type, T.literal_type(var.default) or T.ANY) or (
                        var.type == "int" and isinstance(var.default, float)
                    ):
                        self.d.error(
                            "TYPE001",
                            f"{rp}.blackboards[{b_index}].variables[{v_index}].default",
                            f"default does not match type {var.type}",
                        )
        self._command_cycles()

    def _capability(self, value: str, path: str) -> None:
        if not IDENT.match(value):
            self.d.error("ID001", path, f"capability '{value}' must be a logical identifier")

    def _command_cycles(self) -> None:
        graph = {r.id: r.command.superior for r in self.eff.roles}
        for start in graph:
            seen = {start}
            node = graph.get(start)
            while node is not None and node in graph:
                if node in seen:
                    self.d.error(
                        "ROLE001", f"roles.{start}.command", "command relations form a cycle"
                    )
                    break
                seen.add(node)
                node = graph.get(node)

    def _instance(self, text: str, path: str) -> tuple[str, int | None] | None:
        parsed = parse_instance(text)
        if parsed is None or parsed[0] not in self.roles:
            self.d.error("ROLE001", path, f"'{text}' is not a role or role instance")
            return None
        role = self.roles[parsed[0]]
        if parsed[1] is not None and parsed[1] >= role.count:
            self.d.error(
                "ROLE001", path, f"instance index {parsed[1]} out of range (count {role.count})"
            )
            return None
        return parsed

    # -- messages ------------------------------------------------------------
    def check_messages(self) -> None:
        for m_index, msg in enumerate(self.eff.messages):
            mp = self._where("messages", msg.id, m_index)
            for side, value in (("from", msg.sender), ("to", msg.to)):
                for name in as_list(value):
                    if name not in self.roles:
                        self.d.error("REF001", f"{mp}.{side}", f"role '{name}' undefined")
            names = [(f.name, f"{mp}.fields[{i}].name") for i, f in enumerate(msg.fields)]
            self._unique(names, f"message field ({msg.id})")
            for f_index, fld in enumerate(msg.fields):
                self._ident(fld.name, f"{mp}.fields[{f_index}].name")
                if fld.default is not None and not T.assignable(
                    fld.type, T.literal_type(fld.default) or T.ANY
                ):
                    self.d.error(
                        "TYPE001", f"{mp}.fields[{f_index}].default", "default type mismatch"
                    )
            latency = self._quantity(msg.delivery.latency, "time", f"{mp}.delivery.latency")
            if latency is not None and latency < 0:
                self.d.error("UNIT001", f"{mp}.delivery.latency", "latency must be >= 0")

    # -- formations ------------------------------------------------------------
    def _distance_ref(
        self,
        ref: DistanceRef | None,
        path: str,
        *,
        code: str = "FORM001",
        allow_scale: bool = False,
    ) -> None:
        if ref is None:
            return
        if ref.scale != 1.0 and not allow_scale:
            self.d.error(code, f"{path}.scale", "scale is only allowed on formation slot offsets")
        given = [ref.parameter_ref is not None, ref.literal_distance is not None]
        if sum(given) != 1:
            self.d.error(code, path, "give exactly one of parameter_ref or literal_distance")
            return
        if ref.parameter_ref is not None:
            param = self.params.get(ref.parameter_ref)
            if param is None:
                self.d.error(
                    "REF001", f"{path}.parameter_ref", f"parameter '{ref.parameter_ref}' undefined"
                )
            elif param.type not in ("distance", "altitude"):
                self.d.error("UNIT001", f"{path}.parameter_ref", "parameter must be a distance")
        else:
            assert ref.literal_distance is not None
            self._quantity(ref.literal_distance, "distance", f"{path}.literal_distance")
            if ref.literal_distance.value not in (0, 0.0):
                self.d.warning(
                    "EVD001",
                    f"{path}.literal_distance",
                    "non-zero literal distance has no basis; use a parameter",
                )

    def check_formations(self) -> None:
        for f_index, form in enumerate(self.eff.formations):
            fp = f"formations[{f_index}]"
            self.check_basis(form.basis, fp, required=True, what=f"formation '{form.id}'")
            if not form.slots:
                self.d.error("FORM001", f"{fp}.slots", "formation needs at least one slot")
            seen: dict[tuple[str, int | None], str] = {}
            for s_index, slot in enumerate(form.slots):
                sp = f"{fp}.slots[{s_index}]"
                parsed = self._instance(slot.role_instance, f"{sp}.role_instance")
                if parsed is not None:
                    role = self.roles[parsed[0]]
                    if parsed[1] is None and role.count > 1:
                        self.d.error(
                            "FORM001",
                            f"{sp}.role_instance",
                            f"role '{role.id}' has {role.count} instances; use {role.id}[i]",
                        )
                    key = (parsed[0], parsed[1] if parsed[1] is not None else 0)
                    if key in seen:
                        self.d.error("FORM001", sp, f"duplicate slot for {slot.role_instance}")
                    seen[key] = sp
                self._distance_ref(slot.forward, f"{sp}.forward", allow_scale=True)
                self._distance_ref(slot.right, f"{sp}.right", allow_scale=True)
            if form.tolerance is None:
                self.d.error("FORM001", f"{fp}.tolerance", "formation needs a station tolerance")
            else:
                self._distance_ref(form.tolerance, f"{fp}.tolerance")

    # -- coordination -----------------------------------------------------------
    def check_coordination(self) -> None:
        for c_index, coord in enumerate(self.eff.coordination):
            cp = f"coordination[{c_index}]"
            self.check_basis(coord.basis, cp, required=True, what=f"coordination '{coord.id}'")
            if coord.bound_distance is not None:
                self._distance_ref(coord.bound_distance, f"{cp}.bound_distance", code="COORD001")
            if coord.ack_timeout is not None:
                self._quantity(coord.ack_timeout, "time", f"{cp}.ack_timeout")
            if coord.resync_interval is not None:
                seconds = self._quantity(coord.resync_interval, "time", f"{cp}.resync_interval")
                if seconds is not None and seconds < 0:
                    self.d.error("COORD001", f"{cp}.resync_interval", "must be >= 0 (0 disables)")
            if coord.standoff_distance is not None:
                self._distance_ref(
                    coord.standoff_distance, f"{cp}.standoff_distance", code="COORD001"
                )
            if coord.objective is not None and not IDENT.match(coord.objective):
                self.d.error("COORD001", f"{cp}.objective", "objective must be a waypoint id")
            if (
                coord.pattern == "bounding_overwatch"
                and coord.standoff_distance is None
                and coord.objective is None
            ):
                self.d.warning(
                    "COORD001",
                    cp,
                    "bounding has no stop condition (standoff_distance or objective); "
                    "it advances until the state changes",
                )

    # -- resources ---------------------------------------------------------------
    def check_resources(self) -> None:
        pass  # ids checked in check_ids; usage checked per behavior and parallel node

    def _behavior_resources(self, behavior: Behavior) -> set[str]:
        found = set(behavior.claims)
        for action in behavior.actions:
            definition = self.cat.action(action.command)
            if definition is not None:
                found |= set(definition.resources)
        return {r for r in found if self._exclusive(r)}

    def _exclusive(self, resource: str) -> bool:
        declared = self.resources.get(resource)
        return declared is None or declared.policy == "exclusive"

    # -- trees -------------------------------------------------------------------
    def check_trees(self) -> None:
        for t_index, tree in enumerate(self.eff.trees):
            base = f"{self._where('trees', tree.id, t_index)}.root"
            for node, path in iter_nodes(tree.root, base):
                self._check_node(node, path)
        self._tree_cycles()
        if self.spec.execution.deterministic and not self.spec.execution.random_seed_parameter:
            for t_index, tree in enumerate(self.eff.trees):
                for node, path in iter_nodes(tree.root, f"trees[{t_index}].root"):
                    if node.type == "weighted_random":
                        self.d.warning(
                            "TREE001",
                            path,
                            "weighted_random in a deterministic model needs random_seed_parameter",
                        )

    def _check_node(self, node: TreeNode, path: str) -> None:
        kind = node.type
        leaf = kind in ("behavior", "condition", "subtree")
        if leaf and node.children:
            self.d.error("TREE001", f"{path}.children", f"{kind} node cannot have children")
        if kind == "behavior":
            if not node.behavior:
                self.d.error("TREE001", path, "behavior node needs 'behavior'")
            elif node.behavior not in self.behaviors:
                self.d.error(
                    "REF001", f"{path}.behavior", f"behavior '{node.behavior}' is not defined"
                )
        elif kind == "condition":
            if not node.condition:
                self.d.error("TREE001", path, "condition node needs 'condition'")
            elif node.condition not in self.conditions:
                self.d.error(
                    "REF001", f"{path}.condition", f"condition '{node.condition}' is not defined"
                )
        elif kind == "subtree":
            if not node.tree:
                self.d.error("TREE001", path, "subtree node needs 'tree'")
            elif node.tree not in self.trees:
                self.d.error("REF001", f"{path}.tree", f"tree '{node.tree}' is not defined")
        elif kind == "decorator":
            if len(node.children) != 1:
                self.d.error("TREE001", path, "decorator must have exactly one child")
            if node.decorator is None:
                self.d.error("TREE001", path, "decorator node needs 'decorator'")
            elif node.decorator == "repeat_count" and not (node.count and node.count > 0):
                self.d.error("TREE001", path, "repeat_count needs count > 0")
            elif node.decorator == "repeat_for_duration":
                if node.duration is None:
                    self.d.error("TREE001", path, "repeat_for_duration needs duration")
                else:
                    value = self._quantity(node.duration, "time", f"{path}.duration")
                    if value is not None and value <= 0:
                        self.d.error("TREE001", f"{path}.duration", "duration must be positive")
            elif (
                node.decorator == "repeat_until_done" and node.count is not None and node.count <= 0
            ):
                self.d.error("TREE001", path, "repeat_until_done count cap must be > 0")
        else:
            if not node.children:
                self.d.error("TREE001", path, f"{kind} node needs at least one child")
        if kind == "parallel":
            n = len(node.children)
            if node.success_policy is None or node.failure_policy is None:
                self.d.error("TREE001", path, "parallel needs success_policy and failure_policy")
            for name in ("success_policy", "failure_policy"):
                pol = getattr(node, name)
                if pol is None:
                    continue
                valid = (
                    ("threshold", "all", "one")
                    if name == "success_policy"
                    else ("threshold", "fail_on_one", "fail_on_all")
                )
                if pol.type not in valid:
                    self.d.error("TREE001", f"{path}.{name}", f"'{pol.type}' is not a {name} type")
                if pol.type == "threshold" and not (pol.count and 0 < pol.count <= n):
                    self.d.error("TREE001", f"{path}.{name}", f"threshold must be in 1..{n}")
            sp, fp = node.success_policy, node.failure_policy
            if sp and fp and sp.type == "threshold" and fp.type == "threshold":
                if sp.count and fp.count and sp.count + fp.count <= n:
                    self.d.error(
                        "TREE001",
                        path,
                        "success and failure thresholds can both be met in one tick",
                    )
        for c_index, child in enumerate(node.children):
            cpath = f"{path}.children[{c_index}]"
            if kind == "priority_selector":
                if child.fitness is None:
                    self.d.error("TREE001", cpath, "priority_selector children need 'fitness'")
                else:
                    self._numeric_operand(child.fitness, f"{cpath}.fitness")
            elif kind == "weighted_random":
                if child.weight is None:
                    self.d.error("TREE001", cpath, "weighted_random children need 'weight'")
                else:
                    self._numeric_operand(child.weight, f"{cpath}.weight")
            else:
                for name in ("fitness", "weight"):
                    if getattr(child, name) is not None:
                        self.d.warning("TREE001", f"{cpath}.{name}", f"{name} ignored under {kind}")
        if kind == "weighted_random":
            literals = [
                c.weight.get("literal")
                for c in node.children
                if isinstance(c.weight, dict) and "literal" in c.weight
            ]
            if len(literals) == len(node.children) and all(v == 0 for v in literals):
                self.d.error("TREE001", path, "all weights are zero; add a non-zero fallback")
        if node.until is not None:
            self.check_expr(node.until, Ctx(role=None), f"{path}.until")

    def _numeric_operand(self, operand: Any, path: str) -> None:
        kind = self.operand_type(operand, Ctx(role=None, in_condition=False), path)
        if kind is not None and not T.is_numeric(kind) and kind != T.ANY:
            self.d.error("TYPE001", path, f"expected a numeric value, got {kind}")
        if isinstance(operand, dict) and isinstance(operand.get("literal"), int | float):
            if operand["literal"] < 0:
                self.d.error("TREE001", path, "weights/fitness must not be negative")

    def _tree_cycles(self) -> None:
        edges: dict[str, set[str]] = {}
        for tree in self.eff.trees:
            edges[tree.id] = {
                node.tree
                for node, _ in iter_nodes(tree.root, "")
                if node.type == "subtree" and node.tree
            }
        state: dict[str, int] = {}

        def visit(tree_id: str, stack: list[str]) -> None:
            state[tree_id] = 1
            for nxt in sorted(edges.get(tree_id, ())):
                if state.get(nxt) == 1:
                    cycle = " -> ".join([*stack[stack.index(nxt) :], nxt]) if nxt in stack else nxt
                    self.d.error(
                        "TREE001", f"trees.{tree_id}", f"recursive subtree reference: {cycle}"
                    )
                elif state.get(nxt) is None and nxt in edges:
                    visit(nxt, [*stack, nxt])
            state[tree_id] = 2

        for tree_id in sorted(edges):
            if state.get(tree_id) is None:
                visit(tree_id, [tree_id])

    # -- expressions ---------------------------------------------------------
    def check_expr(self, expr: Any, ctx: Ctx, path: str) -> None:
        if not isinstance(expr, dict) or len(expr) != 1:
            self.d.error("IR002", path, "expression must be a mapping with exactly one key")
            return
        key, value = next(iter(expr.items()))
        if key in ("all", "any"):
            if not isinstance(value, list) or not value:
                self.d.error("IR002", f"{path}.{key}", f"'{key}' needs a non-empty list")
                return
            for index, item in enumerate(value):
                self.check_expr(item, ctx, f"{path}.{key}[{index}]")
        elif key == "not":
            self.check_expr(value, ctx, f"{path}.not")
        elif key == "condition_ref":
            if value not in self.conditions:
                self.d.error(
                    "REF001", f"{path}.condition_ref", f"condition '{value}' is not defined"
                )
        elif key == "compare":
            self._compare(value, ctx, f"{path}.compare")
        else:
            kind = self.operand_type(expr, ctx, path)
            if kind is not None and kind not in (T.BOOL, T.ANY):
                self.d.error("TYPE001", path, f"expression must be boolean, got {kind}")

    def _compare(self, value: Any, ctx: Ctx, path: str) -> None:
        if not isinstance(value, dict) or set(value) != {"left", "operator", "right"}:
            self.d.error("IR002", path, "compare needs exactly left, operator and right")
            return
        op = value["operator"]
        if op not in COMPARE_OPS:
            self.d.error(
                "IR002", f"{path}.operator", f"operator must be one of {list(COMPARE_OPS)}"
            )
        left = self.operand_type(value["left"], ctx, f"{path}.left")
        right = self.operand_type(value["right"], ctx, f"{path}.right")
        if left is None or right is None:
            return
        ok, reason = T.comparable(left, right)
        if not ok:
            code = "UNIT001" if T.is_quantity(left) or T.is_quantity(right) else "TYPE001"
            self.d.error(code, path, reason)
            return
        if reason == "unitless":
            self.d.warning(
                "UNIT001",
                path,
                "comparing a quantity with a unitless number assumes canonical units",
            )
        if op not in ("eq", "ne") and (T.BOOL in (left, right) or T.STRING in (left, right)):
            self.d.error("TYPE001", path, f"operator '{op}' needs numeric operands")
        for side, kind_other in (("left", right), ("right", left)):
            literal = value[side].get("literal") if isinstance(value[side], dict) else None
            if isinstance(literal, int | float) and not isinstance(literal, bool):
                if T.is_quantity(kind_other) and literal != 0:
                    self.d.error(
                        "UNIT001",
                        f"{path}.{side}",
                        "a non-zero number compared with a quantity needs a unit; use a parameter",
                    )
        self._vocab_compare(value, path)

    def _vocab_compare(self, value: dict[str, Any], path: str) -> None:
        for side, other in (("left", "right"), ("right", "left")):
            op = value[side]
            if isinstance(op, dict) and "terrain_value" in op:
                prop = op["terrain_value"]
                prop = prop.get("property") if isinstance(prop, dict) else prop
                lit = value[other].get("literal") if isinstance(value[other], dict) else None
                if prop == "class" and isinstance(lit, str):
                    if lit not in self.cat.vocabularies["terrain_classes"]:
                        self.d.error("TYPE001", f"{path}.{other}", f"unknown terrain class '{lit}'")

    def operand_type(self, op: Any, ctx: Ctx, path: str) -> str | None:
        if not isinstance(op, dict):
            self.d.error("IR002", path, "operand must be a mapping")
            return None
        if set(op) == {"value", "unit"}:
            dim = infer_dimension(str(op["unit"]))
            if dim is None:
                self.d.error("UNIT001", path, f"unknown unit '{op['unit']}'")
                return None
            self.d.warning("EVD001", path, "literal quantity has no basis; prefer a parameter")
            return dim
        if len(op) != 1:
            self.d.error("IR002", path, "operand must have exactly one key")
            return None
        key, value = next(iter(op.items()))
        if key == "literal":
            kind = T.literal_type(value)
            if kind is None:
                self.d.error("TYPE001", path, "literal must be bool, number or string")
            elif kind == T.NUMBER and ctx.in_condition and value not in (0, 1):
                self.d.warning(
                    "EVD001",
                    path,
                    f"bare numeric literal {value} in a condition; use a parameter with basis",
                )
            return kind
        if key == "parameter_ref":
            param = self.params.get(value) if isinstance(value, str) else None
            if param is None:
                self.d.error(
                    "REF001", f"{path}.parameter_ref", f"parameter '{value}' is not defined"
                )
                return None
            return T.parameter_value_type(param.type)
        if key == "blackboard_value":
            return self._blackboard_type(value, ctx, f"{path}.blackboard_value")
        if key == "memory_value":
            if ctx.behavior is None:
                self.d.error("REF001", path, "memory_value is only valid inside a behavior")
                return None
            for mem in ctx.behavior.memory:
                if mem.name == value:
                    return mem.type
            self.d.error("REF001", path, f"memory variable '{value}' undefined")
            return None
        if key == "message_value":
            return self._message_value(value, ctx, f"{path}.message_value")
        if key == "function_call":
            return self._function(value, ctx, f"{path}.function_call")
        operand = self.cat.operand(key)
        if operand is None:
            self.d.error("IR002", path, f"unknown operand '{key}'")
            return None
        if key != operand.name:
            self.d.info("IR002", path, f"operand alias '{key}' normalized to '{operand.name}'")
        return self._component_operand(operand, value, ctx, f"{path}.{key}")

    def _blackboard_type(self, value: Any, ctx: Ctx, path: str) -> str | None:
        if not isinstance(value, str):
            self.d.error("IR002", path, "expected '<blackboard>.<variable>'")
            return None
        parts = value.split(".")
        if len(parts) == 3:
            if ctx.role is not None and parts[0] != ctx.role.id:
                self.d.error(
                    "REF001",
                    path,
                    f"'{value}' reads another role's blackboard; use messages instead",
                )
                return None
            parts = parts[1:]
        if len(parts) != 2:
            self.d.error("IR002", path, "expected '<blackboard>.<variable>'")
            return None
        if ctx.role is None:
            kinds = {
                var.type
                for role in self.eff.roles
                for board in role.blackboards
                if board.id == parts[0]
                for var in board.variables
                if var.name == parts[1]
            }
            return kinds.pop() if len(kinds) == 1 else T.ANY
        for board in ctx.role.blackboards:
            if board.id == parts[0]:
                for var in board.variables:
                    if var.name == parts[1]:
                        return var.type
        self.d.error(
            "REF001",
            path,
            f"blackboard variable '{value}' is not declared for role '{ctx.role.id}'",
        )
        return None

    def _message_value(self, value: Any, ctx: Ctx, path: str) -> str | None:
        if not isinstance(value, dict) or set(value) != {"message", "field"}:
            self.d.error("IR002", path, "message_value needs message and field")
            return None
        msg = self.messages.get(value["message"])
        if msg is None:
            self.d.error("REF001", f"{path}.message", f"message '{value['message']}' undefined")
            return None
        if ctx.role is not None and ctx.role.id not in as_list(msg.to):
            self.d.error("REF001", path, f"role '{ctx.role.id}' does not receive '{msg.id}'")
        for fld in msg.fields:
            if fld.name == value["field"]:
                return fld.type
        self.d.error(
            "REF001", f"{path}.field", f"message '{msg.id}' has no field '{value['field']}'"
        )
        return None

    def _function(self, value: Any, ctx: Ctx, path: str) -> str | None:
        if not isinstance(value, dict) or "name" not in value:
            self.d.error("IR002", path, "function_call needs name and arguments")
            return None
        extra = set(value) - {"name", "arguments"}
        if extra:
            self.d.error("IR002", path, f"unknown keys {sorted(extra)}")
        fn = self.cat.functions.get(value["name"])
        if fn is None:
            self.d.error("ACT001", f"{path}.name", f"function '{value['name']}' is not whitelisted")
            return None
        args = value.get("arguments") or {}
        if not isinstance(args, dict):
            self.d.error("IR002", f"{path}.arguments", "arguments must be a mapping")
            return fn.returns
        for name in sorted(set(args) - set(fn.arguments)):
            self.d.error("ACT001", f"{path}.arguments.{name}", f"unknown argument for {fn.name}")
        for name, kind in fn.arguments.items():
            ap = f"{path}.arguments.{name}"
            if name not in args:
                self.d.error("ACT001", ap, f"missing argument for {fn.name}")
                continue
            arg = args[name]
            if kind == "message":
                msg = self.messages.get(arg) if isinstance(arg, str) else None
                if msg is None:
                    self.d.error("REF001", ap, f"message '{arg}' undefined")
                elif ctx.role is not None and ctx.role.id not in as_list(msg.to):
                    self.d.error("REF001", ap, f"role '{ctx.role.id}' does not receive '{arg}'")
            elif kind == "coordination":
                if arg not in self.coordination:
                    self.d.error("REF001", ap, f"coordination '{arg}' undefined")
            elif kind == "formation":
                if arg not in self.formations:
                    self.d.error("REF001", ap, f"formation '{arg}' undefined")
            elif kind == "string":
                if not isinstance(arg, str):
                    self.d.error("TYPE001", ap, "expected a string")
                elif fn.name == "in_group" and args.get("coordination") in self.coordination:
                    groups = self.coordination[args["coordination"]].groups or {}
                    if arg not in groups:
                        self.d.error("REF001", ap, f"group '{arg}' undefined")
            else:
                got = self.operand_type(arg, ctx, ap)
                if got is not None and not T.assignable(kind, got):
                    if not (got == T.NUMBER and arg.get("literal") == 0):
                        self.d.error(
                            "UNIT001" if T.is_quantity(kind) else "TYPE001",
                            ap,
                            f"expected {kind}, got {got}",
                        )
        return fn.returns

    def _component_operand(self, operand: Any, value: Any, ctx: Ctx, path: str) -> str | None:
        if isinstance(value, str):
            value = {"property": value}
        if not isinstance(value, dict) or "property" not in value:
            self.d.error("IR002", path, "operand needs a property")
            return None
        prop = operand.properties.get(value["property"])
        if prop is None:
            self.d.error(
                "IR002",
                f"{path}.property",
                f"'{value['property']}' is not a property of {operand.name}; "
                f"expected one of {sorted(operand.properties)}",
            )
            return None
        allowed = {"property"}
        if operand.component in ("sensor", "weapon", "comm"):
            allowed.add(operand.component)
            comp_id = value.get(operand.component)
            if comp_id is None:
                self.d.error("IR002", path, f"{operand.name} needs '{operand.component}'")
            elif ctx.role is not None:
                kind = {"sensor": "sensors", "weapon": "weapons", "comm": "comms"}[
                    operand.component
                ]
                ids = [c.id for c in getattr(ctx.role.dependencies, kind)]
                if comp_id not in ids:
                    self.d.error(
                        "REF001",
                        f"{path}.{operand.component}",
                        f"role '{ctx.role.id}' has no {operand.component} '{comp_id}'",
                    )
        if (
            operand.component == "mover"
            and ctx.role is not None
            and ctx.role.dependencies.mover is None
        ):
            self.d.error("REF001", path, f"role '{ctx.role.id}' has no mover dependency")
        if prop.argument:
            allowed.add(prop.argument)
            arg = value.get(prop.argument)
            if arg is None:
                self.d.error("IR002", path, f"property '{prop.name}' needs '{prop.argument}'")
            elif prop.argument == "class" and arg not in self.cat.vocabularies["terrain_classes"]:
                self.d.error("TYPE001", f"{path}.class", f"unknown terrain class '{arg}'")
        if operand.name == "formation_value":
            allowed.add("formation")
            if "formation" in value and value["formation"] not in self.formations:
                self.d.error(
                    "REF001", f"{path}.formation", f"formation '{value['formation']}' undefined"
                )
        extra = set(value) - allowed
        if extra:
            self.d.error("IR002", path, f"unknown keys {sorted(extra)}")
        return prop.type

    # -- conditions and behaviors ---------------------------------------------
    def check_conditions_and_behaviors(self) -> None:
        used_conditions: dict[str, list[Role]] = defaultdict(list)
        used_behaviors: dict[str, list[Role]] = defaultdict(list)
        for role in self.eff.roles:
            for cond in self.usage[role.id].conditions:
                used_conditions[cond].append(role)
            for beh in self.usage[role.id].behaviors:
                used_behaviors[beh].append(role)
        for c_index, cond in enumerate(self.eff.conditions):
            path = f"{self._where('conditions', cond.id, c_index)}.expression"
            roles = used_conditions.get(cond.id) or [None]
            if cond.id not in used_conditions and cond.id not in self.exp.generated_from:
                self.d.info(
                    "REF001",
                    self._where("conditions", cond.id, c_index),
                    "condition is not used by any role",
                )
            for role in roles:
                self.check_expr(cond.expression, Ctx(role=role), path)
        self._condition_cycles()
        for b_index, behavior in enumerate(self.eff.behaviors):
            path = self._where("behaviors", behavior.id, b_index)
            roles = used_behaviors.get(behavior.id) or [None]
            if behavior.id not in used_behaviors and behavior.id not in self.exp.generated_from:
                self.d.info("REF001", path, "behavior is not used by any role")
            for role in roles:
                self._behavior(behavior, role, path)

    def _condition_cycles(self) -> None:
        edges = {c.id: expr_condition_refs(c.expression) for c in self.eff.conditions}
        state: dict[str, int] = {}

        def visit(node: str) -> bool:
            state[node] = 1
            for nxt in sorted(edges.get(node, ())):
                if state.get(nxt) == 1:
                    self.d.error(
                        "REF001", f"conditions.{node}", f"cyclic condition reference via '{nxt}'"
                    )
                    return True
                if state.get(nxt) is None and nxt in edges and visit(nxt):
                    return True
            state[node] = 2
            return False

        for node in sorted(edges):
            if state.get(node) is None:
                visit(node)

    def _behavior(self, behavior: Behavior, role: Role | None, path: str) -> None:
        ctx = Ctx(role=role, behavior=behavior)
        for m_index, mem in enumerate(behavior.memory):
            if not T.assignable(mem.type, T.literal_type(mem.default) or T.ANY):
                self.d.error(
                    "TYPE001", f"{path}.memory[{m_index}].default", "default type mismatch"
                )
        if behavior.precondition is not None:
            self.check_expr(behavior.precondition, ctx, f"{path}.precondition")
        if behavior.success is not None:
            self.check_expr(behavior.success.expression, ctx, f"{path}.success.expression")
        if behavior.failure is not None:
            self.check_expr(behavior.failure.expression, ctx, f"{path}.failure.expression")
        if behavior.timeout is not None:
            seconds = self._quantity(behavior.timeout, "time", f"{path}.timeout")
            if seconds is not None and seconds <= 0:
                self.d.error("BEH001", f"{path}.timeout", "timeout must be positive")
        has_wait = any(self.cat.canonical_action(a.command) == "wait" for a in behavior.actions)
        if (
            behavior.success is None
            and not has_wait
            and behavior.timeout is None
            and not behavior.continuous
        ):
            self.d.error(
                "BEH001",
                path,
                "behavior has no success condition, wait, timeout or continuous: true",
                suggestion="add success, a timeout, or declare continuous: true",
            )
        if behavior.continuous and behavior.success is not None:
            self.d.warning("BEH001", path, "continuous behavior also declares success")
        for c_index, claim in enumerate(behavior.claims):
            if claim not in self.resources:
                self.d.error("REF001", f"{path}.claims[{c_index}]", f"resource '{claim}' undefined")
        if role is not None:
            self.check_basis(behavior.basis, path, required=False, what="behavior")
        needed: set[str] = set()
        for a_index, action in enumerate(behavior.actions):
            needed |= self._action(action, role, ctx, f"{path}.actions[{a_index}]")
        missing = sorted(needed - set(behavior.claims))
        if missing and role is not None:
            self.d.warning(
                "RES001",
                f"{path}.claims",
                f"claims omit resources used by its actions: {missing} (normalizer adds them)",
            )

    def _action(self, action: Any, role: Role | None, ctx: Ctx, path: str) -> set[str]:
        definition = self.cat.action(action.command)
        if definition is None:
            self.d.error(
                "ACT001", f"{path}.command", f"action '{action.command}' is not in the whitelist"
            )
            return set()
        if action.command != definition.name:
            self.d.info(
                "ACT001", f"{path}.command", f"alias '{action.command}' -> '{definition.name}'"
            )
        policy = action.issue_policy
        if definition.external and policy is None:
            self.d.error(
                "BEH001", f"{path}.issue_policy", "actions with side effects need issue_policy"
            )
        if policy == "every_tick" and not definition.every_tick:
            if action.allow_every_tick:
                self.d.warning(
                    "ACT001", f"{path}.issue_policy", f"every_tick exemption for {definition.name}"
                )
            else:
                self.d.error(
                    "ACT001",
                    f"{path}.issue_policy",
                    f"'{definition.name}' may not be issued every tick without allow_every_tick",
                )
        if role is not None and definition.capability_classes:
            if role.capability_class not in definition.capability_classes:
                self.d.error(
                    "ACT001",
                    path,
                    f"'{definition.name}' is not available to {role.capability_class} roles",
                )
        args = action.arguments or {}
        for name in sorted(set(args) - set(definition.arguments)):
            self.d.error(
                "ACT001", f"{path}.arguments.{name}", f"unknown argument for {definition.name}"
            )
        for name, spec in definition.arguments.items():
            if name not in args:
                if spec.required:
                    self.d.error("ACT001", f"{path}.arguments.{name}", "missing required argument")
                continue
            self._argument(
                definition.name, spec.type, args[name], role, ctx, f"{path}.arguments.{name}"
            )
        if role is not None:
            deps = role.dependencies
            checks = {
                "mover": deps.mover is not None,
                "energy": deps.energy is not None,
                "comms": bool(deps.comms),
                "sensor": bool(deps.sensors),
                "weapon": bool(deps.weapons),
                "processor": bool(deps.processors),
            }
            for need in definition.requires:
                if not checks.get(need, True):
                    self.d.error(
                        "REF001",
                        path,
                        f"role '{role.id}' uses '{definition.name}' "
                        f"but declares no {need} dependency",
                    )
        return {r for r in definition.resources}

    def _argument(
        self, action: str, kind: str, value: Any, role: Role | None, ctx: Ctx, path: str
    ) -> None:
        arg_ctx = Ctx(role=role, behavior=ctx.behavior, in_condition=False)
        if kind in ("role",):
            if value not in self.roles:
                self.d.error("REF001", path, f"role '{value}' undefined")
        elif kind == "formation":
            if isinstance(value, str):
                if value not in self.formations:
                    self.d.error("REF001", path, f"formation '{value}' undefined")
            else:
                got = self.operand_type(value, arg_ctx, path)
                if got not in (T.STRING, T.ANY, None):
                    self.d.error("TYPE001", path, "formation argument must be a string")
        elif kind == "message":
            msg = self.messages.get(value) if isinstance(value, str) else None
            if msg is None:
                self.d.error("REF001", path, f"message '{value}' undefined")
            elif role is not None and role.id not in as_list(msg.sender):
                self.d.error("REF001", path, f"role '{role.id}' is not a sender of '{value}'")
        elif kind in ("route", "position"):
            if not isinstance(value, str) or not IDENT.match(value):
                self.d.error("ACT001", path, f"{kind} must be a scenario {kind} id")
        elif kind == "target":
            if not isinstance(value, str) or not IDENT.match(value):
                self.d.error(
                    "ACT001",
                    path,
                    "target must be 'heading', 'nearest_hostile' or a scenario waypoint id",
                )
        elif kind in ("sensor", "weapon", "comm", "processor"):
            if role is not None:
                attr = {
                    "sensor": "sensors",
                    "weapon": "weapons",
                    "comm": "comms",
                    "processor": "processors",
                }[kind]
                ids = [c.id for c in getattr(role.dependencies, attr)]
                if value not in ids:
                    self.d.error("REF001", path, f"role '{role.id}' has no {kind} '{value}'")
        elif kind == "blackboard_key":
            self._blackboard_type(value, arg_ctx, path)
        elif kind == "fields":
            if not isinstance(value, dict):
                self.d.error("ACT001", path, "fields must be a mapping")
                return
            msg_id = ctx.behavior and next(
                (
                    a.arguments.get("message")
                    for a in ctx.behavior.actions
                    if self.cat.canonical_action(a.command) == "send_message"
                    and a.arguments.get("fields") is value
                ),
                None,
            )
            msg = self.messages.get(msg_id) if isinstance(msg_id, str) else None
            for fname, fval in value.items():
                got = self.operand_type(fval, arg_ctx, f"{path}.{fname}")
                if msg is None:
                    continue
                fld = next((f for f in msg.fields if f.name == fname), None)
                if fld is None:
                    self.d.error(
                        "ACT001", f"{path}.{fname}", f"message '{msg.id}' has no field '{fname}'"
                    )
                elif got is not None and not T.assignable(fld.type, got):
                    self.d.error(
                        "TYPE001", f"{path}.{fname}", f"field expects {fld.type}, got {got}"
                    )
        elif kind == "value":
            got = self.operand_type(value, arg_ctx, path)
            behavior = ctx.behavior
            key = None
            if behavior is not None:
                for a in behavior.actions:
                    if a.arguments.get("value") is value:
                        key = a.arguments.get("key")
            if key is not None and got is not None:
                target = self._blackboard_type(key, Ctx(role=role), path) if role else None
                if target is not None and not T.assignable(target, got):
                    self.d.error(
                        "TYPE001", path, f"cannot store {got} in {target} variable '{key}'"
                    )
        else:
            got = self.operand_type(value, arg_ctx, path)
            if got is None:
                return
            if kind in PHYSICAL_TYPES:
                if not T.is_quantity(got) or not T.assignable(kind, got):
                    if not (got == T.NUMBER and value.get("literal") == 0):
                        self.d.error("UNIT001", path, f"'{action}' expects {kind}, got {got}")
            elif not T.assignable(kind, got):
                self.d.error("TYPE001", path, f"'{action}' expects {kind}, got {got}")

    # -- state machines --------------------------------------------------------
    def check_state_machines(self) -> None:
        g = self.exp.generated_from
        for r_index, role in enumerate(self.eff.roles):
            machine = role.state_machine
            if machine is None:
                continue
            mp = f"roles[{r_index}].state_machine"
            states = {s.id: s for s in machine.states}
            if machine.initial_state not in states:
                self.d.error(
                    "FSM001", f"{mp}.initial_state", f"state '{machine.initial_state}' undefined"
                )
            for s_index, state in enumerate(machine.states):
                sp = f"{mp}.states[{s_index}]"
                if state.terminal:
                    if state.transitions:
                        self.d.warning(
                            "FSM001", f"{sp}.transitions", "terminal state transitions are ignored"
                        )
                else:
                    if not state.tree:
                        self.d.error("FSM001", f"{sp}.tree", "non-terminal state needs a tree")
                    elif state.tree not in self.trees:
                        self.d.error("REF001", f"{sp}.tree", f"tree '{state.tree}' is not defined")
                    ordered = sorted(state.transitions, key=lambda t: -t.priority)
                    if not ordered or ordered[-1].when != {"literal": True}:
                        self.d.error(
                            "FSM001",
                            f"{sp}.transitions",
                            "non-terminal state needs a lowest-priority fallback transition "
                            "with when: {literal: true}",
                        )
                priorities: dict[int, str] = {}
                for t_index, tr in enumerate(state.transitions):
                    tp = f"{sp}.transitions[{t_index}]"
                    if tr.transition_id in g:
                        origin = g[tr.transition_id]
                        tp = f"coordination.{origin}(generated transition {tr.transition_id})"
                    if tr.to not in states:
                        self.d.error("FSM001", f"{tp}.to", f"target state '{tr.to}' undefined")
                    if tr.priority in priorities and tr.transition_id not in g:
                        self.d.warning(
                            "FSM001",
                            f"{tp}.priority",
                            f"same priority as '{priorities[tr.priority]}'; "
                            "declaration order decides",
                        )
                    priorities.setdefault(tr.priority, tr.transition_id)
                    self.check_expr(tr.when, Ctx(role=role), f"{tp}.when")
                    fallback = tr.when == {"literal": True}
                    if tr.transition_id not in g:
                        self.check_basis(
                            tr.basis,
                            tp,
                            required=not fallback,
                            what=f"transition '{tr.transition_id}'",
                            severity="warning",
                        )
                    if tr.synchronize_with is not None:
                        coord = self.coordination.get(tr.synchronize_with)
                        pattern = self.cat.patterns.get(coord.pattern) if coord else None
                        if coord is None:
                            self.d.error(
                                "COORD001",
                                f"{tp}.synchronize_with",
                                f"coordination '{tr.synchronize_with}' undefined",
                            )
                        elif pattern is None or not pattern.supports_sync:
                            self.d.error(
                                "COORD001",
                                f"{tp}.synchronize_with",
                                f"pattern '{coord.pattern}' does not support "
                                "synchronized transitions",
                            )
            self._reachability(role, mp, states)

    def _reachability(self, role: Role, mp: str, states: dict[str, Any]) -> None:
        machine = role.state_machine
        assert machine is not None
        if machine.initial_state not in states:
            return
        reached = {machine.initial_state}
        queue = [machine.initial_state]
        while queue:
            state = states[queue.pop()]
            if state.terminal:
                continue
            for tr in state.transitions:
                if tr.to in states and tr.to not in reached:
                    reached.add(tr.to)
                    queue.append(tr.to)
        for s_index, state in enumerate(machine.states):
            if state.id not in reached:
                self.d.error(
                    "FSM001", f"{mp}.states[{s_index}]", f"state '{state.id}' is unreachable"
                )
            elif not state.terminal and all(t.to == state.id for t in state.transitions):
                self.d.warning(
                    "FSM001", f"{mp}.states[{s_index}]", f"state '{state.id}' has no exit"
                )

    def check_parallel_resources(self) -> None:
        cache: dict[str, set[str]] = {}

        def node_resources(node: TreeNode, depth: int = 0) -> set[str]:
            if depth > 64:
                return set()
            if node.type == "behavior" and node.behavior in self.behaviors:
                return self._behavior_resources(self.behaviors[node.behavior])
            if node.type == "subtree" and node.tree in self.trees:
                if node.tree not in cache:
                    cache[node.tree] = set()
                    cache[node.tree] = node_resources(self.trees[node.tree].root, depth + 1)
                return cache[node.tree]
            out: set[str] = set()
            for child in node.children:
                out |= node_resources(child, depth + 1)
            return out

        for t_index, tree in enumerate(self.eff.trees):
            for node, path in iter_nodes(
                tree.root, f"{self._where('trees', tree.id, t_index)}.root"
            ):
                if node.type != "parallel":
                    continue
                seen: dict[str, str] = {}
                for child in node.children:
                    for res in sorted(node_resources(child)):
                        if res in seen and seen[res] != child.node_id:
                            self.d.error(
                                "RES001",
                                path,
                                f"exclusive resource '{res}' used by parallel branches "
                                f"'{seen[res]}' and '{child.node_id}'",
                            )
                        seen.setdefault(res, child.node_id)

    # -- tests -------------------------------------------------------------------
    def check_tests(self) -> None:
        multi = len(self.eff.roles) > 1
        categories: dict[str, set[str]] = defaultdict(set)
        for t_index, test in enumerate(self.spec.tests):
            tp = f"tests[{t_index}]"
            given, expect = test.given, test.expect
            if given.role is None:
                if multi:
                    self.d.error("SCN001", f"{tp}.given.role", "multi-role models need given.role")
                    continue
                role = self.eff.roles[0]
            else:
                role = self.roles.get(given.role)  # type: ignore[assignment]
                if role is None:
                    self.d.error("SCN001", f"{tp}.given.role", f"role '{given.role}' undefined")
                    continue
            if test.category:
                categories[role.id].add(test.category)
            if given.instance >= role.count or given.instance < 0:
                self.d.error("ROLE001", f"{tp}.given.instance", "instance index out of range")
            states = {s.id for s in role.state_machine.states} if role.state_machine else set()
            if given.state is not None and given.state not in states:
                self.d.error("SCN001", f"{tp}.given.state", f"state '{given.state}' undefined")
            if expect.next_state is not None and expect.next_state not in states:
                self.d.error(
                    "SCN001", f"{tp}.expect.next_state", f"state '{expect.next_state}' undefined"
                )
            for name in given.parameters:
                if name not in self.params:
                    self.d.error("SCN001", f"{tp}.given.parameters.{name}", "unknown parameter")
            for key, value in {**given.blackboard, **(expect.blackboard or {})}.items():
                kind = self._blackboard_type(key, Ctx(role=role), f"{tp}.blackboard.{key}")
                if kind is not None and not T.assignable(kind, T.literal_type(value) or T.ANY):
                    self.d.error(
                        "TYPE001", f"{tp}.blackboard.{key}", f"value does not match {kind}"
                    )
            for m_index, item in enumerate(given.messages):
                msg = self.messages.get(item.get("message", ""))
                if msg is None:
                    self.d.error("SCN001", f"{tp}.given.messages[{m_index}]", "unknown message")
                elif role.id not in as_list(msg.to):
                    self.d.error(
                        "SCN001",
                        f"{tp}.given.messages[{m_index}]",
                        f"'{role.id}' does not receive '{msg.id}'",
                    )
            for node in expect.selected_path or []:
                if node not in self.node_paths:
                    self.d.error("SCN001", f"{tp}.expect.selected_path", f"node '{node}' undefined")
            for command in expect.issued_actions or []:
                if self.cat.action(command) is None:
                    self.d.error(
                        "SCN001", f"{tp}.expect.issued_actions", f"unknown action '{command}'"
                    )
            for msg_id in expect.sent_messages or []:
                if msg_id not in self.messages:
                    self.d.error(
                        "SCN001", f"{tp}.expect.sent_messages", f"unknown message '{msg_id}'"
                    )
            for coord_id in given.coordination:
                if coord_id not in self.coordination:
                    self.d.error(
                        "SCN001", f"{tp}.given.coordination.{coord_id}", "unknown coordination"
                    )
        if not self.spec.tests:
            self.d.error("SCN001", "tests", "at least one L2 test is required")
        all_categories = set().union(*categories.values()) if categories else set()
        for category in TEST_CATEGORIES:
            if category not in all_categories:
                self.d.warning("SCN001", "tests", f"no test with category '{category}'")
        for role in self.eff.roles:
            for need in ("nominal", "priority_transition"):
                if role.state_machine and need not in categories.get(role.id, set()):
                    self.d.warning("SCN001", "tests", f"role '{role.id}' has no '{need}' test")

    # -- scenarios ----------------------------------------------------------------
    def check_scenarios(self) -> None:
        transition_ids = {
            tr.transition_id
            for role in self.eff.roles
            if role.state_machine
            for state in role.state_machine.states
            for tr in state.transitions
        }
        traceable = transition_ids | set(self.coordination) | set(self.formations)
        traced: set[str] = set()
        has_comm_event = False
        for s_index, scn in enumerate(self.spec.scenarios):
            sp = f"scenarios[{s_index}]"
            env = scn.environment
            self._quantity(env.duration, "time", f"{sp}.environment.duration")
            if env.time_step is not None:
                self._quantity(env.time_step, "time", f"{sp}.environment.time_step")
            if env.terrain_map is not None:
                path = PurePosixPath(env.terrain_map)
                if path.is_absolute() or ".." in path.parts or "\\" in env.terrain_map:
                    self.d.error(
                        "SCN001",
                        f"{sp}.environment.terrain_map",
                        "terrain_map must be a relative path inside the package",
                    )
            if env.terrain_map is None and env.terrain is None:
                self.d.info(
                    "SCN001", f"{sp}.environment", "no terrain given; all terrain is 'open'"
                )
            for role_id, init in scn.initial.items():
                role = self.roles.get(role_id)
                if role is None:
                    self.d.error("SCN001", f"{sp}.initial.{role_id}", "unknown role")
                    continue
                state = init.get("state")
                if state is not None and (
                    role.state_machine is None
                    or state not in {s.id for s in role.state_machine.states}
                ):
                    self.d.error(
                        "SCN001", f"{sp}.initial.{role_id}.state", f"state '{state}' undefined"
                    )
                formation = init.get("formation")
                if formation is not None and formation not in self.formations:
                    self.d.error(
                        "SCN001",
                        f"{sp}.initial.{role_id}.formation",
                        f"formation '{formation}' undefined",
                    )
                route = init.get("route")
                if route is not None and route not in env.routes:
                    self.d.error(
                        "SCN001", f"{sp}.initial.{role_id}.route", f"route '{route}' undefined"
                    )
            event_ids = {e.id for e in scn.events}
            for e_index, event in enumerate(scn.events):
                ep = f"{sp}.events[{e_index}]"
                self._ident(event.id, f"{ep}.id")
                if event.type in ("comm_degradation", "message_loss"):
                    has_comm_event = True
                if event.at is None and event.when is None:
                    self.d.error("SCN001", ep, "event needs 'at' or 'when'")
                if event.at is not None:
                    self._quantity(event.at, "time", f"{ep}.at")
                if event.when is not None:
                    self._event_when(event.when, f"{ep}.when")
                if event.type == "spawn_hostile" and not event.position:
                    self.d.error("SCN001", ep, "spawn_hostile needs position [x, y]")
                if (
                    event.type == "message_loss"
                    and event.message
                    and event.message not in self.messages
                ):
                    self.d.error("SCN001", ep, f"unknown message '{event.message}'")
                if event.type in ("comm_degradation", "message_loss") and event.duration is None:
                    self.d.error("SCN001", ep, f"{event.type} needs duration")
                if event.duration is not None:
                    self._quantity(event.duration, "time", f"{ep}.duration")
            self._unique([(e.id, f"{sp}.events[{i}]") for i, e in enumerate(scn.events)], "event")
            expect_ids = {x.id for x in scn.expect}
            if not scn.expect:
                self.d.error("SCN001", f"{sp}.expect", "scenario needs at least one expectation")
            for x_index, item in enumerate(scn.expect):
                self._expectation(item, f"{sp}.expect[{x_index}]", event_ids, expect_ids)
            for t_index, target in enumerate(scn.traces_to):
                if target not in traceable:
                    self.d.error(
                        "SCN001",
                        f"{sp}.traces_to[{t_index}]",
                        f"'{target}' is not a transition, coordination or formation",
                    )
                traced.add(target)
        for item_id in sorted(set(self.coordination) | set(self.formations)):
            if item_id not in traced:
                self.d.error(
                    "SCN001",
                    "scenarios",
                    f"'{item_id}' is not covered by any scenario traces_to",
                    suggestion="list it in traces_to of a scenario that exercises it, "
                    "or add a scenario that does",
                )
        if self.eff.messages and not has_comm_event:
            self.d.error(
                "SCN001",
                "scenarios",
                "no scenario includes a comm_degradation or message_loss event",
            )

    def _event_when(self, when: dict[str, Any], path: str) -> None:
        allowed = {"role", "instance", "terrain_class", "x_gte", "x_lte", "y_gte", "y_lte", "state"}
        extra = set(when) - allowed
        if extra:
            self.d.error("SCN001", path, f"unknown event trigger keys {sorted(extra)}")
        role = when.get("role")
        if role not in self.roles:
            self.d.error("SCN001", f"{path}.role", f"unknown role '{role}'")
        cls = when.get("terrain_class")
        if cls is not None and cls not in self.cat.vocabularies["terrain_classes"]:
            self.d.error("SCN001", f"{path}.terrain_class", f"unknown terrain class '{cls}'")

    def _observable_value(
        self, item: Any, comparator: str, dim: str, value: Any, path: str
    ) -> None:
        """The threshold must be in the observable's dimension and must constrain something."""
        if item.observable not in OBSERVABLE_DIMENSIONS:
            return
        wanted = OBSERVABLE_DIMENSIONS[item.observable]
        if wanted is not None and dim != wanted and dim != "ratio":
            self.d.error(
                "UNIT001",
                f"{path}.{comparator}",
                f"{item.observable} is a {wanted}, but the threshold is a {dim}",
            )
            return
        try:
            number = float(value)
        except (TypeError, ValueError):
            return
        if comparator in ("gte", "gt") and number <= 0 and not (comparator == "gt" and number == 0):
            self.d.error(
                "SCN001",
                f"{path}.{comparator}",
                f"{item.observable} {comparator} {number:g} always holds; "
                "give a threshold that the behavior could fail",
            )

    def _expectation(self, item: Any, path: str, events: set[str], expects: set[str]) -> None:
        if item.observable not in OBSERVABLES:
            self.d.error(
                "SCN001",
                f"{path}.observable",
                f"'{item.observable}' is not a whitelisted observable",
            )
            return
        needs_role = item.observable in (
            "role_state",
            "speed",
            "time_to_transition",
            "contact_reaction_time",
            "actions_issued",
            "min_interval",
            "max_interval",
        )
        if needs_role and item.role not in self.roles:
            self.d.error(
                "SCN001", f"{path}.role", f"observable needs a known role (got '{item.role}')"
            )
        if item.role is not None and item.role in self.roles and item.instance is not None:
            if item.instance >= self.roles[item.role].count:
                self.d.error("ROLE001", f"{path}.instance", "instance index out of range")
        if (
            item.observable in ("formation_error_rms", "formation_id")
            and item.formation is not None
        ):
            if item.formation not in self.formations:
                self.d.error(
                    "SCN001", f"{path}.formation", f"formation '{item.formation}' undefined"
                )
        if item.observable == "formation_error_rms" and item.formation is None:
            self.d.error("SCN001", f"{path}.formation", "formation_error_rms needs formation")
        if item.observable == "messages_sent" and item.message not in self.messages:
            self.d.error("SCN001", f"{path}.message", f"unknown message '{item.message}'")
        if item.observable == "actions_issued" and (
            item.command is None or self.cat.action(item.command) is None
        ):
            self.d.error("SCN001", f"{path}.command", f"unknown action '{item.command}'")
        if (
            item.observable in ("time_to_transition", "contact_reaction_time")
            and item.state is None
        ):
            self.d.error("SCN001", f"{path}.state", f"{item.observable} needs target state")
        comparators = [
            n for n in ("equals", "lte", "gte", "lt", "gt") if getattr(item, n) is not None
        ]
        if len(comparators) != 1:
            self.d.error("SCN001", path, "give exactly one of equals/lte/gte/lt/gt")
        else:
            value = getattr(item, comparators[0])
            if isinstance(value, dict):
                if "parameter_ref" in value:
                    if value["parameter_ref"] not in self.params:
                        self.d.error("REF001", f"{path}.{comparators[0]}", "unknown parameter")
                elif set(value) == {"value", "unit"}:
                    dim = infer_dimension(str(value["unit"]))
                    if dim is None:
                        self.d.error("UNIT001", f"{path}.{comparators[0]}", "unknown unit")
                    else:
                        self._observable_value(item, comparators[0], dim, value["value"], path)
                else:
                    self.d.error(
                        "SCN001",
                        f"{path}.{comparators[0]}",
                        "expected parameter_ref or {value, unit}",
                    )
        if item.within is not None:
            anchor = item.within.after_event or item.within.after
            if anchor is not None and anchor not in events | expects | {"start"}:
                self.d.error("SCN001", f"{path}.within", f"unknown event/expectation '{anchor}'")
            try:
                to_canonical(item.within.value, item.within.unit, "time")
            except UnitError as exc:
                self.d.error("UNIT001", f"{path}.within", str(exc))
        if item.during is not None:
            if item.during.start not in events | expects | {"start"}:
                self.d.error(
                    "SCN001",
                    f"{path}.during.from",
                    f"unknown event/expectation '{item.during.start}'",
                )
            if item.during.duration is not None:
                self._quantity(item.during.duration, "time", f"{path}.during.duration")


# --------------------------------------------------------------------------- API


def validate_data(
    data: dict[str, Any],
    *,
    evidence: EvidenceIndex | None = None,
    catalog: Catalog | None = None,
) -> ValidationResult:
    catalog = catalog or load_catalog()
    spec, items = parse_spec(data)
    if spec is None:
        return ValidationResult(None, None, None, sort_diagnostics(items))
    diag = Collector()
    diag.extend(items)
    expansion = expand(spec, catalog, diag)
    effective = effective_spec(spec, expansion)
    Validator(spec, effective, expansion, catalog, evidence or EvidenceIndex(), diag).run()
    unique = list({d.model_dump_json(): d for d in diag.items}.values())
    return ValidationResult(spec, effective, expansion, sort_diagnostics(unique))


def validate_package(package_dir: str | Path) -> ValidationResult:
    root = Path(package_dir)
    loaded = load_spec(root / "behavior_spec.yaml")
    if loaded.data is None:
        return ValidationResult(None, None, None, sort_diagnostics(loaded.diagnostics))
    evidence = load_evidence_index(root / "evidence_package.json")
    result = validate_data(loaded.data, evidence=evidence)
    result.diagnostics = sort_diagnostics([*loaded.diagnostics, *result.diagnostics])
    return result


__all__ = [
    "EvidenceIndex",
    "ValidationResult",
    "effective_spec",
    "iter_nodes",
    "load_evidence_index",
    "parse_spec",
    "role_usage",
    "validate_data",
    "validate_package",
    "distance_operand",
    "math",
]
