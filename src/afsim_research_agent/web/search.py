"""Tavily search boundary returning structured URL candidates."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Literal, Protocol

from tavily import TavilyClient

from afsim_research_agent.models import SearchHit

SearchTopic = Literal["general", "news", "finance"]
SearchDepth = Literal["basic", "advanced"]


class SearchError(RuntimeError):
    """Raised when a search provider cannot return a valid response."""


class SearchClient(Protocol):
    """Small subset of the Tavily client used by this module."""

    def search(self, **kwargs: object) -> object:
        """Run a provider search."""


class TavilySearchProvider:
    """Validated adapter around a Tavily SDK client."""

    name = "tavily"

    def __init__(self, client: SearchClient, *, search_depth: SearchDepth = "basic") -> None:
        if search_depth not in {"basic", "advanced"}:
            raise ValueError("search_depth must be basic or advanced")
        self._client = client
        self._search_depth: SearchDepth = search_depth

    @classmethod
    def from_environment(cls) -> TavilySearchProvider:
        """Create a provider from ``TAVILY_API_KEY`` without exposing the key."""
        api_key = os.getenv("TAVILY_API_KEY", "").strip()
        if not api_key:
            raise SearchError("TAVILY_API_KEY is required for web search.")
        depth = os.getenv("TAVILY_SEARCH_DEPTH", "advanced").strip().lower() or "advanced"
        proxy_url = os.getenv("CROW_HTTP_PROXY", "").strip()
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        return cls(
            client=TavilyClient(api_key=api_key, proxies=proxies),
            search_depth=depth,  # type: ignore[arg-type]
        )

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        topic: SearchTopic = "general",
        include_domains: Sequence[str] = (),
        exclude_domains: Sequence[str] = (),
    ) -> list[SearchHit]:
        """Search Tavily and normalize its response into bounded records."""
        normalized_query = _validate_input(query, limit, topic)
        options: dict[str, object] = {}
        if include_domains:
            options["include_domains"] = list(include_domains)
        if exclude_domains:
            options["exclude_domains"] = list(exclude_domains)
        try:
            response = self._client.search(
                query=normalized_query,
                max_results=limit,
                topic=topic,
                search_depth=self._search_depth,
                include_answer=False,
                include_raw_content=False,
                timeout=30.0,
                **options,
            )
        except Exception as exc:
            raise SearchError("Tavily search failed.") from exc
        return _parse_response(response, limit)


def search_web(
    query: str,
    *,
    limit: int = 5,
    topic: SearchTopic = "general",
    provider: TavilySearchProvider | None = None,
    include_domains: Sequence[str] = (),
    exclude_domains: Sequence[str] = (),
) -> list[SearchHit]:
    """Search the web with an injected provider or the configured Tavily client."""
    active_provider = provider or TavilySearchProvider.from_environment()
    return active_provider.search(
        query,
        limit=limit,
        topic=topic,
        include_domains=include_domains,
        exclude_domains=exclude_domains,
    )


def _validate_input(query: str, limit: int, topic: str) -> str:
    normalized_query = query.strip()
    if not normalized_query:
        raise ValueError("query must not be blank")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
        raise ValueError("limit must be an integer between 1 and 10")
    if topic not in {"general", "news", "finance"}:
        raise ValueError("topic must be general, news, or finance")
    return normalized_query


def _parse_response(response: object, limit: int) -> list[SearchHit]:
    if not isinstance(response, Mapping) or "results" not in response:
        raise SearchError("Tavily returned an invalid response.")
    results = response["results"]
    if not isinstance(results, list):
        raise SearchError("Tavily returned an invalid response.")
    return [_parse_hit(item) for item in results[:limit]]


def _parse_hit(item: object) -> SearchHit:
    if not isinstance(item, Mapping):
        raise SearchError("Tavily returned an invalid response.")
    title = item.get("title")
    url = item.get("url")
    if not isinstance(title, str) or not title.strip() or not isinstance(url, str) or not url:
        raise SearchError("Tavily returned an invalid response.")
    snippet = item.get("content", "")
    score = item.get("score")
    if not isinstance(snippet, str) or score is not None and not isinstance(score, (int, float)):
        raise SearchError("Tavily returned an invalid response.")
    return SearchHit(
        title=title.strip(),
        url=url,
        snippet=snippet,
        provider="tavily",
        provider_score=float(score) if score is not None else None,
        published_at=_parse_date(item.get("published_date")),
    )


def _parse_date(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SearchError("Tavily returned an invalid response.")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            return parsedate_to_datetime(value)
        except (TypeError, ValueError) as exc:
            raise SearchError("Tavily returned an invalid response.") from exc
