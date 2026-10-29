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
DECORATORS = ("inverter", "succeeder", "negator", "repeat")
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
    source: Literal["tactic", "timer", "message", "external"]
    description: str = ""
    tactic: str | None = None  # source=tactic: the tactic-set mission that raises it
    text: str | None = None  # source=tactic: the trigger as the tactic set writes it
    after_s: float | None = None  # source=timer: seconds in the current state


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


class Need(_Strict):
    """An action the tactic needs that the capability set does not have (a development gap)."""

    action: str
    description: str = ""
    params: dict[str, Any] = Field(default_factory=dict)


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
    needs: list[Need] = Field(default_factory=list)
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
