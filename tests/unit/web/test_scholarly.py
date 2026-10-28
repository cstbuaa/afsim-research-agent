"""Tests for the OpenAlex scholarly search adapter."""

from __future__ import annotations

import pytest
import requests

from afsim_research_agent.web.scholarly import OpenAlexSearchProvider, parse_openalex_response
from afsim_research_agent.web.search import SearchError

WORK = {
    "id": "https://openalex.org/W1",
    "doi": "https://doi.org/10.3390/Drones7010001",
    "display_name": "  Formation   control of ground vehicles ",
    "publication_year": 2023,
    "publication_date": "2023-01-05",
    "type": "article",
    "language": "en",
    "cited_by_count": 42,
    "authorships": [
        {
            "author": {"display_name": "A. Li"},
            "institutions": [{"display_name": "Beihang University"}],
        },
        {"author": {"display_name": "B. Wang"}, "institutions": []},
    ],
    "primary_location": {
        "landing_page_url": "https://www.mdpi.com/2504-446X/7/1/1",
        "pdf_url": None,
        "source": {"display_name": "Drones", "host_organization_name": "MDPI"},
    },
    "best_oa_location": {
        "pdf_url": "https://www.mdpi.com/2504-446X/7/1/1/pdf",
        "landing_page_url": "https://www.mdpi.com/2504-446X/7/1/1",
    },
    "open_access": {"is_oa": True, "oa_url": "https://www.mdpi.com/2504-446X/7/1/1"},
    "abstract_inverted_index": {"Vehicles": [0], "keep": [1], "formation": [2]},
}


class FakeHttp:
    def __init__(self, status: int = 200, payload: object | None = None) -> None:
        self.status = status
        self.payload = {"results": [WORK]} if payload is None else payload
        self.calls: list[dict[str, object]] = []

    def get(self, url: str, **kwargs: object) -> object:
        self.calls.append({"url": url, **kwargs})
        status, payload = self.status, self.payload

        class Response:
            status_code = status

            def json(self) -> object:
                return payload

        return Response()


def test_parse_work_prefers_open_access_pdf_and_keeps_metadata() -> None:
    [hit] = parse_openalex_response({"results": [WORK]}, limit=5)

    assert hit.title == "Formation control of ground vehicles"
    assert hit.url == "https://www.mdpi.com/2504-446X/7/1/1/pdf"
    assert hit.snippet == "Vehicles keep formation"
    assert hit.provider == "openalex"
    meta = hit.metadata
    assert meta.doi == "10.3390/drones7010001"
    assert meta.authors == ["A. Li", "B. Wang"]
    assert meta.institutions == ["Beihang University"]
    assert (meta.year, meta.venue, meta.publisher) == (2023, "Drones", "MDPI")
    assert (meta.document_type, meta.cited_by_count, meta.is_open_access) == ("article", 42, True)


def test_closed_work_falls_back_to_landing_page() -> None:
    work = {
        **WORK,
        "best_oa_location": None,
        "open_access": {"is_oa": False, "oa_url": None},
    }

    [hit] = parse_openalex_response({"results": [work]}, limit=5)

    assert hit.url == "https://www.mdpi.com/2504-446X/7/1/1"
    assert hit.metadata.is_open_access is False
    assert hit.metadata.pdf_url is None


def test_malformed_items_are_skipped_but_bad_envelope_fails() -> None:
    assert parse_openalex_response({"results": [{}, "x", WORK]}, limit=5)[0].metadata.year == 2023
    with pytest.raises(SearchError):
        parse_openalex_response({"meta": {}}, limit=5)


def test_provider_sends_filters_contact_and_key() -> None:
    http = FakeHttp()
    provider = OpenAlexSearchProvider(
        mailto="me@example.org", api_key="k", session=http, min_year=2010
    )

    provider.search("formation control terrain", limit=3)

    params = http.calls[0]["params"]
    assert params["search"] == "formation control terrain"
    assert params["per-page"] == 3
    assert params["mailto"] == "me@example.org"
    assert params["api_key"] == "k"
    assert "type:article|" in params["filter"]
    assert "from_publication_date:2010-01-01" in params["filter"]


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_http_errors_become_search_errors(status: int) -> None:
    with pytest.raises(SearchError, match=str(status)):
        OpenAlexSearchProvider(session=FakeHttp(status=status), max_retries=0).search("q")


def test_transport_errors_become_search_errors() -> None:
    class Broken:
        def get(self, url: str, **kwargs: object) -> object:
            raise requests.ConnectionError("x")

    with pytest.raises(SearchError, match="ConnectionError"):
        OpenAlexSearchProvider(session=Broken(), max_retries=0).search("q")


def test_retraction_and_ids_are_parsed() -> None:
    [hit] = parse_openalex_response({"results": [{**WORK, "is_retracted": True}]}, limit=1)

    assert hit.metadata.is_retracted is True
    assert hit.metadata.external_ids == {"openalex": "W1", "doi": "10.3390/drones7010001"}
    assert hit.metadata.abstract == "Vehicles keep formation"


@pytest.mark.parametrize(
    ("method", "relation"), [("references", "cited_by:W1"), ("citations", "cites:W1")]
)
def test_citation_graph_uses_cheap_filter_calls(method: str, relation: str) -> None:
    http = FakeHttp()

    hits = getattr(OpenAlexSearchProvider(session=http), method)(
        "https://openalex.org/W1", limit=5
    )

    params = http.calls[0]["params"]
    assert params["filter"] == relation
    assert "search" not in params
    assert hits[0].metadata.doi == "10.3390/drones7010001"


def test_doi_lookup_is_a_single_work_call() -> None:
    http = FakeHttp(payload=WORK)

    hit = OpenAlexSearchProvider(session=http).lookup_doi("10.3390/drones7010001")

    assert http.calls[0]["url"].endswith("/works/doi:10.3390/drones7010001")
    assert hit.title == "Formation control of ground vehicles"
