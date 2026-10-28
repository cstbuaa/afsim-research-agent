"""Falcon data contracts.

Three kinds of models live here:

* ``*Draft`` models are what the language model is asked to produce
  (structured output). They are permissive; code checks them afterwards.
* Records (``EvidencePackage``, ``RequirementClause`` ...) are what Falcon
  writes after the checks.
* ``FalconRequest`` is the user-editable request file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from afsim_research_agent.ir.models import (
    Behavior,
    Condition,
    Coordination,
    Formation,
    Message,
    Scenario,
    TestCase,
)

Topic = Literal[
    "formation",
    "spacing",
    "transition",
    "contact",
    "bounding",
    "communication",
    "speed",
    "terrain",
    "sensing",
    "other",
]
ClauseKind = Literal[
    "behavior", "condition", "formation", "coordination", "constraint", "parameter", "out_of_scope"
]
BasisStatus = Literal["explicit", "inferred", "conflicting", "unknown", "user_specified"]
SectionName = Literal[
    "parameters", "structure", "conditions", "behaviors", "control", "verification"
]
SECTIONS: tuple[str, ...] = (
    "parameters",
    "structure",
    "conditions",
    "behaviors",
    "control",
    "verification",
)


class _Loose(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


# --------------------------------------------------------------------------- request


class PlatformHint(_Loose):
    role_hint: str
    capability_class: Literal["ground_vehicle", "air_vehicle", "surface_vessel", "static_node"]
    count: int = Field(default=1, ge=1, le=64)


class UserValue(_Loose):
    value: float | int | bool | str
    unit: str | None = None


class FalconRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirement: str = ""
    model_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    # Crow research_result.json; may be omitted with tactic_selection (found next to it)
    crow_result: Path | None = None
    # P4: Crow C5 tactic_selection.yaml (or the tactics directory holding it)
    tactic_selection: Path | None = None
    display_name: str | None = None
    platforms: list[PlatformHint] = Field(default_factory=list)
    terrain_classes: list[str] = Field(default_factory=list)
    user_parameters: dict[str, UserValue] = Field(default_factory=dict)
    target_engine: Path | None = None
    backends: list[str] = Field(default_factory=lambda: ["python_sim", "btcpp_xml", "mermaid"])
    # platform capability judgment: coarse first drafts, refine groups when needed
    capability_granularity: Literal["coarse", "fine"] = "coarse"
    capability_refine: list[str] = Field(default_factory=list)
    # generation layer: v1 = JSON sections; v2 = tactic sheet (falcon-surface-formats-v2)
    generator: Literal["v1", "v2"] = "v1"
    sheet_calls: Literal[1, 3] = 3  # v2: structure / control / verification drafts, or one call
    # v2: behavior trees as Python functions (Code-BT style) or BehaviorTree.CPP XML;
    # the compiler accepts both forms whichever is asked for
    sheet_tree_format: Literal["python", "xml"] = "python"

    @model_validator(mode="after")
    def _inputs(self) -> FalconRequest:
        if self.tactic_selection is None:
            if not self.requirement.strip():
                raise ValueError("requirement is required (or give tactic_selection)")
            if self.crow_result is None:
                raise ValueError("crow_result is required (or give tactic_selection)")
        return self


# --------------------------------------------------------------------------- F1 evidence


class QuantityDraft(_Loose):
    name: str
    min: float
    max: float
    unit: str


class ClaimDraft(_Loose):
    statement: str
    topic: Topic = "other"
    subject: str = ""
    condition: str = ""
    quantity: QuantityDraft | None = None
    quote: str


class ExcerptClaims(_Loose):
    excerpt_id: str
    claims: list[ClaimDraft] = Field(default_factory=list)


class ExtractionDraft(_Loose):
    items: list[ExcerptClaims] = Field(default_factory=list)


class GroupDraft(_Loose):
    statement: str
    members: list[str]


class ConflictDraft(_Loose):
    a: str
    b: str
    reason: str


class GroupingDraft(_Loose):
    groups: list[GroupDraft] = Field(default_factory=list)
    conflicts: list[ConflictDraft] = Field(default_factory=list)


class Excerpt(BaseModel):
    excerpt_id: str
    source_id: str
    location: str
    excerpt: str
    citation: str = ""
    score: int = 0
    questions: list[str] = Field(default_factory=list)


class Quantity(BaseModel):
    name: str
    min: float
    max: float
    unit: str
    dimension: str


class EvidenceSource(BaseModel):
    source_id: str
    title: str
    locator: str = ""
    kind: str = "unknown"
    quality_tier: Literal["A", "B", "C", "D"] | None = None
    quality_score: float | None = None
    classification: Literal["public", "user_private"] = "public"


class EvidenceItem(BaseModel):
    evidence_id: str
    claim: str
    topic: str
    subject: str = ""
    condition: str = ""
    quantity: Quantity | None = None
    quote: str
    excerpt_id: str
    excerpt: str
    source_id: str
    location: str
    relevance_score: float
    quality_score: float
    questions: list[str] = Field(default_factory=list)


class ConflictRecord(BaseModel):
    a: str
    b: str
    reason: str
    origin: Literal["model", "numeric"]


class ClaimRecord(BaseModel):
    claim_id: str
    statement: str
    topic: str
    members: list[str]
    supporting: list[str]
    conflicting: list[str]
    conflicts: list[ConflictRecord] = Field(default_factory=list)


class DroppedClaim(BaseModel):
    excerpt_id: str
    statement: str = ""
    reason: str


class CrowRef(BaseModel):
    run_id: str | None = None
    gate_version: str
    readiness: Literal["passed"] = "passed"
    research_questions: list[str] = Field(default_factory=list)


class EvidencePackage(BaseModel):
    package_id: str
    content_hash: str = ""
    crow: CrowRef
    sources: list[EvidenceSource] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    claims: list[ClaimRecord] = Field(default_factory=list)
    dropped: list[DroppedClaim] = Field(default_factory=list)

    def item(self, evidence_id: str) -> EvidenceItem | None:
        return next((e for e in self.evidence if e.evidence_id == evidence_id), None)

    def conflict_pairs(self) -> set[frozenset[str]]:
        return {frozenset((c.a, c.b)) for claim in self.claims for c in claim.conflicts}


# --------------------------------------------------------------------------- F2 requirements


class UserValueDraft(_Loose):
    name: str
    value: float
    unit: str | None = None


class ClauseDraft(_Loose):
    text: str
    kind: ClauseKind
    roles: list[str] = Field(default_factory=list)
    user_values: list[UserValueDraft] = Field(default_factory=list)
    reason: str = ""


class RequirementDraft(_Loose):
    clauses: list[ClauseDraft] = Field(default_factory=list)


class RequirementClause(BaseModel):
    id: str
    text: str
    kind: ClauseKind
    roles: list[str] = Field(default_factory=list)
    user_values: list[UserValueDraft] = Field(default_factory=list)
    reason: str = ""
    # P4: clauses taken from the selected Crow tactic carry the card's evidence
    origin: Literal["requirement", "tactic"] = "requirement"
    evidence: list[str] = Field(default_factory=list)
    tactic_basis: str | None = None


class UserValueRecord(BaseModel):
    """A value the user stated; the only legal origin of ``user_specified``."""

    name: str
    value: float | int | bool | str
    unit: str | None = None
    dimension: str | None = None
    canonical: float | None = None
    origin: str  # clause id or "falcon_request"


# --------------------------------------------------------------------------- F3 skeleton


class ComponentDraft(_Loose):
    id: str
    capability: str


class VariableDraft(_Loose):
    name: str
    type: Literal["bool", "int", "double", "string"]
    default: bool | int | float | str


class StateDraft(_Loose):
    id: str
    description: str = ""
    tree: str
    terminal: bool = False  # `state --> [*]`: the role stops here (no tree needed)
    implements: list[str] = Field(default_factory=list)


class RoleDraft(_Loose):
    id: str
    description: str = ""
    count: int = Field(default=1, ge=1, le=64)
    capability_class: Literal["ground_vehicle", "air_vehicle", "surface_vessel", "static_node"]
    superior: str | None = None
    mover_capability: str | None = None
    sensors: list[ComponentDraft] = Field(default_factory=list)
    comms: list[ComponentDraft] = Field(default_factory=list)
    weapons: list[ComponentDraft] = Field(default_factory=list)
    blackboard_variables: list[VariableDraft] = Field(default_factory=list)
    initial_state: str | None = None
    states: list[StateDraft] = Field(default_factory=list)
    root_tree: str | None = None
    implements: list[str] = Field(default_factory=list)


class ElementPlan(_Loose):
    id: str
    description: str = ""
    implements: list[str] = Field(default_factory=list)


class CoordinationPlan(ElementPlan):
    pattern: str


class OutOfScope(_Loose):
    clause_id: str
    reason: str


class SkeletonDraft(_Loose):
    display_name: str
    description: str = ""
    intent: str
    domain_concepts: list[str] = Field(default_factory=list)
    architecture: Literal["state_machine_with_behavior_trees", "state_machine", "behavior_tree"] = (
        "state_machine_with_behavior_trees"
    )
    update_interval_s: float = 1.0
    roles: list[RoleDraft]
    formations: list[ElementPlan] = Field(default_factory=list)
    coordination: list[CoordinationPlan] = Field(default_factory=list)
    messages: list[ElementPlan] = Field(default_factory=list)
    out_of_scope: list[OutOfScope] = Field(default_factory=list)


# --------------------------------------------------------------------------- F4 sections


class BasisDraft(_Loose):
    status: BasisStatus = "unknown"
    evidence: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    rationale: str | None = None


class ParameterDraft(_Loose):
    name: str
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
    value: bool | int | float | str | None = None
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    description: str = ""
    basis: BasisDraft = Field(default_factory=BasisDraft)
    implements: list[str] = Field(default_factory=list)


class AssumptionDraft(_Loose):
    id: str
    statement: str
    impact: str | None = None
    affects: list[str] = Field(default_factory=list)
    requires_confirmation: bool = True


class ParametersSection(_Loose):
    parameters: list[ParameterDraft] = Field(default_factory=list)
    assumptions: list[AssumptionDraft] = Field(default_factory=list)


class StructureSection(_Loose):
    formations: list[Formation] = Field(default_factory=list)
    coordination: list[Coordination] = Field(default_factory=list)
    messages: list[Message] = Field(default_factory=list)
    new_parameters: list[ParameterDraft] = Field(default_factory=list)


class ConditionsSection(_Loose):
    conditions: list[Condition] = Field(default_factory=list)
    new_parameters: list[ParameterDraft] = Field(default_factory=list)


class BlackboardVariableDraft(_Loose):
    role: str
    blackboard: str = "mission"
    name: str
    type: Literal["bool", "int", "double", "string"]
    default: bool | int | float | str


class BehaviorsSection(_Loose):
    behaviors: list[Behavior] = Field(default_factory=list)
    blackboard_variables: list[BlackboardVariableDraft] = Field(default_factory=list)
    new_parameters: list[ParameterDraft] = Field(default_factory=list)


class FlatNode(_Loose):
    """A tree node with children by id (flat lists survive structured output better)."""

    node_id: str
    type: str
    description: str = ""
    behavior: str | None = None
    condition: str | None = None
    tree: str | None = None
    decorator: str | None = None
    count: int | None = None
    duration: dict[str, Any] | None = None
    until: dict[str, Any] | None = None
    success_policy: dict[str, Any] | None = None
    failure_policy: dict[str, Any] | None = None
    fitness: dict[str, Any] | None = None
    weight: dict[str, Any] | None = None
    children: list[str] = Field(default_factory=list)


class FlatTree(_Loose):
    id: str
    description: str = ""
    root: str
    nodes: list[FlatNode]


class TransitionDraft(_Loose):
    role: str
    state: str
    transition_id: str
    to: str
    priority: int = 0
    when: dict[str, Any]
    basis: BasisDraft | None = None
    synchronize_with: str | None = None
    implements: list[str] = Field(default_factory=list)


class ControlSection(_Loose):
    trees: list[FlatTree] = Field(default_factory=list)
    transitions: list[TransitionDraft] = Field(default_factory=list)


class VerificationSection(_Loose):
    tests: list[TestCase] = Field(default_factory=list)
    scenarios: list[Scenario] = Field(default_factory=list)


SECTION_MODELS: dict[str, type[BaseModel]] = {
    "parameters": ParametersSection,
    "structure": StructureSection,
    "conditions": ConditionsSection,
    "behaviors": BehaviorsSection,
    "control": ControlSection,
    "verification": VerificationSection,
}


# --------------------------------------------------------------------------- trace


class CallRecord(BaseModel):
    stage: str
    model: str
    prompt_sha256: str
    seconds: float
    ok: bool
    error: str | None = None
    # mechanical corrections applied to the answer, or items dropped when salvaging
    fixes: list[str] = Field(default_factory=list)
    salvaged: bool = False


class TextAnswer(BaseModel):
    """Plain-text model output (the v2 tactic sheet is Markdown, not JSON)."""

    text: str


class EnforcementNote(BaseModel):
    path: str
    rule: str
    detail: str


class RepairRound(BaseModel):
    round: int
    errors: int
    sections: list[str]
    codes: list[str]
    loaded: bool = True  # False: the spec did not load, so later checks did not run


class FalconTrace(BaseModel):
    model_id: str
    falcon_version: str
    prompt_version: str
    catalog_versions: dict[str, str]
    status: Literal["draft_valid", "draft_invalid"] = "draft_valid"
    clauses: list[RequirementClause] = Field(default_factory=list)
    user_values: list[UserValueRecord] = Field(default_factory=list)
    implements: dict[str, list[str]] = Field(default_factory=dict)
    uncovered_clauses: list[str] = Field(default_factory=list)
    out_of_scope: list[OutOfScope] = Field(default_factory=list)
    enforcement: list[EnforcementNote] = Field(default_factory=list)
    repairs: list[RepairRound] = Field(default_factory=list)
    calls: list[CallRecord] = Field(default_factory=list)
    skeleton: dict[str, Any] = Field(default_factory=dict)
    sections: dict[str, Any] = Field(default_factory=dict)
    capability_constraints: dict[str, Any] | None = None
    tactic: dict[str, Any] | None = None
    generator: Literal["v1", "v2"] = "v1"
    sheet: dict[str, Any] | None = None  # v2: blocks, holes, prompt version
