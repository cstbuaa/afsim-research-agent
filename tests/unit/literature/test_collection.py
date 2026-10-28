"""Tests for discovery, triage and acquisition of research sources."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from afsim_research_agent.config import Settings
from afsim_research_agent.literature import (
    SourceCollectionRequest,
    collect_research_sources,
)
from afsim_research_agent.literature.collection import (
    SearchChannel,
    build_default_channels,
    format_citation,
)
from afsim_research_agent.models import SearchHit, SourceMetadata
from afsim_research_agent.web.fetch import FetchedResource, FetchError
from afsim_research_agent.web.search import SearchError


def article(topic: str = "wedge") -> bytes:
    paragraphs = "".join(
        f"<p>Paragraph {i} about {topic}, column and line formations.</p>" for i in range(20)
    )
    return (
        "<html><head><title>Armored formations</title></head><body><article>"
        f"{paragraphs}</article></body></html>"
    ).encode()


ARTICLE = article()
THIN = b"<html><head><title>Login</title></head><body><article><p>Sign in.</p></article></body></html>"  # noqa: E501
PDF_A = b"%PDF-1.7\nATP 3-20.15 tank platoon"
PDF_B = b"%PDF-1.7\nformation control paper"


def hit(url: str, title: str = "Result", **kwargs: object) -> SearchHit:
    return SearchHit(title=title, url=url, provider="test", **kwargs)


def resource(url: str, body: bytes, kind: str) -> FetchedResource:
    return FetchedResource(
        requested_url=url,
        final_url=url,
        status_code=200,
        content_type="application/pdf" if kind == "pdf" else "text/html",
        kind=kind,  # type: ignore[arg-type]
        body=body,
        charset="utf-8",
        attempts=1,
    )


class FakeFetcher:
    def __init__(self, responses: dict[str, FetchedResource | Exception]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, url: str) -> FetchedResource:
        self.calls.append(url)
        item = self.responses[url]
        if isinstance(item, Exception):
            raise item
        return item


def channel(name: str, hits: dict[str, list[SearchHit]], provider: str = "tavily"):
    return SearchChannel(
        name=name,
        provider=provider,
        search=lambda query, limit: hits[query][:limit],
        queries=list(hits),
    )


def collect(tmp_path: Path, channels, fetcher, **request_kwargs):
    request = SourceCollectionRequest(requirement="armored formations", **request_kwargs)
    return collect_research_sources(
        request,
        output_dir=tmp_path,
        channels=channels,
        fetcher=fetcher,
        app_settings=Settings.from_environment(),
    )


def test_every_candidate_gets_exactly_one_explained_decision(tmp_path: Path) -> None:
    official = "https://armypubs.army.mil/ATP_3-20.15.pdf"
    mirror = "https://upload.wikimedia.org/ATP_3-20.15.pdf"
    article = "https://www.defensenews.com/land/formations"
    channels = [
        channel(
            "web",
            {
                "tank platoon formations": [
                    hit("https://www.youtube.com/watch?v=1", "Tank formations video"),
                    hit(article, "Armored formations"),
                    hit(article + "/?utm_source=x", "Armored formations (tracking)"),
                    hit("https://www.army.mil/forbidden", "Army article"),
                ]
            },
        ),
        channel(
            "doctrine",
            {"ATP 3-20.15": [hit(official, "ATP 3-20.15 Tank Platoon"), hit(mirror, "ATP mirror")]},
        ),
    ]
    fetcher = FakeFetcher(
        {
            official: resource(official, PDF_A, "pdf"),
            mirror: resource(mirror, PDF_A, "pdf"),
            article: resource(article, ARTICLE, "html"),
            "https://www.army.mil/forbidden": FetchError(
                "http_error", "HTTP 403 (access denied)", status_code=403
            ),
        }
    )

    result = collect(tmp_path, channels, fetcher, max_web_results=5)

    decisions = {c.url: c.decision for c in result.candidates}
    assert decisions == {
        "https://www.youtube.com/watch?v=1": "rejected",
        article: "accepted",
        article + "/?utm_source=x": "duplicate_url",
        "https://www.army.mil/forbidden": "failed",
        official: "accepted",
        mirror: "duplicate_content",
    }
    assert "https://www.youtube.com/watch?v=1" not in fetcher.calls
    # Official doctrine is fetched first because it scores highest.
    assert fetcher.calls[0] == official

    by_url = {c.url: c for c in result.candidates}
    assert by_url["https://www.youtube.com/watch?v=1"].reason_category == "blocked_domain"
    assert by_url["https://www.army.mil/forbidden"].http_status == 403
    assert by_url[mirror].duplicate_of == by_url[official].source_id

    official_source = next(s for s in result.sources if s.uri == official)
    assert official_source.quality.tier == "A"
    assert official_source.kind == "official_documentation"
    assert official_source.discovered_by == ["doctrine:ATP 3-20.15"]
    assert {f.category for f in result.failures} == {"blocked_domain", "http_error"}
    assert len(result.documents) == len(result.sources) == 2


def test_webpages_are_snapshotted_and_thin_pages_rejected(tmp_path: Path) -> None:
    good = "https://www.rand.org/a"
    thin = "https://www.rand.org/login"
    fetcher = FakeFetcher(
        {good: resource(good, ARTICLE, "html"), thin: resource(thin, THIN, "html")}
    )

    result = collect(tmp_path, [channel("web", {"q": [hit(good), hit(thin)]})], fetcher)

    assert [c.decision for c in result.candidates] == ["accepted", "failed"]
    assert result.candidates[1].reason_category == "no_extractable_text"
    assert list((tmp_path / "raw").glob("*.html"))
    markdown = result.documents[0].path.read_text(encoding="utf-8")
    assert markdown.startswith("# Armored formations")
    assert f"Source: {good}" in markdown


def test_scholarly_duplicates_and_closed_access_are_handled(tmp_path: Path) -> None:
    open_pdf = "https://www.mdpi.com/x/pdf"
    meta = SourceMetadata(
        doi="10.3390/x", authors=["A. Li"], year=2023, venue="Drones", is_open_access=True,
        pdf_url=open_pdf, document_type="article",
    )
    hits = {
        "formation control": [
            hit(open_pdf, "Formation control", metadata=meta),
            hit("https://doi.org/10.3390/X", "Same DOI", metadata=meta),
            hit(
                "https://www.sciencedirect.com/closed",
                "Closed work",
                metadata=SourceMetadata(doi="10.1016/y", is_open_access=False),
            ),
        ]
    }
    fetcher = FakeFetcher({open_pdf: resource(open_pdf, PDF_B, "pdf")})

    result = collect(tmp_path, [channel("scholarly", hits, provider="openalex")], fetcher)

    assert [c.decision for c in result.candidates] == ["accepted", "duplicate_doi", "rejected"]
    assert result.candidates[2].reason_category == "not_open_access"
    document = result.documents[0]
    assert document.doi == "10.3390/x"
    assert document.citation.startswith("A. Li (2023). Formation control. Drones.")
    assert result.sources[0].metadata.venue == "Drones"


def test_budget_limits_accepted_sources_and_records_skips(tmp_path: Path) -> None:
    urls = [f"https://www.rand.org/{i}" for i in range(5)]
    fetcher = FakeFetcher(
        {url: resource(url, article(f"topic {i}"), "html") for i, url in enumerate(urls)}
    )

    result = collect(
        tmp_path,
        [channel("web", {"q": [hit(url) for url in urls]})],
        fetcher,
        max_total_sources=2,
    )

    decisions = [c.decision for c in result.candidates]
    assert decisions.count("accepted") == 2
    assert decisions.count("skipped_budget") == 3
    assert len(fetcher.calls) == 2


def test_search_failures_are_recorded_and_other_channels_continue(tmp_path: Path) -> None:
    def broken(query: str, limit: int) -> list[SearchHit]:
        raise SearchError("OpenAlex returned HTTP 403")

    url = "https://www.rand.org/a"
    channels = [
        SearchChannel(name="scholarly", provider="openalex", search=broken, queries=["q1"]),
        channel("web", {"q2": [hit(url)]}),
    ]

    result = collect(tmp_path, channels, FakeFetcher({url: resource(url, ARTICLE, "html")}))

    assert len(result.sources) == 1
    [failure] = [f for f in result.failures if f.stage == "search"]
    assert failure.category == "search_failed"
    assert failure.detail == "OpenAlex returned HTTP 403"
    assert result.queries == ["q1", "q2"]


def test_unexpected_errors_do_not_leak_internals(tmp_path: Path) -> None:
    url = "https://www.rand.org/a"

    result = collect(
        tmp_path,
        [channel("web", {"q": [hit(url)]})],
        FakeFetcher({url: RuntimeError("secret token xyz")}),
    )

    candidate = result.candidates[0]
    assert candidate.decision == "failed"
    assert candidate.reason_category == "unknown"
    assert "secret" not in (candidate.reason_detail or "")


def test_default_channels_follow_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CROW_ENABLE_SCHOLARLY", "false")
    request = SourceCollectionRequest(
        requirement="r", search_queries=["web q"], doctrine_queries=["FM 3-90"]
    )

    channels = build_default_channels(request, Settings.from_environment())

    assert [(c.name, c.provider, list(c.queries)) for c in channels] == [
        ("web", "tavily", ["web q"]),
        ("doctrine", "tavily", ["FM 3-90"]),
        ("technical_report", "tavily", []),
    ]


def test_default_scholarly_channels_skip_core_without_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CROW_ENABLE_SCHOLARLY", raising=False)
    monkeypatch.delenv("CORE_API_KEY", raising=False)
    request = SourceCollectionRequest(
        requirement="r", search_queries=["w"], scholarly_queries=["s"],
        technical_report_queries=["t"],
    )

    channels = build_default_channels(request, Settings.from_environment())

    assert [(c.name, c.provider) for c in channels if c.name == "scholarly"] == [
        ("scholarly", "openalex"), ("scholarly", "semantic_scholar"), ("scholarly", "arxiv")
    ]
    assert next(c for c in channels if c.name == "technical_report").queries == ["t"]


def test_scholarly_channel_reuses_web_queries_when_planner_gave_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CROW_ENABLE_SCHOLARLY", raising=False)
    request = SourceCollectionRequest(requirement="r", search_queries=["web q"])

    channels = build_default_channels(request, Settings.from_environment())

    assert next(c for c in channels if c.name == "scholarly").queries == ["web q"]


def test_citation_uses_only_known_metadata() -> None:
    assert format_citation("T", "https://e.org/a", SourceMetadata()) == "T. https://e.org/a"
    assert format_citation(
        "T",
        "https://e.org/a",
        SourceMetadata(authors=["A", "B", "C", "D"], year=2020, doi="10.1/X"),
    ) == "A, B, C et al. (2020). T. https://doi.org/10.1/x"


def test_local_sources_are_still_supported(tmp_path: Path) -> None:
    local_root = tmp_path / "local"
    local_root.mkdir()
    (local_root / "doctrine.md").write_text("formation doctrine")
    (local_root / "processor.cpp").write_text("void update_formation();")
    (local_root / "ignored.bin").write_bytes(b"ignored")

    result = collect(tmp_path / "out", [], FakeFetcher({}), local_roots=[local_root])

    assert {source.kind for source in result.sources} == {"local_code", "local_document"}
    code_document = next(d for d in result.documents if "processor" in d.title)
    assert code_document.path.suffix == ".txt"


# ---------------------------------------------------------------- v0.3 stages

from afsim_research_agent.domain import DoctrineCatalog  # noqa: E402
from afsim_research_agent.literature.collection import (  # noqa: E402
    CollectorServices,
    SourceCollector,
)
from afsim_research_agent.literature.models import ScreeningResult  # noqa: E402
from afsim_research_agent.literature.verification import MetadataVerifier  # noqa: E402


class FakeScreener:
    name = "fake"

    def __init__(self, scores: dict[str, int]) -> None:
        self.scores = scores
        self.seen: list[str] = []

    def screen(self, requirement, questions, candidates):
        self.seen.extend(c.title for c in candidates)
        return {
            c.candidate_id: ScreeningResult(score=self.scores.get(c.title, 2), reason="r")
            for c in candidates
        }


class FakeGraph:
    name = "fakegraph"

    def __init__(self, refs: list[SearchHit], cites: list[SearchHit]) -> None:
        self.refs, self.cites = refs, cites
        self.calls: list[tuple[str, str]] = []

    def references(self, work: str, *, limit: int = 20) -> list[SearchHit]:
        self.calls.append(("references", work))
        return self.refs

    def citations(self, work: str, *, limit: int = 20) -> list[SearchHit]:
        self.calls.append(("citations", work))
        return self.cites


def settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings.from_environment()


def test_screening_removes_off_topic_and_reorders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    urls = {t: f"https://www.rand.org/{i}" for i, t in enumerate(["Geology", "Weak", "Strong"])}
    fetcher = FakeFetcher(
        {u: resource(u, article(t), "html") for t, u in urls.items()}
    )
    screener = FakeScreener({"Geology": 0, "Weak": 1, "Strong": 3})
    collector = SourceCollector(
        output_dir=tmp_path,
        app_settings=settings(monkeypatch),
        fetcher=fetcher,
        services=CollectorServices(screener=screener),
    )

    collector.collect(
        SourceCollectionRequest(requirement="r", research_questions=["q"], max_total_sources=1),
        channels=[channel("web", {"q": [hit(u, t) for t, u in urls.items()]})],
    )
    by_title = {c.title: c for c in collector.candidates}

    assert by_title["Geology"].decision == "screened_out"
    assert by_title["Strong"].decision == "accepted"  # fetched first despite rank 3
    assert by_title["Weak"].decision == "skipped_budget"
    assert fetcher.calls == [urls["Strong"]]


def test_snowballing_expands_from_best_scholarly_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_url = "https://www.mdpi.com/seed/pdf"
    ref_url = "https://arxiv.org/pdf/1111.1111"
    seed_meta = SourceMetadata(doi="10.1/seed", external_ids={"openalex": "W1"}, pdf_url=seed_url)
    graph = FakeGraph(
        refs=[hit(ref_url, "Referenced formation paper", metadata=SourceMetadata(pdf_url=ref_url))],
        cites=[hit(seed_url, "Seed again")],  # already known -> duplicate
    )
    collector = SourceCollector(
        output_dir=tmp_path,
        app_settings=settings(monkeypatch, CROW_SNOWBALL_SEEDS="1"),
        fetcher=FakeFetcher(
            {seed_url: resource(seed_url, PDF_A, "pdf"), ref_url: resource(ref_url, PDF_B, "pdf")}
        ),
        services=CollectorServices(
            graph=graph, graph_reference=lambda m: m.external_ids.get("openalex")
        ),
    )

    collector.collect(
        SourceCollectionRequest(requirement="r", max_total_sources=4),
        channels=[channel("scholarly", {"q": [hit(seed_url, "Seed", metadata=seed_meta)]},
                          provider="openalex")],
    )

    assert graph.calls == [("references", "W1"), ("citations", "W1")]
    snow = [c for c in collector.candidates if c.origin == "snowball"]
    assert [c.decision for c in snow] == ["accepted", "duplicate_url"]
    assert snow[0].seed_source_id == collector.sources[0].source_id
    assert snow[0].channel == "snowball"


def test_dedup_spans_rounds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://www.rand.org/a"
    collector = SourceCollector(
        output_dir=tmp_path,
        app_settings=settings(monkeypatch),
        fetcher=FakeFetcher({url: resource(url, ARTICLE, "html")}),
    )

    first = collector.collect(
        SourceCollectionRequest(requirement="r"), channels=[channel("web", {"q": [hit(url)]})]
    )
    second = collector.collect(
        SourceCollectionRequest(requirement="r", round=2),
        channels=[channel("web", {"q2": [hit(url + "/")]})],
    )
    snapshot = collector.snapshot("r")

    assert len(first) == 1 and second == []
    assert [c.decision for c in snapshot.candidates] == ["accepted", "duplicate_url"]
    assert snapshot.candidates[1].round == 2 and snapshot.rounds == 2


def test_normalized_title_dedup_respects_doctrine_editions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hits = [
        hit("https://a.org/1", "Agent-Based Simulation of Armored Formations"),
        hit("https://b.org/2", "agent-based simulation of armored formations!"),
        hit("https://c.mil/3", "FM 3-20.15 Tank Platoon Tactics Techniques"),
        hit("https://c.mil/4", "ATP 3-20.15 Tank Platoon Tactics Techniques"),
    ]
    collector = SourceCollector(
        output_dir=tmp_path,
        app_settings=settings(monkeypatch),
        fetcher=FakeFetcher({}),
        services=CollectorServices(catalog=DoctrineCatalog.default()),
    )

    collector.collect(
        SourceCollectionRequest(requirement="r", max_total_sources=1, max_fetch_attempts=1),
        channels=[channel("web", {"q": hits})],
    )
    decisions = [c.decision for c in collector.candidates]
    old = collector.candidates[2]

    assert decisions[1] == "duplicate_title"
    assert decisions[2] != "duplicate_title" and decisions[3] != "duplicate_title"
    assert "FM 3-20.15 is superseded by ATP 3-20.15" in old.notes
    assert any("superseded" in r for r in old.preliminary_quality.reasons)


def test_verification_rejects_retracted_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def lookup(doi: str) -> SearchHit:
        return hit("https://doi.org/x", "Swarm formation control method",
                   metadata=SourceMetadata(is_retracted=True))

    collector = SourceCollector(
        output_dir=tmp_path,
        app_settings=settings(monkeypatch),
        fetcher=FakeFetcher({}),
        services=CollectorServices(verifier=MetadataVerifier(lookup)),
    )

    collector.collect(
        SourceCollectionRequest(requirement="r"),
        channels=[
            channel(
                "scholarly",
                {"q": [hit("https://x.org/p.pdf", "Swarm formation control method",
                           metadata=SourceMetadata(doi="10.9/r"))]},
                provider="semantic_scholar",
            )
        ],
    )

    candidate = collector.candidates[0]
    assert candidate.decision == "rejected" and candidate.reason_category == "retracted"
    assert candidate.metadata.verified_by == ["openalex"]


def test_library_documents_are_screened_and_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    (library / "a.md").write_text("坦克排 楔形队形 间距 " * 50, encoding="utf-8")
    (library / "a.md.meta.toml").write_text(
        'title = "装甲分队地形编队研究"\nauthors = ["张三"]\nyear = 2021\n'
        'venue = "兵工学报"\ndocument_type = "article"\n',
        encoding="utf-8",
    )
    (library / "b.txt").write_text("unrelated cooking notes", encoding="utf-8")
    (library / "c.txt").write_text("another related note", encoding="utf-8")
    screener = FakeScreener({"b": 0})
    collector = SourceCollector(
        output_dir=tmp_path / "out",
        app_settings=settings(monkeypatch, CROW_MAX_LIBRARY_SOURCES="1"),
        fetcher=FakeFetcher({}),
        services=CollectorServices(screener=screener),
    )

    documents = collector.collect(
        SourceCollectionRequest(requirement="r", library_roots=[library], research_questions=["q"]),
        channels=[],
    )
    by_title = {c.title: c for c in collector.candidates}

    assert by_title["b"].decision == "screened_out"
    assert by_title["装甲分队地形编队研究"].decision == "accepted"
    assert by_title["c"].decision == "skipped_budget"  # library cap of 1
    [source] = collector.sources
    assert source.source_id.startswith("lib:")
    assert source.quality.tier == "B"
    assert source.metadata.venue == "兵工学报"
    assert documents[0].citation.startswith("张三 (2021). 装甲分队地形编队研究. 兵工学报.")
    assert documents[0].path == library / "a.md"


def test_chinese_queries_become_manual_search_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    collector = SourceCollector(
        output_dir=tmp_path, app_settings=settings(monkeypatch), fetcher=FakeFetcher({})
    )

    collector.collect(
        SourceCollectionRequest(requirement="r", chinese_queries=["坦克排 编队"]), channels=[]
    )

    assert collector.snapshot("r").manual_search_queries == ["坦克排 编队"]


def test_alternate_urls_rescue_a_blocked_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    landing = "https://publisher.example/article/1"
    pdf = "https://repository.example/1.pdf"
    meta = SourceMetadata(doi="10.5/abc", landing_url=landing, pdf_url=pdf)
    fetcher = FakeFetcher(
        {
            pdf: FetchError("http_error", "HTTP 403", status_code=403, url=pdf),
            "https://arxiv.org/pdf/2101.00001": resource(
                "https://arxiv.org/pdf/2101.00001", PDF_B, "pdf"
            ),
        }
    )
    meta.external_ids["arxiv"] = "2101.00001"
    collector = SourceCollector(
        output_dir=tmp_path, app_settings=settings(monkeypatch), fetcher=fetcher
    )

    collector.collect(
        SourceCollectionRequest(requirement="r"),
        channels=[channel("scholarly", {"q": [hit(pdf, "Paper", metadata=meta)]},
                          provider="openalex")],
    )

    [candidate] = collector.candidates
    assert candidate.alternate_urls[0] == "https://arxiv.org/pdf/2101.00001"
    assert candidate.decision == "accepted"
    assert fetcher.calls == [pdf, "https://arxiv.org/pdf/2101.00001"]
    assert candidate.attempts == 2
    assert "used alternate" in candidate.notes[0]


def test_failing_host_is_skipped_after_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    urls = [f"https://slow.mil/{i}" for i in range(4)]
    fetcher = FakeFetcher(
        {u: FetchError("timeout", "no response", url=u) for u in urls}
    )
    collector = SourceCollector(
        output_dir=tmp_path,
        app_settings=settings(monkeypatch, CROW_HOST_FAILURE_LIMIT="2"),
        fetcher=fetcher,
    )

    collector.collect(
        SourceCollectionRequest(requirement="r"),
        channels=[channel("web", {"q": [hit(u, f"t{i}") for i, u in enumerate(urls)]})],
    )

    assert len(fetcher.calls) == 2
    skipped = [c for c in collector.candidates if "skipped:" in (c.reason_detail or "")]
    assert len(skipped) == 2 and all(c.attempts == 0 for c in skipped)


def test_rate_limited_provider_is_disabled_for_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def limited(query: str, limit: int) -> list[SearchHit]:
        calls.append(query)
        raise SearchError("Semantic Scholar returned HTTP 429")

    collector = SourceCollector(
        output_dir=tmp_path, app_settings=settings(monkeypatch), fetcher=FakeFetcher({})
    )

    collector.collect(
        SourceCollectionRequest(requirement="r"),
        channels=[SearchChannel("scholarly", "semantic_scholar", limited, ["a", "b", "c"])],
    )

    assert calls == ["a"]
    details = [f.detail for f in collector.failures]
    assert details[0] == "Semantic Scholar returned HTTP 429"
    assert details[1:] == ["disabled after: Semantic Scholar returned HTTP 429"] * 2


def test_parallel_fetching_respects_budget_and_priority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    urls = [f"https://www.rand.org/{i}" for i in range(6)]
    active = {"now": 0, "max": 0}
    lock = threading.Lock()

    def fetcher(url: str) -> FetchedResource:
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        time.sleep(0.05)
        with lock:
            active["now"] -= 1
        return resource(url, article(url), "html")

    collector = SourceCollector(
        output_dir=tmp_path,
        app_settings=settings(monkeypatch, CROW_FETCH_WORKERS="4"),
        fetcher=fetcher,
    )

    collector.collect(
        SourceCollectionRequest(requirement="r", max_total_sources=3, max_web_results=6),
        channels=[channel("web", {"q": [hit(u, f"t{i}") for i, u in enumerate(urls)]})],
    )

    decisions = [c.decision for c in collector.candidates]
    assert decisions == ["accepted"] * 3 + ["skipped_budget"] * 3  # top-priority three
    assert active["max"] == 3  # batch size limited by remaining budget
