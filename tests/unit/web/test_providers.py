"""Tests for the Semantic Scholar, arXiv and CORE adapters."""

from __future__ import annotations

import pytest

from afsim_research_agent.models import SourceMetadata
from afsim_research_agent.web.arxiv import ArxivProvider, build_query, parse_feed
from afsim_research_agent.web.core import CoreProvider
from afsim_research_agent.web.search import SearchError
from afsim_research_agent.web.semantic_scholar import SemanticScholarProvider, paper_reference


class Response:
    def __init__(self, payload: object = None, content: bytes = b"", status: int = 200) -> None:
        self.payload = payload
        self.content = content
        self.status_code = status
        self.headers: dict[str, str] = {}

    def json(self) -> object:
        return self.payload


class Http:
    def __init__(self, response: Response) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def get(self, url: str, **kwargs: object) -> Response:
        self.calls.append({"url": url, **kwargs})
        return self.response


S2_PAPER = {
    "paperId": "abc123",
    "title": "Behavior Trees in Robotics and AI",
    "abstract": "A survey of behavior trees.",
    "year": 2022,
    "venue": "Robotics Auton. Syst.",
    "publicationVenue": {"name": "Robotics and Autonomous Systems"},
    "authors": [{"name": "M. Iovino"}, {"name": "E. Scukins"}],
    "externalIds": {"DOI": "10.1016/J.ROBOT.2022.104096", "ArXiv": "2005.05842"},
    "openAccessPdf": None,
    "isOpenAccess": False,
    "citationCount": 150,
    "publicationTypes": ["JournalArticle", "Review"],
    "url": "https://www.semanticscholar.org/paper/abc123",
}


def s2(payload: object) -> tuple[SemanticScholarProvider, Http]:
    http = Http(Response(payload))
    return SemanticScholarProvider(api_key="k", session=http, max_retries=0), http


def test_s2_search_normalizes_metadata_and_prefers_arxiv_pdf() -> None:
    provider, http = s2({"data": [S2_PAPER]})

    [hit] = provider.search("behavior tree robotics", limit=3)

    assert http.calls[0]["headers"]["x-api-key"] == "k"
    assert hit.url == "https://arxiv.org/pdf/2005.05842"
    meta = hit.metadata
    assert meta.doi == "10.1016/j.robot.2022.104096"
    assert meta.venue == "Robotics and Autonomous Systems"
    assert meta.document_type == "review"
    assert meta.external_ids == {
        "s2": "abc123", "doi": "10.1016/j.robot.2022.104096", "arxiv": "2005.05842"
    }
    assert meta.is_open_access is True  # an arXiv twin is open


@pytest.mark.parametrize(
    ("method", "key"), [("references", "citedPaper"), ("citations", "citingPaper")]
)
def test_s2_graph_unwraps_edges(method: str, key: str) -> None:
    provider, http = s2({"data": [{key: S2_PAPER}, {key: None}]})

    hits = getattr(provider, method)("DOI:10.1/x", limit=5)

    assert http.calls[0]["url"].endswith(f"/paper/DOI:10.1/x/{method}")
    assert [h.title for h in hits] == ["Behavior Trees in Robotics and AI"]


def test_paper_reference_prefers_s2_then_doi_then_arxiv() -> None:
    assert paper_reference(SourceMetadata(external_ids={"s2": "x"}, doi="10.1/a")) == "x"
    assert paper_reference(SourceMetadata(doi="10.1/a")) == "DOI:10.1/a"
    assert paper_reference(SourceMetadata(external_ids={"arxiv": "2401.1"})) == "ARXIV:2401.1"
    assert paper_reference(SourceMetadata()) is None


FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/1709.00084v5</id>
    <published>2017-08-31T20:00:00Z</published>
    <title>Behavior Trees in Robotics and AI:
      An Introduction</title>
    <summary>  A book on behavior trees.  </summary>
    <author><name>Michele Colledanchise</name></author>
    <author><name>Petter Ogren</name></author>
    <link title="pdf" href="http://arxiv.org/pdf/1709.00084v5" rel="related"/>
    <arxiv:doi>10.1201/9780429489105</arxiv:doi>
  </entry>
  <entry><id>bad</id><title></title></entry>
</feed>"""


def test_arxiv_feed_is_parsed() -> None:
    [hit] = parse_feed(FEED, limit=5)

    assert hit.title == "Behavior Trees in Robotics and AI: An Introduction"
    assert hit.url == "https://arxiv.org/pdf/1709.00084v5"
    assert hit.metadata.external_ids == {"arxiv": "1709.00084", "doi": "10.1201/9780429489105"}
    assert hit.metadata.authors == ["Michele Colledanchise", "Petter Ogren"]
    assert hit.metadata.year == 2017
    assert hit.metadata.document_type == "article"


def test_arxiv_query_requires_content_words() -> None:
    assert build_query("Multi-vehicle formation control in terrain") == (
        "all:Multi-vehicle AND all:formation AND all:control AND all:terrain"
    )
    with pytest.raises(ValueError):
        ArxivProvider(session=Http(Response(content=FEED))).search("编队")


def test_arxiv_search_sends_relevance_query() -> None:
    http = Http(Response(content=FEED))

    ArxivProvider(session=http, max_retries=0).search("behavior trees robotics", limit=2)

    assert http.calls[0]["params"]["search_query"] == "all:behavior AND all:trees AND all:robotics"
    assert http.calls[0]["params"]["sortBy"] == "relevance"


def test_arxiv_rejects_malformed_xml() -> None:
    with pytest.raises(SearchError):
        parse_feed(b"<not xml", limit=1)


def test_core_requires_key_and_parses_results() -> None:
    with pytest.raises(SearchError, match="CORE_API_KEY"):
        CoreProvider(api_key=" ")
    http = Http(
        Response(
            {
                "results": [
                    {
                        "id": 42,
                        "title": "Agent-based simulation of armored platoon formations",
                        "abstract": "Thesis.",
                        "authors": [{"name": "J. Doe"}],
                        "doi": "10.1/ABC",
                        "yearPublished": 2015,
                        "downloadUrl": "https://core.ac.uk/download/pdf/42.pdf",
                        "documentType": "thesis",
                        "publisher": "Naval Postgraduate School",
                        "journals": [],
                    }
                ]
            }
        )
    )

    [hit] = CoreProvider(api_key="k", session=http, max_retries=0).search("platoon", limit=2)

    assert http.calls[0]["headers"]["Authorization"] == "Bearer k"
    assert hit.url == "https://core.ac.uk/download/pdf/42.pdf"
    assert hit.metadata.document_type == "dissertation"
    assert hit.metadata.doi == "10.1/abc"
