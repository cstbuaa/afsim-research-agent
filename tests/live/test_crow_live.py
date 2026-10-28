"""Opt-in smoke tests against real search, download and model APIs.

They cost API credits and depend on the network, so they never run by default:

    pytest -m live -s tests/live

Each test skips itself when the keys it needs are missing.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True, scope="module")
def _load_env() -> None:
    # Loaded only when live tests actually run; loading at import time leaked
    # .env values into unit tests during collection.
    load_dotenv()


def require(*names: str) -> None:
    missing = [name for name in names if not os.getenv(name, "").strip()]
    if missing:
        pytest.skip(f"missing {', '.join(missing)}")


def test_openalex_returns_metadata_rich_hits() -> None:
    from afsim_research_agent.web.scholarly import OpenAlexSearchProvider

    hits = OpenAlexSearchProvider.from_environment().search(
        "multi-vehicle formation control", limit=3
    )

    assert hits
    assert any(hit.metadata.doi for hit in hits)


def test_fetcher_sniffs_pdf_without_suffix(tmp_path: Path) -> None:
    from afsim_research_agent.config import Settings
    from afsim_research_agent.literature.collection import build_fetch_policy
    from afsim_research_agent.web.fetch import fetch_url

    resource = fetch_url(
        "https://arxiv.org/pdf/1706.03762",
        policy=build_fetch_policy(Settings.from_environment()),
    )

    assert resource.kind == "pdf"


def test_tavily_doctrine_channel_returns_hits() -> None:
    require("TAVILY_API_KEY")
    from afsim_research_agent.web.search import TavilySearchProvider

    hits = TavilySearchProvider.from_environment().search(
        "tank platoon formations doctrine", limit=3, include_domains=["army.mil", "dtic.mil"]
    )

    assert hits


def test_full_crow_run_produces_complete_ledger(tmp_path: Path) -> None:
    require("TAVILY_API_KEY", "DASHSCOPE_API_KEY")
    from afsim_research_agent.crow.cli import run_research_with_result

    (markdown, json_path), result = asyncio.run(
        run_research_with_result(
            "研究装甲排在开阔地形与城市地形下的编队方式",
            output_dir=tmp_path,
            max_web_results=2,
            max_total_sources=4,
        )
    )

    assert markdown.is_file() and json_path.is_file()
    assert result.collection.candidates
    assert all(candidate.decision for candidate in result.collection.candidates)
    assert result.manifest is not None and result.readiness is not None
    print(markdown.read_text(encoding="utf-8")[:3000])


def test_arxiv_returns_hits() -> None:
    from afsim_research_agent.web.arxiv import ArxivProvider

    assert ArxivProvider.from_environment().search("behavior trees robotics", limit=2)


def test_semantic_scholar_returns_hits() -> None:
    require("SEMANTIC_SCHOLAR_API_KEY")  # the shared keyless pool is usually rate-limited
    from afsim_research_agent.web.semantic_scholar import SemanticScholarProvider

    assert SemanticScholarProvider.from_environment().search("formation control survey", limit=2)


def test_gold_dois_resolve() -> None:
    import argparse

    from afsim_research_agent.evaluation.retrieval import verify

    gold = Path(__file__).resolve().parents[2] / "eval" / "retrieval_gold.toml"
    assert verify(argparse.Namespace(gold=gold)) == 0
