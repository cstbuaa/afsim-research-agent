"""Shared data contracts for retrieval and evidence processing."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

SourceKind = Literal[
    "official_documentation",
    "standard",
    "paper",
    "institutional_report",
    "vendor_documentation",
    "article",
    "forum",
    "local_code",
    "local_document",
    "unknown",
]
EvidenceStatus = Literal["explicit", "inferred", "conflicting", "unknown"]
QualityTier = Literal["A", "B", "C", "D"]
FailureCategory = Literal[
    "blocked_domain",
    "security_rejected",
    "dns_error",
    "timeout",
    "network_error",
    "host_circuit_open",
    "rate_limited",
    "http_error",
    "too_large",
    "unsupported_content_type",
    "invalid_pdf",
    "no_extractable_text",
    "not_open_access",
    "retracted",
    "screened_out",
    "search_failed",
    "ingest_failed",
    "unknown",
]


class SourceMetadata(BaseModel):
    """Bibliographic metadata reported by a provider or parsed from a document.

    Every field is optional: metadata is evidence about a source, never a
    requirement for collecting it.
    """

    doi: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    publisher: str | None = None
    institutions: list[str] = Field(default_factory=list)
    document_type: str | None = None
    language: str | None = None
    cited_by_count: int | None = None
    is_open_access: bool | None = None
    pdf_url: str | None = None
    landing_url: str | None = None
    provider_id: str | None = None
    abstract: str | None = None
    external_ids: dict[str, str] = Field(default_factory=dict)
    is_retracted: bool | None = None
    verified_by: list[str] = Field(default_factory=list)


class SourceQuality(BaseModel):
    """Deterministic, rule-based source quality.

    This is independent of PaperQA's per-question relevance score: it says how
    much a source can be trusted, not how relevant one excerpt is.
    """

    score: float = Field(ge=0, le=1)
    tier: QualityTier
    reasons: list[str] = Field(default_factory=list)
    rules_version: str


class SourceReference(BaseModel):
    """Identity and provenance for one external or local source."""

    source_id: str
    kind: SourceKind = "unknown"
    title: str
    uri: str
    published_at: datetime | None = None
    retrieved_at: datetime
    content_hash: str
    metadata: SourceMetadata = Field(default_factory=SourceMetadata)
    quality: SourceQuality | None = None
    discovered_by: list[str] = Field(default_factory=list)


class SearchHit(BaseModel):
    """A URL candidate returned by a search provider."""

    title: str
    url: str
    snippet: str = ""
    provider: str
    provider_score: float | None = None
    published_at: datetime | None = None
    metadata: SourceMetadata = Field(default_factory=SourceMetadata)


class WebDocument(BaseModel):
    """Normalized webpage content retained before evidence extraction."""

    source: SourceReference
    text: str
    paragraph_ids: list[str] = Field(default_factory=list)
    status_code: int


class KnowledgeChunk(BaseModel):
    """A stable, traceable unit stored in the local knowledge index."""

    chunk_id: str
    source: SourceReference
    text: str
    location: str
    metadata: dict[str, str] = Field(default_factory=dict)


class RetrievalHit(BaseModel):
    """A local retrieval result with separate component ranks."""

    chunk: KnowledgeChunk
    lexical_rank: int | None = None
    semantic_rank: int | None = None
    fused_score: float


class Evidence(BaseModel):
    """A claim grounded in an exact source excerpt."""

    evidence_id: str
    claim: str
    excerpt: str
    source: SourceReference
    location: str
    relevance_score: float = Field(ge=0, le=1)
    quality_score: float = Field(ge=0, le=1)
    status: EvidenceStatus


class EvidenceBundle(BaseModel):
    """Merged evidence for one normalized claim."""

    claim: str
    supporting: list[Evidence] = Field(default_factory=list)
    conflicting: list[Evidence] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
