"""Minimum evidence gate before a research package may reach Tactical Falcon.

The gate is deliberately simple and deterministic. It does not judge whether
the answer is *right*; it judges whether there is enough independent,
trustworthy, relevant material for a human reviewer and Falcon to work from.
Thresholds are explicit so that a failed gate tells the user exactly what is
missing.
"""

from __future__ import annotations

from dataclasses import dataclass

from afsim_research_agent.crow.dimensions import DIMENSIONS, dimension_label
from afsim_research_agent.crow.models import (
    CrowResearchResult,
    DimensionCoverage,
    QuestionCoverage,
    ReadinessAssessment,
    ReadinessCheck,
)

GATE_VERSION = "falcon-gate-2026.10.2"


@dataclass(frozen=True)
class ReadinessThresholds:
    min_sources: int = 3
    min_authoritative_sources: int = 2
    strong_evidence_score: int = 6
    min_strong_evidence_per_question: int = 2
    min_question_coverage: float = 0.75
    min_authoritative_evidence_share: float = 0.5
    max_insufficient_answer_share: float = 0.25
    min_strong_evidence_per_dimension: int = 1
    min_covered_dimensions: int = 5


def assess_readiness(
    result: CrowResearchResult,
    thresholds: ReadinessThresholds | None = None,
) -> ReadinessAssessment:
    limits = thresholds or ReadinessThresholds()
    sources = {source.source_id: source for source in result.collection.sources}
    authoritative_ids = {
        source_id
        for source_id, source in sources.items()
        if source.quality is not None and source.quality.tier in {"A", "B"}
    }

    coverage: list[QuestionCoverage] = []
    strong_total = 0
    strong_authoritative_total = 0
    for report in result.reports:
        strong = [
            item for item in report.evidence if item.score >= limits.strong_evidence_score
        ]
        authoritative = [item for item in strong if item.source_id in authoritative_ids]
        strong_total += len(strong)
        strong_authoritative_total += len(authoritative)
        coverage.append(
            QuestionCoverage(
                question=report.question,
                strong_evidence=len(strong),
                authoritative_evidence=len(authoritative),
                distinct_sources=len({item.source_id or item.citation for item in strong}),
                insufficient_evidence=report.insufficient_evidence,
            )
        )

    question_count = len(result.plan.research_questions)
    covered = sum(
        1
        for item in coverage
        if item.strong_evidence >= limits.min_strong_evidence_per_question
        and not item.insufficient_evidence
    )
    coverage_ratio = covered / question_count if question_count else 0.0
    authoritative_share = (
        strong_authoritative_total / strong_total if strong_total else 0.0
    )
    insufficient_share = (
        sum(1 for item in coverage if item.insufficient_evidence) / question_count
        if question_count
        else 1.0
    )
    usable_sources = len(sources) - len(result.ingest_failures)

    checks = [
        ReadinessCheck(
            name="usable_sources",
            passed=usable_sources >= limits.min_sources,
            value=usable_sources,
            threshold=limits.min_sources,
            detail="可被 PaperQA 解析的来源数量",
        ),
        ReadinessCheck(
            name="authoritative_sources",
            passed=len(authoritative_ids) >= limits.min_authoritative_sources,
            value=len(authoritative_ids),
            threshold=limits.min_authoritative_sources,
            detail="A/B 级（官方、学术或机构）来源数量",
        ),
        ReadinessCheck(
            name="question_coverage",
            passed=coverage_ratio >= limits.min_question_coverage,
            value=round(coverage_ratio, 3),
            threshold=limits.min_question_coverage,
            detail=(
                f"至少 {limits.min_strong_evidence_per_question} 条相关性 ≥ "
                f"{limits.strong_evidence_score}/10 证据且未声明资料不足的问题占比"
            ),
        ),
        ReadinessCheck(
            name="authoritative_evidence_share",
            passed=authoritative_share >= limits.min_authoritative_evidence_share,
            value=round(authoritative_share, 3),
            threshold=limits.min_authoritative_evidence_share,
            detail="强证据中来自 A/B 级来源的比例",
        ),
        ReadinessCheck(
            name="insufficient_answers",
            passed=insufficient_share <= limits.max_insufficient_answer_share,
            value=round(insufficient_share, 3),
            threshold=limits.max_insufficient_answer_share,
            detail="PaperQA 回答中明确表示资料不足的问题占比（启发式识别，上限）",
        ),
    ]
    dimensions = _dimension_coverage(result, coverage, limits)
    if dimensions:
        covered_dimensions = sum(1 for item in dimensions if item.covered)
        checks.append(
            ReadinessCheck(
                name="dimension_coverage",
                passed=covered_dimensions >= limits.min_covered_dimensions,
                value=covered_dimensions,
                threshold=limits.min_covered_dimensions,
                detail=(
                    f"至少有 {limits.min_strong_evidence_per_dimension} 条强证据的研究维度数"
                    f"（共 {len(DIMENSIONS)} 个维度）"
                ),
            )
        )
    return ReadinessAssessment(
        ready=all(check.passed for check in checks),
        checks=checks,
        coverage=coverage,
        dimensions=dimensions,
        gate_version=GATE_VERSION,
    )


def _dimension_coverage(
    result: CrowResearchResult,
    coverage: list[QuestionCoverage],
    limits: ReadinessThresholds,
) -> list[DimensionCoverage]:
    """Strong evidence per research dimension.

    A question's strong evidence counts toward every dimension it is tagged
    with. Plans from before dimension tagging return an empty list and the
    check is skipped.
    """
    plan = result.plan
    if not plan.question_dimensions:
        return []
    strong = {item.question: item.strong_evidence for item in coverage}
    rows: list[DimensionCoverage] = []
    for dimension in DIMENSIONS:
        questions = [q for q in plan.research_questions if dimension in plan.dimensions_of(q)]
        total = sum(strong.get(q, 0) for q in questions)
        rows.append(
            DimensionCoverage(
                dimension=dimension,
                label=dimension_label(dimension),
                questions=questions,
                strong_evidence=total,
                covered=total >= limits.min_strong_evidence_per_dimension,
            )
        )
    return rows
