"""Builders for Crow results used across tests."""

from __future__ import annotations

from datetime import UTC, datetime

from afsim_research_agent.crow.models import CrowResearchResult, TacticalResearchPlan
from afsim_research_agent.literature import (
    PaperQAEvidence,
    PaperQAReport,
    SourceCollection,
    SourceCollectionFailure,
)
from afsim_research_agent.literature.models import SourceCandidate
from afsim_research_agent.models import SourceQuality, SourceReference


def source(source_id: str, tier: str, score: float = 0.8) -> SourceReference:
    return SourceReference(
        source_id=source_id,
        kind="paper",
        title=f"Title {source_id}",
        uri=f"https://example.org/{source_id}",
        retrieved_at=datetime(2026, 10, 12, tzinfo=UTC),
        content_hash=f"hash-{source_id}",
        quality=SourceQuality(score=score, tier=tier, reasons=["test"], rules_version="t"),
    )


def evidence(source_id: str, score: int) -> PaperQAEvidence:
    return PaperQAEvidence(
        citation=f"Citation {source_id}",
        excerpt="The platoon adopts a wedge when contact is likely.",
        summary="Wedge when contact likely.",
        location="pages 3-4",
        score=score,
        source_id=source_id,
    )


def result(
    *,
    questions: list[str] | None = None,
    sources: list[SourceReference] | None = None,
    reports: list[PaperQAReport] | None = None,
    candidates: list[SourceCandidate] | None = None,
    failures: list[SourceCollectionFailure] | None = None,
) -> CrowResearchResult:
    questions = questions or ["什么条件触发编队转换？"]
    return CrowResearchResult(
        plan=TacticalResearchPlan(
            requirement_summary="研究威胁接近时的编队转换。",
            research_questions=questions,
            search_queries=["formation transition threat response"],
            scholarly_queries=["formation switching threat"],
            doctrine_queries=["ATP 3-20.15 formations"],
            technical_report_queries=["formation transition thesis"],
            chinese_queries=["编队 队形转换"],
            key_terms=["formation transition"],
        ),
        collection=SourceCollection(
            requirement="研究四机协同防御编队转换",
            queries=["formation transition threat response"],
            sources=sources or [],
            failures=failures or [],
            candidates=candidates or [],
            manual_search_queries=["编队 队形转换"],
        ),
        reports=reports or [],
    )
