"""Pydantic models for Tactical Behavior IR 2.0 (seven layers).

See docs/design/tactical-behavior-ir-v2-layered.md. Structure is checked here (IR002);
references to the capability set (动作集 / 战术集), blackboard reads, arbitration and
state-machine properties are checked by :mod:`afsim_research_agent.ir.v2.normalize`.
Expressions keep the IR 1.0 shapes (``all`` / ``any`` / ``not`` / ``compare`` /
``condition_ref``); the operand ``bb`` reads a blackboard field.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from afsim_research_agent.ir.models import (
    Assumption,
    ModelMeta,
    Parameter,
    Provenance,
)

SCHEMA_VERSION = "2.0"

Expr = dict[str, Any]

COMPOSITES = (
    "sequence",
    "sequence_with_memory",
    "selector",
    "selector_with_memory",
    "priority_selector",
    "weighted_random",
    "parallel",
)
# failure_as_running: a failure of the child is reported as running (e.g. keep searching
# while no target is found); compiled to selector(child, a leaf that returns running)
DECORATORS = ("inverter", "succeeder", "negator", "repeat", "failure_as_running")
LEAVES = ("action", "condition", "tactic", "subtree")
NODE_TYPES = COMPOSITES + DECORATORS + LEAVES


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --------------------------------------------------------------------- layer 1
class CapabilityRef(_Strict):
    """Filled by normalization from what the other layers reference."""

    set: str | None = None
    content_hash: str | None = None
    slices: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------- layer 3
class FieldDecl(_Strict):
    type: Literal["bool", "int", "double", "string"]
    default: bool | int | float | str | None = None
    unit: str | None = None
    description: str = ""


class Condition(_Strict):
    id: str
    description: str = ""
    expression: Expr


class Event(_Strict):
    """A transition trigger that is not a blackboard condition."""

    id: str
    source: Literal["tactic", "timer", "message", "external", "tree"]
    description: str = ""
    tactic: str | None = None  # source=tactic: the tactic-set mission that raises it
    text: str | None = None  # source=tactic: the trigger as the tactic set writes it
    after_s: float | None = None  # source=timer: seconds in the current state
    # source=tree: the current state's tree finished with this result (e.g. cleanup done)
    result: Literal["success", "failure"] | None = None


# --------------------------------------------------------------------- layer 4
class Hold(_Strict):
    resource: str
    from_state: str = Field(alias="from")
    to: str


class Arbitration(_Strict):
    hold: list[Hold] = Field(default_factory=list)


# --------------------------------------------------------------------- layer 5
class Policy(_Strict):
    type: Literal["one", "all", "threshold"] = "one"
    count: int | None = Field(default=None, ge=1)


class Repeat(_Strict):
    count: int | None = Field(default=None, ge=1)
    until_done: bool = False
    duration_s: float | None = Field(default=None, gt=0)


class Node(_Strict):
    type: Literal[NODE_TYPES]  # type: ignore[valid-type]
    id: str | None = None  # optional label; normalization assigns node ids
    description: str = ""
    children: list[Node] = Field(default_factory=list)
    # leaves
    action: str | None = None
    condition: str | None = None
    tactic: str | None = None
    subtree: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    # composites / decorators
    policy: Policy | None = None  # parallel
    repeat: Repeat | None = None  # repeat
    weight: Any = None  # child of priority_selector / weighted_random: number or operand


class Subtree(_Strict):
    id: str
    description: str = ""
    root: Node


# --------------------------------------------------------------------- layer 7
def _yaml_on(data: Any) -> Any:
    """YAML 1.1 reads an unquoted ``on:`` key as the boolean true."""
    if isinstance(data, dict) and True in data and "on" not in data:
        data = dict(data)
        data["on"] = data.pop(True)
    return data


class Transition(_Strict):
    from_state: str = Field(alias="from")
    to: str
    on: str
    priority: int = 0

    _on = model_validator(mode="before")(_yaml_on)


class GlobalTransition(_Strict):
    to: str
    on: str
    priority: int = 0

    _on = model_validator(mode="before")(_yaml_on)


class Mission(_Strict):
    initial_state: str
    states: list[str] = Field(min_length=1)
    terminal: list[str] = Field(default_factory=list)
    safe: list[str] | None = None  # default: SHUTDOWN when present
    transitions: list[Transition] = Field(default_factory=list)
    global_transitions: list[GlobalTransition] = Field(default_factory=list, alias="global")


class MissionRef(_Strict):
    """The whole mission state machine of a tactic-set mission, with parameter overrides."""

    tactic: str
    role: str | None = None  # the tactic-set role to take when the mission has several
    params: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------- roles, gaps
class Command(_Strict):
    superior: str | None = None
    subordinates: list[str] = Field(default_factory=list)


class Role(_Strict):
    id: str
    description: str = ""
    count: int = Field(default=1, ge=1, le=128)
    capability_class: Literal["ground_vehicle", "air_vehicle", "surface_vessel", "static_node"]
    command: Command = Field(default_factory=Command)


# --------------------------------------------------------------------- gaps
class GapParam(_Strict):
    type: Literal["bool", "int", "double", "string"]
    unit: str | None = None
    required: bool = False
    range: tuple[float, float] | None = None
    default: bool | int | float | str | None = None


class GapBlackboard(_Strict):
    reads: list[str] = Field(default_factory=list)
    writes: list[str] = Field(default_factory=list)


class GapFailure(_Strict):
    codes: list[str] = Field(default_factory=list)
    meaning: str = ""


class GapResult(_Strict):
    running: str = ""
    success: str = ""
    failure: GapFailure = Field(default_factory=GapFailure)
    terminates: bool = True
    max_duration_s: float | None = Field(default=None, gt=0)


class GapStub(_Strict):
    """How a placeholder runs before the gap is filled (L2 and the 2D simulation only)."""

    result: Literal["success", "failure", "running"] = "success"
    success_after_s: float = Field(default=0.0, ge=0)  # running until then, then `result`
    failure_code: str | None = None
    writes: dict[str, bool | int | float | str] = Field(default_factory=dict)  # on completion


class Gap(_Strict):
    """A subtree or action the model needs and the capability set lacks, written as a
    contract (design falcon-v3-composition-and-gaps §4). Trees reference it as
    ``gap.<id>`` in a subtree or action leaf."""

    kind: Literal["subtree", "action"]
    description: str
    reason: str = ""
    clauses: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    capability_class: str | None = None
    params: dict[str, GapParam] = Field(default_factory=dict)
    blackboard: GapBlackboard = Field(default_factory=GapBlackboard)
    resources: list[str] = Field(default_factory=list)
    result: GapResult = Field(default_factory=GapResult)
    uses: list[str] = Field(default_factory=list)  # subtree: existing actions it should use
    used_by: list[str] = Field(default_factory=list)  # filled by normalization when empty
    # action gaps: the rest of the action-set contract
    lifecycle: dict[str, str] = Field(default_factory=dict)
    completion: str = ""
    observable: str = ""
    stub: GapStub = Field(default_factory=GapStub)
    acceptance: list[dict[str, Any]] = Field(default_factory=list)
    local_only: bool = False  # forced true for action gaps by normalization


class BehaviorSpecV2(_Strict):
    schema_version: Literal["2.0"]
    model: ModelMeta
    provenance: Provenance = Field(default_factory=Provenance)
    assumptions: list[Assumption] = Field(default_factory=list)
    capability: CapabilityRef = Field(default_factory=CapabilityRef)
    roles: list[Role] = Field(min_length=1)
    parameters: dict[str, Parameter] = Field(default_factory=dict)
    blackboard: dict[str, FieldDecl] = Field(default_factory=dict)
    conditions: list[Condition] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    arbitration: Arbitration = Field(default_factory=Arbitration)
    subtrees: list[Subtree] = Field(default_factory=list)
    state_trees: dict[str, dict[str, str]] = Field(default_factory=dict)
    missions: dict[str, Mission | MissionRef]
    gaps: dict[str, Gap] = Field(default_factory=dict)
    # kept as written for now; checked with L2 / L3 in step 3
    formations: list[dict[str, Any]] = Field(default_factory=list)
    coordination: list[dict[str, Any]] = Field(default_factory=list)
    tests: list[dict[str, Any]] = Field(default_factory=list)
    scenarios: list[dict[str, Any]] = Field(default_factory=list)


Node.model_rebuild()


def json_schema() -> dict[str, Any]:
    schema = BehaviorSpecV2.model_json_schema(by_alias=True)
    schema["$id"] = "https://afsim-research-agent/schemas/behavior_spec-2.0.json"
    schema["title"] = "Tactical Behavior IR 2.0"
    return schema
