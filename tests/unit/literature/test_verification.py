"""Tests for DOI metadata verification."""

from __future__ import annotations

from afsim_research_agent.literature.models import SourceCandidate
from afsim_research_agent.literature.verification import MetadataVerifier, title_similarity
from afsim_research_agent.models import SearchHit, SourceMetadata


def candidate(title: str, doi: str | None = "10.1/x") -> SourceCandidate:
    return SourceCandidate(
        candidate_id="c1", channel="scholarly", provider="semantic_scholar", query="q", rank=1,
        title=title, url="https://e.org", normalized_url="https://e.org", fetch_url="https://e.org",
        metadata=SourceMetadata(doi=doi, external_ids={"doi": doi} if doi else {}),
    )


def record(title: str, **meta: object) -> SearchHit:
    return SearchHit(
        title=title, url="https://doi.org/10.1/x", provider="openalex",
        metadata=SourceMetadata(external_ids={"openalex": "W9"}, **meta),
    )


def test_matching_record_fills_metadata_and_flags_retraction() -> None:
    calls: list[str] = []
    verifier = MetadataVerifier(
        lambda doi: calls.append(doi)
        or record("Flocking for multi-agent dynamic systems", year=2006, venue="IEEE TAC",
                  cited_by_count=9000, is_retracted=False)
    )
    item = candidate("Flocking for Multi-Agent Dynamic Systems: Algorithms and Theory")

    verifier.verify(item)
    verifier.verify(item)  # already verified: no second lookup

    assert calls == ["10.1/x"]
    assert item.metadata.verified_by == ["openalex"]
    assert (item.metadata.year, item.metadata.venue) == (2006, "IEEE TAC")
    assert item.metadata.external_ids["openalex"] == "W9"
    assert item.metadata.is_retracted is False


def test_mismatched_doi_is_detached() -> None:
    item = candidate("Tank platoon formation control")

    MetadataVerifier(lambda doi: record("Protein folding in yeast")).verify(item)

    assert item.metadata.doi is None
    assert "doi" not in item.metadata.external_ids
    assert "different work" in item.notes[0]


def test_unknown_doi_and_lookup_failure_are_noted() -> None:
    missing = candidate("A title long enough to compare")
    MetadataVerifier(lambda doi: None).verify(missing)
    assert missing.metadata.doi is None

    def boom(doi: str) -> SearchHit:
        raise RuntimeError("down")

    failing = candidate("A title long enough to compare")
    MetadataVerifier(boom).verify(failing)
    assert failing.metadata.doi == "10.1/x"
    assert "could not be verified" in failing.notes[0]


def test_title_similarity_handles_subtitles_and_chinese() -> None:
    assert title_similarity(
        "Behavior Trees in Robotics and AI", "Behavior trees in robotics and AI: An introduction"
    ) == 1.0
    assert title_similarity("多无人机协同编队控制", "多无人机协同编队控制方法研究") >= 0.8
    assert title_similarity("", "x") == 0.0
