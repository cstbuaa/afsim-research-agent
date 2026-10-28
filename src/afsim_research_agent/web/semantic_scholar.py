"""Semantic Scholar Graph API: search plus citation graph.

Strong coverage of computer science, AI, robotics and multi-agent systems.
Without an API key requests share a public rate limit and may receive 429; a
free key (``SEMANTIC_SCHOLAR_API_KEY``) gives a dedicated 1 request/second.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from afsim_research_agent.models import SearchHit, SourceMetadata
from afsim_research_agent.web.api_client import ApiClient, HttpGetter
from afsim_research_agent.web.search import SearchError

S2_BASE = "https://api.semanticscholar.org/graph/v1"
_FIELDS = ",".join(
    [
        "title",
        "abstract",
        "year",
        "venue",
        "publicationVenue",
        "authors",
        "externalIds",
        "openAccessPdf",
        "isOpenAccess",
        "citationCount",
        "publicationTypes",
        "url",
        "publicationDate",
    ]
)
_MAX_ABSTRACT = 1500


class SemanticScholarProvider:
    name = "semantic_scholar"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        session: HttpGetter | None = None,
        proxy_url: str | None = None,
        max_retries: int = 2,
    ) -> None:
        key = (api_key or "").strip()
        self._client = ApiClient(
            "Semantic Scholar",
            session=session,
            headers={"x-api-key": key} if key else None,
            proxy_url=proxy_url,
            # 1 rps with a key; be gentler on the shared public pool.
            min_interval=1.05 if key else 3.0,
            max_retries=max_retries,
            backoff_seconds=5.0,
        )
        self.has_key = bool(key)

    @classmethod
    def from_environment(cls) -> SemanticScholarProvider:
        return cls(
            api_key=os.getenv("SEMANTIC_SCHOLAR_API_KEY"),
            proxy_url=os.getenv("CROW_HTTP_PROXY") or None,
        )

    def search(self, query: str, *, limit: int = 5) -> list[SearchHit]:
        if not query.strip():
            raise ValueError("query must not be blank")
        payload = self._client.get_json(
            f"{S2_BASE}/paper/search",
            {"query": query.strip(), "limit": max(1, min(limit, 100)), "fields": _FIELDS},
        )
        if not isinstance(payload, Mapping):
            raise SearchError("Semantic Scholar returned an invalid response")
        return _parse_papers(payload.get("data") or [], limit)

    def references(self, paper: str, *, limit: int = 20) -> list[SearchHit]:
        return self._graph(paper, "references", "citedPaper", limit)

    def citations(self, paper: str, *, limit: int = 20) -> list[SearchHit]:
        return self._graph(paper, "citations", "citingPaper", limit)

    def _graph(self, paper: str, edge: str, key: str, limit: int) -> list[SearchHit]:
        payload = self._client.get_json(
            f"{S2_BASE}/paper/{paper}/{edge}",
            {"limit": max(1, min(limit, 100)), "fields": _FIELDS},
        )
        if not isinstance(payload, Mapping):
            raise SearchError("Semantic Scholar returned an invalid response")
        papers = [
            item.get(key) for item in payload.get("data") or [] if isinstance(item, Mapping)
        ]
        return _parse_papers(papers, limit)


def paper_reference(metadata: SourceMetadata) -> str | None:
    """Identifier the Graph API accepts for an already-known work."""
    ids = metadata.external_ids
    if ids.get("s2"):
        return ids["s2"]
    if metadata.doi:
        return f"DOI:{metadata.doi}"
    if ids.get("arxiv"):
        return f"ARXIV:{ids['arxiv']}"
    return None


def _parse_papers(items: list[object], limit: int) -> list[SearchHit]:
    hits: list[SearchHit] = []
    for item in items:
        hit = _parse_paper(item)
        if hit is not None:
            hits.append(hit)
        if len(hits) >= limit:
            break
    return hits


def _parse_paper(item: object) -> SearchHit | None:
    if not isinstance(item, Mapping):
        return None
    title = item.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    external = item.get("externalIds") if isinstance(item.get("externalIds"), Mapping) else {}
    doi = _str(external.get("DOI"))
    arxiv = _str(external.get("ArXiv"))
    oa = item.get("openAccessPdf") if isinstance(item.get("openAccessPdf"), Mapping) else {}
    pdf_url = _str(oa.get("url"))
    landing = _str(item.get("url"))
    if arxiv and not pdf_url:
        pdf_url = f"https://arxiv.org/pdf/{arxiv}"
    url = pdf_url or (f"https://doi.org/{doi}" if doi else None) or landing
    if not url:
        return None
    venue = item.get("publicationVenue")
    venue_name = _str(venue.get("name")) if isinstance(venue, Mapping) else None
    types = item.get("publicationTypes") or []
    abstract = (_str(item.get("abstract")) or "")[:_MAX_ABSTRACT]
    ids = {"s2": str(item.get("paperId"))} if item.get("paperId") else {}
    if doi:
        ids["doi"] = doi.lower()
    if arxiv:
        ids["arxiv"] = arxiv
    year = item.get("year")
    cited = item.get("citationCount")
    is_oa = item.get("isOpenAccess")
    return SearchHit(
        title=" ".join(title.split()),
        url=url,
        snippet=abstract,
        provider="semantic_scholar",
        metadata=SourceMetadata(
            doi=doi.lower() if doi else None,
            authors=[
                a["name"]
                for a in item.get("authors") or []
                if isinstance(a, Mapping) and isinstance(a.get("name"), str)
            ][:20],
            year=year if isinstance(year, int) else None,
            venue=venue_name or _str(item.get("venue")),
            document_type=_map_type(types),
            cited_by_count=cited if isinstance(cited, int) else None,
            is_open_access=(is_oa if isinstance(is_oa, bool) else None) or bool(pdf_url) or None,
            pdf_url=pdf_url,
            landing_url=landing,
            provider_id=ids.get("s2"),
            abstract=abstract or None,
            external_ids=ids,
        ),
    )


def _map_type(types: object) -> str | None:
    values = {str(t).lower() for t in types} if isinstance(types, list) else set()
    if "review" in values:
        return "review"
    if "journalarticle" in values:
        return "article"
    if "conference" in values:
        return "proceedings-article"
    if "booksection" in values:
        return "book-chapter"
    if "book" in values:
        return "book"
    return None


def _str(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
