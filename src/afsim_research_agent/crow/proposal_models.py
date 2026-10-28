"""Data contracts for C4 tactic proposal (design crow-tactic-proposal-v1 §5).

`*Draft` classes are what the model is asked to produce (structured output;
no free-form dict fields, so strict JSON schema decoding works). The card and
result classes add fields that only code computes: IDs, support status,
scores, gaps and selection notes.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Basis = Literal["evidence", "user_specified", "inferred", "unsupported"]
Provenance = Literal["retrieved", "adapted", "combined", "transferred"]
Authority = Literal["leader_assigns", "autonomous", "hybrid"]
ContextMatch = Literal["exact", "similar", "different"]
Support = Literal["supported", "partial", "inferred", "unsupported", "empty"]

SECTIONS = ("applicability", "roles", "geometry", "steps", "exceptions")
INNOVATIVE: tuple[str, ...] = ("adapted", "combined", "transferred")


class _Element(BaseModel):
    evidence: list[str] = Field(
        default_factory=list, description="evidence_id values (ev_...) that state this element"
    )
    basis: Basis = "evidence"


class Applicability(_Element):
    situation: str


class RoleAssignment(_Element):
    role: str
    members: list[str] = Field(default_factory=list)


class GeometryItem(_Element):
    formation: str
    when: str = ""
    spacing: str = ""


class Step(_Element):
    phase: str
    do: str
    until: str = Field(description="End condition of the step; required.")


class ExceptionItem(_Element):
    situation: str
    then: str


class DerivationSource(BaseModel):
    tactic: str = Field(description="Name or id of the retrieved tactic used as source.")
    evidence: list[str] = Field(default_factory=list)


class Change(BaseModel):
    op: str
    before: str = ""
    after: str
    why: str


class MorphChoice(BaseModel):
    dimension: str
    option: str


class Derivation(BaseModel):
    sources: list[DerivationSource] = Field(default_factory=list)
    operators: list[str] = Field(default_factory=list)
    changes: list[Change] = Field(default_factory=list)
    combination: list[MorphChoice] = Field(default_factory=list)


class Technique(BaseModel):
    """A building block the evidence describes (formation, movement technique,
    report procedure...). Techniques are never candidates on their own."""

    id: str = Field(description="Short id such as a1, a2.")
    name: str
    kind: Literal["formation", "movement", "transition", "coordination", "engagement", "other"]
    description: str
    evidence: list[str] = Field(default_factory=list)
    unit_type: str = ""


class TacticDraft(BaseModel):
    """One COMPLETE tactic as written by the model."""

    name: str
    narrative: str = Field(
        description=(
            "The complete tactic in natural Chinese prose, from start to end: when it applies, "
            "who does what in which order, how phases hand over, coordination, exceptions and "
            "how it ends."
        )
    )
    patterns: list[str] = Field(default_factory=list)
    techniques: list[str] = Field(
        default_factory=list, description="Ids of the techniques this tactic is built from."
    )
    summary: str
    source_context: str = Field(
        default="", description="Unit type / echelon / domain the evidence describes."
    )
    context_match: ContextMatch = "similar"
    applicability: list[Applicability] = Field(default_factory=list)
    roles: list[RoleAssignment] = Field(default_factory=list)
    geometry: list[GeometryItem] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    exceptions: list[ExceptionItem] = Field(default_factory=list)
    decision_authority: Authority = "leader_assigns"
    addresses: list[str] = Field(
        default_factory=list,
        description="user_specified phrases (verbatim) this tactic implements, any basis.",
    )
    covers: list[str] = Field(
        default_factory=list,
        description="user_specified phrases this tactic's evidence-backed content addresses.",
    )
    conflicts_with_user: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class ExtractionResult(BaseModel):
    techniques: list[Technique] = Field(default_factory=list)
    tactics: list[TacticDraft] = Field(default_factory=list)


class IdeaDraft(TacticDraft):
    provenance: Literal["adapted", "combined"]
    derivation: Derivation


class InnovationResult(BaseModel):
    ideas: list[IdeaDraft] = Field(default_factory=list)


class Score(BaseModel):
    coverage: float = 0.0
    context: float = 0.0
    evidence_strength: float = 0.0
    total: float = 0.0


# ---------------------------------------------------------------------- C4b capability needs
Necessity = Literal["required", "enhancing", "substitutable"]


class AlternativeDraft(BaseModel):
    via: str = Field(description="How this alternative obtains the capability, in Chinese.")
    maps_to: list[str] = Field(
        default_factory=list, description="Catalog references (capability or capability:interface)."
    )


class NeedDraft(BaseModel):
    """One platform capability the tactic needs, as mapped by the model (status is not)."""

    need: str = Field(description="What the platform must be able to do, in Chinese.")
    source: str = Field(description="Tactic element path, e.g. steps[2].until, exceptions[0].")
    necessity: Necessity = "required"
    maps_to: list[str] = Field(
        default_factory=list,
        description="Catalog references; empty when nothing in the catalog fits.",
    )
    why: str = Field(default="", description="Why the entries do exactly this need.")
    missing: str = Field(default="", description="Part of the need nothing in the catalog does.")
    reference: str = Field(
        default="",
        description="A number the need depends on, as 'name≈value unit' (a reference value).",
    )
    group: str = Field(default="", description="Tactical capability group (coarse drafts).")
    alternatives: list[AlternativeDraft] = Field(default_factory=list)


class NeedsDraft(BaseModel):
    needs: list[NeedDraft] = Field(default_factory=list)


class MappingCheck(BaseModel):
    """Review of one need's mapping: entries whose definition is not this need are dropped."""

    index: int
    drop: list[str] = Field(default_factory=list, description="References to remove.")
    missing: str = Field(default="", description="Key part the kept entries still lack.")
    reason: str = ""


class MappingReview(BaseModel):
    items: list[MappingCheck] = Field(default_factory=list)


class AlternativeResult(BaseModel):
    via: str
    maps_to: list[str] = Field(default_factory=list)
    status: str = ""


class CapabilityNeed(BaseModel):
    """A need after code validation and catalog judgment (crow-tactic-proposal-v1 §18)."""

    need: str
    source: str
    necessity: Necessity = "required"
    maps_to: list[str] = Field(default_factory=list)
    why: str = ""
    missing: str = ""
    reference: str = ""
    group: str | None = None
    status: str = Field(
        description="ready | composable | partial | simplified | confirm | ir_extension | develop"
    )
    label: str = ""
    gaps: list[str] = Field(default_factory=list)
    gap_type: str | None = None
    alternatives: list[AlternativeResult] = Field(default_factory=list)
    chosen: str | None = Field(default=None, description="'via' of the alternative used.")
    notes: list[str] = Field(default_factory=list)


class FeasibilityInfo(BaseModel):
    level: Literal["ready", "needs_confirmation", "needs_development"]
    counts: dict[str, int] = Field(default_factory=dict)
    blocking_gaps: list[str] = Field(default_factory=list)


class GapUse(BaseModel):
    candidate: str
    sources: list[str] = Field(default_factory=list)


class CapabilityGap(BaseModel):
    """One capability gap across all candidates; the id is stable across runs."""

    id: str
    contract: str = Field(description="Catalog reference, or the need text when nothing maps.")
    display_name: str
    gap_type: Literal["confirm", "ir_extension", "develop"]
    used_by: list[GapUse] = Field(default_factory=list)
    candidates_count: int = 0
    selected: bool = False
    status: Literal["open", "confirmed", "in_development", "implemented", "rejected"] = "open"
    source: Literal["crow_estimate", "ir_check"] = "crow_estimate"


# ---------------------------------------------------------------------- C5 red team (§6.2)
Level = Literal["low", "medium", "high"]


class CriticalAssumption(BaseModel):
    assumption: str
    fragility: Level = "medium"
    why: str = ""


class CounterTactic(BaseModel):
    id: str = Field(description="ct_01, ct_02, ...")
    description: str
    enemy_capabilities_needed: list[str] = Field(default_factory=list)
    trigger: str = ""
    expected_effect: str = ""
    severity: Level = "medium"


class OverallJudgment(BaseModel):
    soundness: Level = "medium"
    main_weakness: str = ""


class RedTeamDraft(BaseModel):
    """What the red-team model returns for one candidate."""

    critical_assumptions: list[CriticalAssumption] = Field(default_factory=list)
    counter_tactics: list[CounterTactic] = Field(default_factory=list)
    failure_conditions: list[str] = Field(default_factory=list)
    capability_doubts: list[str] = Field(default_factory=list)
    overall: OverallJudgment = Field(default_factory=OverallJudgment)


class ResponseItem(BaseModel):
    target: str = Field(description="Item answered: ct_01, fc_1, ca_1 or cd_1.")
    decision: Literal["revise", "keep"]
    reason: str = Field(description="Why the tactic is changed, or why it is kept as it is.")


class RevisionOp(BaseModel):
    """One change to the tactic in answer to the red team."""

    op: Literal["add_exception", "modify_exception", "add_step", "modify_step"]
    index: int | None = Field(
        default=None,
        description="modify_*: index of the element; add_step: insert after this index.",
    )
    phase: str = ""
    do: str = ""
    until: str = ""
    situation: str = ""
    then: str = ""
    responds_to: list[str] = Field(default_factory=list)


class ResponseDraft(BaseModel):
    """The proposer's answer: per red-team item revise or keep; the changes; new narrative."""

    responses: list[ResponseItem] = Field(default_factory=list)
    revisions: list[RevisionOp] = Field(default_factory=list)
    narrative: str = Field(default="", description="Revised narrative when anything changed.")


class ConstraintViolation(BaseModel):
    constraint: str = Field(description="The user constraint, verbatim.")
    how: str = Field(description="What in the revision contradicts or weakens it.")


class ConstraintCheck(BaseModel):
    violations: list[ConstraintViolation] = Field(default_factory=list)


class TacticSnapshot(BaseModel):
    revision: int
    narrative: str
    steps: list[Step] = Field(default_factory=list)
    exceptions: list[ExceptionItem] = Field(default_factory=list)


class RedTeamReview(BaseModel):
    candidate: str
    revision_reviewed: int = 1
    revision_after: int = 1
    critical_assumptions: list[CriticalAssumption] = Field(default_factory=list)
    counter_tactics: list[CounterTactic] = Field(default_factory=list)
    failure_conditions: list[str] = Field(default_factory=list)
    capability_doubts: list[str] = Field(default_factory=list)
    overall: OverallJudgment | None = None
    responses: list[ResponseItem] = Field(default_factory=list)
    revisions: list[RevisionOp] = Field(default_factory=list)
    feasibility_before: str | None = Field(
        default=None, description="Platform feasibility level of the reviewed revision."
    )
    feasibility_after: str | None = None
    notes: list[str] = Field(default_factory=list)


class TacticCard(TacticDraft):
    id: str
    provenance: Provenance = "retrieved"
    derivation: Derivation | None = None
    fills_gap: str | None = None
    support: dict[str, Support] = Field(default_factory=dict)
    score: Score = Field(default_factory=Score)
    change_size: int = 0
    complete: bool = True
    completeness_issues: list[str] = Field(default_factory=list)
    unit_match: float = 0.6
    review_flags: list[str] = Field(
        default_factory=list, description="Points a human must confirm (shown prominently)."
    )
    variants: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    capability_needs: list[CapabilityNeed] = Field(default_factory=list)
    feasibility: FeasibilityInfo | None = None
    revision: int = 1
    previous: list[TacticSnapshot] = Field(
        default_factory=list, description="Earlier revisions (before red-team responses)."
    )


class Gap(BaseModel):
    id: str
    kind: Literal["too_few_retrieved", "uncovered_clause", "dimension"]
    detail: str
    target: str = ""
    fills_needed: int = 1
    filled_by: list[str] = Field(default_factory=list)


class Idea(BaseModel):
    """An innovative idea with its outcome (selected or the reason it was not)."""

    card: TacticCard
    gap: str
    status: Literal["selected", "not_selected", "rejected"]
    reason: str = ""


class ProposalResult(BaseModel):
    format: Literal["TacticCandidates"] = "TacticCandidates"
    format_version: str = "0.1.0"
    requirement: str
    evidence_package: str = ""
    max_candidates: int
    max_innovative: int
    candidates: list[TacticCard] = Field(default_factory=list)
    retrieved_pool: list[TacticCard] = Field(default_factory=list)
    fragments: list[TacticCard] = Field(default_factory=list)
    techniques: list[Technique] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list)
    retrieval_rounds: int = 0
    notes: list[str] = Field(default_factory=list)
    ideas: list[Idea] = Field(default_factory=list)
    prompt_version: str = ""
    capability_gaps: list[CapabilityGap] = Field(default_factory=list)
    catalog_version: str = ""
    capability_granularity: dict[str, Any] | None = None
