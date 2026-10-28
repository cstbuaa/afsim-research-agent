"""Tactic sheet → v1 skeleton + section records (design falcon-surface-formats-v2 §3).

① parse every block, ② link names through one symbol table, ③ infer what the model
should not have to write (behaviors from action leaves, blackboard declarations,
fallback transitions, priorities, ``synchronize_with``, scenario initial states,
``traces_to``), ④ emit the records the v1 assembler turns into ``behavior_spec.yaml``
plus a source map (element → block and line) used to point IR errors back at the sheet.

Whatever cannot be parsed or linked becomes a hole (:class:`SheetProblem`) and is left
out; the rest still compiles, so the repair loop can rewrite only the broken blocks.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from afsim_research_agent.capability.assess import gap_id
from afsim_research_agent.capability.constraints import VocabularyConstraints
from afsim_research_agent.falcon.models import (
    AssumptionDraft,
    BasisDraft,
    BehaviorsSection,
    BlackboardVariableDraft,
    ComponentDraft,
    ConditionsSection,
    ControlSection,
    CoordinationPlan,
    ElementPlan,
    FalconRequest,
    FlatNode,
    FlatTree,
    OutOfScope,
    ParameterDraft,
    ParametersSection,
    RequirementClause,
    RoleDraft,
    SkeletonDraft,
    StateDraft,
    StructureSection,
    TransitionDraft,
    VariableDraft,
    VerificationSection,
)
from afsim_research_agent.falcon.sheet.blocks import TREE_KINDS, Block, SheetProblem, split_sheet
from afsim_research_agent.falcon.sheet.btxml import XmlNode, parse_xml
from afsim_research_agent.falcon.sheet.mermaid import Diagram, parse_diagram
from afsim_research_agent.falcon.sheet.pyexpr import (
    ExprCompiler,
    ExprSymbols,
    compile_conditions,
    condition_names,
)
from afsim_research_agent.falcon.sheet.pytree import parse_pytree
from afsim_research_agent.falcon.sheet.timing import scenario_timing
from afsim_research_agent.falcon.sheet.values import (
    basis_from,
    distance_ref,
    is_id,
    near,
    operand_value,
    parameter_type,
    parse_quantity,
    quantity,
    slug,
)
from afsim_research_agent.ir.catalog import Catalog
from afsim_research_agent.ir.models import (
    Behavior,
    Condition,
    Coordination,
    Formation,
    Message,
    Scenario,
    TestCase,
)
from afsim_research_agent.ir.units import PHYSICAL_TYPES, UnitError, to_canonical

STAGES = ("structure", "control", "full")
REQUIRED = {  # "trees": an xml or python block per role
    "structure": ("model", "mermaid"),
    "control": ("model", "mermaid", "conditions", "trees"),
    "full": ("model", "mermaid", "conditions", "trees", "verification"),
}
OUTSIDE_VOCABULARY = (
    "动作表之外的节点不能自造：改用表中的节点；确实需要表中没有的能力时，"
    "在 ```yaml needs``` 中登记（会成为能力缺口），树里用替代写法"
)
DEFAULT_MOVER = {
    "ground_vehicle": "route_following_ground",
    "air_vehicle": "route_following_air",
    "surface_vessel": "route_following_surface",
}
CONTROL = {
    "ReactiveSequence": "sequence",
    "Sequence": "sequence_with_memory",
    "SequenceWithMemory": "sequence_with_memory",
    "ReactiveFallback": "selector",
    "Fallback": "selector_with_memory",
    "Selector": "selector",
    "Parallel": "parallel",
}
DECORATORS = {
    "Inverter": "inverter",
    "ForceSuccess": "succeeder",
    "Repeat": "repeat_count",
    "RetryUntilSuccessful": "repeat_until_done",
}
ACTION_ALIASES = {"SetBlackboard": "set_blackboard", "Sleep": "wait", "Wait": "wait"}
RESERVED_PORTS = {
    "name",
    "description",
    "_successIf",
    "_failureIf",
    "_while",
    "timeout",
    "continuous",
}
ID_ARGS = {
    "route",
    "formation",
    "message",
    "role",
    "sensor",
    "weapon",
    "comm",
    "processor",
    "target",
    "position",
}
NUMERIC_ARGS = {"speed", "distance", "time", "angle", "altitude", "ratio"}
EXAMPLE_QUANTITY = {
    "speed": '"20 km/h"',
    "distance": '"100 m"',
    "time": '"10 s"',
    "angle": '"90 deg"',
    "altitude": '"500 m"',
    "ratio": "0.2",
}
# when an action is complete (design §3.2); None = keeps running
DEFAULT_SUCCESS: dict[str, dict[str, Any] | None] = {
    "follow_route": {"mover_value": {"property": "route_complete"}},
    "follow_route_with_offset": {"mover_value": {"property": "route_complete"}},
    "move_to_position": {"mover_value": {"property": "at_destination"}},
    "move_to_platform": {"mover_value": {"property": "at_destination"}},
    "move_to_track": {"mover_value": {"property": "at_destination"}},
    "bound_to_position": {"mover_value": {"property": "at_destination"}},
    "keep_station": None,
    "hold_overwatch_position": None,
    "wait": None,  # the runtime ends a wait after its duration
}
MOVER_OK = {"mover_value": {"property": "available"}}
# the action each generated coordination behavior issues (ir/coordination.py)
GENERATED_COMMANDS = {
    "announce_formation": "send_message",
    "keep_station": "keep_station",
    "bound": "bound_to_position",
    "announce_bound": "send_message",
    "overwatch": "hold_overwatch_position",
    "send_report": "report_contact",
    "acknowledge": "send_message",
}
STAY_PRIORITY = 0


def camel_to_snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def snake_to_camel(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


@dataclass
class CompileResult:
    skeleton: SkeletonDraft | None
    sections: dict[str, BaseModel]
    problems: list[SheetProblem]
    source_map: dict[str, dict[str, Any]]
    blocks: list[Block]
    symbols: dict[str, Any] = field(default_factory=dict)
    needs: list[dict[str, Any]] = field(default_factory=list)  # yaml needs → capability gaps

    @property
    def errors(self) -> list[SheetProblem]:
        return [p for p in self.problems if p.severity == "error"]

    @property
    def warnings(self) -> list[SheetProblem]:
        return [p for p in self.problems if p.severity != "error"]


# ---------------------------------------------------------------------- YAML lines
def yaml_lines(text: str, first_line: int) -> dict[tuple[Any, ...], int]:
    """(key, key, …) → sheet line of that mapping key / sequence item (3 levels)."""
    out: dict[tuple[Any, ...], int] = {}
    try:
        root = yaml.compose(text)
    except yaml.YAMLError:
        return out

    def walk(node: Any, path: tuple[Any, ...], depth: int) -> None:
        if depth > 3 or node is None:
            return
        if isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                k = key.value
                out[(*path, k)] = first_line + key.start_mark.line
                walk(value, (*path, k), depth + 1)
        elif isinstance(node, yaml.SequenceNode):
            for index, item in enumerate(node.value):
                out[(*path, index)] = first_line + item.start_mark.line
                walk(item, (*path, index), depth + 1)

    walk(root, (), 0)
    return out


def _load_yaml(block: Block, problems: list[SheetProblem]) -> dict[str, Any]:
    try:
        data = yaml.safe_load(block.text) or {}
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = block.line + (mark.line if mark else 0)
        problems.append(
            SheetProblem(block.key, line, f"YAML 格式错误：{getattr(exc, 'problem', exc)}")
        )
        return {}
    if not isinstance(data, dict):
        problems.append(SheetProblem(block.key, block.line, "YAML 块的顶层应是映射（键: 值）"))
        return {}
    return data


# ---------------------------------------------------------------------- the compiler
class SheetCompiler:
    def __init__(
        self,
        text: str,
        *,
        request: FalconRequest,
        clauses: list[RequirementClause],
        catalog: Catalog,
        stage: str = "full",
        constraints: VocabularyConstraints | None = None,
        known_gaps: list[dict[str, Any]] | None = None,
        observed: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """``constraints``: the platform vocabulary from the capability catalog; nodes
        and reads the role's platform does not have are holes (closed vocabulary).
        ``known_gaps``: Crow's capability gaps of the selected tactic, which ``needs``
        entries may refer to by ID."""
        self.text = text
        self.constraints = constraints
        # L2 tests: the sheet states the intent (next state, transition, blackboard); the
        # mechanical results (actions, messages, tree status, path) come from the recorded
        # first run (``observed_expectations.yaml``), reviewed by a person
        self.observed = observed or {}
        self.written_mechanics: dict[str, dict[str, Any]] = {}
        self.test_inputs: dict[str, list[str]] = {}
        self.test_given: dict[str, list[str]] = {}  # test → given.<section>.<key> it sets
        self.tested: set[str] = set()  # transition ids some test expects to fire
        self.generated_tests: list[str] = []  # tests repeated for `* --> x` exceptions
        self.untested: list[str] = []
        # tests a person decided to set aside (verification: `deferred: true`); not compiled
        self.deferred_tests: list[dict[str, Any]] = []
        self.deferred_cover: set[str] = set()  # transitions only those tests would fire
        self.known_gaps = {str(g.get("id")): g for g in known_gaps or [] if g.get("id")}
        self.needs: list[dict[str, Any]] = []
        self.request = request
        self.clauses = clauses
        self.catalog = catalog
        self.stage = stage
        self.problems: list[SheetProblem] = []
        self.source: dict[str, dict[str, Any]] = {}
        self.blocks: dict[str, Block] = {}
        # results
        self.meta: dict[str, Any] = {}
        self.roles: dict[str, dict[str, Any]] = {}
        self.parameters: dict[str, ParameterDraft] = {}
        self.formations: list[dict[str, Any]] = []
        self.coordination: dict[str, dict[str, Any]] = {}
        self.assumptions: list[AssumptionDraft] = []
        self.messages: list[dict[str, Any]] = []
        self.diagrams: dict[str, Diagram] = {}
        self.transitions: list[dict[str, Any]] = []  # {role, state, id, to, when, sync, line}
        self.conditions: dict[
            str, dict[str, Any]
        ] = {}  # id -> {expression, description, line, block}
        self.condition_reads: dict[str, set[str]] = {}
        self.behaviors: dict[str, dict[str, Any]] = {}
        self._behavior_keys: dict[str, str] = {}
        self.trees: list[FlatTree] = []
        self.state_trees: dict[tuple[str, str], str] = {}
        self.role_conditions: dict[str, set[str]] = {}
        self.writes: dict[str, list[tuple[str, Any, str]]] = {}  # role -> (var, value, tree)
        self.declared: dict[str, dict[str, Any]] = {}  # role -> {var: default}
        # leader role -> (coordination id, {leader state: formation}) from `formations:`
        self.state_formations: dict[str, tuple[str, dict[str, str]]] = {}
        self.implements: dict[str, list[str]] = {}
        self.out_of_scope: list[OutOfScope] = []
        self.tests: list[TestCase] = []
        self.scenarios: list[Scenario] = []
        self.transition_basis: dict[str, dict[str, Any]] = {}
        self.node_ids: set[str] = set()
        self._prescanned: set[str] = set()
        self._failed_trees: set[tuple[str, str]] = set()
        self._tree_state: dict[str, str] = {}
        self.custom_messages: dict[str, set[str]] = {}
        self._tree_ids: dict[str, str] = {}
        self._implements_line: int | None = None
        self._formation_var: tuple[str, str] | None = None

    # ------------------------------------------------------------------ helpers
    def fail(
        self,
        block: str,
        line: int | None,
        message: str,
        candidates: list[str] | None = None,
        *,
        warning: bool = False,
    ) -> None:
        self.problems.append(
            SheetProblem(
                block, line, message, candidates or [], severity="warning" if warning else "error"
            )
        )

    def mark(self, key: str, block: str, line: int | None) -> None:
        self.source.setdefault(key, {"block": block, "line": line})

    def unavailable(self, element: str, role: str) -> str | None:
        """Why the role's platform cannot use a vocabulary element (None: it can).

        Closed vocabulary (BTGenBot-2, CommandSwarm): the prompt lists only what the
        platform has; anything else written anyway is refused here, not left to the
        capability check after generation."""
        if self.constraints is None or role not in self.roles:
            return None
        platform = self.roles[role]["platform"]
        known = self.constraints.platform_classes
        if known and platform not in known:
            return None  # a class the constraints were not built for; K3 judges it
        tier = self.constraints.tier(element)
        only = self.constraints.only_for.get(element)
        if tier == "unavailable" or (only and platform not in only):
            label = self.constraints.label(element)
            return f"平台 {platform} 不具备 {element}（能力目录：{label}）。{OUTSIDE_VOCABULARY}"
        return None

    def available_actions(self, role: str, category: str | None = None) -> list[str]:
        names = [
            snake_to_camel(a.name)
            for a in self.catalog.actions.values()
            if (category is None or a.category == category) and not self.unavailable(a.name, role)
        ]
        return names[:8]

    def symbols(self) -> ExprSymbols:
        components: dict[str, set[str]] = {"sensor": set(), "weapon": set(), "comm": set()}
        for role in self.roles.values():
            for kind, key in (("sensor", "sensors"), ("weapon", "weapons"), ("comm", "comms")):
                components[kind] |= {c["id"] for c in role[key]}
        return ExprSymbols(
            catalog=self.catalog,
            parameters=set(self.parameters),
            conditions=set(self.conditions) | set(self._prescanned),
            coordination={
                k: v["pattern"] for k, v in self.coordination.items() if not v.get("_bad")
            },
            roles=set(self.roles),
            components=components,
        )

    # ------------------------------------------------------------------ run
    def run(self) -> CompileResult:
        blocks, problems = split_sheet(self.text)
        self.problems += problems
        self.blocks = {b.key: b for b in blocks}
        for kind in REQUIRED[self.stage]:
            kinds = TREE_KINDS if kind == "trees" else (kind,)
            if not any(b.kind in kinds for b in blocks):
                tree_kind = "pytree" if self.request.sheet_tree_format == "python" else "xml"
                self.fail(tree_kind if kind == "trees" else kind, None, f"缺少代码块 {kind}")
        model = self.blocks.get("model")
        if model is not None:
            self.model_block(model)
        self._prescanned = (
            set(condition_names(self.blocks["conditions"].text))
            if "conditions" in self.blocks
            else set()
        )
        for block in blocks:
            if block.kind == "mermaid":
                self.mermaid_block(block)
        missing = [r for r in self.roles if r not in self.diagrams]
        for role in missing:
            self.fail(f"mermaid:{role}", None, f"角色 {role} 没有状态图（```mermaid {role}）")
        self.coordination_block_checks()
        if "conditions" in self.blocks:
            self.conditions_block(self.blocks["conditions"])
        self.transition_conditions()
        self.coordination_conditions()
        tree_roles = sorted({b.role or "" for b in blocks if b.kind in TREE_KINDS})
        for role in tree_roles:
            self.tree_blocks(role, [b for b in blocks if b.kind in TREE_KINDS and b.role == role])
        if self.stage != "structure":
            for role, diagram in self.diagrams.items():
                for state in diagram.states:
                    if state in diagram.terminal:
                        continue  # an end state needs no tree
                    if (role, state) not in self.state_trees and (
                        role,
                        state,
                    ) not in self._failed_trees:
                        self.fail(
                            self.tree_key(role),
                            None,
                            f'状态 {role}.{state} 没有行为树（<BehaviorTree ID="{state}">，'
                            f"或 Python 块中的 def {state}():）",
                        )
        if "basis" in self.blocks:
            self.basis_block(self.blocks["basis"])
        if "needs" in self.blocks:
            self.needs_block(self.blocks["needs"])
        self.vocabulary_reads()
        self.generated_reads()
        if "verification" in self.blocks:
            self.verification_block(self.blocks["verification"])
            self.wildcard_tests()
            self.test_coverage(self.blocks["verification"])
            for sid, message in scenario_timing(self):
                where = self.source.get(f"scenario:{sid}", {})
                self.fail("verification", where.get("line"), message)
        self.coverage()
        return self.result()

    # ------------------------------------------------------------------ model block
    def model_block(self, block: Block) -> None:
        data = _load_yaml(block, self.problems)
        lines = yaml_lines(block.text, block.line)
        key = block.key
        self.meta = dict(data.get("model") or {})
        intents = list(self.catalog.vocabularies["intents"])
        intent = self.meta.get("intent")
        if isinstance(intent, list) and intent and intent[0] in intents:
            intent = self.meta["intent"] = intent[0]  # a one-item list: the intent itself
        if intent not in intents:
            self.fail(
                key,
                lines.get(("model",)),
                f"model.intent '{intent}' 不在意图词表中",
                near(str(intent), intents),
            )
        for name, spec in (data.get("roles") or {}).items():
            self.role(name, spec or {}, key, lines.get(("roles", name)))
        for name, spec in (data.get("parameters") or {}).items():
            self.parameter(name, spec, key, lines.get(("parameters", name)))
        for name, spec in (data.get("formations") or {}).items():
            self.formation(name, spec or {}, key, lines.get(("formations", name)))
        for name, spec in (data.get("coordination") or {}).items():
            if not is_id(name):
                self.fail(
                    key,
                    lines.get(("coordination", name)),
                    f"协同 ID '{name}' 不合法（英文蛇形，不含 __）",
                )
                continue
            if not isinstance(spec, dict):
                self.fail(key, lines.get(("coordination", name)), f"协同 {name} 应是映射")
                continue
            self.coordination[name] = {**spec, "_line": lines.get(("coordination", name))}
            self.mark(f"coordination:{name}", key, lines.get(("coordination", name)))
        for name, spec in (data.get("assumptions") or {}).items():
            line = lines.get(("assumptions", name))
            if isinstance(spec, str):
                spec = {"statement": spec}
            if not isinstance(spec, dict) or not spec.get("statement"):
                self.fail(key, line, f"假设 {name} 需要 statement")
                continue
            aid = name if str(name).startswith("assumption_") else f"assumption_{name}"
            self.assumptions.append(
                AssumptionDraft(
                    id=aid,
                    statement=str(spec["statement"]),
                    impact=spec.get("impact"),
                    affects=[str(a) for a in spec.get("affects") or []],
                )
            )
            self.mark(f"assumption:{aid}", key, line)
        for index, spec in enumerate(data.get("messages") or []):
            self.messages.append(spec)
            self.mark(
                f"message:{spec.get('id') if isinstance(spec, dict) else index}",
                key,
                lines.get(("messages", index)),
            )
        for clause, elements in (data.get("implements") or {}).items():
            names = [elements] if isinstance(elements, str) else list(elements or [])
            self.implements[str(clause)] = [str(n) for n in names]
        self._implements_line = lines.get(("implements",))
        for clause, reason in (data.get("out_of_scope") or {}).items():
            self.out_of_scope.append(OutOfScope(clause_id=str(clause), reason=str(reason or "")))
        unknown = set(data) - {
            "model",
            "roles",
            "parameters",
            "formations",
            "coordination",
            "assumptions",
            "messages",
            "implements",
            "out_of_scope",
        }
        for name in sorted(unknown):
            self.fail(
                key, lines.get((name,)), f"model 块中未知的键 '{name}'（已忽略）", warning=True
            )

    def role(self, name: str, spec: dict[str, Any], block: str, line: int | None) -> None:
        if not is_id(name):
            self.fail(block, line, f"角色 ID '{name}' 不合法（英文蛇形，不含 __）")
            return
        platform = spec.get("platform") or spec.get("capability_class") or "ground_vehicle"
        classes = ("ground_vehicle", "air_vehicle", "surface_vessel", "static_node")
        if platform not in classes:
            self.fail(block, line, f"角色 {name} 的平台类别 '{platform}' 无效", list(classes))
            platform = "ground_vehicle"

        def components(value: Any) -> list[dict[str, str]]:
            if isinstance(value, dict):
                return [{"id": str(k), "capability": str(v)} for k, v in value.items()]
            if isinstance(value, list):
                out = []
                for item in value:
                    if isinstance(item, dict) and "id" in item:
                        out.append(
                            {"id": str(item["id"]), "capability": str(item.get("capability", ""))}
                        )
                return out
            return []

        mover = spec.get("mover", True)
        if mover is True or mover in ("default", "yes"):  # "has a mover": the class default
            mover = DEFAULT_MOVER.get(platform)
        elif mover in (False, None, "none", "no"):
            mover = None
        elif not isinstance(mover, str):
            self.fail(
                block,
                line,
                f"角色 {name} 的 mover 应是机动能力名（如 route_following_ground），得到 {mover!r}；按平台默认",
                warning=True,
            )
            mover = DEFAULT_MOVER.get(platform)
        superior = spec.get("superior")
        if superior is not None and not isinstance(superior, str):
            self.fail(block, line, f"角色 {name} 的 superior 应是一个角色名，得到 {superior!r}")
            superior = None
        count = spec.get("count", 1)
        if not isinstance(count, int) or count < 1:
            self.fail(block, line, f"角色 {name} 的 count 应是正整数")
            count = 1
        self.roles[name] = {
            "count": count,
            "platform": platform,
            "description": str(spec.get("description") or ""),
            "superior": superior,
            "mover": mover,
            "sensors": components(spec.get("sensors")),
            "comms": components(spec.get("comms")),
            "weapons": components(spec.get("weapons")),
            "line": line,
        }
        board = spec.get("blackboard") or {}
        if isinstance(board, dict):
            self.declared[name] = dict(board)
        self.mark(f"role:{name}", block, line)

    def parameter(self, name: str, spec: Any, block: str, line: int | None) -> None:
        if not is_id(name):
            self.fail(block, line, f"参数名 '{name}' 不合法（英文蛇形）")
            return
        if not isinstance(spec, dict):
            spec = {"value": spec}
        kind, value, unit = parameter_type(spec.get("value"), spec.get("type"))
        if kind is None:
            self.fail(
                block,
                line,
                f'参数 {name} 的值 {spec.get("value")!r} 无法识别（写成 "50 m"、3、true）',
            )
            return
        entry: dict[str, Any] = {"name": name, "type": kind, "value": value}
        if unit and kind in PHYSICAL_TYPES:
            entry["unit"] = unit
        rng = spec.get("range")
        if isinstance(rng, list) and len(rng) == 2 and kind in PHYSICAL_TYPES and unit:
            bounds = []
            for item in rng:
                q = parse_quantity(item) if isinstance(item, str) else None
                if q is None and isinstance(item, int | float):
                    bounds.append(float(item))
                    continue
                try:
                    factor = to_canonical(1.0, unit, kind)
                    bounds.append(
                        round(to_canonical(float(q[0]), q[1], kind) / factor, 6) if q else None
                    )
                except (UnitError, TypeError):
                    bounds.append(None)
            if None in bounds:
                self.fail(block, line, f"参数 {name} 的 range {rng} 无法识别", warning=True)
            else:
                entry["minimum"], entry["maximum"] = bounds
        if spec.get("description"):
            entry["description"] = str(spec["description"])
        entry["basis"] = BasisDraft(**basis_from(spec.get("basis"), spec))
        entry["implements"] = [str(c) for c in spec.get("implements") or []]
        try:
            self.parameters[name] = ParameterDraft(**entry)
        except ValidationError as exc:
            self.fail(block, line, f"参数 {name} 无效：{exc.errors()[0]['msg']}")
            return
        self.mark(f"parameter:{name}", block, line)

    def formation(self, name: str, spec: dict[str, Any], block: str, line: int | None) -> None:
        if not is_id(name):
            self.fail(block, line, f"编队 ID '{name}' 不合法")
            return
        params = set(self.parameters)
        slots = []
        for instance, offsets in (spec.get("slots") or {}).items():
            role = str(instance).split("[", 1)[0]
            if role not in self.roles:
                self.fail(
                    block,
                    line,
                    f"编队 {name} 的站位 {instance} 引用了未定义的角色",
                    near(role, self.roles),
                )
                continue
            if not isinstance(offsets, dict):
                self.fail(
                    block, line, f"编队 {name} 的站位 {instance} 应写成 {{forward: …, right: …}}"
                )
                continue
            refs = {}
            for axis in ("forward", "right"):
                ref, error = distance_ref(offsets.get(axis, "0 m"), params)
                if error:
                    self.fail(block, line, f"编队 {name} {instance}.{axis}：{error}")
                    break
                refs[axis] = ref
            else:
                slots.append({"role_instance": str(instance), **refs})
        if not slots:
            self.fail(block, line, f"编队 {name} 没有有效站位（领队在原点，只列跟随者的站位）")
            return
        item: dict[str, Any] = {"id": name, "slots": slots}
        if spec.get("description"):
            item["description"] = str(spec["description"])
        if spec.get("tolerance") is not None:
            ref, error = distance_ref(spec["tolerance"], params)
            if error:
                self.fail(block, line, f"编队 {name} 的 tolerance：{error}")
            else:
                item["tolerance"] = ref
        item["basis"] = basis_from(spec.get("basis"), spec)
        self.formations.append(item)
        self.mark(f"formation:{name}", block, line)

    # ------------------------------------------------------------------ mermaid
    def mermaid_block(self, block: Block) -> None:
        role = block.role or ""
        if role not in self.roles:
            self.fail(
                block.key,
                block.line,
                f"状态图的角色 '{role}' 未在 model.roles 中定义",
                near(role, self.roles),
            )
            return
        diagram, problems = parse_diagram(block.text, role, block.key, block.line)
        self.problems += problems
        for state in diagram.states:
            if not is_id(state):
                self.fail(
                    block.key, diagram.lines[state], f"状态名 '{state}' 不合法（英文蛇形，不含 __）"
                )
            self.mark(f"state:{role}.{state}", block.key, diagram.lines[state])
        self.diagrams[role] = diagram

    def transition_conditions(self) -> None:
        """Mermaid labels → expressions; ids, priorities, sync and fallbacks."""
        used: set[str] = set()
        for role, diagram in self.diagrams.items():
            block = f"mermaid:{role}"
            rank: dict[str, int] = {}
            for tr in diagram.transitions:
                if tr.source == tr.target:
                    self.fail(
                        block,
                        tr.line,
                        f"{tr.source} --> {tr.target} 回到本状态：条件不成立时留在本状态由编译器自动补全，"
                        "删去这一行（写出来会重新进入状态、重置计时）",
                    )
                    continue
                compiler = ExprCompiler(self.symbols(), block, tr.line)
                label = tr.label
                when: dict[str, Any] | None
                bare = re.fullmatch(r"(not\s+)?([A-Za-z_]\w*)", label)
                if (
                    bare
                    and bare.group(2) not in self.symbols().conditions
                    and bare.group(2) not in self.parameters
                ):
                    # defined in the control draft; later, a hole in the conditions block
                    # (the transition is kept so nothing that refers to it breaks too)
                    when = {"condition_ref": bare.group(2)}
                    if self.stage != "structure":
                        self.fail(
                            "conditions",
                            None,
                            f"条件 '{bare.group(2)}' 在 {role} 的状态图中使用（第 {tr.line} 行），但没有定义",
                            near(bare.group(2), self.symbols().conditions),
                        )
                    if bare.group(1):
                        when = {"not": when}
                else:
                    when = compiler.parse(label)
                    self.problems += compiler.problems
                    if when is None:
                        continue
                    if not bare:  # an expression label becomes an anonymous condition
                        cid = slug(f"{role}_{tr.source}_to_{tr.target}_when")
                        self.conditions[cid] = {
                            "expression": when,
                            "description": label,
                            "line": tr.line,
                            "block": block,
                        }
                        self.condition_reads[cid] = compiler.blackboard_reads
                        self.mark(f"condition:{cid}", block, tr.line)
                        when = {"condition_ref": cid}
                self._note_conditions(role, when)
                tid = slug(f"{role}_{tr.source}_to_{tr.target}")
                base, n = tid, 2
                while tid in used:
                    tid, n = f"{base}_{n}", n + 1
                used.add(tid)
                k = rank.get(tr.source, 0)
                rank[tr.source] = k + 1
                sync = None
                if tr.sync:
                    sync = self._sync_for(role)
                    if sync is None:
                        self.fail(
                            block,
                            tr.line,
                            f"[sync] 需要 {role} 作为 leader_follower 的 leader 或 synchronized_transition 的 initiator",
                        )
                self.transitions.append(
                    {
                        "role": role,
                        "state": tr.source,
                        "id": tid,
                        "to": tr.target,
                        "when": when,
                        "priority": max(100 - 10 * k, 10),
                        "sync": sync,
                        "line": tr.line,
                    }
                )
                self.mark(f"transition:{tid}", block, tr.line)
            for state in diagram.states:
                if state in diagram.terminal:
                    continue
                tid = slug(f"{role}_{state}_stay")
                self.transitions.append(
                    {
                        "role": role,
                        "state": state,
                        "id": tid,
                        "to": state,
                        "when": {"literal": True},
                        "priority": STAY_PRIORITY,
                        "sync": None,
                        "line": diagram.lines[state],
                    }
                )
                self.mark(f"transition:{tid}", block, diagram.lines[state])
        # the same arrow twice: the second never fires (or [sync] was written separately)
        first: dict[tuple[str, str, str], dict[str, Any]] = {}
        for tr in self.transitions:
            if tr["when"] == {"literal": True}:
                continue
            key = (tr["role"], tr["state"], tr["to"])
            if key in first:
                self.fail(
                    f"mermaid:{tr['role']}",
                    tr["line"],
                    f"转换 {tr['state']} --> {tr['to']} 写了两次（第 {first[key]['line']} 行已有）："
                    "同一转换只写一行，需要同步给跟随角色就在那一行末尾加 [sync]",
                )
            else:
                first[key] = tr
        for tr in self.transitions:
            if not tr["sync"]:
                continue
            coord = self.coordination[tr["sync"]]
            for member in self._sync_members(tr["sync"], coord):
                own = first.get((member, tr["state"], tr["to"]))
                if own is not None and not own["sync"]:
                    self.fail(
                        f"mermaid:{member}",
                        own["line"],
                        f"{member} 的 {tr['state']} --> {tr['to']} 也由 {tr['role']} 的 [sync] 转换同步：若这是"
                        "跟随角色自己发现情况后的转换（如自己发现敌情）可以保留；若只是接收 leader 的命令，删去这一行",
                        warning=True,
                    )
        # followers must have the states a synchronized transition goes to
        for tr in self.transitions:
            if not tr["sync"]:
                continue
            coord = self.coordination[tr["sync"]]
            for member in self._sync_members(tr["sync"], coord):
                diagram = self.diagrams.get(member)
                if diagram is not None and tr["to"] not in diagram.states:
                    self.fail(
                        f"mermaid:{member}",
                        None,
                        f"{tr['role']} 的 [sync] 转换去往 {tr['to']}，跟随角色 {member} 也需要同名状态 {tr['to']}",
                    )

    def _receivers(self, cid: str, name: str) -> list[str] | None:
        """Roles that receive the message behind a generated condition (None: any)."""
        spec = self.coordination.get(cid) or {}
        field_name = GENERATED_RECEIVER.get(str(spec.get("pattern")), {}).get(name)
        if field_name is None:
            return None
        value = spec.get(field_name)
        items = [value] if isinstance(value, str) else list(value or [])
        return sorted({str(v).split("[", 1)[0] for v in items})

    def _message_receivers(self, cid: str, name: str) -> list[str] | None:
        """Roles a coordination message is sent to (None: unknown, not checked)."""
        spec = self.coordination.get(cid) or {}
        field_name = MESSAGE_RECEIVER.get(str(spec.get("pattern")), {}).get(name)
        if field_name is None:
            return None
        value = spec.get(field_name)
        items = [value] if isinstance(value, str) else list(value or [])
        return sorted({str(v).split("[", 1)[0] for v in items}) or None

    def _component_ids(self, role: str) -> list[str]:
        spec = self.roles.get(role) or {}
        out = []
        for kind in ("sensors", "comms", "weapons"):
            for item in spec.get(kind) or []:
                if isinstance(item, dict) and item.get("id"):
                    out.append(str(item["id"]))
        return out

    def generated_reads(self) -> None:
        """A generated condition only becomes true for the role that receives its
        message: report_and_wait's ack_received for the reporter, report_received for
        the receiver, leader_follower's formation_known for the followers."""
        reported: set[tuple[str, str]] = set()
        for role in sorted(self.role_conditions):
            seen: set[str] = set()
            stack = list(self.role_conditions[role])
            while stack:
                cid = stack.pop()
                if cid in seen:
                    continue
                seen.add(cid)
                if cid in self.conditions:
                    stack += _refs(self.conditions[cid]["expression"])
            for ref in sorted(seen):
                coord, sep, name = ref.partition("__")
                if not sep or coord not in self.coordination:
                    continue
                receivers = self._receivers(coord, name)
                if receivers is None or role in receivers or (role, ref) in reported:
                    continue
                reported.add((role, ref))
                users = [
                    c
                    for c, spec in self.conditions.items()
                    if ref in _refs(spec["expression"]) and c in seen
                ]
                where = self.conditions[users[0]] if users else None
                others = [
                    f"{coord}.{n}"
                    for n, _f in GENERATED_RECEIVER.get(
                        str(self.coordination[coord].get("pattern")), {}
                    ).items()
                    if role in (self._receivers(coord, n) or [])
                ]
                self.fail(
                    where["block"] if where else f"mermaid:{role}",
                    where["line"] if where else None,
                    f"{coord}.{name} 只有 {'/'.join(receivers)} 会收到对应消息，{role} 用它永远不成立"
                    + (f"；{role} 应使用 {'、'.join(others)}" if others else ""),
                    others,
                )

    def vocabulary_reads(self) -> None:
        """Closed vocabulary for reads: every condition a role evaluates (through
        condition references) may only read what the role's platform has."""
        if self.constraints is None:
            return
        reported: set[tuple[str, str]] = set()
        for role in sorted(self.role_conditions):
            seen: set[str] = set()
            stack = list(self.role_conditions[role])
            while stack:
                cid = stack.pop()
                if cid in seen or cid not in self.conditions:
                    continue
                seen.add(cid)
                stack += _refs(self.conditions[cid]["expression"])
            for cid in sorted(seen):
                c = self.conditions[cid]
                for element in vocabulary_elements(c["expression"], self.catalog):
                    refused = self.unavailable(element, role)
                    if refused and (cid, element) not in reported:
                        reported.add((cid, element))
                        self.fail(c["block"], c["line"], f"条件 {cid}（{role} 使用）：{refused}")
        for tr in self.transitions:
            for element in vocabulary_elements(tr["when"], self.catalog):
                refused = self.unavailable(element, tr["role"])
                if refused:
                    self.fail(f"mermaid:{tr['role']}", tr["line"], f"转换条件：{refused}")

    def _sync_for(self, role: str) -> str | None:
        for cid, spec in self.coordination.items():
            if spec.get("pattern") == "leader_follower" and spec.get("leader") == role:
                return cid
            if spec.get("pattern") == "synchronized_transition" and spec.get("initiator") == role:
                return cid
        return None

    @staticmethod
    def _sync_members(cid: str, spec: dict[str, Any]) -> list[str]:
        value = (
            spec.get("followers")
            if spec.get("pattern") == "leader_follower"
            else spec.get("participants")
        )
        items = [value] if isinstance(value, str) else list(value or [])
        return [str(v).split("[", 1)[0] for v in items]

    def _note_conditions(self, role: str, expression: Any) -> None:
        found = self.role_conditions.setdefault(role, set())

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                ref = node.get("condition_ref")
                if isinstance(ref, str):
                    found.add(ref)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(expression)

    # ------------------------------------------------------------------ coordination
    def coordination_block_checks(self) -> None:
        """Fields that do not need conditions (those come after the conditions block)."""
        block = "model"
        params = set(self.parameters)
        for cid, spec in self.coordination.items():
            line = spec.get("_line")
            pattern = spec.get("pattern")
            if pattern not in self.catalog.patterns:
                self.fail(
                    block,
                    line,
                    f"协同 {cid} 的模式 '{pattern}' 不在目录中",
                    sorted(self.catalog.patterns),
                )
                spec["_bad"] = True
                continue
            item: dict[str, Any] = {"id": cid, "pattern": pattern}
            if spec.get("description"):
                item["description"] = str(spec["description"])

            def role_ref(value: Any, field_name: str) -> Any:
                items = [value] if isinstance(value, str) else list(value or [])
                for v in items:
                    base = str(v).split("[", 1)[0]
                    if base not in self.roles:
                        self.fail(
                            block,
                            line,
                            f"协同 {cid}.{field_name} 引用了未定义的角色 '{v}'",
                            near(base, self.roles),
                        )
                return value

            if pattern == "leader_follower":
                twin = [
                    other
                    for other, o in self.coordination.items()
                    if other != cid
                    and o.get("pattern") == "leader_follower"
                    and o.get("leader") == spec.get("leader")
                    and o.get("followers") == spec.get("followers")
                    and list(self.coordination).index(other) < list(self.coordination).index(cid)
                ]
                if twin:
                    self.fail(
                        block,
                        line,
                        f"协同 {cid} 与 {twin[0]} 是同一组 leader/followers：只需一个 leader_follower；"
                        "切换编队由 leader 在行为树中用 SetBlackboard 改写编队变量",
                    )
                    spec["_bad"] = True
                    continue
                item["leader"] = role_ref(spec.get("leader"), "leader")
                item["followers"] = role_ref(spec.get("followers"), "followers")
                by_state = spec.get("formations") or spec.get("formation_by_state")
                if isinstance(by_state, dict) and by_state:
                    known = {f["id"] for f in self.formations}
                    mapping = {}
                    for state, formation in by_state.items():
                        if str(formation) not in known:
                            self.fail(
                                block,
                                line,
                                f"协同 {cid}.formations 中状态 {state} 的编队 '{formation}' 未定义",
                                near(str(formation), known),
                            )
                            continue
                        mapping[str(state)] = str(formation)
                    leader = str(spec.get("leader") or "").split("[", 1)[0]
                    diagram = self.diagrams.get(leader)
                    if diagram is not None:
                        for state in [st for st in mapping if st not in diagram.states]:
                            self.fail(
                                block,
                                line,
                                f"协同 {cid}.formations 中的 '{state}' 不是 {leader} 的状态",
                                near(state, set(diagram.states)),
                            )
                            del mapping[state]
                    self.state_formations[leader] = (cid, mapping)
                    unused = sorted(known - set(mapping.values()))
                    if unused:
                        self.fail(
                            block,
                            line,
                            f"协同 {cid}.formations 没有把编队 {unused} 分配给任何 {leader} 状态"
                            "（每个编队写成 状态: 编队）",
                        )
                    spec.setdefault("formation_from", f"{leader}.{FORMATION_VAR}")
                source = spec.get("formation_from")
                if isinstance(source, str) and "." in source:
                    owner, var = source.split(".", 1)
                    var = var.replace("mission.", "").replace("bb.", "")
                    item["formation_ref"] = {"blackboard_value": f"{owner}.mission.{var}"}
                    self.declared.setdefault(owner, {}).setdefault(var, None)
                    self._formation_var = (owner, var)
                elif spec.get("formation"):
                    item["formation_ref"] = {"literal": str(spec["formation"])}
                else:
                    self.fail(
                        block,
                        line,
                        f"协同 {cid} 需要 formations: {{leader 的状态: 编队ID, …}}"
                        "（只有一个编队时也可写 formation: 编队ID）",
                    )
                if spec.get("resync_interval") is not None:
                    item["resync_interval"] = quantity(spec["resync_interval"])
            elif pattern == "bounding_overwatch":
                groups = spec.get("groups") or {
                    "bounding": spec.get("bounding"),
                    "overwatch": spec.get("overwatch"),
                }
                item["groups"] = {
                    k: [str(v) for v in (groups.get(k) or [])] for k in ("bounding", "overwatch")
                }
                for k, members in item["groups"].items():
                    role_ref(members, k)
                for target, names in (
                    ("bound_distance", ("bound_distance",)),
                    ("standoff_distance", ("standoff", "standoff_distance")),
                ):
                    value = next((spec[n] for n in names if spec.get(n) is not None), None)
                    if value is None:
                        if target == "bound_distance":
                            self.fail(block, line, f"协同 {cid} 需要 bound_distance")
                        continue
                    ref, error = distance_ref(value, params)
                    if error:
                        self.fail(block, line, f"协同 {cid}.{target}：{error}")
                    else:
                        item[target] = ref
                if spec.get("objective"):
                    item["objective"] = str(spec["objective"])
            elif pattern == "report_and_wait":
                item["reporter"] = role_ref(spec.get("reporter"), "reporter")
                item["receiver"] = role_ref(spec.get("receiver"), "receiver")
                timeout = quantity(spec.get("ack_timeout", "30 s"))
                if timeout is None:
                    self.fail(block, line, f'协同 {cid}.ack_timeout 应写成 "30 s"')
                item["ack_timeout"] = timeout
            elif pattern == "synchronized_transition":
                item["initiator"] = role_ref(spec.get("initiator"), "initiator")
                participants = spec.get("participants")
                item["participants"] = (
                    [participants] if isinstance(participants, str) else list(participants or [])
                )
                role_ref(item["participants"], "participants")
            item["basis"] = basis_from(spec.get("basis"), spec)
            spec["_item"] = item

    def coordination_conditions(self) -> None:
        for cid, spec in self.coordination.items():
            item = spec.get("_item")
            if item is None or spec.get("pattern") != "bounding_overwatch":
                continue
            active = spec.get("active_when")
            if active is None:
                self.fail("model", spec.get("_line"), f"协同 {cid} 需要 active_when（条件名）")
                continue
            if (
                self.stage == "structure"
                and isinstance(active, str)
                and re.fullmatch(r"[A-Za-z_]\w*", active)
            ):
                item["active_when"] = {"condition_ref": active}
                continue
            compiler = ExprCompiler(self.symbols(), "model", spec.get("_line") or 1)
            expression = compiler.parse(str(active))
            self.problems += compiler.problems
            if expression is not None:
                item["active_when"] = expression

    # ------------------------------------------------------------------ conditions
    def conditions_block(self, block: Block) -> None:
        defs, problems = compile_conditions(block.text, self.symbols(), block.key, block.line)
        self.problems += problems
        for d in defs:
            if not is_id(d.name):
                self.fail(block.key, d.line, f"条件名 '{d.name}' 不合法（英文蛇形，不含 __）")
                continue
            if d.name in self.parameters:
                self.fail(block.key, d.line, f"条件名 '{d.name}' 与参数同名")
                continue
            if d.expression is None:
                continue
            self.conditions[d.name] = {
                "expression": d.expression,
                "description": d.description,
                "line": d.line,
                "block": block.key,
            }
            self.condition_reads[d.name] = d.reads
            self.mark(f"condition:{d.name}", block.key, d.line)

    # ------------------------------------------------------------------ xml
    def tree_key(self, role: str) -> str:
        """The block a role's trees are written in (the requested form when neither exists)."""
        for kind in ("xml", "pytree"):
            if f"{kind}:{role}" in self.blocks:
                return f"{kind}:{role}"
        return f"pytree:{role}" if self.request.sheet_tree_format == "python" else f"xml:{role}"

    def tree_blocks(self, role: str, blocks: list[Block]) -> None:
        """A role's ``xml`` and ``python`` blocks; trees from both are linked together."""
        if role not in self.roles:
            for block in blocks:
                self.fail(
                    block.key,
                    block.line,
                    f"{block.label} 的角色 '{role}' 未在 model.roles 中定义",
                    near(role, self.roles),
                )
            return
        trees: list[tuple[XmlNode, str]] = []
        incomplete: set[str] = set()
        coord_trees = {
            f"{c}.{t}"
            for c in self.symbols().coordination
            for t in self.symbols().generated(c, "trees")
        }
        for block in blocks:
            if block.kind == "pytree":
                root, problems, failed = parse_pytree(
                    block.text, block.key, block.line, coord_trees
                )
                incomplete |= failed
            else:
                root, problems = parse_xml(block.text, block.key, block.line)
            self.problems += problems
            if root is None:
                continue
            found = (
                [n for n in root.children if n.tag == "BehaviorTree"]
                if root.tag == "root"
                else ([root] if root.tag == "BehaviorTree" else [])
            )
            if not found and block.kind == "xml":
                self.fail(block.key, root.line, '没有 <BehaviorTree ID="状态名"> 定义')
            trees += [(t, block.key) for t in found]
        states = set(self.diagrams[role].states) if role in self.diagrams else set()
        names: dict[str, tuple[XmlNode, str]] = {}
        for tree, key in trees:
            name = tree.attrs.get("ID", "")
            if name in names:
                self.fail(key, tree.line, f"树 {name} 定义了两次（使用后一个）", warning=True)
            names[name] = (tree, key)
        self._expand_state_formations(role, names, states)
        self._expand_once(role, names, states)
        self._tree_ids = {name: slug(f"{role}_{name}_tree") for name in names if name}
        referenced = {n.attrs.get("ID") for t, _ in trees for n in _walk(t) if n.tag == "SubTree"}
        for name, (tree, key) in names.items():
            if not name:
                self.fail(key, tree.line, "BehaviorTree 缺少 ID")
                continue
            if name in incomplete:
                if name in states:
                    self._failed_trees.add((role, name))
                continue
            if len(tree.children) != 1:
                self.fail(key, tree.line, f"BehaviorTree {name} 应恰好有一个根节点")
                continue
            tree_id = self._tree_ids[name]
            if name in states:
                self._tree_state[tree_id] = name
            nodes: list[FlatNode] = []
            root_id = self.node(tree.children[0], role, tree_id, key, nodes)
            if root_id is None:
                self._failed_trees.add((role, name))
                continue
            self.trees.append(FlatTree(id=tree_id, root=root_id, nodes=nodes))
            self.mark(f"tree:{tree_id}", key, tree.line)
            if name in states:
                self.state_trees[(role, name)] = tree_id
            elif name not in referenced:
                self.fail(
                    key,
                    tree.line,
                    f"树 '{name}' 既不是 {role} 的状态，也没有被引用为子树",
                    sorted(states),
                    warning=True,
                )

    # ------------------------------------------------------------------ generated tree parts
    def _expand_state_formations(
        self, role: str, names: dict[str, tuple[XmlNode, str]], states: set[str]
    ) -> None:
        """`formations: {state: formation}` on a leader_follower: the leader's state trees
        get the formation write and the announcement, so the sheet does not spell them."""
        if role not in self.state_formations:
            return
        cid, mapping = self.state_formations[role]
        for state, formation in mapping.items():
            if state not in states or state not in names:
                continue  # the missing tree is reported with the other trees
            tree, _key = names[state]
            if len(tree.children) != 1:
                continue
            root = tree.children[0]
            nodes = list(_walk(root))
            add = []
            if not any(
                n.tag == "SetBlackboard" and n.attrs.get("key", "").split(".")[-1] == FORMATION_VAR
                for n in nodes
            ):
                add.append(
                    XmlNode(
                        "SetBlackboard",
                        {"key": FORMATION_VAR, "value": formation},
                        root.line,
                    )
                )
            announce = f"{cid}.announce_formation"
            if not any(n.tag == "Coordination" and n.attrs.get("use") == announce for n in nodes):
                add.append(XmlNode("Coordination", {"use": announce}, root.line))
            if not add:
                continue
            if root.tag == "Sequence":
                root.children[:0] = add
            else:
                tree.children = [XmlNode("Sequence", {}, root.line, [*add, root])]

    def _expand_once(
        self, role: str, names: dict[str, tuple[XmlNode, str]], states: set[str]
    ) -> None:
        """`with once():` → done only once per entry into the state: a flag the block sets
        when it succeeds, checked before it, and cleared by every other state of the role."""
        flags: dict[str, list[str]] = {}
        for name, (tree, key) in names.items():
            count = 0
            for parent in list(_walk(tree)):
                for index, child in enumerate(parent.children):
                    if child.tag != "Once":
                        continue
                    if name not in states:
                        self.fail(
                            key,
                            child.line,
                            f"with once() 只能写在状态树里（{name} 是子树）：它表示每次进入状态只做一次",
                        )
                        parent.children[index] = XmlNode("Sequence", {}, child.line, child.children)
                        continue
                    if not child.children:
                        self.fail(key, child.line, "with once() 里没有语句")
                        continue
                    count += 1
                    flag = slug(f"once_{name}" + (f"_{count}" if count > 1 else ""))
                    flags.setdefault(name, []).append(flag)
                    self.declared.setdefault(role, {})[flag] = False
                    parent.children[index] = XmlNode(
                        "Fallback",
                        {"name": f"{role}_{flag}"},
                        child.line,
                        [
                            XmlNode("Condition", {"ID": f"bb.{flag}"}, child.line),
                            XmlNode(
                                "Sequence",
                                {},
                                child.line,
                                [
                                    *child.children,
                                    XmlNode(
                                        "SetBlackboard",
                                        {"key": flag, "value": "true"},
                                        child.line,
                                    ),
                                ],
                            ),
                        ],
                    )
        for state, state_flags in flags.items():
            for other in sorted(states - {state}):
                if other not in names:
                    continue
                tree, _key = names[other]
                if len(tree.children) != 1:
                    continue
                root = tree.children[0]
                resets = [
                    XmlNode("SetBlackboard", {"key": flag, "value": "false"}, root.line)
                    for flag in state_flags
                ]
                if root.tag == "Sequence":
                    root.children[:0] = resets
                else:
                    tree.children = [XmlNode("Sequence", {}, root.line, [*resets, root])]

    def _node_id(self, node: XmlNode, tree_id: str) -> str:
        wanted = node.attrs.get("name")
        base = slug(wanted) if wanted else f"{tree_id}_{len(self.node_ids) + 1}"
        nid, n = base, 2
        while nid in self.node_ids:
            nid, n = f"{base}_{n}", n + 1
        self.node_ids.add(nid)
        return nid

    def node(
        self, node: XmlNode, role: str, tree_id: str, block: str, out: list[FlatNode]
    ) -> str | None:
        tag = node.tag
        nid = self._node_id(node, tree_id)
        self.mark(f"node:{nid}", block, node.line)
        if tag in CONTROL or tag in DECORATORS:
            children = [self.node(c, role, tree_id, block, out) for c in node.children]
            if not node.children:
                self.fail(block, node.line, f"<{tag}> 没有子节点")
                return None
            if not all(children):
                return None  # the failing children are reported already
            item: dict[str, Any] = {"node_id": nid, "children": children}
            if tag in CONTROL:
                item["type"] = CONTROL[tag]
                if tag == "Parallel":
                    n = len(children)
                    ok = int(node.attrs.get("success_count", n))
                    bad = int(node.attrs.get("failure_count", 1))
                    item["success_policy"] = {"type": "threshold", "count": ok}
                    item["failure_policy"] = {"type": "threshold", "count": bad}
            else:
                if len(children) != 1:
                    self.fail(block, node.line, f"装饰节点 <{tag}> 只能有一个子节点")
                    return None
                item["type"] = "decorator"
                item["decorator"] = DECORATORS[tag]
                if tag == "Repeat":
                    item["count"] = int(node.attrs.get("num_cycles", 1))
                if tag == "RetryUntilSuccessful":
                    item["count"] = int(node.attrs.get("num_attempts", 3))
            out.append(FlatNode(**item))
            return nid
        if node.children:
            self.fail(block, node.line, f"<{tag}> 是叶子节点，不能有子节点")
            return None
        if tag == "Condition" or tag == "CheckCondition":
            attrs = node.attrs
            text = (
                attrs.get("ID") or attrs.get("condition") or attrs.get("expr") or attrs.get("name")
            )
            if not text:
                self.fail(block, node.line, '<Condition> 需要 ID="条件名"')
                return None
            cid = self.condition_for(text, role, f"{tree_id}_check", block, node.line)
            if cid is None:
                return None
            state = self._tree_state.get(tree_id)
            leaving = {
                (t["when"].get("condition_ref") if isinstance(t["when"], dict) else None)
                for t in self.transitions
                if t["role"] == role and t["state"] == state and t["when"] != {"literal": True}
            }
            if state and cid in leaving:
                self.fail(
                    block,
                    node.line,
                    f"条件 {cid} 是状态 {state} 的转换条件，已写在状态图中；行为树只描述在该状态内持续做什么，"
                    "不要在树里检查转换条件（否则在条件成立前树什么也不做）",
                )
                return None
            out.append(FlatNode(node_id=nid, type="condition", condition=cid))
            return nid
        if tag == "Coordination":
            use = node.attrs.get("use", "")
            coord, _, suffix = use.partition(".")
            generated = self.symbols().generated(coord, "behaviors")
            if coord not in self.symbols().coordination or suffix not in generated:
                self.fail(
                    block,
                    node.line,
                    f'<Coordination use="{use}"> 不是协同生成的行为',
                    [
                        f"{c}.{b}"
                        for c in self.coordination
                        for b in self.symbols().generated(c, "behaviors")
                    ],
                )
                return None
            out.append(FlatNode(node_id=nid, type="behavior", behavior=f"{coord}__{suffix}"))
            self.mark(f"use:{coord}__{suffix}:{role}", block, node.line)
            return nid
        if tag == "SubTree":
            ref = node.attrs.get("ID", "")
            coord, _, suffix = ref.partition(".")
            if coord in self.symbols().coordination and suffix:
                if suffix not in self.symbols().generated(coord, "trees"):
                    self.fail(
                        block,
                        node.line,
                        f"协同 {coord} 没有生成树 '{suffix}'",
                        self.symbols().generated(coord, "trees"),
                    )
                    return None
                out.append(FlatNode(node_id=nid, type="subtree", tree=f"{coord}__{suffix}"))
                self.mark(f"use:{coord}__{suffix}:{role}", block, node.line)
                return nid
            target = self._tree_ids.get(ref)
            if target is None:
                self.fail(
                    block,
                    node.line,
                    f'<SubTree ID="{ref}"> 引用的树未在本角色 XML 中定义',
                    sorted(self._tree_ids),
                )
                return None
            out.append(FlatNode(node_id=nid, type="subtree", tree=target))
            return nid
        behavior = self.action_leaf(node, role, block)
        if behavior is None:
            return None
        out.append(FlatNode(node_id=nid, type="behavior", behavior=behavior))
        return nid

    def condition_for(self, text: str, role: str, anon: str, block: str, line: int) -> str | None:
        compiler = ExprCompiler(self.symbols(), block, line)
        expression = compiler.parse(text)
        self.problems += compiler.problems
        if expression is None:
            return None
        self._note_conditions(role, expression)
        if set(expression) == {"condition_ref"}:
            if "__" in expression["condition_ref"]:
                self.mark(f"use:{expression['condition_ref']}:{role}", block, line)
            return expression["condition_ref"]
        cid, n = slug(anon), 2
        while cid in self.conditions:
            cid, n = f"{slug(anon)}_{n}", n + 1
        self.conditions[cid] = {
            "expression": expression,
            "description": text,
            "line": line,
            "block": block,
        }
        self.condition_reads[cid] = compiler.blackboard_reads
        self.role_conditions.setdefault(role, set()).add(cid)
        self.mark(f"condition:{cid}", block, line)
        return cid

    def action_leaf(self, node: XmlNode, role: str, block: str) -> str | None:
        tag = node.tag
        command = (
            ACTION_ALIASES.get(tag)
            or (node.attrs.get("ID") if tag == "Action" else None)
            or camel_to_snake(tag)
        )
        definition = self.catalog.action(command)
        if definition is None:
            names = (
                [snake_to_camel(a) for a in self.catalog.actions] + list(CONTROL) + list(DECORATORS)
            )
            names += ["Condition", "Coordination", "SubTree"]
            self.fail(
                block,
                node.line,
                f"未知的节点 <{tag}>：{OUTSIDE_VOCABULARY}",
                near(tag, names),
            )
            return None
        refused = self.unavailable(definition.name, role)
        if refused:
            self.fail(
                block,
                node.line,
                f"<{tag}>：{refused}",
                self.available_actions(role, definition.category),
            )
            return None
        attrs = dict(node.attrs)
        if command == "wait" and "msec" in attrs:
            try:
                attrs["duration"] = f"{float(attrs.pop('msec')) / 1000:g} s"
            except ValueError:
                pass
        if command == "set_blackboard" and "output_key" in attrs:
            attrs["key"] = attrs.pop("output_key")
        arguments: dict[str, Any] = {}
        for port, raw in attrs.items():
            if port in RESERVED_PORTS:
                continue
            spec = definition.arguments.get(port)
            if spec is None:
                self.fail(
                    block, node.line, f"<{tag}> 没有端口 '{port}'", list(definition.arguments)
                )
                return None
            value = self.port_value(spec.type, str(raw), role, block, node.line)
            if value is None:
                return None
            arguments[port] = value
        missing = [n for n, s in definition.arguments.items() if s.required and n not in arguments]
        if missing:
            self.fail(block, node.line, f"<{tag}> 缺少必填端口 {missing}")
            return None
        if command == "set_blackboard":
            key = arguments["key"]
            var = key.split(".")[-1]
            literal = (
                arguments["value"].get("literal") if isinstance(arguments["value"], dict) else None
            )
            self.writes.setdefault(role, []).append((var, literal, ""))
        behavior: dict[str, Any] = {
            "description": node.attrs.get("description") or "",
            "actions": [{"command": command, "arguments": arguments}],
            "claims": list(definition.resources),
        }
        if str(node.attrs.get("continuous", "")).lower() == "true":
            behavior["continuous"] = True
        if command == "keep_station":
            behavior["actions"][0]["issue_policy"] = "every_tick"
            follows = [
                cid
                for cid, spec in self.coordination.items()
                if spec.get("pattern") == "leader_follower"
                and role in self._sync_members(cid, spec)
            ]
            if arguments.get("leader") == role:
                self.fail(
                    block,
                    node.line,
                    "<KeepStation> 的 leader 不能是本角色自己；leader 自己用机动动作（如 <FollowRoute>）行进",
                )
                return None
            if follows:
                self.fail(
                    block,
                    node.line,
                    f'{role} 是协同 {follows[0]} 的跟随者：用 <Coordination use="{follows[0]}.keep_station"/> 保持站位，不要再写 <KeepStation>',
                )
                return None
        elif definition.external:
            behavior["actions"][0]["issue_policy"] = "once_per_activation"
        if "mover" in definition.requires:
            behavior["precondition"] = MOVER_OK
            behavior["failure"] = {"expression": {"not": MOVER_OK}}
        success = DEFAULT_SUCCESS.get(command, {"literal": True})
        if success is not None:
            behavior["success"] = {"expression": success}
        for port, key in (
            ("_successIf", "success"),
            ("_failureIf", "failure"),
            ("_while", "precondition"),
        ):
            if port in node.attrs:
                compiler = ExprCompiler(self.symbols(), block, node.line)
                expression = compiler.parse(node.attrs[port])
                self.problems += compiler.problems
                if expression is None:
                    return None
                for element in vocabulary_elements(expression, self.catalog):
                    refused = self.unavailable(element, role)
                    if refused:
                        self.fail(block, node.line, f"{port}：{refused}")
                        return None
                self._note_conditions(role, expression)
                behavior[key] = expression if key == "precondition" else {"expression": expression}
        if "timeout" in node.attrs:
            q = quantity(node.attrs["timeout"])
            if q is None:
                self.fail(
                    block, node.line, f'timeout 应写成 "30 s"，得到 {node.attrs["timeout"]!r}'
                )
                return None
            behavior["timeout"] = {**q, "result": "failure"}
        signature = json.dumps(behavior | {"description": ""}, sort_keys=True, ensure_ascii=False)
        if signature in self._behavior_keys:
            return self._behavior_keys[signature]
        bid = self._behavior_id(command, arguments)
        behavior["id"] = bid
        if not behavior["description"]:
            behavior["description"] = (
                f"{command}（{', '.join(f'{k}={_short(v)}' for k, v in arguments.items())}）"
            )
        self.behaviors[bid] = behavior
        self._behavior_keys[signature] = bid
        self.mark(f"behavior:{bid}", block, node.line)
        return bid

    def _behavior_id(self, command: str, arguments: dict[str, Any]) -> str:
        parts = [command]
        if command == "set_blackboard":
            parts = ["set", str(arguments["key"]).split(".")[-1], _short(arguments["value"])]
        else:
            for value in arguments.values():
                text = _short(value)
                if text and len(parts) < 3:
                    parts.append(text)
        base = slug("_".join(parts), "behavior")
        bid, n = base, 2
        while bid in self.behaviors:
            bid, n = f"{base}_{n}", n + 1
        return bid

    def port_value(self, kind: str, raw: str, role: str, block: str, line: int) -> Any:
        params = set(self.parameters)
        blackboard = re.fullmatch(r"\{\s*([A-Za-z_][\w.]*)\s*\}", raw)
        if blackboard and kind in NUMERIC_ARGS:
            name = blackboard.group(1)
            if name in params:
                return {"parameter_ref": name}
            same = [n for n, p in self.parameters.items() if p.type == kind]
            self.fail(
                block,
                line,
                f"{kind} 端口不能读黑板变量 {{{name}}}（黑板不存物理量）：写 model.parameters 中的参数名",
                same or sorted(params),
            )
            return None
        if blackboard:
            name = blackboard.group(1)
            if "." in name and name.split(".", 1)[0] in self.roles:
                owner, var = name.split(".", 1)
                return {"blackboard_value": f"{owner}.mission.{var.replace('mission.', '')}"}
            return {"blackboard_value": f"mission.{name.replace('mission.', '')}"}
        if kind in NUMERIC_ARGS:
            if raw in params:
                ptype = self.parameters[raw].type
                if ptype != kind and ptype in NUMERIC_ARGS:
                    same = sorted(n for n, p in self.parameters.items() if p.type == kind)
                    hint = (
                        f"改用 {kind} 类参数"
                        if same
                        else f"model.parameters 中没有 {kind} 类参数：直接写数值（如 {EXAMPLE_QUANTITY[kind]}），"
                        "或在 model 块中增加参数"
                    )
                    self.fail(
                        block,
                        line,
                        f"{kind} 端口不能用 {raw}（它是 {ptype}）：{hint}",
                        same,
                    )
                    return None
                return {"parameter_ref": raw}
            q = quantity(raw)
            if q is not None:
                return q
            if "." in raw:
                self.fail(
                    block,
                    line,
                    f'端口不能读取运行时属性 {raw}：写参数名或 "数 单位"',
                    sorted(n for n, p in self.parameters.items() if p.type == kind),
                )
                return None
            self.fail(block, line, f"'{raw}' 不是参数名，也不是 \"数 单位\"", near(raw, params))
            return None
        if kind == "blackboard_key":
            return raw if raw.startswith("mission.") else f"mission.{raw}"
        if kind == "value":
            if raw in params:
                return {"parameter_ref": raw}
            if raw.lower() in ("true", "false"):
                return {"literal": raw.lower() == "true"}
            try:
                number = float(raw)
                return {"literal": int(number) if number.is_integer() else number}
            except ValueError:
                return {"literal": raw}
        if kind == "message":
            message = raw.replace(".", "__")
            if "__" not in message:
                self.custom_messages.setdefault(message, set()).add(role)
            return message
        if kind == "fields":
            self.fail(block, line, "send_message 的 fields 端口暂不支持，请用协同模式生成的消息")
            return None
        if kind == "formation" and raw not in {f["id"] for f in self.formations}:
            self.fail(
                block, line, f"未定义的编队 '{raw}'", near(raw, {f["id"] for f in self.formations})
            )
            return None
        if kind == "role" and raw not in self.roles:
            self.fail(block, line, f"未定义的角色 '{raw}'", near(raw, self.roles))
            return None
        if kind in ID_ARGS:
            return raw
        return {"literal": raw}  # string-typed arguments are operands

    # ------------------------------------------------------------------ basis
    def basis_block(self, block: Block) -> None:
        data = _load_yaml(block, self.problems)
        lines = yaml_lines(block.text, block.line)
        index = {
            (t["role"], t["state"], t["to"]): t
            for t in self.transitions
            if t["when"] != {"literal": True}
        }
        entries: list[tuple[str, str, Any, int | None, bool]] = []
        for role, items in data.items():
            if str(role) == "*" and isinstance(items, dict):
                # `*: {"* -> x": …}` (or any arrow): the entry holds for every role
                for r in self.roles:
                    for key, value in items.items():
                        entries.append((r, str(key), value, lines.get((role, key)), True))
                continue
            if isinstance(items, dict) and role in self.roles:
                for key, value in items.items():
                    entries.append((role, str(key), value, lines.get((role, key)), False))
            elif ":" in str(role):
                r, _, rest = str(role).partition(":")
                entries.append((r.strip(), rest.strip(), items, lines.get((role,)), False))
            else:
                self.fail(
                    block.key,
                    lines.get((role,)),
                    f"basis 的键应是角色名（{role}）",
                    near(str(role), self.roles),
                )
        for role, arrow, value, line, any_role in entries:
            star = re.fullmatch(r"\s*\*\s*-+>\s*([A-Za-z_]\w*)\s*", arrow)
            if star:  # the basis of a `* --> x` exception covers each expanded transition
                for (r, _a, c), tr in index.items():
                    if r == role and c == star.group(1):
                        self.transition_basis.setdefault(
                            tr["id"], basis_from(value, value if isinstance(value, dict) else None)
                        )
                continue
            match = re.fullmatch(r"\s*([A-Za-z_]\w*)\s*-+>\s*([A-Za-z_]\w*)\s*", arrow)
            if not match:
                self.fail(
                    block.key, line, f"basis 的转换应写成 '起始状态 -> 目标状态'，得到 '{arrow}'"
                )
                continue
            if match.group(1) == match.group(2):
                continue  # a self-loop is refused in the state diagram already
            tr = index.get((role, match.group(1), match.group(2)))
            if tr is None and any_role:
                continue
            if tr is None:
                self.fail(
                    block.key,
                    line,
                    f"{role} 没有转换 {arrow}",
                    [f"{a} -> {c}" for r, a, c in index if r == role],
                )
                continue
            self.transition_basis[tr["id"]] = basis_from(
                value, value if isinstance(value, dict) else None
            )

    # ------------------------------------------------------------------ needs
    def needs_block(self, block: Block) -> None:
        """``yaml needs``: capabilities the tactic needs that the vocabulary lacks.

        Each entry becomes a capability gap (``capability_needs.yaml`` → the IR
        capability check) instead of an invented node; the trees use the stated
        substitute meanwhile."""
        data = _load_yaml(block, self.problems)
        lines = yaml_lines(block.text, block.line)
        if isinstance(data.get("needs"), dict) and len(data) == 1:
            data, prefix = data["needs"], ("needs",)
        else:
            prefix = ()
        known = {r for r in self.roles} | {
            f"{r}.{st}" for r, d in self.diagrams.items() for st in d.states
        }
        clause_ids = {c.id for c in self.clauses}
        for nid, spec in data.items():
            line = lines.get((*prefix, nid))
            if not is_id(str(nid)):
                self.fail(block.key, line, f"needs 的键 '{nid}' 应是英文蛇形 ID")
                continue
            if isinstance(spec, str):
                spec = {"need": spec}
            if not isinstance(spec, dict):
                self.fail(block.key, line, f"needs.{nid} 应写成 {{need, used_by, substitute}}")
                continue
            text = " ".join(str(spec.get("need") or spec.get("description") or "").split())
            if not text:
                self.fail(block.key, line, f"needs.{nid} 缺少 need（一句话写清需要的能力）")
                continue
            ref = spec.get("gap")
            crow = None
            if ref is not None:
                crow = self.known_gaps.get(str(ref))
                if crow is None:
                    self.fail(
                        block.key,
                        line,
                        f"needs.{nid} 的 gap '{ref}' 不是所选战术已识别的能力缺口"
                        + ("" if self.known_gaps else "（本次没有 Crow 缺口可引用，删去 gap）"),
                        sorted(self.known_gaps),
                    )
                    continue
            contract = str(crow.get("contract") or "") if crow else ""
            element = self.constraints.element_for(contract) if self.constraints else None
            if element is not None:
                self.fail(
                    block.key,
                    line,
                    f"needs.{nid} 对应的能力在词表中已有：{element}"
                    f"（{self.constraints.label(element)}）。直接在条件或动作中使用它，不要登记 needs；"  # type: ignore[union-attr]
                    "需确认的条目生成后会自动列为待确认缺口",
                )
                continue
            used = spec.get("used_by") or []
            used = [used] if isinstance(used, str) else [str(u) for u in used]
            bad = [u for u in used if u not in known]
            if bad or not used:
                self.fail(
                    block.key,
                    line,
                    f"needs.{nid} 的 used_by 应列出用到它的角色或 角色.状态"
                    + (f"，未定义：{bad}" if bad else ""),
                    near(bad[0], known) if bad else sorted(known)[:5],
                )
                continue
            clauses = spec.get("clauses") or []
            clauses = [clauses] if isinstance(clauses, str) else [str(c) for c in clauses]
            unknown = [c for c in clauses if c not in clause_ids]
            if unknown:
                self.fail(
                    block.key,
                    line,
                    f"needs.{nid} 的 clauses 中 {unknown} 不是条款编号",
                    near(unknown[0], clause_ids),
                )
                continue
            substitute = " ".join(str(spec.get("substitute") or "").split())
            if not substitute:
                self.fail(
                    block.key,
                    line,
                    f"needs.{nid} 没有写 substitute（能力到位前行为树里的替代写法）",
                    warning=True,
                )
            self.needs.append(
                {
                    "id": str(nid),
                    "need": text,
                    "gap_id": str(ref) if crow else gap_id(text),
                    "crow_gap": str(ref) if crow else None,
                    "contract": str(crow.get("contract") or text) if crow else f"need:{text}",
                    "display_name": str(crow.get("display_name") or text) if crow else text,
                    "gap_type": str(crow.get("gap_type") or "develop") if crow else "develop",
                    "used_by": used,
                    "roles": sorted({u.split(".")[0] for u in used}),
                    "substitute": substitute,
                    "clauses": clauses,
                    "line": line,
                }
            )
            self.mark(f"need:{nid}", block.key, line)

    # ------------------------------------------------------------------ verification
    def verification_block(self, block: Block) -> None:
        data = _load_yaml(block, self.problems)
        lines = yaml_lines(block.text, block.line)
        for index, raw in enumerate(data.get("tests") or []):
            line = lines.get(("tests", index))
            if isinstance(raw, dict) and raw.get("deferred"):
                self.defer_test(raw, block.key, line)
                continue
            try:
                test = self.test(raw, block.key, line)
            except (ValueError, TypeError, KeyError) as exc:
                self.fail(block.key, line, f"测试 {index + 1}：{exc}")
                continue
            if test is not None:
                self.tests.append(test)
                self.mark(f"test:{test.id}", block.key, line)
        for index, raw in enumerate(data.get("scenarios") or []):
            line = lines.get(("scenarios", index))
            try:
                scenario = self.scenario(raw, block.key, line)
            except (ValueError, TypeError, KeyError) as exc:
                self.fail(block.key, line, f"想定 {index + 1}：{exc}")
                continue
            if scenario is not None:
                self.scenarios.append(scenario)
                self.mark(f"scenario:{scenario.id}", block.key, line)

    def defer_test(self, raw: dict[str, Any], block: str, line: int | None) -> None:
        """A test set aside by a human decision: kept in the sheet, left out of the IR.

        The transition it names still counts as covered (pending), so coverage does not
        force the repair loop to write a replacement that pre-empts the decision."""
        tid = str(raw.get("id") or f"test_{len(self.tests) + len(self.deferred_tests) + 1}")
        transition = (
            (raw.get("expect") or {}).get("transition")
            if isinstance(raw.get("expect"), dict)
            else None
        )
        covers = None
        if transition and raw.get("role") in self.roles:
            try:
                covers = self._transition_id(str(raw["role"]), str(transition), raw.get("state"))
            except ValueError:
                covers = None
        if covers:
            self.deferred_cover.add(covers)
        reason = str(raw.get("deferred_reason") or "").strip()
        self.deferred_tests.append(
            {
                "id": tid,
                "role": raw.get("role"),
                "state": raw.get("state"),
                "transition": transition,
                "reason": reason,
                "line": line,
            }
        )
        self.mark(f"test:{tid}", block, line)
        self.fail(
            block,
            line,
            f"测试 {tid} 已按人工决定暂缓（deferred），不参加检查"
            + (f"：{reason}" if reason else "")
            + "；决定后删除 deferred 并按决定改写",
            warning=True,
        )

    def transition_checklist(self) -> list[dict[str, Any]]:
        """Every transition an L2 test must fire, with its condition and the inputs it reads."""
        out = []
        for tr in self.transitions:
            if tr["when"] == {"literal": True} or tr["role"] not in self.diagrams:
                continue
            arrow = f"{tr['state']} -> {tr['to']}"
            inputs = self._missing_inputs(tr["role"], tr["state"], arrow, {})
            out.append(
                {
                    "id": tr["id"],
                    "role": tr["role"],
                    "transition": arrow,
                    "when": _when_text(tr["when"]),
                    "inputs": inputs,
                }
            )
        return out

    def wildcard_tests(self) -> None:
        """A `* --> x` exception is one rule written once: a test of it from one state is
        repeated (same inputs) from every other state it was expanded to."""
        self.generated_tests: list[str] = []
        for role, diagram in self.diagrams.items():
            for w in diagram.wildcards:
                expanded = [
                    t
                    for t in self.transitions
                    if t["role"] == role
                    and t["to"] == w.target
                    and t["line"] == w.line
                    and t["state"] != w.target
                    and t["when"] != {"literal": True}
                ]
                ids = {t["id"] for t in expanded}
                seed = next(
                    (
                        t
                        for t in self.tests
                        if t.expect.fired_transition in ids and t.given.role == role
                    ),
                    None,
                )
                if seed is None:
                    continue
                for tr in expanded:
                    if tr["id"] in self.tested:
                        continue
                    data = seed.model_dump(mode="json", exclude_none=True)
                    data["id"] = slug(f"{seed.id}_from_{tr['state']}")
                    data["given"]["state"] = tr["state"]
                    data["expect"] = {
                        k: v
                        for k, v in data["expect"].items()
                        if k not in ("fired_transition", "blackboard", *MECHANICAL_FIELDS)
                    }
                    data["expect"]["fired_transition"] = tr["id"]
                    data["description"] = f"由 {seed.id} 按 * --> {w.target} 复制到 {tr['state']}"
                    self.tests.append(TestCase.model_validate(data))
                    self.tested.add(tr["id"])
                    self.generated_tests.append(data["id"])
                    self.source.setdefault(
                        f"test:{data['id']}", self.source.get(f"test:{seed.id}", {})
                    )

    def test_coverage(self, block: Block) -> None:
        """Static L2 coverage: each transition needs a test whose expect.transition names it.

        Checked here, not only after the L2 run, so a draft that stops at L1 still shows
        which transitions are untested."""
        missing = [
            t
            for t in self.transition_checklist()
            if t["id"] not in self.tested and t["id"] not in self.deferred_cover
        ]
        self.untested = [t["id"] for t in missing]
        by_role: dict[str, list[dict[str, Any]]] = {}
        for item in missing:
            by_role.setdefault(item["role"], []).append(item)
        for role, items in by_role.items():
            listed = "；".join(
                f"{t['transition']}（条件 {t['when']}"
                + (f"，需设定 {', '.join(t['inputs'])}" if t["inputs"] else "")
                + "）"
                for t in items
            )
            self.fail(
                block.key,
                None,
                f"{role} 有 {len(items)} 条转换没有单步测试（每条转换至少一个测试，"
                f"expect.transition 写这条转换）：{listed}",
            )

    def _quantities(self, node: Any) -> Any:
        if isinstance(node, dict):
            return {k: self._quantities(v) for k, v in node.items()}
        if isinstance(node, list):
            return [self._quantities(v) for v in node]
        q = quantity(node)
        return q if q is not None else node

    def _missing_inputs(
        self, role: str, state: Any, transition: Any, given: dict[str, Any]
    ) -> list[str]:
        """Inputs the expected transition's condition reads that the test does not set
        (they take their defaults, which usually keeps the condition false)."""
        if not transition or not state:
            return []
        match = re.fullmatch(r"\s*([A-Za-z_]\w*)\s*-+>\s*([A-Za-z_]\w*)\s*", str(transition))
        if not match:
            return []
        source, target = match.groups()
        whens = [
            t["when"]
            for t in self.transitions
            if t["role"] == role and t["state"] == source and t["to"] == target
        ]
        reads: list[str] = []
        seen: set[str] = set()
        stack = list(whens)
        while stack:
            expr = stack.pop()
            reads += vocabulary_elements(expr, self.catalog)
            for ref in _refs(expr):
                if ref not in seen:
                    seen.add(ref)
                    if ref in self.conditions:
                        stack.append(self.conditions[ref]["expression"])
        missing = []
        got = {str(m.get("message")) for m in given.get("messages") or [] if isinstance(m, dict)}
        for ref in sorted(seen):
            coord, sep, name = ref.partition("__")
            if (
                sep
                and name in GENERATED_MESSAGE
                and f"{coord}__{GENERATED_MESSAGE[name]}" not in got
            ):
                missing.append(f"given.messages: [{{{coord}.{GENERATED_MESSAGE[name]}: {{}}}}]")
        reads = [x for r in reads for x in FUNCTION_INPUTS.get(r, (r,))]
        for element in dict.fromkeys(reads):
            path = given_path(element, self.roles.get(role, {}))
            if path is None:
                continue
            section, key = path
            values = given.get(section) or {}
            if section == "components":
                present = any(str(k).startswith(key) for k in values)
            else:
                present = any(str(k).split(".")[0] == key for k in values)
            if not present:
                missing.append(f"given.{section}.{key}")
        return missing

    def _command(self, name: str) -> str:
        """An action named in a test or scenario: node name (FollowRoute), action name, or a
        coordination behavior (coord.send_report → the action it issues)."""
        head, dot, tail = name.partition(".")
        if dot and head in self.coordination and tail in GENERATED_COMMANDS:
            return GENERATED_COMMANDS[tail]
        if name and name[0].isupper():
            return ACTION_ALIASES.get(name) or camel_to_snake(name)
        return name

    def _transition_id(self, role: str, text: str, state: str | None) -> str:
        match = re.fullmatch(r"\s*([A-Za-z_]\w*)\s*-+>\s*([A-Za-z_]\w*)\s*", text)
        if not match:
            return text
        a, b = match.groups()
        for tr in self.transitions:
            if tr["role"] == role and tr["state"] == a and tr["to"] == b:
                return tr["id"]
        for cid, spec in self.coordination.items():
            if role in self._sync_members(cid, spec):
                return f"{cid}__{a}__to__{b}"
        raise ValueError(f"{role} 没有转换 {text}")

    def test(self, raw: dict[str, Any], block: str, line: int | None) -> TestCase | None:
        role = raw.get("role")
        if role not in self.roles:
            self.fail(block, line, f"测试的角色 '{role}' 未定义", near(str(role), self.roles))
            return None
        given = {k: self._quantities(v) for k, v in (raw.get("given") or {}).items()}
        for section in ("terrain", "tracks", "platform", "formation", "team", "simulation"):
            # distance_to_class: {urban: 150 m} → distance_to_class.urban: 150 m
            values = given.get(section)
            if isinstance(values, dict):
                given[section] = _flatten_inputs(values)
        given.update(
            {"role": role, "instance": int(raw.get("instance", 0)), "state": raw.get("state")}
        )
        if "blackboard" in given and isinstance(given["blackboard"], dict):
            # "contact_report.report_received: …" is a coordination condition, not a variable:
            # true → the message that makes it true arrives this tick; false → nothing arrives
            for key in list(given["blackboard"]):
                coord, dot, name = str(key).partition(".")
                if dot and coord in self.coordination and name in GENERATED_MESSAGE:
                    if given["blackboard"].pop(key):
                        given.setdefault("messages", []).append(
                            {f"{coord}.{GENERATED_MESSAGE[name]}": {}}
                        )
                    self.fail(
                        block,
                        line,
                        f"测试 {raw.get('id')}：{key} 是协同 {coord} 生成的条件，不是黑板变量；"
                        f"已按 given.messages 处理（为真写 [{{{coord}.{GENERATED_MESSAGE[name]}: {{}}}}]，"
                        "为假不写）",
                        warning=True,
                    )
            # "team.formation_error_rms: 5 m" is a world input the condition reads (team.X →
            # given.team.X), not a blackboard variable: move it to its section
            written_vars = {v for items in self.writes.values() for v, _v, _t in items}
            components = set(self._component_ids(role))
            for key in list(given.get("blackboard") or {}):
                head, dot, prop = str(key).partition(".")
                if not dot or key in written_vars:
                    continue
                section = INPUT_SECTIONS.get(head)
                target_key = prop
                if section is None and (head == "mover" or head in components):
                    section, target_key = "components", str(key)
                if section is None:
                    continue
                value = given["blackboard"].pop(key)
                given.setdefault(section, {})
                if isinstance(given[section], dict):
                    given[section][target_key] = value
                self.fail(
                    block,
                    line,
                    f"测试 {raw.get('id')}：{key} 是条件读取的输入，不是黑板变量；"
                    f"已移到 given.{section}.{target_key}（写法：given: {{{section}: {{{target_key}: …}}}}）",
                    warning=True,
                )
            if not given["blackboard"]:
                del given["blackboard"]
        if "blackboard" in given:
            written = {var for items in self.writes.values() for var, _v, _t in items}
            wrong = [k for k in given["blackboard"] if k in self.conditions and k not in written]
            if wrong:
                self.fail(
                    block,
                    line,
                    f"测试 {raw.get('id')} 的 given.blackboard 中 {wrong} 是条件名，不是黑板变量："
                    "条件由输入算出，用 given 的 tracks / terrain / messages / components 设定让它成立的输入",
                )
                return None
            # a variable this role's blackboard does not have cannot change the tick
            own = {v.name for v in self.blackboards().get(role, [])}
            for key in list(given["blackboard"]):
                if str(key).split(".")[-1] not in own:
                    del given["blackboard"][key]
                    self.fail(
                        block,
                        line,
                        f"测试 {raw.get('id')}：{role} 的黑板没有变量 {key}"
                        "（战术稿中没有写入或读取它），已忽略",
                        warning=True,
                    )
            given["blackboard"] = {
                (k if "." in k else f"mission.{k}"): v for k, v in given["blackboard"].items()
            }
        messages = []
        for m in given.get("messages") or []:
            if isinstance(m, dict) and "message" in m:
                messages.append({**m, "message": str(m["message"]).replace(".", "__")})
            elif isinstance(m, dict) and len(m) == 1:
                name, fields = next(iter(m.items()))
                messages.append({"message": str(name).replace(".", "__"), "fields": fields or {}})
        coord_given = given.get("coordination")
        if isinstance(coord_given, dict):
            for cid, values in list(coord_given.items()):
                if not isinstance(values, dict):
                    continue
                for name in [n for n in values if n in GENERATED_MESSAGE]:
                    # "ack_received: true" means the ack message arrived this tick
                    if values.pop(name):
                        messages.append(
                            {"message": f"{cid}__{GENERATED_MESSAGE[name]}", "fields": {}}
                        )
                if not values:
                    del coord_given[cid]
        for m in messages:
            # a synchronized transition carries only the target state (to_state)
            if str(m["message"]).endswith("__transition") and isinstance(m.get("fields"), dict):
                f = m["fields"]
                target = f.get("to_state") or f.get("to") or f.get("state") or f.get("target")
                m["fields"] = {"to_state": target} if target else {}
        kept = []
        for m in messages:
            coord, sep, name = str(m["message"]).partition("__")
            receivers = self._message_receivers(coord, name) if sep else None
            if receivers is not None and role not in receivers:
                self.fail(
                    block,
                    line,
                    f"测试 {raw.get('id')}：{role} 收不到 {coord}.{name}（只发给 {', '.join(receivers)}），"
                    f"已从 given.messages 去掉；{role} 自己的转换由它的条件触发，不需要这条消息",
                    warning=True,
                )
                continue
            kept.append(m)
        messages = kept
        if messages:
            given["messages"] = messages
        else:
            given.pop("messages", None)
        if "elapsed" in given and not isinstance(given["elapsed"], dict):
            given["elapsed"] = {"state": self._quantities(given["elapsed"])}
        exp = dict(raw.get("expect") or {})
        expect: dict[str, Any] = {}
        if "next_state" in exp:
            expect["next_state"] = exp["next_state"]
        transition = exp.get("transition") or exp.get("fired_transition")
        if transition:
            expect["fired_transition"] = self._transition_id(
                role, str(transition), raw.get("state")
            )
            self.tested.add(expect["fired_transition"])
        tid = slug(str(raw.get("id") or f"test_{len(self.tests) + 1}"))
        formation = self.state_formations.get(role, (None, {}))[1].get(
            str(expect.get("next_state"))
        )
        bb_expect = dict(exp.get("blackboard") or {})
        if formation and not any(
            str(k).split(".")[-1] == FORMATION_VAR for k in bb_expect
        ):  # the declared formation of the state the test ends in
            exp["blackboard"] = {**bb_expect, FORMATION_VAR: formation}
        written: dict[str, Any] = {}
        actions = exp.get("actions") or exp.get("issued_actions")
        if actions:
            written["issued_actions"] = [self._command(str(a)) for a in actions]
        sent = exp.get("messages") or exp.get("sent_messages")
        if sent:
            written["sent_messages"] = [str(m).replace(".", "__") for m in sent]
        status = exp.get("status") or exp.get("tree_status")
        if status:
            written["tree_status"] = status
        path = exp.get("path") or exp.get("selected_path")
        if path:
            written["selected_path"] = [slug(p) for p in path]
        if written:
            self.written_mechanics[tid] = written
        for key, value in (self.observed.get(tid) or {}).items():
            if key in MECHANICAL_FIELDS and value is not None:
                expect[key] = value
        self.test_inputs[tid] = self._missing_inputs(role, raw.get("state"), transition, given)
        self.test_given[tid] = [
            f"given.{section}.{key}"
            for section, values in (raw.get("given") or {}).items()
            if isinstance(values, dict) and section not in ("coordination",)
            for key in values
        ]
        if exp.get("blackboard"):
            expect["blackboard"] = {
                (k if "." in k else f"mission.{k}"): v for k, v in exp["blackboard"].items()
            }
        item = {"id": tid, "given": given, "expect": expect}
        if raw.get("category"):
            item["category"] = raw["category"]
        if raw.get("description"):
            item["description"] = str(raw["description"])
        try:
            return TestCase.model_validate(item)
        except ValidationError as exc:
            err = exc.errors()[0]
            self.fail(
                block, line, f"测试 {tid}：{'.'.join(str(p) for p in err['loc'])} {err['msg']}"
            )
            return None

    def scenario(self, raw: dict[str, Any], block: str, line: int | None) -> Scenario | None:
        sid = slug(str(raw.get("id") or f"scenario_{len(self.scenarios) + 1}"))
        routes = raw.get("routes") or ({"main_route": raw["route"]} if raw.get("route") else {})
        if not routes:
            self.fail(block, line, f"想定 {sid} 需要 route 或 routes（[[x, y], …]，米）")
            return None
        env: dict[str, Any] = {
            "routes": routes,
            "duration": quantity(raw.get("duration", "5 min")),
            "random_seed": int(raw.get("seed", raw.get("random_seed", 1))),
        }
        terrain = raw.get("terrain")
        if isinstance(terrain, dict):
            regions = []
            for region in terrain.get("regions") or []:
                if "x" in region and "y" in region:
                    regions.append(
                        {
                            "class": region["class"],
                            "x_min": region["x"][0],
                            "x_max": region["x"][1],
                            "y_min": region["y"][0],
                            "y_max": region["y"][1],
                        }
                    )
                else:
                    regions.append(region)
            env["terrain"] = {
                "default_class": terrain.get("default", terrain.get("default_class", "open")),
                "regions": regions,
            }
        initial = dict(raw.get("initial") or {})
        route = next(iter(routes.values()))
        heading = 90.0
        if len(route) >= 2:
            dx, dy = route[1][0] - route[0][0], route[1][1] - route[0][1]
            heading = round(math.degrees(math.atan2(dx, dy)) % 360, 1)
        followers = {
            m: cid
            for cid, spec in self.coordination.items()
            if spec.get("pattern") == "leader_follower"
            for m in self._sync_members(cid, spec)
        }
        for role in self.roles:
            entry = dict(initial.get(role) or {})
            diagram = self.diagrams.get(role)
            entry.setdefault("state", diagram.initial if diagram else None)
            if role in followers:
                entry.setdefault("formation", self._initial_formation(role))
            else:
                entry.setdefault("position", list(route[0]))
                entry.setdefault("heading_deg", heading)
            initial[role] = entry
        events = []
        for index, e in enumerate(raw.get("events") or []):
            item = {k: self._quantities(v) if k in ("at", "duration") else v for k, v in e.items()}
            item["id"] = slug(str(item.get("id") or f"event_{index + 1}"), "event")
            if item.get("message"):
                item["message"] = str(item["message"]).replace(".", "__")
            events.append(item)
        expect = []
        params = set(self.parameters)
        for index, e in enumerate(raw.get("expect") or []):
            item = dict(e)
            item["id"] = slug(str(item.get("id") or f"e_{index + 1}"), "e")
            for comp in ("equals", "lte", "gte", "lt", "gt"):
                if (
                    comp in item
                    and isinstance(item[comp], str)
                    and item["observable"] != "role_state"
                ):
                    item[comp] = (
                        operand_value(item[comp], params)
                        if (item[comp] in params or quantity(item[comp]))
                        else item[comp]
                    )
            if item.get("message"):
                item["message"] = str(item["message"]).replace(".", "__")
            if (
                item.get("observable") in ("min_interval", "max_interval")
                and not item.get("role")
                and item.get("formation")
            ):
                # spacing inside a formation: measured on the roles that hold its slots
                slots = next(
                    (f.get("slots") or [] for f in self.formations if f["id"] == item["formation"]),
                    [],
                )
                members = [str(x.get("role_instance", "")).split("[")[0] for x in slots]
                members = [m for m in members if m in self.roles]
                if members:
                    item["role"] = members[0]
            if item.get("command"):
                item["command"] = self._command(str(item["command"]))
            needed = {"messages_sent": "message", "actions_issued": "command"}.get(
                str(item.get("observable"))
            )
            if needed and not item.get(needed):
                options = (
                    [
                        f"{c}.{m}"
                        for c in self.coordination
                        for m in self.symbols().generated(c, "messages")
                    ]
                    if needed == "message"
                    else sorted(
                        {
                            snake_to_camel(b["actions"][0]["command"])
                            for b in self.behaviors.values()
                        }
                    )
                )
                self.fail(
                    block,
                    line,
                    f"想定 {sid} 的期望 {item['id']}：观测量 {item['observable']} 必须写 {needed}"
                    f"（统计哪一种{'消息' if needed == 'message' else '动作'}）",
                    options,
                )
                continue
            within = item.get("within")
            if isinstance(within, dict) and "time" in within:
                q = quantity(within["time"]) or {}
                after = within.get("after") or within.get("after_event")
                item["within"] = {
                    "after_event": slug(str(after), "event") if after else after,
                    **q,
                }
            during = item.get("during")
            if isinstance(during, dict) and "for" in during:
                item["during"] = {
                    "from": during.get("from", "start"),
                    "duration": quantity(during["for"]),
                }
            expect.append(item)
        traces = [self._trace_name(t) for t in raw.get("traces_to") or []]
        traces += self._inferred_traces(expect)
        item = {
            "id": sid,
            "description": str(raw.get("description") or ""),
            "environment": env,
            "initial": initial,
            "events": events,
            "expect": expect,
            "traces_to": list(dict.fromkeys(t for t in traces if t)),
        }
        try:
            return Scenario.model_validate(item)
        except ValidationError as exc:
            err = exc.errors()[0]
            self.fail(
                block, line, f"想定 {sid}：{'.'.join(str(p) for p in err['loc'])} {err['msg']}"
            )
            return None

    def _initial_formation(self, role: str) -> str | None:
        owner_var = self._formation_var
        if owner_var:
            owner, var = owner_var
            default = self._blackboard_default(owner, var)
            if isinstance(default, str) and default:
                return default
        for f in self.formations:
            if any(s["role_instance"].startswith(role) for s in f["slots"]):
                return f["id"]
        return None

    def _trace_name(self, text: Any) -> str | None:
        text = str(text)
        match = re.fullmatch(r"\s*([A-Za-z_]\w*)\s*:\s*(.+)", text)
        if match and match.group(1) in self.roles:
            try:
                return self._transition_id(match.group(1), match.group(2), None)
            except ValueError:
                return None
        return text

    def _inferred_traces(self, expect: list[dict[str, Any]]) -> list[str]:
        out: list[str] = []
        for e in expect:
            if e.get("formation"):
                out.append(e["formation"])
            message = e.get("message")
            if message and "__" in message:
                out.append(message.split("__", 1)[0])
            if (
                e.get("observable") == "role_state"
                and e.get("role")
                and isinstance(e.get("equals"), str)
            ):
                role, state = e["role"], e["equals"]
                for tr in self.transitions:
                    if (
                        tr["to"] == state
                        and tr["when"] != {"literal": True}
                        and (
                            tr["role"] == role
                            or (
                                tr["sync"]
                                and role
                                in self._sync_members(tr["sync"], self.coordination[tr["sync"]])
                            )
                        )
                    ):
                        out.append(tr["id"])
                        if tr["sync"]:
                            out.append(tr["sync"])
        return out

    # ------------------------------------------------------------------ coverage
    def _expand_star(self, element: Any) -> list[Any]:
        """`role: * -> x` in implements names every transition the `*` line expanded to."""
        match = re.fullmatch(r"\s*([A-Za-z_]\w*)\s*:\s*\*\s*-+>\s*([A-Za-z_]\w*)\s*", str(element))
        if not match:
            return [element]
        role, target = match.groups()
        found = [
            f"{role}: {t['state']} -> {target}"
            for t in self.transitions
            if t["role"] == role and t["to"] == target and t["when"] != {"literal": True}
        ]
        return found or [element]

    def _implements_element(self, element: str) -> str:
        """What a clause names as implementing it, normalized: a coordination's generated
        behavior, condition or tree (``bounding.bound``, ``not report.report_received``)
        stands for the coordination itself."""
        text = re.sub(r"^\s*not\s+", "", element.strip())
        head, dot, _rest = text.partition(".")
        if dot and head in self.coordination and head not in self.roles:
            return head
        return text

    def coverage(self) -> None:
        self.implements = {
            clause: list(
                dict.fromkeys(
                    self._implements_element(x) for e in elements for x in self._expand_star(e)
                )
            )
            for clause, elements in self.implements.items()
        }
        if True:
            known = set(self.roles) | {
                f"{r}.{s}" for r, d in self.diagrams.items() for s in d.states
            }
            known |= (
                {f["id"] for f in self.formations} | set(self.coordination) | set(self.parameters)
            )
            arrows = {}
            for tr in self.transitions:
                arrows[f"{tr['role']}: {tr['state']} -> {tr['to']}"] = tr["id"]
                known.add(tr["id"])
            for role, diagram in self.diagrams.items():
                for w in diagram.wildcards:
                    arrows[f"{role}: * -> {w.target}"] = f"{role}_any_to_{w.target}"
            clause_ids = {c.id for c in self.clauses}
            excused = {o.clause_id for o in self.out_of_scope if o.reason.strip()}
            excused |= {c for n in self.needs for c in n["clauses"]}
            line = self._implements_line
            for clause, elements in self.implements.items():
                if clause not in clause_ids:
                    self.fail(
                        "model",
                        line,
                        f"implements 中的条款 '{clause}' 不存在",
                        near(clause, clause_ids),
                        warning=True,
                    )
                    continue
                for element in elements:
                    normalized = re.sub(r"\s*-+>\s*", " -> ", re.sub(r"\s*:\s*", ": ", element))
                    if element not in known and normalized not in arrows:
                        self.fail(
                            "model",
                            line,
                            f"implements.{clause} 中的 '{element}' 不是已定义的元素",
                            near(element, known),
                        )
            for clause in self.clauses:
                if clause.kind == "out_of_scope" or clause.id in excused:
                    continue
                if clause.id not in self.implements:
                    self.fail(
                        "model",
                        line,
                        f"条款 {clause.id}（{clause.text[:40]}）没有列入 implements，也没有写入 out_of_scope",
                    )

    # ------------------------------------------------------------------ blackboard
    def _blackboard_default(self, role: str, var: str) -> Any:
        declared = self.declared.get(role, {})
        if declared.get(var) is not None:
            return declared[var]
        writes = [v for name, v, _ in self.writes.get(role, []) if name == var and v is not None]
        initial = self.diagrams[role].initial if role in self.diagrams else None
        tree = self.state_trees.get((role, initial)) if initial else None
        if tree:
            for name, value in self._tree_writes(tree):
                if name == var and value is not None:
                    return value
        return writes[0] if writes else None

    def _tree_writes(self, tree_id: str) -> list[tuple[str, Any]]:
        tree = next((t for t in self.trees if t.id == tree_id), None)
        out = []
        if tree is None:
            return out
        for node in tree.nodes:
            if node.type == "behavior" and node.behavior in self.behaviors:
                action = self.behaviors[node.behavior]["actions"][0]
                if action["command"] == "set_blackboard":
                    out.append(
                        (
                            str(action["arguments"]["key"]).split(".")[-1],
                            action["arguments"]["value"].get("literal"),
                        )
                    )
        return out

    def blackboards(self) -> dict[str, list[VariableDraft]]:
        need: dict[str, set[str]] = {r: set(self.declared.get(r, {})) for r in self.roles}
        for role, writes in self.writes.items():
            need.setdefault(role, set()).update(name for name, _, _ in writes)
        # reads through the conditions each role uses (transitively)
        for role, used in self.role_conditions.items():
            seen: set[str] = set()
            stack = list(used)
            while stack:
                cid = stack.pop()
                if cid in seen:
                    continue
                seen.add(cid)
                for read in self.condition_reads.get(cid, set()):
                    owner, _, var = read.partition(".")
                    need.setdefault(owner or role, set()).add(var)
                expression = self.conditions.get(cid, {}).get("expression")
                stack += _refs(expression)
        out: dict[str, list[VariableDraft]] = {}
        for role, names in need.items():
            if role not in self.roles:
                continue
            for var in sorted(names):
                default = self._blackboard_default(role, var)
                if default is None:
                    default = self._formation_default(role, var)
                kind = (
                    "bool"
                    if isinstance(default, bool)
                    else "int"
                    if isinstance(default, int)
                    else ("double" if isinstance(default, float) else "string")
                )
                if default is None:
                    kind, default = "bool", False
                out.setdefault(role, []).append(VariableDraft(name=var, type=kind, default=default))
        return out

    def _formation_default(self, role: str, var: str) -> Any:
        if self._formation_var == (role, var):
            return self.formations[0]["id"] if self.formations else ""
        return None

    # ------------------------------------------------------------------ result
    def result(self) -> CompileResult:
        boards = self.blackboards()
        by_element: dict[str, list[str]] = {}
        arrows = {f"{t['role']}: {t['state']} -> {t['to']}": t["id"] for t in self.transitions}
        for clause, elements in self.implements.items():
            for element in elements:
                normalized = re.sub(r"\s*-+>\s*", " -> ", re.sub(r"\s*:\s*", ": ", element))
                by_element.setdefault(arrows.get(normalized, element), []).append(clause)
        roles = []
        for name, spec in self.roles.items():
            diagram = self.diagrams.get(name)
            states = []
            if diagram is not None:
                for state in diagram.states:
                    states.append(
                        StateDraft(
                            id=state,
                            description=diagram.descriptions.get(state, ""),
                            tree=self.state_trees.get((name, state), f"{name}_{state}_tree"),
                            implements=by_element.get(f"{name}.{state}", []),
                            terminal=state in diagram.terminal,
                        )
                    )
            try:
                role_draft = RoleDraft(
                    id=name,
                    description=spec["description"],
                    count=spec["count"],
                    capability_class=spec["platform"],
                    superior=spec["superior"],
                    mover_capability=spec["mover"],
                    sensors=[ComponentDraft(**c) for c in spec["sensors"]],
                    comms=[ComponentDraft(**c) for c in spec["comms"]],
                    weapons=[ComponentDraft(**c) for c in spec["weapons"]],
                    blackboard_variables=boards.get(name, []),
                    initial_state=diagram.initial if diagram else None,
                    states=states,
                    implements=by_element.get(name, []),
                )
            except ValidationError as exc:
                err = exc.errors()[0]
                self.fail(
                    "model",
                    spec.get("line"),
                    f"角色 {name}：{'.'.join(str(p) for p in err['loc'])} {err['msg']}",
                )
                continue
            roles.append(role_draft)
        intents = list(self.catalog.vocabularies["intents"])
        skeleton = None
        if roles:
            skeleton = SkeletonDraft(
                display_name=str(
                    self.meta.get("display_name")
                    or self.request.display_name
                    or self.request.model_id
                ),
                description=str(self.meta.get("description") or ""),
                intent=self.meta.get("intent")
                if self.meta.get("intent") in intents
                else intents[0],
                domain_concepts=[
                    str(c)
                    for c in self.meta.get("concepts") or self.meta.get("domain_concepts") or []
                ],
                roles=roles,
                formations=[
                    ElementPlan(id=f["id"], implements=by_element.get(f["id"], []))
                    for f in self.formations
                ],
                coordination=[
                    CoordinationPlan(id=c, pattern=s["pattern"], implements=by_element.get(c, []))
                    for c, s in self.coordination.items()
                    if s.get("_item")
                ],
                out_of_scope=self.out_of_scope,
            )
        for name, param in self.parameters.items():
            param.implements = list(dict.fromkeys(param.implements + by_element.get(name, [])))
        sections: dict[str, BaseModel] = {
            "parameters": ParametersSection(
                parameters=list(self.parameters.values()), assumptions=self.assumptions
            ),
        }
        structure = StructureSection()
        for f in self.formations:
            try:
                structure.formations.append(Formation.model_validate(f))
            except ValidationError as exc:
                self.fail(
                    "model",
                    self.source.get(f"formation:{f['id']}", {}).get("line"),
                    f"编队 {f['id']}：{exc.errors()[0]['msg']}",
                )
        for cid, spec in self.coordination.items():
            item = spec.get("_item")
            if not item:
                continue
            try:
                structure.coordination.append(Coordination.model_validate(item))
            except ValidationError as exc:
                err = exc.errors()[0]
                self.fail(
                    "model",
                    spec.get("_line"),
                    f"协同 {cid}：{'.'.join(str(p) for p in err['loc'])} {err['msg']}",
                )
        declared = {m.get("id") for m in self.messages if isinstance(m, dict)}
        for message, senders in sorted(self.custom_messages.items()):
            if message in declared:
                continue
            receivers = sorted(set(self.roles) - senders) or sorted(senders)
            self.messages.append({"id": message, "from": sorted(senders), "to": receivers})
            self.fail(
                "model",
                None,
                f"自定义消息 {message} 未声明，按 {sorted(senders)} → {receivers}、无字段补充（简化处理）",
                warning=True,
            )
        for m in self.messages:
            try:
                structure.messages.append(Message.model_validate(m))
            except ValidationError as exc:
                self.fail(
                    "model",
                    None,
                    f"消息 {m.get('id') if isinstance(m, dict) else m}：{exc.errors()[0]['msg']}",
                )
        sections["structure"] = structure
        conditions = []
        for cid, c in self.conditions.items():
            try:
                conditions.append(
                    Condition(id=cid, description=c["description"], expression=c["expression"])
                )
            except ValidationError as exc:
                self.fail(c["block"], c["line"], f"条件 {cid}：{exc.errors()[0]['msg']}")
        sections["conditions"] = ConditionsSection(conditions=conditions)
        behaviors = []
        for bid, b in self.behaviors.items():
            try:
                behaviors.append(Behavior.model_validate(b))
            except ValidationError as exc:
                where = self.source.get(f"behavior:{bid}", {})
                self.fail(
                    where.get("block", "xml"),
                    where.get("line"),
                    f"行为 {bid}：{exc.errors()[0]['msg']}",
                )
        blackboard_vars = [
            BlackboardVariableDraft(role=r, name=v.name, type=v.type, default=v.default)
            for r, items in boards.items()
            for v in items
        ]
        sections["behaviors"] = BehaviorsSection(
            behaviors=behaviors, blackboard_variables=blackboard_vars
        )
        transitions = []
        for t in self.transitions:
            basis = self.transition_basis.get(t["id"])
            transitions.append(
                TransitionDraft(
                    role=t["role"],
                    state=t["state"],
                    transition_id=t["id"],
                    to=t["to"],
                    priority=t["priority"],
                    when=t["when"],
                    synchronize_with=t["sync"],
                    basis=BasisDraft(**basis)
                    if basis
                    else (None if t["when"] == {"literal": True} else BasisDraft()),
                    implements=by_element.get(t["id"], []),
                )
            )
        sections["control"] = ControlSection(trees=self.trees, transitions=transitions)
        sections["verification"] = VerificationSection(tests=self.tests, scenarios=self.scenarios)
        symbols = {
            "roles": {
                r: {
                    "count": s["count"],
                    "states": self.diagrams[r].states if r in self.diagrams else [],
                    **{
                        kind: [c["id"] for c in s[kind]]
                        for kind in ("sensors", "comms", "weapons")
                        if s[kind]
                    },
                }
                for r, s in self.roles.items()
            },
            "parameters": sorted(self.parameters),
            "conditions": sorted(self.conditions),
            "needed_conditions": sorted(
                {
                    t["when"].get("condition_ref")
                    or (t["when"].get("not") or {}).get("condition_ref")
                    for t in self.transitions
                    if isinstance(t["when"], dict)
                }
                - {None}
                - set(self.conditions)
            ),
            "coordination": {c: s.get("pattern") for c, s in self.coordination.items()},
            "transitions": [
                f"{t['role']}: {t['state']} -> {t['to']}"
                for t in self.transitions
                if t["when"] != {"literal": True}
            ],
            "trees": {f"{r}.{s}": tid for (r, s), tid in self.state_trees.items()},
            "nodes": sorted(self.node_ids),
            "formations": [f["id"] for f in self.formations],
            "failed_trees": sorted(slug(f"{r}_{st}_tree") for r, st in self._failed_trees),
            "generated": {
                c: {
                    kind: self.symbols().generated(c, kind)
                    for kind in ("messages", "conditions", "behaviors", "trees")
                    if self.symbols().generated(c, kind)
                }
                for c in self.symbols().coordination
            },
        }
        symbols["needs"] = [n["id"] for n in self.needs]
        symbols["test_inputs"] = {k: v for k, v in self.test_inputs.items() if v}
        symbols["untested"] = list(self.untested)
        symbols["deferred_tests"] = [dict(d) for d in self.deferred_tests]
        symbols["generated_tests"] = list(self.generated_tests)
        symbols["test_given"] = self.test_given
        symbols["transition_checklist"] = self.transition_checklist()
        symbols["written_mechanics"] = self.written_mechanics
        return CompileResult(
            skeleton,
            sections,
            self.problems,
            self.source,
            list(self.blocks.values()),
            symbols,
            needs=[{k: v for k, v in n.items() if k != "line"} for n in self.needs],
        )


def _when_text(expr: Any) -> str:
    """A transition guard in sheet words: a condition name, not x, or a short form."""
    if isinstance(expr, dict):
        if "condition_ref" in expr:
            return str(expr["condition_ref"]).replace("__", ".")
        if "not" in expr:
            return f"not {_when_text(expr['not'])}"
        for op in ("all", "any"):
            if op in expr and isinstance(expr[op], list):
                joiner = " and " if op == "all" else " or "
                return "(" + joiner.join(_when_text(e) for e in expr[op]) + ")"
    return json.dumps(expr, ensure_ascii=False)[:80]


# generated condition → the coordination field naming the roles that receive its message
GENERATED_RECEIVER = {
    "report_and_wait": {"report_received": "receiver", "ack_received": "reporter"},
    "leader_follower": {"formation_known": "followers"},
}
FORMATION_VAR = "current_formation"  # leader blackboard variable behind `formations:`
# coordination message → the coordination field naming the roles it is sent to
MESSAGE_RECEIVER = {
    "leader_follower": {"transition": "followers", "formation": "followers"},
    "report_and_wait": {"report": "receiver", "ack": "reporter"},
    "synchronized_transition": {"transition": "participants"},
}
# sheet operand / section name → the L2 given section that sets it
INPUT_SECTIONS = {
    "self": "platform",
    "platform": "platform",
    "track": "tracks",
    "tracks": "tracks",
    "terrain": "terrain",
    "formation": "formation",
    "team": "team",
    "simulation": "simulation",
}
# generated condition → the message that makes it true (an L2 test sets it in given.messages)
GENERATED_MESSAGE = {
    "report_received": "report",
    "ack_received": "ack",
    "formation_known": "formation",
}
MECHANICAL_FIELDS = ("issued_actions", "sent_messages", "tree_status", "selected_path")
_GIVEN_SECTIONS = {
    "self_value": "platform",
    "track_value": "tracks",
    "terrain_value": "terrain",
    "formation_value": "formation",
    "team_value": "team",
    "simulation_value": "simulation",
}


# what the runtime functions read (ir/runtime.py ``_function``)
FUNCTION_INPUTS = {
    "range_to_current_target_lte": ("track_value.current_target_range",),
    "formation_is": ("formation_value.current_id",),
    "standoff_reached": ("track_value.nearest_hostile_range",),
    "time_in_state_gte": ("elapsed.state",),
}


def _flatten_inputs(values: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Nested input maps become dotted keys; quantities ({value, unit}) stay values."""
    out: dict[str, Any] = {}
    for key, value in values.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict) and value and set(value) != {"value", "unit"}:
            out.update(_flatten_inputs(value, f"{name}."))
        else:
            out[name] = value
    return out


def given_path(element: str, role: dict[str, Any]) -> tuple[str, str] | None:
    """``track_value.hostile_count`` → (``tracks``, ``hostile_count``): where an L2 test
    sets the input an operand reads (``ir/l2.py`` ``_world_data``). Functions: None."""
    operand, dot, prop = element.partition(".")
    if not dot:
        return None
    if operand == "elapsed":
        return "elapsed", prop
    if operand in _GIVEN_SECTIONS:
        return _GIVEN_SECTIONS[operand], prop
    if operand == "mover_value":
        return "components", f"mover.{prop}"
    return None  # sensors / weapons / comms are per component; not checked


def vocabulary_elements(expression: Any, catalog: Catalog) -> list[str]:
    """Operand properties (``terrain_value.class``) and functions an expression reads."""
    out: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in catalog.operands and isinstance(value, dict) and "property" in value:
                    out.append(f"{key}.{value['property']}")
                elif key == "function_call" and isinstance(value, dict) and value.get("name"):
                    out.append(str(value["name"]))
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(expression)
    return list(dict.fromkeys(out))


def _refs(expression: Any) -> list[str]:
    out: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("condition_ref"), str):
                out.append(node["condition_ref"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(expression)
    return out


def _walk(node: XmlNode):
    yield node
    for child in node.children:
        yield from _walk(child)


def _short(value: Any) -> str:
    if isinstance(value, dict):
        if "parameter_ref" in value:
            return str(value["parameter_ref"])
        if "literal" in value:
            return str(value["literal"])
        if "value" in value and "unit" in value:
            return (
                f"{value['value']:g}{value['unit']}"
                if isinstance(value["value"], int | float)
                else ""
            )
        if "blackboard_value" in value:
            return str(value["blackboard_value"]).split(".")[-1]
        return ""
    return str(value)


def compile_sheet(
    text: str,
    *,
    request: FalconRequest,
    clauses: list[RequirementClause],
    catalog: Catalog,
    stage: str = "full",
    constraints: VocabularyConstraints | None = None,
    known_gaps: list[dict[str, Any]] | None = None,
    observed: dict[str, dict[str, Any]] | None = None,
) -> CompileResult:
    return SheetCompiler(
        text,
        request=request,
        clauses=clauses,
        catalog=catalog,
        stage=stage,
        constraints=constraints,
        known_gaps=known_gaps,
        observed=observed,
    ).run()
