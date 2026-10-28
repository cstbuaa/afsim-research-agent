"""Tests for the Crow → Falcon evidence gate."""

from __future__ import annotations

from crow_fixtures import evidence, result, source

from afsim_research_agent.crow.readiness import ReadinessThresholds, assess_readiness
from afsim_research_agent.literature import PaperQAReport


def checks(assessment) -> dict[str, bool]:
    return {check.name: check.passed for check in assessment.checks}


def test_well_supported_package_passes() -> None:
    sources = [source("a", "A"), source("b", "B"), source("c", "C", 0.4)]
    reports = [
        PaperQAReport(
            question=q,
            answer="Wedge (A2020).",
            evidence=[evidence("a", 8), evidence("b", 7), evidence("c", 3)],
        )
        for q in ["q1", "q2"]
    ]

    assessment = assess_readiness(
        result(questions=["q1", "q2"], sources=sources, reports=reports)
    )

    assert assessment.ready
    assert all(checks(assessment).values())
    assert [c.distinct_sources for c in assessment.coverage] == [2, 2]


def test_last_real_run_shape_fails_with_clear_reasons() -> None:
    # Mirrors 2026-10-12 run: 2 sources (mirror PDF + trade article), weak evidence.
    sources = [source("pdf", "B", 0.58), source("web", "C", 0.35)]
    reports = [
        PaperQAReport(
            question="q1",
            answer="I cannot answer.",
            evidence=[evidence("pdf", 2), evidence("web", 7)],
            insufficient_evidence=True,
        ),
        PaperQAReport(question="q2", answer="x", evidence=[evidence("pdf", 4)]),
    ]

    assessment = assess_readiness(
        result(questions=["q1", "q2"], sources=sources, reports=reports)
    )

    assert not assessment.ready
    assert checks(assessment) == {
        "usable_sources": False,
        "authoritative_sources": False,
        "question_coverage": False,
        "authoritative_evidence_share": False,
        "insufficient_answers": False,
    }


def test_thresholds_are_configurable() -> None:
    sources = [source("a", "A")]
    reports = [PaperQAReport(question="q", answer="x", evidence=[evidence("a", 9)])]
    lenient = ReadinessThresholds(
        min_sources=1, min_authoritative_sources=1, min_strong_evidence_per_question=1
    )

    built = result(questions=["q"], sources=sources, reports=reports)

    assert assess_readiness(built, lenient).ready


def test_empty_result_is_not_ready() -> None:
    assessment = assess_readiness(result())

    assert not assessment.ready
    assert assessment.coverage == []


def test_dimension_coverage_check_counts_tagged_questions() -> None:
    from afsim_research_agent.crow.models import PlannedQuestion

    sources = [source("a", "A"), source("b", "B"), source("c", "C", 0.4)]
    strong = [evidence("a", 8), evidence("b", 7)]
    reports = [
        PaperQAReport(question="q1", answer="x (A2020).", evidence=strong),
        PaperQAReport(question="q2", answer="x (A2020).", evidence=strong),
        PaperQAReport(question="q3", answer="x (A2020).", evidence=[evidence("c", 3)]),
    ]
    package = result(questions=["q1", "q2", "q3"], sources=sources, reports=reports)
    package.plan.question_dimensions = [
        PlannedQuestion(question="q1", dimensions=["applicability", "roles_geometry"]),
        PlannedQuestion(question="q2", dimensions=["sequence_termination", "exceptions_abort"]),
        PlannedQuestion(question="q3", dimensions=["coordination_authority", "effectiveness"]),
    ]

    assessment = assess_readiness(package)

    rows = {row.dimension: row for row in assessment.dimensions}
    assert rows["roles_geometry"].strong_evidence == 2 and rows["roles_geometry"].covered
    assert not rows["effectiveness"].covered and rows["effectiveness"].questions == ["q3"]
    check = next(c for c in assessment.checks if c.name == "dimension_coverage")
    assert (check.value, check.threshold, check.passed) == (4, 5, False)

    relaxed = assess_readiness(package, ReadinessThresholds(min_covered_dimensions=4))
    assert checks(relaxed)["dimension_coverage"]


def test_plans_without_dimension_tags_skip_the_dimension_check() -> None:
    assessment = assess_readiness(result())

    assert assessment.dimensions == []
    assert "dimension_coverage" not in checks(assessment)
