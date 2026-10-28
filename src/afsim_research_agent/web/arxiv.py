"""arXiv API search (no key). arXiv asks clients to wait ~3 s between calls."""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime

from afsim_research_agent.models import SearchHit, SourceMetadata
from afsim_research_agent.web.api_client import ApiClient, HttpGetter
from afsim_research_agent.web.search import SearchError

ARXIV_API = "https://export.arxiv.org/api/query"
_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}
_STOPWORDS = frozenset(
    {"a", "an", "and", "the", "of", "for", "in", "on", "to", "with", "by", "via", "using", "under"}
)
_ID = re.compile(r"arxiv\.org/abs/(?P<id>[^v\s]+?)(?:v\d+)?$")


class ArxivProvider:
    name = "arxiv"

    def __init__(
        self,
        *,
        session: HttpGetter | None = None,
        proxy_url: str | None = None,
        max_retries: int = 2,
    ) -> None:
        self._client = ApiClient(
            "arXiv",
            session=session,
            proxy_url=proxy_url,
            min_interval=3.0,
            max_retries=max_retries,
            backoff_seconds=5.0,
        )

    @classmethod
    def from_environment(cls) -> ArxivProvider:
        return cls(proxy_url=os.getenv("CROW_HTTP_PROXY") or None)

    def search(self, query: str, *, limit: int = 5) -> list[SearchHit]:
        expression = build_query(query)
        if not expression:
            raise ValueError("query must not be blank")
        response = self._client.get(
            ARXIV_API,
            {
                "search_query": expression,
                "start": 0,
                "max_results": max(1, min(limit, 50)),
                "sortBy": "relevance",
            },
        )
        return parse_feed(response.content if hasattr(response, "content") else b"", limit)


def build_query(query: str) -> str:
    """Require every content word in title or abstract (arXiv has no ranking-only mode)."""
    words = [
        word
        for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9-]+", query)
        if word.lower() not in _STOPWORDS
    ][:6]
    return " AND ".join(f"all:{word}" for word in words)


def parse_feed(content: bytes, limit: int) -> list[SearchHit]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise SearchError("arXiv returned invalid XML") from exc
    hits: list[SearchHit] = []
    for entry in root.findall("atom:entry", _NS):
        hit = _parse_entry(entry)
        if hit is not None:
            hits.append(hit)
        if len(hits) >= limit:
            break
    return hits


def _parse_entry(entry: ET.Element) -> SearchHit | None:
    title = " ".join((entry.findtext("atom:title", "", _NS) or "").split())
    abs_url = (entry.findtext("atom:id", "", _NS) or "").strip()
    match = _ID.search(abs_url)
    if not title or not match:
        return None
    arxiv_id = match.group("id")
    pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
    for link in entry.findall("atom:link", _NS):
        if link.get("title") == "pdf" and link.get("href"):
            pdf_url = link.get("href", pdf_url).replace("http://", "https://")
    summary = " ".join((entry.findtext("atom:summary", "", _NS) or "").split())[:1500]
    published = entry.findtext("atom:published", "", _NS)
    try:
        published_at = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except ValueError:
        published_at = None
    doi = (entry.findtext("arxiv:doi", "", _NS) or "").strip().lower() or None
    journal = (entry.findtext("arxiv:journal_ref", "", _NS) or "").strip() or None
    ids = {"arxiv": arxiv_id}
    if doi:
        ids["doi"] = doi
    return SearchHit(
        title=title,
        url=pdf_url,
        snippet=summary,
        provider="arxiv",
        published_at=published_at,
        metadata=SourceMetadata(
            doi=doi,
            authors=[
                " ".join((a.findtext("atom:name", "", _NS) or "").split())
                for a in entry.findall("atom:author", _NS)
            ][:20],
            year=published_at.year if published_at else None,
            venue=journal or "arXiv",
            document_type="article" if journal or doi else "preprint",
            is_open_access=True,
            pdf_url=pdf_url,
            landing_url=f"https://arxiv.org/abs/{arxiv_id}",
            provider_id=arxiv_id,
            abstract=summary or None,
            external_ids=ids,
        ),
    )
