"""OpenAlex scholarly search returning metadata-rich URL candidates.

OpenAlex indexes journals, conference papers, technical reports, theses and
arXiv preprints, and reports DOI, authors, institutions, venue, citation count
and open-access full-text links. Those fields feed deterministic source-quality
scoring; nothing here is decided by a language model.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import datetime

from afsim_research_agent.models import SearchHit, SourceMetadata
from afsim_research_agent.web.api_client import ApiClient, HttpGetter
from afsim_research_agent.web.search import SearchError

OPENALEX_WORKS_URL = "https://api.openalex.org/works"
# Work types that can carry tactical or technical evidence.
_ALLOWED_TYPES = (
    "article",
    "book-chapter",
    "book",
    "report",
    "preprint",
    "dissertation",
    "review",
    "standard",
)
_SELECT_FIELDS = ",".join(
    [
        "id",
        "doi",
        "display_name",
        "publication_year",
        "publication_date",
        "type",
        "language",
        "cited_by_count",
        "authorships",
        "primary_location",
        "best_oa_location",
        "open_access",
        "abstract_inverted_index",
        "is_retracted",
    ]
)
_MAX_SNIPPET_CHARS = 1500


class OpenAlexSearchProvider:
    """Adapter around OpenAlex ``/works``: search, DOI lookup and citation graph.

    Cost note (OpenAlex usage pricing): ``search`` calls are the expensive kind;
    filter-only list calls (citation graph) are ~10x cheaper and single-work
    lookups (DOI verification) are free.
    """

    name = "openalex"

    def __init__(
        self,
        *,
        mailto: str | None = None,
        api_key: str | None = None,
        session: HttpGetter | None = None,
        timeout: float = 30.0,
        min_year: int | None = None,
        proxy_url: str | None = None,
        max_retries: int = 2,
    ) -> None:
        self._mailto = (mailto or "").strip() or None
        self._api_key = (api_key or "").strip() or None
        self._min_year = min_year
        self._client = ApiClient(
            "OpenAlex",
            session=session,
            proxy_url=proxy_url,
            timeout=timeout,
            min_interval=0.12,
            max_retries=max_retries,
        )

    @classmethod
    def from_environment(cls) -> OpenAlexSearchProvider:
        min_year = os.getenv("OPENALEX_MIN_YEAR", "").strip()
        return cls(
            mailto=os.getenv("OPENALEX_MAILTO") or os.getenv("CROW_CONTACT_EMAIL"),
            api_key=os.getenv("OPENALEX_API_KEY"),
            min_year=int(min_year) if min_year else None,
            proxy_url=os.getenv("CROW_HTTP_PROXY") or None,
        )

    def _auth(self, params: dict[str, object]) -> dict[str, object]:
        if self._mailto:
            params["mailto"] = self._mailto
        if self._api_key:
            params["api_key"] = self._api_key
        return params

    def search(self, query: str, *, limit: int = 5) -> list[SearchHit]:
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("query must not be blank")
        _check_limit(limit)
        filters = [f"type:{'|'.join(_ALLOWED_TYPES)}"]
        if self._min_year:
            filters.append(f"from_publication_date:{self._min_year}-01-01")
        payload = self._client.get_json(
            OPENALEX_WORKS_URL,
            self._auth(
                {
                    "search": normalized_query,
                    "per-page": limit,
                    "filter": ",".join(filters),
                    "select": _SELECT_FIELDS,
                }
            ),
        )
        return parse_openalex_response(payload, limit)

    def references(self, work: str, *, limit: int = 20) -> list[SearchHit]:
        """Works cited by ``work`` (backward snowballing)."""
        return self._graph("cited_by", work, limit)

    def citations(self, work: str, *, limit: int = 20) -> list[SearchHit]:
        """Works citing ``work`` (forward snowballing), most cited first."""
        return self._graph("cites", work, limit)

    def _graph(self, relation: str, work: str, limit: int) -> list[SearchHit]:
        _check_limit(limit)
        work_id = work.rsplit("/", 1)[-1]
        payload = self._client.get_json(
            OPENALEX_WORKS_URL,
            self._auth(
                {
                    "filter": f"{relation}:{work_id}",
                    "per-page": limit,
                    "sort": "cited_by_count:desc",
                    "select": _SELECT_FIELDS,
                }
            ),
        )
        return parse_openalex_response(payload, limit)

    def lookup_doi(self, doi: str) -> SearchHit | None:
        """Free single-work lookup used to verify a DOI's metadata."""
        payload = self._client.get_json(
            f"{OPENALEX_WORKS_URL}/doi:{doi}",
            self._auth({"select": _SELECT_FIELDS}),
        )
        return _parse_work(payload)


def _check_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValueError("limit must be an integer between 1 and 50")


def parse_openalex_response(payload: object, limit: int) -> list[SearchHit]:
    """Normalize an OpenAlex ``/works`` response; malformed items are skipped."""
    if not isinstance(payload, Mapping) or not isinstance(payload.get("results"), list):
        raise SearchError("OpenAlex returned an invalid response")
    hits: list[SearchHit] = []
    for item in payload["results"]:
        hit = _parse_work(item)
        if hit is not None:
            hits.append(hit)
        if len(hits) >= limit:
            break
    return hits


def _parse_work(item: object) -> SearchHit | None:
    if not isinstance(item, Mapping):
        return None
    title = item.get("display_name")
    if not isinstance(title, str) or not title.strip():
        return None

    primary = _mapping(item.get("primary_location"))
    best_oa = _mapping(item.get("best_oa_location"))
    open_access = _mapping(item.get("open_access"))
    source = _mapping(primary.get("source"))
    doi = _normalize_doi(item.get("doi"))

    pdf_url = _str(best_oa.get("pdf_url")) or _str(primary.get("pdf_url"))
    landing_url = (
        _str(best_oa.get("landing_page_url"))
        or _str(primary.get("landing_page_url"))
        or (f"https://doi.org/{doi}" if doi else None)
    )
    oa_url = _str(open_access.get("oa_url"))
    url = pdf_url or oa_url or landing_url or _str(item.get("id"))
    if not url:
        return None

    authors: list[str] = []
    institutions: list[str] = []
    for authorship in item.get("authorships") or []:
        authorship = _mapping(authorship)
        name = _str(_mapping(authorship.get("author")).get("display_name"))
        if name and name not in authors:
            authors.append(name)
        for institution in authorship.get("institutions") or []:
            institution_name = _str(_mapping(institution).get("display_name"))
            if institution_name and institution_name not in institutions:
                institutions.append(institution_name)

    openalex_id = _str(item.get("id"))
    external_ids = {"openalex": openalex_id.rsplit("/", 1)[-1]} if openalex_id else {}
    if doi:
        external_ids["doi"] = doi
    abstract = _abstract(item.get("abstract_inverted_index"))
    retracted = item.get("is_retracted")
    year = item.get("publication_year")
    cited_by = item.get("cited_by_count")
    score = item.get("relevance_score")
    is_oa = open_access.get("is_oa")
    return SearchHit(
        title=" ".join(title.split()),
        url=url,
        snippet=abstract,
        provider="openalex",
        provider_score=float(score) if isinstance(score, (int, float)) else None,
        published_at=_parse_date(item.get("publication_date")),
        metadata=SourceMetadata(
            doi=doi,
            authors=authors[:20],
            year=year if isinstance(year, int) else None,
            venue=_str(source.get("display_name")),
            publisher=_str(source.get("host_organization_name")),
            institutions=institutions[:10],
            document_type=_str(item.get("type")),
            language=_str(item.get("language")),
            cited_by_count=cited_by if isinstance(cited_by, int) else None,
            is_open_access=is_oa if isinstance(is_oa, bool) else None,
            pdf_url=pdf_url,
            landing_url=landing_url,
            provider_id=openalex_id,
            abstract=abstract or None,
            external_ids=external_ids,
            is_retracted=retracted if isinstance(retracted, bool) else None,
        ),
    )


def _abstract(inverted_index: object) -> str:
    """Rebuild an abstract from OpenAlex's inverted index."""
    if not isinstance(inverted_index, Mapping):
        return ""
    positions: list[tuple[int, str]] = []
    for word, indexes in inverted_index.items():
        if isinstance(word, str) and isinstance(indexes, Sequence):
            positions.extend((index, word) for index in indexes if isinstance(index, int))
    text = " ".join(word for _, word in sorted(positions))
    return text[:_MAX_SNIPPET_CHARS]


def _normalize_doi(value: object) -> str | None:
    doi = _str(value)
    if not doi:
        return None
    lowered = doi.lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if lowered.startswith(prefix):
            lowered = lowered[len(prefix) :]
    return lowered or None


def _parse_date(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _str(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
