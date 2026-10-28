"""State contracts for the Tactical Crow research stage."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from afsim_research_agent.crow.dimensions import Dimension
from afsim_research_agent.crow.mission import MissionProfile
from afsim_research_agent.literature.models import (
    CorpusIngestFailure,
    PaperQAReport,
    SourceCollection,
)


class PlannedQuestion(BaseModel):
    question: str = Field(min_length=1)
    dimensions: list[Dimension] = Field(default_factory=list)


class PlannerDraft(BaseModel):
    """What the language model is asked to produce (structured output schema)."""

    requirement_summary: str = Field(min_length=1)
    research_questions: list[PlannedQuestion] = Field(min_length=1)
    pattern_hypotheses: list[str] = Field(default_factory=list)
    analogy_queries: list[str] = Field(default_factory=list)
    search_queries: list[str] = Field(min_length=1)
    scholarly_queries: list[str] = Field(default_factory=list)
    doctrine_queries: list[str] = Field(default_factory=list)
    technical_report_queries: list[str] = Field(default_factory=list)
    chinese_queries: list[str] = Field(default_factory=list)
    key_terms: list[str] = Field(default_factory=list)

    @field_validator("research_questions", mode="before")
    @classmethod
    def _accept_plain_strings(cls, value: object) -> object:
        if isinstance(value, list):
            return [{"question": v} if isinstance(v, str) else v for v in value]
        return value


class FollowupDraft(BaseModel):
    """Targeted queries for research questions the first round did not cover."""

    search_queries: list[str] = Field(default_factory=list)
    scholarly_queries: list[str] = Field(default_factory=list)
    doctrine_queries: list[str] = Field(default_factory=list)


class TacticalResearchPlan(BaseModel):
    """A requirement decomposed into evidence questions and search queries.

    Query lists are not length-validated; the planner truncates them so an
    over-eager model cannot fail the whole run.
    """

    requirement_summary: str = Field(min_length=1)
    research_questions: list[str] = Field(min_length=1, max_length=6)
    question_dimensions: list[PlannedQuestion] = Field(default_factory=list)
    pattern_hypotheses: list[str] = Field(default_factory=list)
    analogy_queries: list[str] = Field(default_factory=list)
    search_queries: list[str] = Field(min_length=1)
    scholarly_queries: list[str] = Field(default_factory=list)
    doctrine_queries: list[str] = Field(default_factory=list)
    technical_report_queries: list[str] = Field(default_factory=list)
    chinese_queries: list[str] = Field(default_factory=list)
    key_terms: list[str] = Field(default_factory=list)
    matched_concepts: list[str] = Field(default_factory=list)
    planning_notes: list[str] = Field(default_factory=list)
    followup_rounds: list[FollowupDraft] = Field(default_factory=list)

    def dimensions_of(self, question: str) -> list[str]:
        for item in self.question_dimensions:
            if item.question == question:
                return list(item.dimensions)
        return []


class StageTiming(BaseModel):
    stage: str
    seconds: float


class RunManifest(BaseModel):
    """Everything needed to explain and repeat one research run (no secrets)."""

    run_id: str
    started_at: datetime
    finished_at: datetime | None = None
    requirement_sha256: str
    package_version: str
    python_version: str
    platform: str
    dependency_versions: dict[str, str] = Field(default_factory=dict)
    git_commit: str | None = None
    planner_prompt_version: str
    quality_rules_version: str
    chat_model: str
    embedding_model: str
    embedding_dimension: int
    paperqa_settings: dict[str, object] = Field(default_factory=dict)
    search_settings: dict[str, object] = Field(default_factory=dict)
    timings: list[StageTiming] = Field(default_factory=list)


class ReadinessCheck(BaseModel):
    name: str
    passed: bool
    value: float
    threshold: float
    detail: str


class QuestionCoverage(BaseModel):
    question: str
    strong_evidence: int
    authoritative_evidence: int
    distinct_sources: int
    insufficient_evidence: bool


class DimensionCoverage(BaseModel):
    dimension: str
    label: str
    questions: list[str] = Field(default_factory=list)
    strong_evidence: int = 0
    covered: bool = False


class ReadinessAssessment(BaseModel):
    """Whether this evidence package may be handed to Tactical Falcon."""

    ready: bool
    checks: list[ReadinessCheck] = Field(default_factory=list)
    coverage: list[QuestionCoverage] = Field(default_factory=list)
    dimensions: list[DimensionCoverage] = Field(default_factory=list)
    gate_version: str


class CrowResearchResult(BaseModel):
    """Auditable output before tactical behavior synthesis."""

    plan: TacticalResearchPlan
    mission_profile: MissionProfile | None = None
    collection: SourceCollection
    reports: list[PaperQAReport] = Field(default_factory=list)
    ingest_failures: list[CorpusIngestFailure] = Field(default_factory=list)
    readiness: ReadinessAssessment | None = None
    manifest: RunManifest | None = None
