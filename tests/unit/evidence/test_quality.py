"""Tests for deterministic source-quality scoring."""

from __future__ import annotations

import pytest

from afsim_research_agent.evidence.quality import (
    RULES_VERSION,
    QualityInput,
    has_doctrine_designation,
    is_blocked_host,
    score_source,
)
from afsim_research_agent.models import SourceMetadata


@pytest.mark.parametrize(
    ("item", "tier", "kind"),
    [
        (
            QualityInput(
                url="https://armypubs.army.mil/epubs/DR_pubs/ATP_3-20.15.pdf",
                title="ATP 3-20.15 Tank Platoon",
                is_pdf=True,
            ),
            "A",
            "official_documentation",
        ),
        (
            QualityInput(
                url="https://upload.wikimedia.org/x/Tank_Platoon_ATP_3-20.15.pdf",
                title="Tank Platoon ATP 3-20.15",
                is_pdf=True,
            ),
            "B",
            "official_documentation",
        ),
        (
            QualityInput(
                url="https://www.mdpi.com/2504-446X/7/1/1/pdf",
                title="Formation control",
                is_pdf=True,
                metadata=SourceMetadata(
                    doi="10.3390/drones7010001",
                    authors=["A"],
                    year=2023,
                    venue="Drones",
                    document_type="article",
                    cited_by_count=42,
                ),
            ),
            "A",
            "paper",
        ),
        (
            QualityInput(url="https://www.rand.org/pubs/research_reports/RR1.html", title="x"),
            "B",
            "institutional_report",
        ),
        (
            QualityInput(url="https://battle-updates.com/update/x/", title="Update"),
            "C",
            "article",
        ),
        (
            QualityInput(url="https://www.youtube.com/watch?v=1", title="Tank formations"),
            "D",
            "forum",
        ),
    ],
)
def test_tiers_follow_published_rules(item: QualityInput, tier: str, kind: str) -> None:
    quality, inferred_kind = score_source(item)

    assert quality.tier == tier
    assert inferred_kind == kind
    assert quality.rules_version == RULES_VERSION
    assert quality.reasons


def test_score_is_deterministic_and_bounded() -> None:
    item = QualityInput(
        url="https://www.nato.int/a.pdf",
        title="AJP-3.2 STANAG 2288",
        is_pdf=True,
        metadata=SourceMetadata(
            doi="10.1/x", authors=["a"], year=2020, venue="v", cited_by_count=500,
            document_type="article",
        ),
    )

    first, _ = score_source(item)
    second, _ = score_source(item)

    assert first == second
    assert 0 <= first.score <= 1


def test_preprints_rank_below_published_articles() -> None:
    base = dict(url="https://arxiv.org/pdf/2401.1", title="t", is_pdf=True)
    article, _ = score_source(
        QualityInput(**base, metadata=SourceMetadata(document_type="article"))
    )
    preprint, _ = score_source(
        QualityInput(**base, metadata=SourceMetadata(document_type="preprint"))
    )

    assert preprint.score < article.score


@pytest.mark.parametrize(
    "text",
    ["FM 3-20.15", "ATP 3-21.71", "AJP-3.2", "STANAG 2116", "JP 3-0", "MCWP 3-10"],
)
def test_doctrine_designations_are_recognized(text: str) -> None:
    assert has_doctrine_designation(f"Manual {text} (2025)")


def test_plain_words_are_not_doctrine() -> None:
    assert not has_doctrine_designation("Fm radio tactics in 2025")


@pytest.mark.parametrize(
    ("url", "blocked"),
    [
        ("https://www.youtube.com/watch?v=1", True),
        ("https://m.youtube.com/watch?v=1", True),
        ("https://youtu.be/1", True),
        ("https://www.army.mil/article/1", False),
    ],
)
def test_blocked_hosts(url: str, blocked: bool) -> None:
    assert is_blocked_host(url) is blocked
