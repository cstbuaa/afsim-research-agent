"""CORE (core.ac.uk) search: aggregated open-access full texts from repositories.

Useful for theses, institutional repository copies (e.g. NPS Calhoun) and
author-archived versions of paywalled papers. Requires a free API key.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from afsim_research_agent.models import SearchHit, SourceMetadata
from afsim_research_agent.web.api_client import ApiClient, HttpGetter
from afsim_research_agent.web.search import SearchError

CORE_SEARCH = "https://api.core.ac.uk/v3/search/works"


class CoreProvider:
    name = "core"

    def __init__(
        self,
        *,
        api_key: str,
        session: HttpGetter | None = None,
        proxy_url: str | None = None,
        max_retries: int = 2,
    ) -> None:
        if not api_key.strip():
            raise SearchError("CORE_API_KEY is required for CORE search.")
        self._client = ApiClient(
            "CORE",
            session=session,
            headers={"Authorization": f"Bearer {api_key.strip()}"},
            proxy_url=proxy_url,
            min_interval=1.0,
            max_retries=max_retries,
            backoff_seconds=5.0,
        )

    @classmethod
    def from_environment(cls) -> CoreProvider:
        return cls(
            api_key=os.getenv("CORE_API_KEY", ""),
            proxy_url=os.getenv("CROW_HTTP_PROXY") or None,
        )

    def search(self, query: str, *, limit: int = 5) -> list[SearchHit]:
        if not query.strip():
            raise ValueError("query must not be blank")
        payload = self._client.get_json(
            CORE_SEARCH, {"q": query.strip(), "limit": max(1, min(limit, 50))}
        )
        if not isinstance(payload, Mapping) or not isinstance(payload.get("results"), list):
            raise SearchError("CORE returned an invalid response")
        hits: list[SearchHit] = []
        for item in payload["results"]:
            hit = _parse(item)
            if hit is not None:
                hits.append(hit)
            if len(hits) >= limit:
                break
        return hits


def _parse(item: object) -> SearchHit | None:
    if not isinstance(item, Mapping):
        return None
    title = item.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    download = _str(item.get("downloadUrl"))
    doi = (_str(item.get("doi")) or "").lower() or None
    links = [
        _str(link.get("url"))
        for link in item.get("links") or []
        if isinstance(link, Mapping)
    ]
    landing = next((link for link in links if link), None)
    url = download or (f"https://doi.org/{doi}" if doi else None) or landing
    if not url:
        return None
    journals = item.get("journals") or []
    venue = None
    if journals and isinstance(journals[0], Mapping):
        venue = _str(journals[0].get("title"))
    year = item.get("yearPublished")
    abstract = (_str(item.get("abstract")) or "")[:1500]
    ids = {"core": str(item.get("id"))} if item.get("id") else {}
    if doi:
        ids["doi"] = doi
    doc_type = (_str(item.get("documentType")) or "").lower()
    return SearchHit(
        title=" ".join(title.split()),
        url=url,
        snippet=abstract,
        provider="core",
        metadata=SourceMetadata(
            doi=doi,
            authors=[
                a["name"]
                for a in item.get("authors") or []
                if isinstance(a, Mapping) and isinstance(a.get("name"), str)
            ][:20],
            year=year if isinstance(year, int) else None,
            venue=venue,
            publisher=_str(item.get("publisher")),
            document_type={"thesis": "dissertation", "research": "article"}.get(doc_type)
            or (doc_type or None),
            is_open_access=bool(download) or None,
            pdf_url=download,
            landing_url=landing,
            provider_id=ids.get("core"),
            abstract=abstract or None,
            external_ids=ids,
        ),
    )


def _str(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
