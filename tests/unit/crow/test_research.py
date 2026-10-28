"""Tests for the Tactical Crow research pipeline, including the gap round."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

from crow_fixtures import source

from afsim_research_agent.config import Settings
from afsim_research_agent.crow import CrowResearchPipeline
from afsim_research_agent.crow.models import FollowupDraft, TacticalResearchPlan
from afsim_research_agent.literature import (
    PaperQADocument,
    PaperQAEvidence,
    PaperQAReport,
    SourceCollection,
)
from afsim_research_agent.literature.models import CorpusIngestFailure
from afsim_research_agent.literature.paperqa_backend import PaperQACorpus


class FakePlanner:
    def __init__(self, followup: FollowupDraft | None = None) -> None:
        self.followup = followup
        self.followup_calls: list[list[str]] = []

    async def plan(self, requirement: str) -> TacticalResearchPlan:
        return TacticalResearchPlan(
            requirement_summary=requirement,
            research_questions=["question one", "question two"],
            search_queries=["query one", "query two"],
            scholarly_queries=["scholarly one"],
            doctrine_queries=["doctrine one"],
            technical_report_queries=["report one"],
            chinese_queries=["编队"],
            key_terms=["formation"],
        )

    async def plan_followup(self, requirement, plan, weak, titles) -> FollowupDraft:
        self.followup_calls.append(list(weak))
        return self.followup or FollowupDraft()


class FakeCollector:
    def __init__(self, rounds: list[list[str]]) -> None:
        self.rounds = rounds
        self.requests: list[object] = []
        self.sources = []
        self.documents: list[PaperQADocument] = []

    def collect(self, request, *, channels=None) -> list[PaperQADocument]:
        self.requests.append(request)
        ids = self.rounds[len(self.requests) - 1] if len(self.requests) <= len(self.rounds) else []
        new = [
            PaperQADocument(path=Path(f"/tmp/{i}.pdf"), citation=f"c {i}", source_id=i)
            for i in ids
        ]
        self.documents.extend(new)
        self.sources.extend(source(i, "A", 0.9) for i in ids)
        return new

    def snapshot(self, requirement: str) -> SourceCollection:
        return SourceCollection(
            requirement=requirement,
            queries=[],
            documents=list(self.documents),
            sources=list(self.sources),
            rounds=len(self.requests),
        )

    @property
    def accepted_titles(self) -> list[str]:
        return [s.title for s in self.sources]


class FakeBackend:
    """Answers with strong evidence from every source in the corpus."""

    def __init__(self, *, fail_all: bool = False, evidence_per_source: int = 1) -> None:
        self.built: list[list[str]] = []
        self.added: list[list[str]] = []
        self.questions: list[str] = []
        self.fail_all = fail_all
        self.evidence_per_source = evidence_per_source

    async def build_corpus(self, documents: Sequence[PaperQADocument]) -> PaperQACorpus:
        self.built.append([d.source_id for d in documents])
        corpus = PaperQACorpus(docs=object(), documents=list(documents))
        if self.fail_all:
            corpus.ingest_failures = [
                CorpusIngestFailure(source_id=d.source_id, path=str(d.path), detail="x")
                for d in documents
            ]
        return corpus

    async def add_to_corpus(self, corpus, documents):
        self.added.append([d.source_id for d in documents])
        corpus.documents.extend(documents)
        return []

    async def query_corpus(self, question: str, corpus: PaperQACorpus) -> PaperQAReport:
        self.questions.append(question)
        return PaperQAReport(
            question=question,
            answer=f"answer for {question}",
            evidence=[
                PaperQAEvidence(
                    citation="c", excerpt="e", summary="s", location="p1", score=8,
                    source_id=document.source_id,
                )
                for document in corpus.documents
                for _ in range(self.evidence_per_source)
            ],
        )


def pipeline(planner, collector, backend) -> CrowResearchPipeline:
    return CrowResearchPipeline(
        planner=planner,
        collector_factory=lambda output_dir: collector,
        literature_backend=backend,
        app_settings=Settings.from_environment(),
    )


def test_round_one_embeds_once_and_passes_every_query_family(tmp_path: Path) -> None:
    collector = FakeCollector([["a", "b", "c"]])
    backend = FakeBackend()

    result = asyncio.run(
        pipeline(FakePlanner(), collector, backend).run(
            "formation requirement",
            output_dir=tmp_path / "20261012-220000",
            max_web_results=4,
            max_total_sources=7,
            library_roots=[tmp_path / "lib"],
        )
    )

    request = collector.requests[0]
    assert request.scholarly_queries == ["scholarly one"]
    assert request.doctrine_queries == ["doctrine one"]
    assert request.technical_report_queries == ["report one"]
    assert request.chinese_queries == ["编队"]
    assert request.research_questions == ["question one", "question two"]
    assert request.library_roots == [tmp_path / "lib"]
    assert (request.max_web_results, request.max_total_sources) == (4, 7)
    assert backend.built == [["a", "b", "c"]]
    assert result.readiness.ready
    assert len(collector.requests) == 1  # nothing weak, no gap round
    evidence = result.reports[0].evidence[0]
    assert (evidence.source_quality_tier, evidence.source_quality_score) == ("A", 0.9)
    assert [t.stage for t in result.manifest.timings] == [
        "plan", "collect_round_1", "embed_round_1", "answer_round_1"
    ]


def test_gap_round_targets_weak_questions_and_extends_the_corpus(tmp_path: Path) -> None:
    planner = FakePlanner(FollowupDraft(scholarly_queries=["new angle"]))
    collector = FakeCollector([["a"], ["b", "c"]])
    backend = FakeBackend()

    result = asyncio.run(
        pipeline(planner, collector, backend).run("r", output_dir=tmp_path, gap_rounds=1)
    )

    assert planner.followup_calls == [["question one", "question two"]]
    second = collector.requests[1]
    assert second.round == 2
    assert second.scholarly_queries == ["new angle"]
    assert second.research_questions == ["question one", "question two"]
    assert backend.built == [["a"]]
    assert backend.added == [["b", "c"]]
    assert backend.questions.count("question one") == 2
    assert result.plan.followup_rounds[0].scholarly_queries == ["new angle"]
    assert result.reports[0].evidence and len(result.reports[0].evidence) == 3
    assert "collect_round_2" in [t.stage for t in result.manifest.timings]


def test_gap_round_stops_when_planner_has_nothing_new(tmp_path: Path) -> None:
    collector = FakeCollector([["a"]])

    asyncio.run(pipeline(FakePlanner(), collector, FakeBackend()).run("r", output_dir=tmp_path))

    assert len(collector.requests) == 1


def test_gap_rounds_can_be_disabled(tmp_path: Path) -> None:
    planner = FakePlanner(FollowupDraft(search_queries=["x"]))
    collector = FakeCollector([["a"], ["b"]])

    asyncio.run(
        pipeline(planner, collector, FakeBackend()).run("r", output_dir=tmp_path, gap_rounds=0)
    )

    assert len(collector.requests) == 1 and not planner.followup_calls


def test_empty_first_round_builds_corpus_in_gap_round(tmp_path: Path) -> None:
    planner = FakePlanner(FollowupDraft(search_queries=["x"]))
    collector = FakeCollector([[], ["a"]])
    backend = FakeBackend()

    result = asyncio.run(pipeline(planner, collector, backend).run("r", output_dir=tmp_path))

    assert backend.built == [["a"]]
    assert len(result.reports) == 2


def test_unparseable_corpus_skips_questions(tmp_path: Path) -> None:
    backend = FakeBackend(fail_all=True)

    result = asyncio.run(
        pipeline(FakePlanner(), FakeCollector([["a"]]), backend).run(
            "r", output_dir=tmp_path, gap_rounds=0
        )
    )

    assert len(result.ingest_failures) == 1
    assert not result.readiness.ready


class ProfilePlanner(FakePlanner):
    def __init__(self) -> None:
        super().__init__()
        self.profiles: list[object] = []

    async def plan(self, requirement: str, *, mission_profile=None) -> TacticalResearchPlan:
        self.profiles.append(mission_profile)
        plan = await super().plan(requirement)
        plan.analogy_queries = ["leapfrog naval escort"]
        return plan


def test_profile_reaches_planner_and_result_and_analogies_are_searched(tmp_path: Path) -> None:
    from afsim_research_agent.crow.mission import MissionDraft, normalize_profile
    from afsim_research_agent.crow.report import write_research_bundle

    profile = normalize_profile(
        MissionDraft.model_validate(
            {
                "domain": "ground",
                "echelon": "platoon",
                "mission": {"type": "movement_to_contact", "objective": "reach the town"},
            }
        ),
        "formation requirement",
    )
    planner = ProfilePlanner()
    collector = FakeCollector([["a", "b", "c"]])

    result = asyncio.run(
        pipeline(planner, collector, FakeBackend()).run(
            "formation requirement", output_dir=tmp_path / "run", mission_profile=profile
        )
    )

    assert planner.profiles == [profile]
    assert result.mission_profile == profile
    assert collector.requests[0].search_queries == [
        "query one", "query two", "leapfrog naval escort"
    ]
    markdown, _ = write_research_bundle(result, tmp_path / "out")
    assert (tmp_path / "out" / "mission_profile.yaml").is_file()
    text = markdown.read_text(encoding="utf-8")
    assert "### 任务画像" in text and "### 相似场景检索式" in text
