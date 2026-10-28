"""Write inspectable Tactical Crow research results."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from urllib.parse import quote

from afsim_research_agent.crow.dimensions import PatternTable, dimension_label
from afsim_research_agent.crow.mission import MissionProfile, write_profile
from afsim_research_agent.crow.models import CrowResearchResult
from afsim_research_agent.literature.models import SourceCandidate

_DECISION_LABELS = {
    "accepted": "采用",
    "duplicate_url": "重复 URL",
    "duplicate_doi": "重复 DOI",
    "duplicate_content": "重复内容（镜像）",
    "duplicate_title": "重复标题",
    "rejected": "预筛拒绝",
    "screened_out": "摘要筛除",
    "failed": "采集失败",
    "skipped_budget": "超出预算未采集",
}
_CHECK_LABELS = {
    "usable_sources": "可用来源数",
    "authoritative_sources": "权威来源数",
    "question_coverage": "研究问题覆盖率",
    "authoritative_evidence_share": "强证据权威占比",
    "insufficient_answers": "资料不足回答占比",
    "dimension_coverage": "研究维度覆盖数",
}


def render_research_markdown(result: CrowResearchResult) -> str:
    """Render one complete research result for human review."""
    lines: list[str] = ["# Tactical Crow 研究报告", ""]
    _render_summary(result, lines)
    _render_plan(result, lines)
    _render_sources(result, lines)
    _render_reports(result, lines)
    _render_ledger(result, lines)
    _render_manifest(result, lines)
    return "\n".join(lines).rstrip() + "\n"


def _render_summary(result: CrowResearchResult, lines: list[str]) -> None:
    lines.extend(["## 0. 结论摘要", ""])
    readiness = result.readiness
    if readiness is None:
        lines.extend(["未执行 Falcon 输入门槛评估。", ""])
    else:
        verdict = "✅ 通过，可交给 Tactical Falcon" if readiness.ready else (
            "❌ 未通过，需补充资料或人工判断后再进入 Falcon"
        )
        lines.extend(
            [
                f"**Falcon 输入门槛：{verdict}**（{readiness.gate_version}）",
                "",
                "| 检查项 | 结果 | 实际值 | 门槛 | 说明 |",
                "| --- | --- | ---: | ---: | --- |",
            ]
        )
        for check in readiness.checks:
            comparator = "≤" if check.name == "insufficient_answers" else "≥"
            lines.append(
                f"| {_CHECK_LABELS.get(check.name, check.name)} | "
                f"{'通过' if check.passed else '未通过'} | {_num(check.value)} | "
                f"{comparator} {_num(check.threshold)} | {check.detail} |"
            )
        if readiness.coverage:
            lines.extend(
                [
                    "",
                    "| 研究问题 | 强证据 | 其中权威 | 独立来源 | 资料不足 |",
                    "| --- | ---: | ---: | ---: | --- |",
                ]
            )
            for item in readiness.coverage:
                lines.append(
                    f"| {_cell(item.question)} | {item.strong_evidence} | "
                    f"{item.authoritative_evidence} | {item.distinct_sources} | "
                    f"{'是' if item.insufficient_evidence else '否'} |"
                )
        if readiness.dimensions:
            lines.extend(
                ["", "| 研究维度 | 强证据 | 覆盖 | 相关问题数 |", "| --- | ---: | --- | ---: |"]
            )
            for row in readiness.dimensions:
                lines.append(
                    f"| {row.label} | {row.strong_evidence} | "
                    f"{'是' if row.covered else '否'} | {len(row.questions)} |"
                )
        lines.append("")

    counts = Counter(candidate.decision for candidate in result.collection.candidates)
    if counts:
        summary = "，".join(
            f"{_DECISION_LABELS[decision]} {counts[decision]}"
            for decision in _DECISION_LABELS
            if counts.get(decision)
        )
        lines.extend([f"候选来源 {sum(counts.values())} 个：{summary}。", ""])
    screened = [c.screening.score for c in result.collection.candidates if c.screening]
    if screened:
        distribution = Counter(screened)
        lines.extend(
            [
                f"摘要筛选 {len(screened)} 个候选："
                + "，".join(f"{score} 分 {distribution[score]}" for score in (3, 2, 1, 0))
                + "。",
                "",
            ]
        )
    origins = Counter(
        c.origin for c in result.collection.candidates if c.decision == "accepted"
    )
    if origins.get("snowball") or origins.get("library"):
        lines.extend(
            [
                f"采用来源中：引文扩展 {origins.get('snowball', 0)} 个，"
                f"本地文献库 {origins.get('library', 0)} 个；"
                f"共 {result.collection.rounds} 轮检索。",
                "",
            ]
        )
    tiers = Counter(
        source.quality.tier for source in result.collection.sources if source.quality
    )
    if tiers:
        lines.extend(
            [
                "采用来源质量分布："
                + "，".join(f"{tier} 级 {tiers[tier]}" for tier in "ABCD" if tiers.get(tier))
                + "。",
                "",
            ]
        )


def _render_plan(result: CrowResearchResult, lines: list[str]) -> None:
    plan = result.plan
    lines.extend(
        [
            "## 1. 需求解释",
            "",
            f"- 原始需求：{result.collection.requirement}",
            f"- 需求解释：{plan.requirement_summary}",
            "",
        ]
    )
    if result.mission_profile is not None:
        _render_profile(result.mission_profile, lines)
    lines.extend(["## 2. 检索计划", "", "### 研究问题", ""])
    for i, question in enumerate(plan.research_questions, start=1):
        dims = plan.dimensions_of(question)
        tag = f"（{'、'.join(dimension_label(d) for d in dims)}）" if dims else ""
        lines.append(f"{i}. {question}{tag}")
    if plan.pattern_hypotheses:
        table = PatternTable.default()
        names = [
            f"`{p}` {table.get(p).name}" if table.get(p) else f"`{p}`"
            for p in plan.pattern_hypotheses
        ]
        lines.extend(["", "### 战术模式假设", "", "、".join(names)])
    if plan.analogy_queries:
        lines.extend(["", "### 相似场景检索式", ""])
        lines.extend(f"{i}. `{q}`" for i, q in enumerate(plan.analogy_queries, start=1))
    for heading, queries in (
        ("通用互联网检索式", plan.search_queries),
        ("学术索引检索式（OpenAlex / Semantic Scholar / arXiv / CORE）", plan.scholarly_queries),
        ("条令与官方文献检索式", plan.doctrine_queries),
        ("技术报告检索式（DTIC / NPS / RAND / AFIT）", plan.technical_report_queries),
    ):
        if queries:
            lines.extend(["", f"### {heading}", ""])
            lines.extend(f"{i}. `{q}`" for i, q in enumerate(queries, start=1))
    if plan.key_terms:
        lines.extend(["", "### 关键词", "", ", ".join(plan.key_terms)])
    if plan.matched_concepts:
        lines.extend(["", "### 命中的术语词表概念", "", ", ".join(plan.matched_concepts)])
    if plan.planning_notes:
        lines.extend(["", "### 规划提示", ""])
        lines.extend(f"- {note}" for note in plan.planning_notes)
    for index, followup in enumerate(plan.followup_rounds, start=2):
        lines.extend(["", f"### 第 {index} 轮补充检索式（针对证据不足的问题）", ""])
        for query in [
            *followup.search_queries, *followup.scholarly_queries, *followup.doctrine_queries
        ]:
            lines.append(f"- `{query}`")
    lines.append("")


def _render_profile(profile: MissionProfile, lines: list[str]) -> None:
    lines.extend(
        [
            "### 任务画像",
            "",
            f"- 领域 / 层级：{profile.domain} / {profile.echelon}",
            f"- 任务：`{profile.mission.type}` {profile.mission.objective}",
        ]
    )
    if profile.own_forces:
        lines.append(
            "- 己方："
            + "，".join(
                f"{f.role_hint} × {f.count}"
                + (f"（{f.platform_class}）" if f.platform_class else "")
                for f in profile.own_forces
            )
        )
    lines.append(
        f"- 敌情：{profile.enemy.known}"
        + (f"；假设：{'；'.join(profile.enemy.assumed)}（需确认）" if profile.enemy.assumed else "")
    )
    if profile.environment.terrain or profile.environment.other:
        lines.append(
            "- 环境：" + "、".join([*profile.environment.terrain, *profile.environment.other])
        )
    if profile.user_specified:
        lines.append("- 用户指定（后续不得改动）：" + "；".join(profile.user_specified))
    if profile.success_criteria:
        lines.append("- 成功标准：" + "；".join(c.description for c in profile.success_criteria))
    if profile.open_questions:
        lines.extend(["", "| 问题 | 阻塞 | 采用的回答 | 来源 |", "| --- | --- | --- | --- |"])
        for q in profile.open_questions:
            source = {"user": "用户回答", "default": "默认假设"}.get(q.answered_by or "", "未处理")
            lines.append(
                f"| {_cell(q.text)} | {'是' if q.blocking else '否'} | "
                f"{_cell(q.answer or q.default_assumption)} | {source} |"
            )
    if profile.notes:
        lines.extend(["", "整理提示："])
        lines.extend(f"- {note}" for note in profile.notes)
    lines.append("")


def _render_sources(result: CrowResearchResult, lines: list[str]) -> None:
    collection = result.collection
    candidate_notes: dict[str, list[str]] = {}
    screenings = {}
    for candidate in collection.candidates:
        if candidate.decision == "accepted" and candidate.source_id:
            candidate_notes[candidate.source_id] = candidate.notes
            if candidate.screening is not None:
                screenings[candidate.source_id] = candidate.screening
    lines.extend(["## 3. 来源", "", "### 3.1 采用来源", ""])
    if not collection.sources:
        lines.append("未采集到可用来源。")
    for index, source in enumerate(collection.sources, start=1):
        quality = source.quality
        meta = source.metadata
        lines.append(f"{index}. **{source.title}**")
        lines.append(f"   - 来源 ID：`{source.source_id}`")
        lines.append(f"   - 地址：{source.uri}")
        lines.append(f"   - 类型：{source.kind}")
        if quality is not None:
            lines.append(f"   - 来源质量：**{quality.tier} 级**（{quality.score:.2f}）")
            lines.append(f"   - 评分依据：{'；'.join(quality.reasons)}")
        bib = [
            f"DOI {meta.doi}" if meta.doi else None,
            f"作者 {', '.join(meta.authors[:4])}" if meta.authors else None,
            f"年份 {meta.year}" if meta.year else None,
            f"载体 {meta.venue}" if meta.venue else None,
            f"机构 {', '.join(meta.institutions[:3])}" if meta.institutions else None,
            f"被引 {meta.cited_by_count}" if meta.cited_by_count is not None else None,
        ]
        bib_text = "；".join(item for item in bib if item)
        if bib_text:
            lines.append(f"   - 文献信息：{bib_text}")
        if meta.verified_by:
            lines.append(f"   - 元数据核验：{', '.join(meta.verified_by)}")
        notes = candidate_notes.get(source.source_id, [])
        if notes:
            lines.append(f"   - 注意：{'；'.join(notes)}")
        screening = screenings.get(source.source_id)
        if screening is not None:
            lines.append(f"   - 摘要筛选：{screening.score}/3，{screening.reason}")
        if source.discovered_by:
            lines.append(f"   - 发现途径：{'；'.join(source.discovered_by)}")
        lines.append(f"   - 获取时间：{source.retrieved_at.isoformat()}")
        lines.append(f"   - 内容哈希：`{source.content_hash}`")

    if result.ingest_failures:
        lines.extend(["", "### 3.2 PaperQA 解析失败", ""])
        for failure in result.ingest_failures:
            lines.append(f"- `{failure.source_id}` {failure.path}：{failure.detail}")

    lines.extend(["", "### 3.3 采集失败与拒绝（按类别）", ""])
    if not collection.failures:
        lines.append("无。")
    else:
        by_category = Counter(failure.category for failure in collection.failures)
        lines.append(
            "，".join(f"`{category}` {count}" for category, count in by_category.most_common())
        )
        lines.append("")
        for index, failure in enumerate(collection.failures, start=1):
            status = f"，HTTP {failure.status_code}" if failure.status_code else ""
            stage = f"[{failure.stage}] " if failure.stage else ""
            lines.append(
                f"{index}. {stage}`{failure.category}`{status}：{failure.uri}"
            )
            if failure.detail:
                lines.append(f"   - 详情：{failure.detail}")

    worklist = _manual_worklist(collection.candidates)
    if worklist:
        lines.extend(
            [
                "",
                "### 3.4 人工补采清单",
                "",
                "以下候选预估质量为 A/B 级，但自动采集失败或无开放全文。"
                "可通过机构网络手动下载后作为本地资料补充。",
                "",
            ]
        )
        for candidate in worklist:
            quality = candidate.preliminary_quality
            tier = f"{quality.tier} 级" if quality else "未评分"
            doi = f"，DOI {candidate.metadata.doi}" if candidate.metadata.doi else ""
            lines.append(
                f"- [{tier}] {candidate.title}{doi} — {candidate.url}"
                f"（`{candidate.reason_category}`：{candidate.reason_detail}）"
            )
    if collection.manual_search_queries:
        lines.extend(
            [
                "",
                "### 3.5 中文文献人工检索清单",
                "",
                "知网、万方等中文数据库没有公开接口，需要人工检索。请将下载的全文放入本地文献库目录"
                "（`--library` 或 `CROW_LIBRARY_DIRS`），可另附 `<文件名>.meta.toml` 记录题名、"
                "作者、年份、期刊等信息，下次运行时会自动筛选并纳入。",
                "",
            ]
        )
        for query in collection.manual_search_queries:
            encoded = quote(query)
            lines.append(
                f"- `{query}` — [知网](https://kns.cnki.net/kns8s/defaultresult/index?kw={encoded})"
                f" · [万方](https://s.wanfangdata.com.cn/paper?q={encoded})"
            )
    lines.append("")


def _render_reports(result: CrowResearchResult, lines: list[str]) -> None:
    lines.extend(
        [
            "## 4. 研究报告",
            "",
            "> 相关性评分（0–10）由 PaperQA2 针对当前问题给出；来源质量（A–D）由确定性规则"
            "给出。两者含义不同，请分别审查。",
            "",
        ]
    )
    if not result.reports:
        lines.append("没有可用文档，未生成研究报告。")
    for report_index, report in enumerate(result.reports, start=1):
        flag = "（⚠️ 回答声明资料不足）" if report.insufficient_evidence else ""
        lines.extend(
            [
                f"### 4.{report_index} {report.question}{flag}",
                "",
                "#### 综合回答",
                "",
                report.answer,
                "",
                "#### 证据",
                "",
            ]
        )
        if not report.evidence:
            lines.append("未返回证据摘录。")
            lines.append("")
            continue
        for evidence_index, evidence in enumerate(report.evidence, start=1):
            quality = (
                f"{evidence.source_quality_tier} 级（{evidence.source_quality_score:.2f}）"
                if evidence.source_quality_tier and evidence.source_quality_score is not None
                else "未知"
            )
            lines.extend(
                [
                    f"##### 证据 {report_index}.{evidence_index}",
                    "",
                    f"- 引用：{evidence.citation}",
                    f"- 来源 ID：`{evidence.source_id or '未匹配'}`",
                    f"- 位置：{evidence.location}",
                    f"- 相关性评分：{evidence.score}/10",
                    f"- 来源质量：{quality}",
                    "- 摘录：",
                    "",
                    _blockquote(evidence.excerpt),
                    "",
                    "- 证据摘要：",
                    "",
                    evidence.summary,
                    "",
                ]
            )


def _render_ledger(result: CrowResearchResult, lines: list[str]) -> None:
    candidates = result.collection.candidates
    if not candidates:
        return
    lines.extend(
        [
            "## 附录 A. 候选来源决策台账",
            "",
            "| ID | 轮次 | 渠道 / 提供方 | 检索式 | 排名 | 预估质量 | 筛选 | 优先级 | 决策 | 原因 |"
            " 标题 / URL |",
            "| --- | ---: | --- | --- | ---: | --- | --- | ---: | --- | --- | --- |",
        ]
    )
    for candidate in candidates:
        quality = candidate.preliminary_quality
        quality_text = f"{quality.tier} {quality.score:.2f}" if quality else ""
        reason = candidate.reason_category or ""
        if candidate.duplicate_of:
            reason = f"同 {candidate.duplicate_of}"
        elif candidate.decision == "skipped_budget":
            reason = candidate.reason_detail or ""
        elif candidate.http_status and candidate.decision == "failed":
            reason = f"{reason} (HTTP {candidate.http_status})"
        screening = (
            f"{candidate.screening.score}/3" if candidate.screening is not None else ""
        )
        if candidate.decision == "screened_out":
            reason = candidate.reason_detail or ""
        lines.append(
            f"| {candidate.candidate_id} | {candidate.round} | "
            f"{candidate.channel} / {candidate.provider} | {_cell(candidate.query, 40)} | "
            f"{candidate.rank} | {quality_text} | {screening} | "
            f"{'' if candidate.priority is None else f'{candidate.priority:.2f}'} | "
            f"{_DECISION_LABELS[candidate.decision]} | {_cell(reason, 50)} | "
            f"{_cell(candidate.title, 60)}<br>{_cell(candidate.url, 90)} |"
        )
    lines.append("")


def _render_manifest(result: CrowResearchResult, lines: list[str]) -> None:
    manifest = result.manifest
    if manifest is None:
        return
    lines.extend(
        [
            "## 附录 B. 运行清单",
            "",
            f"- 运行 ID：`{manifest.run_id}`",
            f"- 开始 / 结束：{manifest.started_at.isoformat()} / "
            f"{manifest.finished_at.isoformat() if manifest.finished_at else '未完成'}",
            f"- 代码版本：{manifest.package_version}；Git：`{manifest.git_commit or '无'}`",
            f"- Python：{manifest.python_version}；平台：{manifest.platform}",
            f"- 模型：{manifest.chat_model}；嵌入：{manifest.embedding_model}"
            f"（{manifest.embedding_dimension} 维）",
            f"- 提示词版本：{manifest.planner_prompt_version}；"
            f"质量规则：{manifest.quality_rules_version}",
            f"- PaperQA 参数：{_kv(manifest.paperqa_settings)}",
            f"- 检索参数：{_kv(manifest.search_settings)}",
            "- 阶段耗时："
            + "，".join(f"{timing.stage} {timing.seconds:.1f}s" for timing in manifest.timings),
            "- 依赖版本：" + "，".join(f"{k} {v}" for k, v in manifest.dependency_versions.items()),
            "",
        ]
    )


def _manual_worklist(candidates: list[SourceCandidate]) -> list[SourceCandidate]:
    return [
        candidate
        for candidate in candidates
        if candidate.decision in {"failed", "rejected"}
        and candidate.reason_category != "blocked_domain"
        and candidate.preliminary_quality is not None
        and candidate.preliminary_quality.tier in {"A", "B"}
    ]


def write_research_bundle(
    result: CrowResearchResult,
    output_dir: Path,
) -> tuple[Path, Path]:
    """Write readable Markdown and lossless JSON for one research run."""
    output_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = output_dir / "research_report.md"
    json_path = output_dir / "research_result.json"
    markdown_path.write_text(render_research_markdown(result), encoding="utf-8")
    json_path.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
    if result.mission_profile is not None:
        write_profile(result.mission_profile, output_dir / "mission_profile.yaml")
    return markdown_path, json_path


def _blockquote(text: str) -> str:
    return "\n".join(f"> {line}" for line in text.splitlines())


def _cell(text: str, limit: int | None = None) -> str:
    value = " ".join(text.split()).replace("|", "\\|")
    if limit is not None and len(value) > limit:
        value = value[: limit - 1] + "…"
    return value


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}"


def _kv(values: dict[str, object]) -> str:
    return "，".join(f"{key}={value}" for key, value in values.items())


def collection_diagnostics(collection) -> list[str]:
    """Short console summary: why candidates were lost, per category, channel and host."""
    from urllib.parse import urlparse

    lines: list[str] = []
    decisions = Counter(c.decision for c in collection.candidates)
    lines.append("  决策：" + "，".join(f"{_DECISION_LABELS[d]} {n}" for d, n in decisions.items()))
    failed = [c for c in collection.candidates if c.decision == "failed"]
    if failed:
        categories = Counter(
            f"{c.reason_category}{f' {c.http_status}' if c.http_status else ''}" for c in failed
        )
        lines.append(
            "  采集失败类别：" + "，".join(f"{k} {v}" for k, v in categories.most_common())
        )
        hosts = Counter((urlparse(c.fetch_url).hostname or "?") for c in failed)
        lines.append("  失败最多的站点：" + "，".join(f"{k} {v}" for k, v in hosts.most_common(6)))
    by_channel: dict[str, Counter] = {}
    for candidate in collection.candidates:
        key = f"{candidate.channel}/{candidate.provider}"
        by_channel.setdefault(key, Counter())[candidate.decision] += 1
    lines.append(
        "  各渠道（候选/采用）："
        + "，".join(
            f"{key} {sum(c.values())}/{c.get('accepted', 0)}" for key, c in by_channel.items()
        )
    )
    search_failures = Counter(
        (f.detail or "") for f in collection.failures if f.stage == "search"
    )
    if search_failures:
        lines.append(
            "  检索失败："
            + "，".join(f"{k} ×{v}" for k, v in search_failures.most_common(5))
        )
    return lines
