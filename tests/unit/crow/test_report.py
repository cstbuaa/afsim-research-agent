"""Tests for the human-readable Tactical Crow research bundle."""

from __future__ import annotations

from pathlib import Path

from crow_fixtures import evidence, result, source

from afsim_research_agent.crow.models import CrowResearchResult
from afsim_research_agent.crow.readiness import assess_readiness
from afsim_research_agent.crow.report import write_research_bundle
from afsim_research_agent.literature import PaperQAReport, SourceCollectionFailure
from afsim_research_agent.literature.models import SourceCandidate
from afsim_research_agent.models import SourceQuality


def candidate(cid: str, decision: str, **kwargs: object) -> SourceCandidate:
    return SourceCandidate(
        candidate_id=cid,
        channel="doctrine",
        provider="tavily",
        query="ATP 3-20.15 formations",
        rank=1,
        title=f"Candidate {cid}",
        url=f"https://example.mil/{cid}",
        normalized_url=f"https://example.mil/{cid}",
        fetch_url=f"https://example.mil/{cid}",
        decision=decision,
        **kwargs,
    )


def test_bundle_shows_quality_ledger_worklist_and_gate(tmp_path: Path) -> None:
    good = SourceQuality(score=0.88, tier="A", reasons=["official"], rules_version="t")
    built = result(
        sources=[source("pdf:a", "A", 0.88)],
        reports=[
            PaperQAReport(
                question="什么条件触发编队转换？",
                answer="威胁评估结果满足转换条件时触发。",
                evidence=[evidence("pdf:a", 9)],
            )
        ],
        candidates=[
            candidate("c001", "accepted", source_id="pdf:a", preliminary_quality=good),
            candidate(
                "c002",
                "failed",
                reason_category="http_error",
                reason_detail="HTTP 403 (access denied)",
                http_status=403,
                preliminary_quality=good,
            ),
            candidate("c003", "duplicate_content", duplicate_of="pdf:a"),
        ],
        failures=[
            SourceCollectionFailure(
                uri="https://example.mil/c002",
                reason="http_error: HTTP 403",
                category="http_error",
                stage="fetch",
                detail="HTTP 403 (access denied)",
                status_code=403,
            )
        ],
    )
    built.reports[0].evidence[0].source_quality_tier = "A"
    built.reports[0].evidence[0].source_quality_score = 0.88
    built.readiness = assess_readiness(built)

    markdown_path, json_path = write_research_bundle(built, tmp_path)
    markdown = markdown_path.read_text(encoding="utf-8")

    for expected in [
        "## 0. 结论摘要",
        "Falcon 输入门槛：❌ 未通过",
        "| 可用来源数 | 未通过 | 1 | ≥ 3 |",
        "候选来源 3 个：采用 1，重复内容（镜像） 1，采集失败 1。",
        "### 学术索引检索式（OpenAlex / Semantic Scholar / arXiv / CORE）",
        "### 技术报告检索式（DTIC / NPS / RAND / AFIT）",
        "### 3.5 中文文献人工检索清单",
        "[知网](https://kns.cnki.net/kns8s/defaultresult/index?kw=",
        "### 条令与官方文献检索式",
        "来源质量：**A 级**（0.88）",
        "`http_error` 1",
        "### 3.4 人工补采清单",
        "[A 级] Candidate c002",
        "相关性评分：9/10",
        "来源质量：A 级（0.88）",
        "来源 ID：`pdf:a`",
        "## 附录 A. 候选来源决策台账",
        "| c003 | 1 | doctrine / tavily |",
        "同 pdf:a",
    ]:
        assert expected in markdown, expected

    restored = CrowResearchResult.model_validate_json(json_path.read_text(encoding="utf-8"))
    assert restored == built


def test_old_json_without_new_fields_still_loads() -> None:
    legacy = {
        "plan": {
            "requirement_summary": "s",
            "research_questions": ["q"],
            "search_queries": ["s"],
            "key_terms": [],
        },
        "collection": {
            "requirement": "r",
            "queries": ["s"],
            "documents": [],
            "sources": [],
            "failures": [{"uri": "https://x", "reason": "source collection failed"}],
        },
        "reports": [],
    }

    restored = CrowResearchResult.model_validate(legacy)

    assert restored.collection.failures[0].category == "unknown"
    assert restored.manifest is None


def test_collection_diagnostics_summarize_losses() -> None:
    from afsim_research_agent.crow.report import collection_diagnostics

    built = result(
        candidates=[
            candidate("c1", "accepted"),
            candidate("c2", "failed", reason_category="http_error", http_status=403),
            candidate("c3", "failed", reason_category="timeout"),
        ],
        failures=[
            SourceCollectionFailure(
                uri="search:x", reason="r", category="search_failed", stage="search",
                detail="Semantic Scholar returned HTTP 429",
            )
        ],
    )

    text = "\n".join(collection_diagnostics(built.collection))

    assert "http_error 403 1" in text and "timeout 1" in text
    assert "example.mil 2" in text
    assert "doctrine/tavily 3/1" in text
    assert "Semantic Scholar returned HTTP 429 ×1" in text
