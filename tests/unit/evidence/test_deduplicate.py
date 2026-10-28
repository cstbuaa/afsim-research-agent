"""Tests for URL and DOI identity keys."""

from __future__ import annotations

import pytest

from afsim_research_agent.evidence.deduplicate import (
    normalize_doi,
    normalize_url,
    preferred_fetch_url,
)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("http://www.Example.org/a/?utm_source=x#frag", "https://example.org/a"),
        ("https://example.org/a?b=2&a=1", "https://example.org/a?a=1&b=2"),
        ("https://arxiv.org/abs/2401.01234v2", "https://arxiv.org/pdf/2401.01234.pdf"),
        (
            "https://www.youtube.com/watch?v=fG73&xstg=CAMS",
            "https://youtube.com/watch?v=fG73",
        ),
    ],
)
def test_equivalent_urls_share_a_key(left: str, right: str) -> None:
    assert normalize_url(left) == normalize_url(right)


def test_meaningful_query_parameters_are_kept() -> None:
    assert normalize_url("https://e.org/view?id=1") != normalize_url("https://e.org/view?id=2")


def test_arxiv_abstract_pages_fetch_the_pdf() -> None:
    assert preferred_fetch_url("https://arxiv.org/abs/2401.01234") == (
        "https://arxiv.org/pdf/2401.01234"
    )
    assert preferred_fetch_url("https://example.org/abs/1") == "https://example.org/abs/1"


@pytest.mark.parametrize(
    "value",
    ["10.1000/ABC", "https://doi.org/10.1000/abc", "doi:10.1000/abc", " 10.1000/abc "],
)
def test_doi_normalization(value: str) -> None:
    assert normalize_doi(value) == "10.1000/abc"
