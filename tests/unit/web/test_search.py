"""Tests for the Tavily-backed web search boundary."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from afsim_research_agent.models import SourceMetadata
from afsim_research_agent.web.search import (
    SearchError,
    TavilySearchProvider,
    search_web,
)


class FakeTavilyClient:
    """Record search arguments and return a fixed provider response."""

    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def search(self, **kwargs: object) -> object:
        """Return the configured response."""
        self.calls.append(kwargs)
        return self.response


class FailingTavilyClient:
    """Stand in for a provider transport failure."""

    def search(self, **kwargs: object) -> object:
        """Raise without exposing implementation details to callers."""
        raise RuntimeError("provider internals")


def test_search_returns_structured_hits_and_safe_provider_options() -> None:
    client = FakeTavilyClient(
        {
            "results": [
                {
                    "title": "Cooperative task allocation",
                    "url": "https://example.org/paper",
                    "content": "A short discovery snippet.",
                    "score": 0.91,
                    "published_date": "2026-09-12T10:30:00Z",
                }
            ]
        }
    )
    provider = TavilySearchProvider(client=client)

    hits = search_web("multi-agent formation tactics", limit=3, provider=provider)

    assert len(hits) == 1
    assert hits[0].model_dump() == {
        "title": "Cooperative task allocation",
        "url": "https://example.org/paper",
        "snippet": "A short discovery snippet.",
        "provider": "tavily",
        "provider_score": 0.91,
        "published_at": datetime(2026, 9, 12, 10, 30, tzinfo=UTC),
        "metadata": SourceMetadata().model_dump(),
    }
    assert client.calls == [
        {
            "query": "multi-agent formation tactics",
            "max_results": 3,
            "topic": "general",
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
            "timeout": 30.0,
        }
    ]


@pytest.mark.parametrize("query", ["", "   "])
def test_search_rejects_blank_query(query: str) -> None:
    provider = TavilySearchProvider(client=FakeTavilyClient({"results": []}))

    with pytest.raises(ValueError, match="query"):
        provider.search(query)


@pytest.mark.parametrize("limit", [0, 11, True])
def test_search_rejects_invalid_result_limit(limit: int) -> None:
    provider = TavilySearchProvider(client=FakeTavilyClient({"results": []}))

    with pytest.raises(ValueError, match="limit"):
        provider.search("formation tactics", limit=limit)


def test_search_requires_api_key_when_building_default_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    with pytest.raises(SearchError, match="TAVILY_API_KEY"):
        search_web("formation tactics")


def test_search_normalizes_provider_failure() -> None:
    provider = TavilySearchProvider(client=FailingTavilyClient())

    with pytest.raises(SearchError, match="Tavily search failed") as raised:
        provider.search("formation tactics")

    assert "provider internals" not in str(raised.value)


@pytest.mark.parametrize("response", [{}, {"results": "invalid"}, {"results": [{}]}])
def test_search_rejects_malformed_provider_response(response: object) -> None:
    provider = TavilySearchProvider(client=FakeTavilyClient(response))

    with pytest.raises(SearchError, match="invalid response"):
        provider.search("formation tactics")


def test_search_caps_untrusted_provider_output() -> None:
    results = [
        {"title": f"Result {index}", "url": f"https://example.org/{index}"} for index in range(4)
    ]
    provider = TavilySearchProvider(client=FakeTavilyClient({"results": results}))

    hits = provider.search("formation tactics", limit=2)

    assert [hit.title for hit in hits] == ["Result 0", "Result 1"]


def test_search_passes_depth_and_domain_filters() -> None:
    client = FakeTavilyClient({"results": []})
    provider = TavilySearchProvider(client=client, search_depth="advanced")

    provider.search("tank platoon formations", include_domains=["army.mil", "nato.int"])

    assert client.calls[0]["search_depth"] == "advanced"
    assert client.calls[0]["include_domains"] == ["army.mil", "nato.int"]
    assert "exclude_domains" not in client.calls[0]


def test_search_rejects_unknown_depth() -> None:
    with pytest.raises(ValueError, match="search_depth"):
        TavilySearchProvider(client=FakeTavilyClient({"results": []}), search_depth="deep")
