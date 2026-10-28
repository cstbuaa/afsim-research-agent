"""Collect research sources with a complete, explainable decision ledger.

Each collection round runs these deterministic stages:

1. **Discovery** – search channels run the planner's query families: general
   web (Tavily), doctrine and official domains (Tavily ``include_domains``),
   defense technical reports (Tavily on DTIC/NPS/RAND/AFIT…), and scholarly
   indexes (OpenAlex, Semantic Scholar, arXiv, CORE). The user's local library
   joins as one more channel.
2. **Triage** – duplicates are collapsed by normalized URL, DOI and normalized
   title (across all rounds); blocked hosts, closed-access works and
   retractions are rejected; DOIs are verified against OpenAlex; superseded
   doctrine editions are flagged; deterministic source quality is scored.
3. **Screening** – an LLM scores each candidate's title and abstract 0-3
   against the research questions; 0 removes it, the score joins the ranking.
4. **Acquisition** – candidates are fetched by priority until the budget is
   filled; PDFs/HTML are sniffed, stored by content hash and mirror-deduplicated.
5. **Citation snowballing** – references and citations of the best accepted
   scholarly sources enter the same triage → screening → acquisition path.

Every candidate ends with one decision and, when not accepted, a reason.
"""

from __future__ import annotations

import hashlib
import shutil
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from afsim_research_agent.config import Settings
from afsim_research_agent.domain.doctrine import DoctrineCatalog, find_designations
from afsim_research_agent.evidence.deduplicate import (
    normalize_doi,
    normalize_url,
    preferred_fetch_url,
)
from afsim_research_agent.evidence.quality import (
    QualityInput,
    is_blocked_host,
    score_source,
)
from afsim_research_agent.literature.library import scan_library
from afsim_research_agent.literature.models import (
    PaperQADocument,
    SourceCandidate,
    SourceCollection,
    SourceCollectionFailure,
    SourceCollectionRequest,
)
from afsim_research_agent.literature.screening import Screener
from afsim_research_agent.literature.verification import MetadataVerifier, normalize_title
from afsim_research_agent.models import SearchHit, SourceMetadata, SourceReference
from afsim_research_agent.web.fetch import (
    FetchedResource,
    FetchError,
    FetchPolicy,
    build_user_agent,
    fetch_url,
)
from afsim_research_agent.web.pdf import PDFDownloadError, save_pdf
from afsim_research_agent.web.reader import (
    WebReadError,
    decode_html,
    extract_webpage,
    write_snapshot,
)
from afsim_research_agent.web.search import SearchError
from afsim_research_agent.web.security import parse_networks

_PAPERQA_NATIVE_EXTENSIONS = frozenset(
    {".pdf", ".md", ".txt", ".html", ".docx", ".pptx", ".xlsx"}
)
_CODE_EXTENSIONS = frozenset(
    {".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".py", ".json", ".yaml", ".yml", ".afsim"}
)
_MIN_WEB_TEXT_CHARS = 400
_MAX_SNIPPET_CHARS = 1500
_MIN_TITLE_KEY_CHARS = 25
_MAX_VERIFICATIONS_PER_ROUND = 40
_NEUTRAL_SCREENING = 0.5

SearchFunction = Callable[[str, int], list[SearchHit]]
Fetcher = Callable[[str], FetchedResource]


@dataclass(frozen=True)
class SearchChannel:
    """One provider configuration applied to one list of queries."""

    name: str
    provider: str
    search: SearchFunction
    queries: Sequence[str]


class CitationGraph(Protocol):
    name: str

    def references(self, work: str, *, limit: int = 20) -> list[SearchHit]: ...

    def citations(self, work: str, *, limit: int = 20) -> list[SearchHit]: ...


@dataclass
class CollectorServices:
    """Optional collaborators; each stage is skipped when its service is absent."""

    screener: Screener | None = None
    verifier: MetadataVerifier | None = None
    graph: CitationGraph | None = None
    graph_reference: Callable[[SourceMetadata], str | None] | None = None
    catalog: DoctrineCatalog | None = None


def build_fetch_policy(app_settings: Settings) -> FetchPolicy:
    return FetchPolicy(
        user_agent=build_user_agent(app_settings.crow_contact_email),
        proxy_url=app_settings.crow_http_proxy,
        allowed_networks=parse_networks(app_settings.crow_allowed_proxy_cidrs),
    )


def build_default_channels(
    request: SourceCollectionRequest,
    app_settings: Settings,
) -> list[SearchChannel]:
    """Create configured channels; providers are constructed lazily on first use."""
    from afsim_research_agent.web.arxiv import ArxivProvider
    from afsim_research_agent.web.core import CoreProvider
    from afsim_research_agent.web.scholarly import OpenAlexSearchProvider
    from afsim_research_agent.web.search import TavilySearchProvider
    from afsim_research_agent.web.semantic_scholar import SemanticScholarProvider

    @cache
    def tavily() -> TavilySearchProvider:
        return TavilySearchProvider.from_environment()

    scholarly_factories = {
        "openalex": cache(OpenAlexSearchProvider.from_environment),
        "semantic_scholar": cache(SemanticScholarProvider.from_environment),
        "arxiv": cache(ArxivProvider.from_environment),
        "core": cache(CoreProvider.from_environment),
    }

    channels = [
        SearchChannel(
            name="web",
            provider="tavily",
            search=lambda query, limit: tavily().search(query, limit=limit),
            queries=request.search_queries,
        )
    ]
    if app_settings.crow_enable_doctrine:
        doctrine_domains = list(app_settings.crow_doctrine_domains)
        channels.append(
            SearchChannel(
                name="doctrine",
                provider="tavily",
                search=lambda query, limit: tavily().search(
                    query, limit=limit, include_domains=doctrine_domains
                ),
                queries=request.doctrine_queries,
            )
        )
    if app_settings.crow_enable_technical_reports:
        report_domains = list(app_settings.crow_technical_report_domains)
        channels.append(
            SearchChannel(
                name="technical_report",
                provider="tavily",
                search=lambda query, limit: tavily().search(
                    query, limit=limit, include_domains=report_domains
                ),
                queries=request.technical_report_queries or request.scholarly_queries[:2],
            )
        )
    if app_settings.crow_enable_scholarly:
        scholarly_queries = request.scholarly_queries or request.search_queries
        for name in app_settings.crow_scholarly_providers:
            if name == "core" and not app_settings.core_api_key_configured:
                continue
            factory = scholarly_factories.get(name)
            if factory is None:
                continue
            channels.append(
                SearchChannel(
                    name="scholarly",
                    provider=name,
                    search=lambda query, limit, factory=factory: factory().search(
                        query, limit=limit
                    ),
                    queries=scholarly_queries,
                )
            )
    return channels


def build_default_services(app_settings: Settings) -> CollectorServices:
    """Screening, DOI verification and citation graph as configured."""
    from afsim_research_agent.literature.screening import LLMScreener
    from afsim_research_agent.web.scholarly import OpenAlexSearchProvider
    from afsim_research_agent.web.semantic_scholar import (
        SemanticScholarProvider,
        paper_reference,
    )

    openalex = OpenAlexSearchProvider.from_environment()
    screener = (
        LLMScreener(app_settings=app_settings)
        if app_settings.crow_enable_screening and app_settings.dashscope_api_key
        else None
    )
    if app_settings.semantic_scholar_api_key_configured:
        graph: CitationGraph = SemanticScholarProvider.from_environment()
        reference = paper_reference
    else:
        graph = openalex

        def reference(metadata: SourceMetadata) -> str | None:
            return metadata.external_ids.get("openalex")

    return CollectorServices(
        screener=screener,
        verifier=MetadataVerifier(openalex.lookup_doi),
        graph=graph if app_settings.crow_snowball_seeds > 0 else None,
        graph_reference=reference,
        catalog=DoctrineCatalog.default(),
    )


def collect_research_sources(
    request: SourceCollectionRequest,
    *,
    output_dir: Path,
    channels: Sequence[SearchChannel] | None = None,
    fetcher: Fetcher | None = None,
    app_settings: Settings | None = None,
    services: CollectorServices | None = None,
) -> SourceCollection:
    """Single-round convenience wrapper around :class:`SourceCollector`."""
    collector = SourceCollector(
        output_dir=output_dir,
        app_settings=app_settings,
        fetcher=fetcher,
        services=services,
    )
    collector.collect(request, channels=channels)
    return collector.snapshot(request.requirement)


class SourceCollector:
    """Stateful collector: deduplication and budgets span every round of a run."""

    def __init__(
        self,
        *,
        output_dir: Path,
        app_settings: Settings | None = None,
        fetcher: Fetcher | None = None,
        services: CollectorServices | None = None,
    ) -> None:
        self.output_dir = output_dir
        self.settings = app_settings or Settings.from_environment()
        self.services = services or CollectorServices()
        if fetcher is None:
            policy = build_fetch_policy(self.settings)

            def fetcher(url: str) -> FetchedResource:
                return fetch_url(url, policy=policy)

        self._fetcher = fetcher
        self.candidates: list[SourceCandidate] = []
        self.documents: list[PaperQADocument] = []
        self.sources: list[SourceReference] = []
        self.failures: list[SourceCollectionFailure] = []
        self.queries: list[str] = []
        self.manual_search_queries: list[str] = []
        self.rounds = 0
        self._by_url: dict[str, SourceCandidate] = {}
        self._by_doi: dict[str, SourceCandidate] = {}
        self._by_title: dict[tuple[str, tuple[str, ...]], SourceCandidate] = {}
        self._by_hash: dict[str, SourceReference] = {}
        self._library_accepted = 0
        self._disabled_providers: dict[str, str] = {}
        self._host_failures: dict[str, int] = {}

    # ------------------------------------------------------------- public API

    def collect(
        self,
        request: SourceCollectionRequest,
        *,
        channels: Sequence[SearchChannel] | None = None,
    ) -> list[PaperQADocument]:
        """Run one round; return only the documents accepted in this round."""
        self.rounds = max(self.rounds, request.round)
        active_channels = (
            list(channels)
            if channels is not None
            else build_default_channels(request, self.settings)
        )
        for channel in active_channels:
            for query in channel.queries:
                if query.strip() and query.strip() not in self.queries:
                    self.queries.append(query.strip())
        for query in request.chinese_queries:
            if query not in self.manual_search_queries:
                self.manual_search_queries.append(query)

        new = self._discover(request, active_channels)
        if request.library_roots and request.round == 1:
            new.extend(self._discover_library(request))
        pending = self._triage(new)
        self._screen(request, pending)

        documents_before = len(self.documents)
        reserve = 0
        if self.services.graph is not None and request.round == 1:
            reserve = min(self.settings.crow_snowball_seeds, request.max_total_sources // 4)
        budget = request.max_total_sources
        round_accepted: list[SourceReference] = []
        leftover = self._acquire(
            pending, request=request, limit=budget - reserve, accepted=round_accepted
        )

        if self.services.graph is not None and self.settings.crow_snowball_seeds > 0:
            snowball = self._triage(self._snowball(round_accepted, request))
            self._screen(request, snowball)
            leftover = sorted([*leftover, *snowball], key=_priority_key)

        leftover = self._acquire(leftover, request=request, limit=budget, accepted=round_accepted)
        for candidate in leftover:
            candidate.decision = "skipped_budget"
            candidate.reason_detail = "source budget filled"

        if request.local_roots and request.round == 1:
            local_documents, local_sources, local_failures = _collect_local_sources(
                request.local_roots,
                output_dir=self.output_dir,
                limit=request.max_local_documents,
            )
            self.documents.extend(local_documents)
            self.sources.extend(local_sources)
            self.failures.extend(local_failures)
        return self.documents[documents_before:]

    def snapshot(self, requirement: str) -> SourceCollection:
        failures = list(self.failures)
        for candidate in self.candidates:
            if candidate.decision in {"failed", "rejected"}:
                failures.append(
                    SourceCollectionFailure(
                        uri=candidate.url,
                        reason=f"{candidate.reason_category}: {candidate.reason_detail}",
                        category=candidate.reason_category or "unknown",
                        stage="prefilter" if candidate.decision == "rejected" else "fetch",
                        detail=candidate.reason_detail,
                        status_code=candidate.http_status,
                        query=candidate.query,
                    )
                )
        return SourceCollection(
            requirement=requirement,
            queries=list(self.queries),
            documents=list(self.documents),
            sources=list(self.sources),
            failures=failures,
            candidates=list(self.candidates),
            manual_search_queries=list(self.manual_search_queries),
            rounds=max(1, self.rounds),
        )

    @property
    def accepted_titles(self) -> list[str]:
        return [source.title for source in self.sources]

    # -------------------------------------------------------------- discovery

    def _new_candidate(
        self, hit: SearchHit, *, channel: str, provider: str, query: str, rank: int,
        request: SourceCollectionRequest, origin: str = "search", seed: str | None = None,
    ) -> SourceCandidate:
        candidate = SourceCandidate(
            candidate_id=f"c{len(self.candidates) + 1:03d}",
            channel=channel,
            provider=provider,
            query=query,
            rank=rank,
            title=hit.title,
            url=hit.url,
            normalized_url=normalize_url(hit.url) if not hit.url.startswith("file:") else hit.url,
            fetch_url=preferred_fetch_url(hit.url),
            alternate_urls=_alternate_urls(hit),
            snippet=hit.snippet[:_MAX_SNIPPET_CHARS],
            provider_score=hit.provider_score,
            metadata=hit.metadata.model_copy(deep=True),
            round=request.round,
            origin=origin,  # type: ignore[arg-type]
            seed_source_id=seed,
        )
        self.candidates.append(candidate)
        return candidate

    def _discover(
        self, request: SourceCollectionRequest, channels: Sequence[SearchChannel]
    ) -> list[SourceCandidate]:
        found: list[SourceCandidate] = []
        for channel in channels:
            for query in channel.queries:
                normalized_query = query.strip()
                if not normalized_query:
                    continue
                if channel.provider in self._disabled_providers:
                    self.failures.append(
                        SourceCollectionFailure(
                            uri=f"search:{channel.name}:{channel.provider}:{normalized_query}",
                            reason="search_failed: provider disabled for this run",
                            category="search_failed",
                            stage="search",
                            detail=self._disabled_providers[channel.provider],
                            query=normalized_query,
                        )
                    )
                    continue
                try:
                    hits = channel.search(normalized_query, request.max_web_results)
                except Exception as exc:  # noqa: BLE001 - one provider must not abort the run
                    detail = str(exc) if isinstance(exc, SearchError) else type(exc).__name__
                    if any(f"HTTP {code}" in detail for code in (401, 403, 429)):
                        # Rate-limited or unauthorized: stop hammering this provider.
                        self._disabled_providers[channel.provider] = (
                            f"disabled after: {detail}"
                        )
                    self.failures.append(
                        SourceCollectionFailure(
                            uri=f"search:{channel.name}:{channel.provider}:{normalized_query}",
                            reason=f"search_failed: {detail}",
                            category="search_failed",
                            stage="search",
                            detail=detail,
                            query=normalized_query,
                        )
                    )
                    continue
                for rank, hit in enumerate(hits, start=1):
                    found.append(
                        self._new_candidate(
                            hit, channel=channel.name, provider=channel.provider,
                            query=normalized_query, rank=rank, request=request,
                        )
                    )
        return found

    def _discover_library(self, request: SourceCollectionRequest) -> list[SourceCandidate]:
        hits, problems = scan_library(request.library_roots)
        for problem in problems:
            self.failures.append(
                SourceCollectionFailure(
                    uri="library", reason=problem, category="unknown", stage="search",
                    detail=problem,
                )
            )
        return [
            self._new_candidate(
                hit, channel="library", provider="local_library", query="(library)",
                rank=rank, request=request, origin="library",
            )
            for rank, hit in enumerate(hits, start=1)
        ]

    def _snowball(
        self, seeds_from: list[SourceReference], request: SourceCollectionRequest
    ) -> list[SourceCandidate]:
        graph = self.services.graph
        reference = self.services.graph_reference
        if graph is None or reference is None:
            return []
        screened = {c.source_id: c for c in self.candidates if c.source_id and c.screening}
        ranked = sorted(
            (
                source
                for source in seeds_from
                if reference(source.metadata)
                and (
                    source.source_id not in screened
                    or screened[source.source_id].screening.score >= 2  # type: ignore[union-attr]
                )
            ),
            key=lambda s: -(s.quality.score if s.quality else 0.0),
        )
        found: list[SourceCandidate] = []
        limit = self.settings.crow_snowball_per_seed
        for source in ranked[: self.settings.crow_snowball_seeds]:
            work = reference(source.metadata)
            directions = (("references", graph.references), ("cited by", graph.citations))
            for direction, call in directions:
                label = f"{direction}: {source.title[:60]}"
                try:
                    hits = call(work, limit=limit)  # type: ignore[arg-type]
                except Exception as exc:  # noqa: BLE001
                    detail = str(exc) if isinstance(exc, SearchError) else type(exc).__name__
                    self.failures.append(
                        SourceCollectionFailure(
                            uri=f"snowball:{graph.name}:{work}",
                            reason=f"search_failed: {detail}",
                            category="search_failed",
                            stage="search",
                            detail=detail,
                            query=label,
                        )
                    )
                    continue
                for rank, hit in enumerate(hits, start=1):
                    found.append(
                        self._new_candidate(
                            hit, channel="snowball", provider=graph.name, query=label,
                            rank=rank, request=request, origin="snowball",
                            seed=source.source_id,
                        )
                    )
        return found

    # ----------------------------------------------------------------- triage

    def _triage(self, candidates: list[SourceCandidate]) -> list[SourceCandidate]:
        catalog = self.services.catalog
        verifier = self.services.verifier
        verifications = 0
        pending: list[SourceCandidate] = []
        for candidate in candidates:
            if self._is_duplicate(candidate):
                continue
            if (
                verifier is not None
                and candidate.metadata.doi
                and candidate.provider != "openalex"
                and verifications < _MAX_VERIFICATIONS_PER_ROUND
            ):
                verifications += 1
                verifier.verify(candidate)

            superseded_by = None
            if catalog is not None:
                for old, current, verified in catalog.superseded_in(
                    f"{candidate.title} {urlparse(candidate.fetch_url).path}"
                ):
                    superseded_by = current
                    candidate.notes.append(
                        f"{old} is superseded by {current}"
                        + ("" if verified else " (lineage unverified)")
                    )
            quality, _ = score_source(
                QualityInput(
                    url=candidate.fetch_url,
                    title=candidate.title,
                    is_pdf=_looks_like_pdf_url(candidate.fetch_url)
                    or bool(candidate.metadata.pdf_url),
                    metadata=candidate.metadata,
                    provider=candidate.provider,
                    superseded_by=superseded_by,
                )
            )
            candidate.preliminary_quality = quality

            if candidate.origin != "library" and is_blocked_host(candidate.fetch_url):
                self._reject(
                    candidate, "blocked_domain",
                    f"{urlparse(candidate.fetch_url).hostname} is video, social or forum "
                    "content and cannot serve as text evidence",
                )
                continue
            if candidate.metadata.is_retracted:
                self._reject(candidate, "retracted", "publication is marked as retracted")
                continue
            if (
                candidate.origin != "library"
                and candidate.metadata.is_open_access is False
                and not candidate.metadata.pdf_url
                and candidate.provider in {"openalex", "semantic_scholar", "core"}
            ):
                self._reject(
                    candidate, "not_open_access",
                    "no open-access full text; acquire manually if needed",
                )
                continue
            candidate.priority = _priority(candidate, screened=False)
            pending.append(candidate)
        pending.sort(key=_priority_key)
        return pending

    def _is_duplicate(self, candidate: SourceCandidate) -> bool:
        first = self._by_url.get(candidate.normalized_url)
        if first is not None:
            candidate.decision = "duplicate_url"
            candidate.duplicate_of = first.candidate_id
            return True
        self._by_url[candidate.normalized_url] = candidate

        doi = normalize_doi(candidate.metadata.doi)
        if doi:
            first = self._by_doi.get(doi)
            if first is not None:
                candidate.decision = "duplicate_doi"
                candidate.duplicate_of = first.candidate_id
                return True
            self._by_doi[doi] = candidate

        title = normalize_title(candidate.title)
        if len(title) >= _MIN_TITLE_KEY_CHARS:
            key = (title, tuple(find_designations(candidate.title)))
            first = self._by_title.get(key)
            if first is not None:
                candidate.decision = "duplicate_title"
                candidate.duplicate_of = first.candidate_id
                return True
            self._by_title[key] = candidate
        return False

    @staticmethod
    def _reject(candidate: SourceCandidate, category: str, detail: str) -> None:
        candidate.decision = "rejected"
        candidate.reason_category = category  # type: ignore[assignment]
        candidate.reason_detail = detail

    # -------------------------------------------------------------- screening

    def _screen(self, request: SourceCollectionRequest, pending: list[SourceCandidate]) -> None:
        screener = self.services.screener
        if screener is None or not pending:
            return
        batch = pending[: self.settings.crow_max_screened_candidates]
        try:
            results = screener.screen(
                request.requirement, request.research_questions or [request.requirement], batch
            )
        except Exception as exc:  # noqa: BLE001 - screening failure keeps deterministic ranking
            self.failures.append(
                SourceCollectionFailure(
                    uri="screening", reason=f"screening failed: {type(exc).__name__}",
                    category="unknown", stage="screening", detail=type(exc).__name__,
                )
            )
            return
        kept: list[SourceCandidate] = []
        for candidate in pending:
            result = results.get(candidate.candidate_id)
            candidate.screening = result
            if result is not None and result.score < self.settings.crow_screen_min_score:
                candidate.decision = "screened_out"
                candidate.reason_category = "screened_out"
                candidate.reason_detail = result.reason or "judged off-topic from title/abstract"
                continue
            candidate.priority = _priority(candidate, screened=True)
            kept.append(candidate)
        pending[:] = sorted(kept, key=_priority_key)

    # ------------------------------------------------------------ acquisition

    def _acquire(
        self,
        pending: list[SourceCandidate],
        *,
        request: SourceCollectionRequest,
        limit: int,
        accepted: list[SourceReference],
    ) -> list[SourceCandidate]:
        """Fetch in priority order, a few in parallel, until ``limit`` web sources are accepted.

        Batches never exceed the remaining budget, so parallelism does not change
        which candidates are accepted, only how long it takes.
        """
        leftover: list[SourceCandidate] = []
        max_attempts = request.max_fetch_attempts or request.max_total_sources * 3
        queue = list(pending)
        while queue:
            need = limit - sum(1 for s in accepted if not s.source_id.startswith("lib:"))
            batch: list[SourceCandidate] = []
            while queue and len(batch) < min(self.settings.crow_fetch_workers, need):
                candidate = queue.pop(0)
                if candidate.origin == "library":
                    self._take_library(candidate, accepted, leftover)
                    continue
                host = (urlparse(candidate.fetch_url).hostname or "").lower()
                if self._host_failures.get(host, 0) >= self.settings.crow_host_failure_limit:
                    self._fail(
                        candidate, "host_circuit_open",
                        f"skipped: {host} failed {self._host_failures[host]} times in this run",
                        None,
                    )
                    continue
                if self._attempts(request.round) + len(batch) >= max_attempts:
                    leftover.append(candidate)
                    continue
                batch.append(candidate)
            if not batch:
                for candidate in queue:
                    if candidate.origin == "library":
                        self._take_library(candidate, accepted, leftover)
                    else:
                        leftover.append(candidate)
                break
            for candidate, outcome in zip(batch, self._fetch_batch(batch), strict=True):
                if isinstance(outcome, Exception):
                    self._record_failure(candidate, outcome)
                    continue
                self._accept(candidate, *outcome, accepted=accepted)
        return leftover

    def _fetch_batch(
        self, batch: list[SourceCandidate]
    ) -> list[tuple[PaperQADocument, SourceReference] | Exception]:
        if len(batch) == 1:
            return [self._fetch_one(batch[0])]
        with ThreadPoolExecutor(max_workers=len(batch)) as pool:
            return list(pool.map(self._fetch_one, batch))

    def _fetch_one(
        self, candidate: SourceCandidate
    ) -> tuple[PaperQADocument, SourceReference] | Exception:
        """Try the primary URL, then known alternates (PDF twin, landing page, DOI)."""
        started = time.perf_counter()
        last_error: Exception | None = None
        try:
            for index, url in enumerate([candidate.fetch_url, *candidate.alternate_urls]):
                try:
                    resource = self._fetcher(url)
                    candidate.attempts += resource.attempts
                    candidate.http_status = resource.status_code
                    candidate.final_url = resource.final_url
                    result = _materialize(
                        candidate, resource, output_dir=self.output_dir,
                        catalog=self.services.catalog,
                    )
                    if index:
                        candidate.notes.append(f"primary URL failed; used alternate {url}")
                    return result
                except FetchError as exc:
                    candidate.attempts += 1
                    last_error = exc
                    if exc.category in {"security_rejected", "too_large"}:
                        break
                except (WebReadError, PDFDownloadError) as exc:
                    candidate.attempts += 1
                    last_error = exc
                except Exception as exc:  # noqa: BLE001 - record and continue
                    candidate.attempts += 1
                    last_error = exc
                    break
            return last_error or RuntimeError("no URL to fetch")
        finally:
            candidate.elapsed_ms = int((time.perf_counter() - started) * 1000)

    def _record_failure(self, candidate: SourceCandidate, error: Exception) -> None:
        if isinstance(error, FetchError):
            self._fail(candidate, error.category, error.detail, error.status_code)
            if error.category in {"timeout", "network_error", "dns_error"} or (
                error.status_code in {401, 403, 429}
            ):
                host = (urlparse(error.url or candidate.fetch_url).hostname or "").lower()
                self._host_failures[host] = self._host_failures.get(host, 0) + 1
        elif isinstance(error, (WebReadError, PDFDownloadError)):
            self._fail(candidate, error.category, str(error), None)
        else:
            self._fail(candidate, "unknown", type(error).__name__, None)

    def _accept(
        self,
        candidate: SourceCandidate,
        document: PaperQADocument,
        source: SourceReference,
        *,
        accepted: list[SourceReference],
    ) -> None:
        existing = self._by_hash.get(source.content_hash)
        if existing is not None:
            candidate.decision = "duplicate_content"
            candidate.duplicate_of = existing.source_id
            candidate.source_id = existing.source_id
            tag = f"{candidate.channel}:{candidate.query}"
            if tag not in existing.discovered_by:
                existing.discovered_by.append(tag)
            return
        self._by_hash[source.content_hash] = source
        candidate.decision = "accepted"
        candidate.source_id = source.source_id
        if candidate.origin == "library":
            self._library_accepted += 1
        self.documents.append(document)
        self.sources.append(source)
        accepted.append(source)

    def _take_library(
        self,
        candidate: SourceCandidate,
        accepted: list[SourceReference],
        leftover: list[SourceCandidate],
    ) -> None:
        if self._library_accepted >= self.settings.crow_max_library_sources:
            leftover.append(candidate)
            return
        try:
            document, source = self._materialize_library(candidate)
        except Exception as exc:  # noqa: BLE001
            self._fail(candidate, "unknown", type(exc).__name__, None)
            return
        self._accept(candidate, document, source, accepted=accepted)

    def _attempts(self, round_number: int) -> int:
        return sum(
            1
            for c in self.candidates
            if c.round == round_number and c.attempts > 0 and c.origin != "library"
        )

    @staticmethod
    def _fail(
        candidate: SourceCandidate, category: str, detail: str, status: int | None
    ) -> None:
        candidate.decision = "failed"
        candidate.reason_category = category  # type: ignore[assignment]
        candidate.reason_detail = detail
        candidate.http_status = status

    def _materialize_library(
        self, candidate: SourceCandidate
    ) -> tuple[PaperQADocument, SourceReference]:
        path = Path(urlparse(candidate.url).path)
        body = path.read_bytes()
        content_hash = hashlib.sha256(body).hexdigest()
        source_id = f"lib:{content_hash[:16]}"
        paperqa_path = path
        if path.suffix.lower() == ".html":
            library_dir = self.output_dir / "library"
            library_dir.mkdir(parents=True, exist_ok=True)
            paperqa_path = library_dir / f"{source_id.replace(':', '-')}.html"
            shutil.copyfile(path, paperqa_path)
        metadata = candidate.metadata
        quality, kind = score_source(
            QualityInput(
                url=candidate.url,
                title=candidate.title,
                is_pdf=path.suffix.lower() == ".pdf",
                metadata=metadata,
                provider="local_library",
            )
        )
        source = SourceReference(
            source_id=source_id,
            kind=kind,
            title=candidate.title,
            uri=metadata.landing_url or candidate.url,
            published_at=datetime(metadata.year, 1, 1, tzinfo=UTC) if metadata.year else None,
            retrieved_at=datetime.now(UTC),
            content_hash=content_hash,
            metadata=metadata,
            quality=quality,
            discovered_by=["library"],
        )
        document = PaperQADocument(
            path=paperqa_path,
            citation=format_citation(candidate.title, source.uri, metadata),
            title=candidate.title,
            doi=normalize_doi(metadata.doi),
            authors=metadata.authors,
            source_id=source_id,
        )
        return document, source


def _alternate_urls(hit: SearchHit) -> list[str]:
    """Other ways to reach the same work, tried in order if the primary URL fails."""
    meta = hit.metadata
    options = [
        meta.pdf_url,
        f"https://arxiv.org/pdf/{meta.external_ids['arxiv']}"
        if meta.external_ids.get("arxiv")
        else None,
        meta.landing_url,
        f"https://doi.org/{meta.doi}" if meta.doi else None,
    ]
    primary = normalize_url(preferred_fetch_url(hit.url))
    result: list[str] = []
    seen = {primary}
    for option in options:
        if not option or option.startswith("file:"):
            continue
        key = normalize_url(option)
        if key not in seen:
            seen.add(key)
            result.append(preferred_fetch_url(option))
    return result[:3]


def _priority(candidate: SourceCandidate, *, screened: bool) -> float:
    quality = candidate.preliminary_quality.score if candidate.preliminary_quality else 0.3
    relevance = (
        candidate.provider_score
        if candidate.provider == "tavily" and candidate.provider_score is not None
        else 1.0 / (1.0 + 0.25 * (candidate.rank - 1))
    )
    relevance = min(1.0, max(0.0, relevance))
    if not screened:
        return round(0.7 * quality + 0.3 * relevance, 4)
    screening = (
        candidate.screening.score / 3 if candidate.screening is not None else _NEUTRAL_SCREENING
    )
    return round(0.45 * quality + 0.40 * screening + 0.15 * relevance, 4)


def _priority_key(candidate: SourceCandidate) -> tuple[float, str]:
    return (-(candidate.priority or 0.0), candidate.candidate_id)


def _materialize(
    candidate: SourceCandidate,
    resource: FetchedResource,
    *,
    output_dir: Path,
    catalog: DoctrineCatalog | None = None,
) -> tuple[PaperQADocument, SourceReference]:
    retrieved_at = datetime.now(UTC)
    metadata = candidate.metadata
    if resource.kind == "pdf":
        downloaded = save_pdf(resource, output_dir / "pdf")
        content_hash = downloaded.content_hash
        source_id = f"pdf:{content_hash[:16]}"
        path = downloaded.path
        title = candidate.title
        published_at = None
    else:
        html = decode_html(resource)
        webpage = extract_webpage(
            html,
            url=resource.final_url,
            status_code=resource.status_code,
            retrieved_at=retrieved_at,
        )
        if len(webpage.text) < _MIN_WEB_TEXT_CHARS:
            raise WebReadError(
                f"only {len(webpage.text)} characters of main text (likely a landing, "
                "cookie or login page)",
                category="no_extractable_text",
            )
        write_snapshot(output_dir / "raw", webpage, html)
        content_hash = webpage.source.content_hash
        source_id = webpage.source.source_id
        title = candidate.title or webpage.source.title
        published_at = webpage.source.published_at
        web_dir = output_dir / "web"
        web_dir.mkdir(parents=True, exist_ok=True)
        path = web_dir / f"{source_id.replace(':', '-')}.md"
        path.write_text(
            f"# {webpage.source.title}\n\nSource: {resource.final_url}\n\n{webpage.text}\n",
            encoding="utf-8",
        )

    superseded = catalog.superseded_in(title) if catalog is not None else []
    quality, kind = score_source(
        QualityInput(
            url=resource.final_url,
            title=title,
            is_pdf=resource.kind == "pdf",
            metadata=metadata,
            provider=candidate.provider,
            superseded_by=superseded[0][1] if superseded else None,
        )
    )
    if metadata.year and published_at is None:
        published_at = datetime(metadata.year, 1, 1, tzinfo=UTC)
    source = SourceReference(
        source_id=source_id,
        kind=kind,
        title=title,
        uri=resource.final_url,
        published_at=published_at,
        retrieved_at=retrieved_at,
        content_hash=content_hash,
        metadata=metadata,
        quality=quality,
        discovered_by=[f"{candidate.channel}:{candidate.query}"],
    )
    document = PaperQADocument(
        path=path,
        citation=format_citation(title, resource.final_url, metadata),
        title=title,
        doi=normalize_doi(metadata.doi),
        authors=metadata.authors,
        source_id=source_id,
    )
    return document, source


def format_citation(title: str, url: str, metadata: SourceMetadata) -> str:
    """Build a readable citation from whatever metadata is actually known."""
    parts: list[str] = []
    if metadata.authors:
        authors = ", ".join(metadata.authors[:3])
        if len(metadata.authors) > 3:
            authors += " et al."
        parts.append(authors)
    if metadata.year:
        parts.append(f"({metadata.year})")
    head = " ".join(parts)
    citation = f"{head}. {title}." if head else f"{title}."
    if metadata.venue:
        citation += f" {metadata.venue}."
    doi = normalize_doi(metadata.doi)
    citation += f" https://doi.org/{doi}" if doi else f" {url}"
    return citation


def _looks_like_pdf_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(".pdf") or "/pdf/" in path


# ------------------------------------------------------------ local sources


def _collect_local_sources(
    roots: list[Path],
    *,
    output_dir: Path,
    limit: int,
) -> tuple[list[PaperQADocument], list[SourceReference], list[SourceCollectionFailure]]:
    documents: list[PaperQADocument] = []
    sources: list[SourceReference] = []
    failures: list[SourceCollectionFailure] = []
    seen: set[Path] = set()

    candidates: list[Path] = []
    for root in roots:
        resolved_root = root.expanduser().resolve()
        if not resolved_root.is_dir():
            failures.append(
                SourceCollectionFailure(
                    uri=str(root),
                    reason="local source root is unavailable",
                    category="unknown",
                    stage="fetch",
                )
            )
            continue
        for path in sorted(resolved_root.rglob("*")):
            if len(candidates) >= limit:
                break
            relative_parts = path.relative_to(resolved_root).parts
            if not path.is_file() or any(part.startswith(".") for part in relative_parts):
                continue
            if path.suffix.lower() not in _PAPERQA_NATIVE_EXTENSIONS | _CODE_EXTENSIONS:
                continue
            resolved_path = path.resolve()
            if resolved_path not in seen:
                seen.add(resolved_path)
                candidates.append(resolved_path)

    for path in candidates:
        try:
            document, source = _materialize_local_source(path, output_dir=output_dir)
        except (OSError, UnicodeError):
            failures.append(
                SourceCollectionFailure(
                    uri=str(path),
                    reason="local source could not be read",
                    category="unknown",
                    stage="fetch",
                )
            )
            continue
        documents.append(document)
        sources.append(source)
    return documents, sources, failures


def _materialize_local_source(
    path: Path,
    *,
    output_dir: Path,
) -> tuple[PaperQADocument, SourceReference]:
    body = path.read_bytes()
    content_hash = hashlib.sha256(body).hexdigest()
    path_key = hashlib.sha256(str(path).encode()).hexdigest()[:16]
    source_id = f"local:{path_key}"
    is_code = path.suffix.lower() in _CODE_EXTENSIONS
    paperqa_path = path
    if is_code:
        local_dir = output_dir / "local"
        local_dir.mkdir(parents=True, exist_ok=True)
        paperqa_path = local_dir / f"{path_key}.txt"
        paperqa_path.write_text(body.decode("utf-8"), encoding="utf-8")

    source = SourceReference(
        source_id=source_id,
        kind="local_code" if is_code else "local_document",
        title=path.name,
        uri=str(path),
        retrieved_at=datetime.now(UTC),
        content_hash=content_hash,
    )
    return (
        PaperQADocument(
            path=paperqa_path,
            citation=f"{path.name}. Local resource.",
            title=path.name,
            source_id=source_id,
        ),
        source,
    )
