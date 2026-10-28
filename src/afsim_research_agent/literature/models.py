"""Project-owned contracts for source collection and PaperQA2 evidence queries."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from afsim_research_agent.models import (
    FailureCategory,
    SourceMetadata,
    SourceQuality,
    SourceReference,
)

CandidateDecision = Literal[
    "accepted",
    "duplicate_url",
    "duplicate_doi",
    "duplicate_content",
    "duplicate_title",
    "rejected",
    "screened_out",
    "failed",
    "skipped_budget",
]
FailureStage = Literal["search", "prefilter", "screening", "fetch", "extract", "ingest"]
CandidateOrigin = Literal["search", "snowball", "library", "supersession"]


class ScreeningResult(BaseModel):
    """LLM judgement of a candidate's title and abstract before download.

    0 = off-topic, 1 = tangential, 2 = relevant, 3 = directly answers a question.
    """

    score: int = Field(ge=0, le=3)
    question_index: int | None = None
    reason: str = ""
    screener: str = ""


class PaperQADocument(BaseModel):
    """One local source supplied to a PaperQA2 query."""

    path: Path
    citation: str
    title: str | None = None
    doi: str | None = None
    authors: list[str] = Field(default_factory=list)
    source_id: str | None = None


class PaperQAEvidence(BaseModel):
    """One evidence context selected by PaperQA2.

    ``score`` is PaperQA2's 0-10 relevance of this excerpt to the question. It
    is not a judgement of source quality; see ``source_quality_tier``.
    """

    citation: str
    excerpt: str
    summary: str
    location: str
    score: int = Field(ge=0, le=10)
    source_id: str | None = None
    source_quality_tier: str | None = None
    source_quality_score: float | None = None


class PaperQAReport(BaseModel):
    """Normalized answer returned to Crow or another orchestrator."""

    question: str
    answer: str
    evidence: list[PaperQAEvidence] = Field(default_factory=list)
    insufficient_evidence: bool = False
    elapsed_seconds: float | None = None


class CorpusIngestFailure(BaseModel):
    """A collected document PaperQA2 could not parse or embed."""

    source_id: str | None
    path: str
    detail: str


class SourceCollectionRequest(BaseModel):
    """Crow-approved search queries and limits for one requirement."""

    requirement: str = Field(min_length=1)
    search_queries: list[str] = Field(default_factory=list)
    scholarly_queries: list[str] = Field(default_factory=list)
    doctrine_queries: list[str] = Field(default_factory=list)
    local_roots: list[Path] = Field(default_factory=list)
    max_web_results: int = Field(default=5, ge=1, le=10)
    max_total_sources: int = Field(default=12, ge=1, le=60)
    max_fetch_attempts: int | None = Field(default=None, ge=1, le=200)
    max_local_documents: int = Field(default=100, ge=1, le=1000)
    research_questions: list[str] = Field(default_factory=list)
    chinese_queries: list[str] = Field(default_factory=list)
    technical_report_queries: list[str] = Field(default_factory=list)
    library_roots: list[Path] = Field(default_factory=list)
    round: int = Field(default=1, ge=1)


class SourceCollectionFailure(BaseModel):
    """A search or source that could not be used, with a diagnosable reason."""

    uri: str
    reason: str
    category: FailureCategory = "unknown"
    stage: FailureStage | None = None
    detail: str | None = None
    status_code: int | None = None
    query: str | None = None


class SourceCandidate(BaseModel):
    """One search result and the deterministic decision taken about it."""

    candidate_id: str
    channel: str
    provider: str
    query: str
    rank: int
    title: str
    url: str
    normalized_url: str
    fetch_url: str
    alternate_urls: list[str] = Field(default_factory=list)
    snippet: str = ""
    provider_score: float | None = None
    metadata: SourceMetadata = Field(default_factory=SourceMetadata)
    preliminary_quality: SourceQuality | None = None
    priority: float | None = None
    decision: CandidateDecision = "skipped_budget"
    reason_category: FailureCategory | None = None
    reason_detail: str | None = None
    http_status: int | None = None
    final_url: str | None = None
    source_id: str | None = None
    duplicate_of: str | None = None
    attempts: int = 0
    elapsed_ms: int | None = None
    round: int = 1
    origin: CandidateOrigin = "search"
    seed_source_id: str | None = None
    screening: ScreeningResult | None = None
    notes: list[str] = Field(default_factory=list)


class SourceCollection(BaseModel):
    """Traceable documents ready for a PaperQA2 evidence query."""

    requirement: str
    queries: list[str]
    documents: list[PaperQADocument] = Field(default_factory=list)
    sources: list[SourceReference] = Field(default_factory=list)
    failures: list[SourceCollectionFailure] = Field(default_factory=list)
    candidates: list[SourceCandidate] = Field(default_factory=list)
    manual_search_queries: list[str] = Field(default_factory=list)
    rounds: int = 1
