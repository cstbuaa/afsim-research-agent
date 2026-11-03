"""Pydantic models for ``behavior_spec.yaml`` (Tactical Behavior IR 1.0).

Structure is validated here (IR002); semantics (references, types, units,
evidence, graphs) are validated by :mod:`afsim_research_agent.ir.validate`.
Expressions and operands are kept as plain mappings so the semantic
validator can report precise paths instead of union-matching noise.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Expr = dict[str, Any]
Operand = dict[str, Any]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class QuantitySpec(_Strict):
    value: float
    unit: str


class Basis(_Strict):
    status: Literal["explicit", "inferred", "conflicting", "unknown", "user_specified"]
    evidence: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    rationale: str | None = None
    confirmation: Literal["pending", "confirmed", "rejected"] = "pending"
    confirmed_by: str | None = None


class ModelMeta(_Strict):
    id: str
    display_name: str
    version: str
    description: str = ""
    intent: str
    authoring_mode: Literal["agent_generated", "user_authored", "mixed"] = "agent_generated"
    domain_concepts: list[str] = Field(default_factory=list)


class Requirement(_Strict):
    source_id: str
    kind: Literal["user_input", "user_document"] = "user_input"
    locator: str = "conversation"
    summary: str


class EvidencePackageRef(_Strict):
    package_id: str
    content_hash: str | None = None
    readiness: Literal["passed", "failed", "overridden"] = "passed"
    path: str = "evidence_package.json"


class Reference(_Strict):
    source_id: str
    kind: Literal["crow_source", "user_document", "standard", "other"]
    locator: str = ""
    quality_tier: Literal["A", "B", "C", "D"] | None = None
    content_hash: str | None = None
    classification: Literal["public", "user_private"] = "public"


class Provenance(_Strict):
    requirements: list[Requirement] = Field(default_factory=list)
    evidence_package: EvidencePackageRef | None = None
    references: list[Reference] = Field(default_factory=list)


class Assumption(_Strict):
    id: str
    statement: str
    impact: str | None = None
    affects: list[str] = Field(default_factory=list)
    requires_confirmation: bool = False
    confirmation: Literal["pending", "confirmed", "rejected"] = "pending"
    confirmed_by: str | None = None


class DebugOptions(_Strict):
    behavior_tree_trace: bool = False
    state_transitions: bool = False
    script_writes: bool = False


class Execution(_Strict):
    architecture: Literal[
        "script_processor", "behavior_tree", "state_machine", "state_machine_with_behavior_trees"
    ]
    controller_name: str = "tactical_controller"
    update_interval: QuantitySpec
    deterministic: bool = True
    random_seed_parameter: str | None = None
    debug: DebugOptions = Field(default_factory=DebugOptions)


# --- roles -----------------------------------------------------------------


class Command(_Strict):
    superior: str | None = None
    subordinates: list[str] = Field(default_factory=list)


class MoverDependency(_Strict):
    required: bool = True
    capability: str


class ComponentDependency(_Strict):
    id: str
    capability: str
    required: bool = True


class EnergyDependency(_Strict):
    required: bool = True
    capability: str = "fuel_tracking"


class Dependencies(_Strict):
    mover: MoverDependency | None = None
    energy: EnergyDependency | None = None
    sensors: list[ComponentDependency] = Field(default_factory=list)
    comms: list[ComponentDependency] = Field(default_factory=list)
    weapons: list[ComponentDependency] = Field(default_factory=list)
    processors: list[ComponentDependency] = Field(default_factory=list)


class Variable(_Strict):
    name: str
    type: Literal["bool", "int", "double", "string"]
    default: bool | int | float | str


class Blackboard(_Strict):
    id: str
    scope: Literal["platform"] = "platform"
    variables: list[Variable] = Field(default_factory=list)


class Transition(_Strict):
    transition_id: str
    to: str
    priority: int = 0
    when: Expr
    basis: Basis | None = None
    synchronize_with: str | None = None


class State(_Strict):
    id: str
    tree: str | None = None
    terminal: bool = False
    description: str = ""
    transitions: list[Transition] = Field(default_factory=list)


class StateMachine(_Strict):
    id: str
    initial_state: str
    states: list[State]


class Role(_Strict):
    id: str
    description: str = ""
    count: int = Field(default=1, ge=1, le=64)
    capability_class: Literal["ground_vehicle", "air_vehicle", "surface_vessel", "static_node"]
    command: Command = Field(default_factory=Command)
    dependencies: Dependencies = Field(default_factory=Dependencies)
    blackboards: list[Blackboard] = Field(default_factory=list)
    state_machine: StateMachine | None = None
    root_tree: str | None = None


# --- formations / coordination / messages -----------------------------------


class DistanceRef(_Strict):
    parameter_ref: str | None = None
    literal_distance: QuantitySpec | None = None
    scale: float = 1.0  # geometric multiplier, e.g. -2 = two intervals behind


class Slot(_Strict):
    role_instance: str
    forward: DistanceRef
    right: DistanceRef


class Formation(_Strict):
    id: str
    description: str = ""
    reference: Literal["leader"] = "leader"
    slots: list[Slot]
    tolerance: DistanceRef | None = None
    basis: Basis | None = None


class Coordination(_Strict):
    id: str
    pattern: str
    description: str = ""
    leader: str | None = None
    followers: str | None = None
    formation_ref: Operand | None = None
    groups: dict[str, list[str]] | None = None
    bound_distance: DistanceRef | None = None
    active_when: Expr | None = None
    reporter: str | None = None
    receiver: str | None = None
    ack_timeout: QuantitySpec | None = None
    initiator: str | None = None
    participants: list[str] | None = None
    priority: int | None = None
    resync_interval: QuantitySpec | None = None
    standoff_distance: DistanceRef | None = None
    objective: str | None = None
    basis: Basis | None = None


class MessageField(_Strict):
    name: str
    type: Literal["bool", "int", "double", "string"]
    default: bool | int | float | str | None = None


class Delivery(_Strict):
    latency: QuantitySpec = Field(default_factory=lambda: QuantitySpec(value=0, unit="sec"))
    reliability: float = Field(default=1.0, ge=0.0, le=1.0)


class Message(_Strict):
    id: str
    description: str = ""
    sender: str | list[str] = Field(alias="from")
    to: str | list[str]
    fields: list[MessageField] = Field(default_factory=list)
    delivery: Delivery = Field(default_factory=Delivery)


# --- parameters / resources --------------------------------------------------


class Parameter(_Strict):
    type: Literal[
        "distance",
        "speed",
        "time",
        "angle",
        "altitude",
        "mass",
        "frequency",
        "ratio",
        "integer",
        "boolean",
        "string",
    ]
    value: bool | int | float | str | None
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    description: str = ""
    vocabulary: list[str] | None = None
    basis: Basis | None = None
    # a value only the scenario knows (IR 2.0, ``ir.v2.scenario_interface``):
    # "point:<id>:lat" / "point:<id>:lon" / "route:<id>"; the scenario binding supplies it
    scenario: str | None = None


class Resource(_Strict):
    id: str
    policy: Literal["exclusive", "shared"] = "exclusive"


# --- conditions / behaviors / trees ----------------------------------------


class Condition(_Strict):
    id: str
    description: str = ""
    expression: Expr


class MemoryVar(_Strict):
    name: str
    type: Literal["bool", "int", "double", "string"]
    default: bool | int | float | str


class ActionCall(_Strict):
    command: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    issue_policy: Literal["once_per_activation", "once_per_model_lifetime", "every_tick"] | None = (
        None
    )
    allow_every_tick: bool = False


class Outcome(_Strict):
    expression: Expr


class Timeout(_Strict):
    value: float
    unit: str
    result: Literal["success", "failure"] = "failure"


class BehaviorMessages(_Strict):
    running: str | None = None
    success: str | None = None
    failure: str | None = None


class Behavior(_Strict):
    id: str
    description: str = ""
    kind: Literal["action"] = "action"
    memory: list[MemoryVar] = Field(default_factory=list)
    precondition: Expr | None = None
    claims: list[str] = Field(default_factory=list)
    actions: list[ActionCall] = Field(default_factory=list)
    success: Outcome | None = None
    failure: Outcome | None = None
    timeout: Timeout | None = None
    continuous: bool = False
    messages: BehaviorMessages = Field(default_factory=BehaviorMessages)
    basis: Basis | None = None


class Policy(_Strict):
    type: Literal["threshold", "all", "one", "fail_on_one", "fail_on_all"]
    count: int | None = None


class TreeNode(_Strict):
    node_id: str
    type: Literal[
        "behavior",
        "condition",
        "sequence",
        "sequence_with_memory",
        "selector",
        "selector_with_memory",
        "priority_selector",
        "weighted_random",
        "parallel",
        "decorator",
        "subtree",
    ]
    description: str = ""
    behavior: str | None = None
    condition: str | None = None
    tree: str | None = None
    decorator: (
        Literal[
            "inverter",
            "succeeder",
            "negator",
            "repeat_count",
            "repeat_until_done",
            "repeat_for_duration",
        ]
        | None
    ) = None
    count: int | None = None
    duration: QuantitySpec | None = None
    until: Expr | None = None
    success_policy: Policy | None = None
    failure_policy: Policy | None = None
    fitness: Operand | None = None
    weight: Operand | None = None
    children: list[TreeNode] = Field(default_factory=list)


class Tree(_Strict):
    id: str
    description: str = ""
    root: TreeNode


# --- tests / scenarios -----------------------------------------------------


class TestGiven(_Strict):
    role: str | None = None
    instance: int = 0
    state: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    blackboard: dict[str, Any] = Field(default_factory=dict)
    platform: dict[str, Any] = Field(default_factory=dict)
    components: dict[str, Any] = Field(default_factory=dict)
    terrain: dict[str, Any] = Field(default_factory=dict)
    tracks: dict[str, Any] = Field(default_factory=dict)
    formation: dict[str, Any] = Field(default_factory=dict)
    team: dict[str, Any] = Field(default_factory=dict)
    simulation: dict[str, Any] = Field(default_factory=dict)
    messages: list[dict[str, Any]] = Field(default_factory=list)
    elapsed: dict[str, Any] = Field(default_factory=dict)
    coordination: dict[str, dict[str, Any]] = Field(default_factory=dict)


class TestExpect(_Strict):
    next_state: str | None = None
    fired_transition: str | None = None
    tree_status: Literal["success", "failure", "running"] | None = None
    selected_path: list[str] | None = None
    issued_actions: list[str] | None = None
    sent_messages: list[str] | None = None
    blackboard: dict[str, Any] | None = None


class TestCase(_Strict):
    __test__ = False  # not a pytest class

    id: str
    description: str = ""
    category: (
        Literal[
            "nominal",
            "precondition_failure",
            "priority_transition",
            "resource_unavailable",
            "timeout_or_loss",
        ]
        | None
    ) = None
    given: TestGiven
    expect: TestExpect


class Environment(_Strict):
    terrain_map: str | None = None
    terrain: dict[str, Any] | None = None
    routes: dict[str, list[list[float]]] = Field(default_factory=dict)
    waypoints: dict[str, list[float]] = Field(default_factory=dict)
    duration: QuantitySpec
    time_step: QuantitySpec | None = None
    random_seed: int = 0


class ScenarioEvent(_Strict):
    id: str
    type: Literal["marker", "spawn_hostile", "comm_degradation", "message_loss", "remove_hostile"]
    at: QuantitySpec | None = None
    when: dict[str, Any] | None = None
    position: list[float] | None = None
    duration: QuantitySpec | None = None
    loss: float = Field(default=1.0, ge=0.0, le=1.0)
    message: str | None = None
    target_id: str | None = None


class Window(_Strict):
    after_event: str | None = None
    after: str | None = None
    value: float
    unit: str


class During(_Strict):
    start: str = Field(default="start", alias="from")
    duration: QuantitySpec | None = None


class ScenarioExpectation(_Strict):
    id: str
    observable: str
    role: str | None = None
    instance: int | None = None
    formation: str | None = None
    message: str | None = None
    command: str | None = None
    state: str | None = None
    equals: Any = None
    lte: Any = None
    gte: Any = None
    lt: Any = None
    gt: Any = None
    within: Window | None = None
    during: During | None = None


class Scenario(_Strict):
    id: str
    description: str = ""
    environment: Environment
    initial: dict[str, dict[str, Any]] = Field(default_factory=dict)
    events: list[ScenarioEvent] = Field(default_factory=list)
    expect: list[ScenarioExpectation] = Field(default_factory=list)
    traces_to: list[str] = Field(default_factory=list)


class BehaviorSpec(_Strict):
    schema_version: str
    model: ModelMeta
    provenance: Provenance = Field(default_factory=Provenance)
    assumptions: list[Assumption] = Field(default_factory=list)
    execution: Execution
    roles: list[Role] = Field(min_length=1)
    formations: list[Formation] = Field(default_factory=list)
    coordination: list[Coordination] = Field(default_factory=list)
    messages: list[Message] = Field(default_factory=list)
    parameters: dict[str, Parameter]
    resources: list[Resource] = Field(default_factory=list)
    conditions: list[Condition]
    behaviors: list[Behavior]
    trees: list[Tree]
    tests: list[TestCase]
    scenarios: list[Scenario] = Field(default_factory=list)


TreeNode.model_rebuild()
