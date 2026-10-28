"""Tests for the internet-only Tactical Crow command-line workflow."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from afsim_research_agent.crow.cli import run_research
from afsim_research_agent.crow.models import CrowResearchResult, TacticalResearchPlan
from afsim_research_agent.literature import SourceCollection
from afsim_research_agent.models import SourceReference


class FakePipeline:
    def __init__(self, result: CrowResearchResult) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    async def run(self, requirement: str, **kwargs: object) -> CrowResearchResult:
        self.calls.append({"requirement": requirement, **kwargs})
        return self.result


def test_run_research_uses_only_internet_sources_and_writes_report(tmp_path: Path) -> None:
    result = CrowResearchResult(
        plan=TacticalResearchPlan(
            requirement_summary="研究协同编队转换",
            research_questions=["如何触发转换？"],
            search_queries=["cooperative formation transition"],
        ),
        collection=SourceCollection(
            requirement="研究协同编队转换",
            queries=["cooperative formation transition"],
            sources=[
                SourceReference(
                    source_id="web:paper",
                    kind="paper",
                    title="Formation paper",
                    uri="https://example.org/paper.pdf",
                    retrieved_at=datetime(2026, 10, 12, tzinfo=UTC),
                    content_hash="hash",
                )
            ],
        ),
    )
    pipeline = FakePipeline(result)

    markdown_path, json_path = asyncio.run(
        run_research(
            "研究协同编队转换",
            output_dir=tmp_path,
            max_web_results=2,
            pipeline=pipeline,
        )
    )

    assert pipeline.calls == [
        {
            "requirement": "研究协同编队转换",
            "local_roots": (),
            "output_dir": tmp_path,
            "max_web_results": 2,
            "max_total_sources": None,
            "library_roots": None,
            "gap_rounds": None,
        }
    ]
    assert markdown_path.is_file()
    assert json_path.is_file()

