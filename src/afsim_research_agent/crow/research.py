"""Orchestrate Tactical Crow planning, collection, and evidence queries.

Round 1 runs the full plan. When the Falcon gate shows research questions with
too little strong evidence, a bounded follow-up round asks the planner for new,
differently-worded queries for exactly those questions, collects more sources
(deduplicated against everything already seen), adds them to the same PaperQA
corpus and re-answers only the weak questions.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TypeVar

from afsim_research_agent.config import Settings
from afsim_research_agent.crow.manifest import new_manifest
from afsim_research_agent.crow.mission import MissionProfile
from afsim_research_agent.crow.models import (
    CrowResearchResult,
    FollowupDraft,
    RunManifest,
    StageTiming,
    TacticalResearchPlan,
)
from afsim_research_agent.crow.planner import TacticalResearchPlanner
from afsim_research_agent.crow.readiness import ReadinessThresholds, assess_readiness
from afsim_research_agent.literature.collection import (
    SearchChannel,
    SourceCollector,
    build_default_services,
)
from afsim_research_agent.literature.models import (
    CorpusIngestFailure,
    PaperQADocument,
    PaperQAReport,
    SourceCollection,
    SourceCollectionRequest,
)
from afsim_research_agent.literature.paperqa_backend import PaperQABackend, PaperQACorpus


class ResearchPlanner(Protocol):
    async def plan(self, requirement: str) -> TacticalResearchPlan: ...


class LiteratureBackend(Protocol):
    async def build_corpus(self, documents: Sequence[PaperQADocument]) -> PaperQACorpus: ...

    async def add_to_corpus(
        self, corpus: PaperQACorpus, documents: Sequence[PaperQADocument]
    ) -> list[CorpusIngestFailure]: ...

    async def query_corpus(self, question: str, corpus: PaperQACorpus) -> PaperQAReport: ...


class Collector(Protocol):
    def collect(
        self,
        request: SourceCollectionRequest,
        *,
        channels: Sequence[SearchChannel] | None = None,
    ) -> list[PaperQADocument]: ...

    def snapshot(self, requirement: str) -> SourceCollection: ...

    @property
    def accepted_titles(self) -> list[str]: ...


CollectorFactory = Callable[[Path], Collector]
T = TypeVar("T")


class CrowResearchPipeline:
    """Run the evidence-building portion of Tactical Crow."""

    def __init__(
        self,
        *,
        planner: ResearchPlanner | None = None,
        collector_factory: CollectorFactory | None = None,
        literature_backend: LiteratureBackend | None = None,
        app_settings: Settings | None = None,
        readiness_thresholds: ReadinessThresholds | None = None,
    ) -> None:
        self._settings = app_settings or Settings.from_environment()
        self._planner = planner or TacticalResearchPlanner(app_settings=self._settings)
        self._collector_factory = collector_factory or self._default_collector
        self._literature_backend = literature_backend or PaperQABackend(
            app_settings=self._settings
        )
        self._thresholds = readiness_thresholds or ReadinessThresholds()

    def _default_collector(self, output_dir: Path) -> Collector:
        return SourceCollector(
            output_dir=output_dir,
            app_settings=self._settings,
            services=build_default_services(self._settings),
        )

    async def run(
        self,
        requirement: str,
        *,
        local_roots: Sequence[Path] = (),
        library_roots: Sequence[Path] | None = None,
        output_dir: Path | None = None,
        max_web_results: int = 5,
        max_total_sources: int | None = None,
        gap_rounds: int | None = None,
        mission_profile: MissionProfile | None = None,
    ) -> CrowResearchResult:
        """Plan, collect, embed once, answer, and fill evidence gaps."""
        active_output_dir = output_dir or (
            self._settings.data_dir
            / "runs"
            / "crow-research"
            / datetime.now().strftime("%Y%m%d-%H%M%S")
        )
        budget = max_total_sources or self._settings.crow_max_sources
        rounds_allowed = self._settings.crow_gap_rounds if gap_rounds is None else gap_rounds
        libraries = (
            list(library_roots)
            if library_roots is not None
            else [Path(p) for p in self._settings.crow_library_dirs]
        )
        manifest = new_manifest(
            requirement,
            run_id=active_output_dir.name,
            app_settings=self._settings,
            search_settings={
                "max_web_results_per_query": max_web_results,
                "max_total_sources": budget,
                "gap_rounds": rounds_allowed,
                "library_roots": [str(p) for p in libraries],
            },
        )

        started = time.perf_counter()
        if mission_profile is None:
            plan = await self._planner.plan(requirement)
        else:
            plan = await self._planner.plan(requirement, mission_profile=mission_profile)
        manifest.timings.append(_timing("plan", started))

        collector = self._collector_factory(active_output_dir)
        request = SourceCollectionRequest(
            requirement=requirement.strip(),
            search_queries=[*plan.search_queries, *plan.analogy_queries],
            scholarly_queries=plan.scholarly_queries,
            doctrine_queries=plan.doctrine_queries,
            technical_report_queries=plan.technical_report_queries,
            chinese_queries=plan.chinese_queries,
            research_questions=plan.research_questions,
            local_roots=list(local_roots),
            library_roots=libraries,
            max_web_results=max_web_results,
            max_total_sources=budget,
        )
        started = time.perf_counter()
        documents = await _in_thread(collector.collect, request)
        manifest.timings.append(_timing("collect_round_1", started))

        corpus: PaperQACorpus | None = None
        reports: dict[str, PaperQAReport] = {}
        if documents:
            started = time.perf_counter()
            corpus = await self._literature_backend.build_corpus(documents)
            manifest.timings.append(_timing("embed_round_1", started))
            if not corpus.is_empty:
                started = time.perf_counter()
                for question in plan.research_questions:
                    reports[question] = await self._literature_backend.query_corpus(
                        question, corpus
                    )
                manifest.timings.append(_timing("answer_round_1", started))

        assemble_args = (requirement, plan, collector)
        result = self._assemble(*assemble_args, reports, corpus, manifest, mission_profile)

        followup_planner = getattr(self._planner, "plan_followup", None)
        for round_number in range(2, rounds_allowed + 2):
            if followup_planner is None or result.readiness is None:
                break
            weak = [
                item.question
                for item in result.readiness.coverage
                if item.insufficient_evidence
                or item.strong_evidence < self._thresholds.min_strong_evidence_per_question
            ]
            if not result.reports:
                weak = list(plan.research_questions)
            if not weak:
                break
            started = time.perf_counter()
            followup: FollowupDraft = await followup_planner(
                requirement, plan, weak, collector.accepted_titles
            )
            if not (
                followup.search_queries or followup.scholarly_queries or followup.doctrine_queries
            ):
                break
            plan.followup_rounds.append(followup)
            new_documents = await _in_thread(
                collector.collect,
                SourceCollectionRequest(
                    requirement=requirement.strip(),
                    search_queries=followup.search_queries,
                    scholarly_queries=followup.scholarly_queries,
                    doctrine_queries=followup.doctrine_queries,
                    research_questions=weak,
                    max_web_results=max_web_results,
                    max_total_sources=max(1, self._settings.crow_gap_round_sources),
                    round=round_number,
                ),
            )
            manifest.timings.append(_timing(f"collect_round_{round_number}", started))
            if not new_documents:
                break
            started = time.perf_counter()
            if corpus is None:
                corpus = await self._literature_backend.build_corpus(new_documents)
            else:
                await self._literature_backend.add_to_corpus(corpus, new_documents)
            if corpus.is_empty:
                break
            for question in weak:
                reports[question] = await self._literature_backend.query_corpus(question, corpus)
            manifest.timings.append(_timing(f"answer_round_{round_number}", started))
            result = self._assemble(*assemble_args, reports, corpus, manifest, mission_profile)

        manifest.finished_at = datetime.now(UTC)
        return result

    def _assemble(
        self,
        requirement: str,
        plan: TacticalResearchPlan,
        collector: Collector,
        reports: dict[str, PaperQAReport],
        corpus: PaperQACorpus | None,
        manifest: RunManifest,
        mission_profile: MissionProfile | None = None,
    ) -> CrowResearchResult:
        collection = collector.snapshot(requirement.strip())
        quality = {
            source.source_id: source.quality
            for source in collection.sources
            if source.quality is not None
        }
        ordered = [reports[q] for q in plan.research_questions if q in reports]
        for report in ordered:
            for item in report.evidence:
                source_quality = quality.get(item.source_id or "")
                if source_quality is not None:
                    item.source_quality_tier = source_quality.tier
                    item.source_quality_score = source_quality.score
        result = CrowResearchResult(
            plan=plan,
            mission_profile=mission_profile,
            collection=collection,
            reports=ordered,
            ingest_failures=list(corpus.ingest_failures) if corpus else [],
            manifest=manifest,
        )
        result.readiness = assess_readiness(result, self._thresholds)
        return result


async def _in_thread(function: Callable[..., T], *args: object) -> T:
    return await asyncio.to_thread(function, *args)


def _timing(stage: str, started: float) -> StageTiming:
    return StageTiming(stage=stage, seconds=round(time.perf_counter() - started, 2))
