"""Write C4 results: tactic_candidates.yaml/.md, tactic_ideas.yaml, tactic_trace.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from afsim_research_agent.crow.dimensions import PatternTable
from afsim_research_agent.crow.proposal_models import (
    INNOVATIVE,
    SECTIONS,
    ProposalResult,
    TacticCard,
)
from afsim_research_agent.falcon.models import EvidencePackage

_PROVENANCE = {
    "retrieved": "检索战术",
    "adapted": "改编（创新补位）",
    "combined": "组合（创新补位）",
    "transferred": "迁移（创新补位）",
}
_SUPPORT = {
    "supported": "有依据",
    "partial": "部分有依据",
    "inferred": "推断",
    "unsupported": "无依据",
    "empty": "未给出",
}
_SECTION_LABELS = {
    "applicability": "适用情况",
    "roles": "角色",
    "geometry": "队形与几何",
    "steps": "步骤",
    "exceptions": "例外与中止",
}
_FEASIBILITY = {
    "ready": "可实现",
    "needs_confirmation": "需确认",
    "needs_development": "需开发",
}
_GAP_TYPE = {"confirm": "需确认", "ir_extension": "需扩展 IR", "develop": "需开发"}
_NECESSITY = {"required": "必需", "enhancing": "增强", "substitutable": "可替代"}
_BASIS = {
    "evidence": "",
    "user_specified": "〔用户指定〕",
    "inferred": "〔推断〕",
    "unsupported": "〔无依据，需补充调研〕",
}


def _prune(value: Any) -> Any:
    if isinstance(value, dict):
        out = {k: _prune(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v not in (None, [], {}, "")}
    if isinstance(value, list):
        return [_prune(v) for v in value]
    return value


def _dump(data: Any, path: Path) -> Path:
    path.write_text(
        yaml.safe_dump(_prune(data), allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )
    return path


def write_proposal(
    result: ProposalResult,
    package: EvidencePackage,
    out_dir: Path,
    trace: list[dict[str, Any]] | None = None,
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    header = result.model_dump(
        mode="json",
        include={
            "format",
            "format_version",
            "requirement",
            "evidence_package",
            "max_candidates",
            "max_innovative",
            "prompt_version",
            "retrieval_rounds",
            "catalog_version",
        },
    )
    paths = {
        "candidates": _dump(
            {
                **header,
                "notes": result.notes,
                "gaps": [g.model_dump(mode="json") for g in result.gaps],
                "candidates": [c.model_dump(mode="json") for c in result.candidates],
            },
            out_dir / "tactic_candidates.yaml",
        ),
        "ideas": _dump(
            {
                "format": "TacticIdeas",
                "format_version": result.format_version,
                "gaps": [g.model_dump(mode="json") for g in result.gaps],
                "ideas": [i.model_dump(mode="json") for i in result.ideas],
                "techniques": [t.model_dump(mode="json") for t in result.techniques],
                "fragments": [c.model_dump(mode="json") for c in result.fragments],
                "retrieved_pool": [c.model_dump(mode="json") for c in result.retrieved_pool],
            },
            out_dir / "tactic_ideas.yaml",
        ),
    }
    if result.catalog_version:
        paths["capability_gaps"] = _dump(
            {
                "format": "CapabilityGaps",
                "format_version": "0.1.0",
                "catalog_version": result.catalog_version,
                "granularity": result.capability_granularity,
                "note": "Crow 预估；只有用户选定战术的缺口（selected: true）进入开发队列",
                "gaps": [g.model_dump(mode="json") for g in result.capability_gaps],
            },
            out_dir / "capability_gaps.yaml",
        )
    markdown = out_dir / "tactic_candidates.md"
    markdown.write_text(render_markdown(result, package), encoding="utf-8")
    paths["markdown"] = markdown
    if trace is not None:
        path = out_dir / "tactic_trace.json"
        path.write_text(json.dumps(trace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        paths["trace"] = path
    return paths


def render_markdown(result: ProposalResult, package: EvidencePackage) -> str:
    patterns = PatternTable.default()
    retrieved = [c for c in result.candidates if c.provenance == "retrieved"]
    innovative = [c for c in result.candidates if c.provenance in INNOVATIVE]
    lines = [
        "# 候选战术",
        "",
        f"需求：{result.requirement}",
        "",
        f"共 {len(result.candidates)} 个候选：检索战术 {len(retrieved)} 个，"
        f"创新补位 {len(innovative)} 个（上限 {result.max_innovative}）。"
        f"补充检索 {result.retrieval_rounds} 轮。",
        "",
        "| 编号 | 名称 | 来源 | 模式 | 证据覆盖条款 | 单位匹配 | 证据强度 | 综合分 | 平台可行性 |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for card in result.candidates:
        names = "、".join(patterns.get(p).name if patterns.get(p) else p for p in card.patterns)
        lines.append(
            f"| {card.id} | {_cell(card.name)} | {_PROVENANCE[card.provenance]} | {names} | "
            f"{card.score.coverage:.2f} | {card.unit_match:.2f} | "
            f"{card.score.evidence_strength:.2f} | {card.score.total:.2f} | "
            f"{_feasibility_cell(card)} |"
        )
    if not result.candidates:
        lines.append("| — | 没有完整战术 | | | | | | | |")
    if result.catalog_version:
        lines += ["", "平台可行性由能力目录判定，与综合分分开，不影响候选排序。"]
        granularity = result.capability_granularity or {}
        if granularity.get("granularity") == "coarse":
            refine = granularity.get("refine") or []
            lines.append(
                "按粗粒度判定：只精确核对核心能力组"
                + (f"及细化的 {', '.join(refine)}" if refine else "")
                + "，其余能力缺失或未确认时按简化处理，不计为缺口。"
            )
    if result.gaps:
        lines += ["", "## 材料缺口", ""]
        for gap in result.gaps:
            filled = f"（由 {', '.join(gap.filled_by)} 补位）" if gap.filled_by else "（未补位）"
            lines.append(f"- {gap.id}：{gap.detail}{filled}")
    if result.notes:
        lines += ["", "## 处理说明", ""]
        lines += [f"- {note}" for note in result.notes]

    evidence = {e.evidence_id: e for e in package.evidence}
    sources = {s.source_id: s for s in package.sources}
    cited: list[str] = []
    for heading, cards in (("检索战术", retrieved), ("创新补位候选（非条令直接给出）", innovative)):
        if not cards:
            continue
        lines += ["", f"## {heading}"]
        for card in cards:
            lines += _render_card(card, patterns, cited)
    if result.capability_gaps:
        lines += [
            "",
            "## 平台能力缺口",
            "",
            "Crow 预估。只有用户选定战术的缺口进入开发队列；IR 生成后会精确核对并补充。",
            "",
            "| 编号 | 能力 | 类型 | 涉及候选 |",
            "| --- | --- | --- | --- |",
        ]
        for gap in result.capability_gaps:
            users = "；".join(f"{u.candidate}（{', '.join(u.sources)}）" for u in gap.used_by)
            lines.append(
                f"| {gap.id} | {_cell(gap.display_name)} | {_GAP_TYPE[gap.gap_type]} | {users} |"
            )
    if result.fragments:
        lines += ["", "## 不完整的提取结果（未作为候选）", ""]
        for card in result.fragments:
            lines.append(f"- {card.id} {card.name}：{'；'.join(card.completeness_issues)}")
    if result.techniques:
        lines += [
            "",
            "## 证据中的战术动作",
            "",
            "| 编号 | 名称 | 类别 | 适用单位 | 说明 | 证据 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for t in result.techniques:
            lines.append(
                f"| {t.id} | {_cell(t.name)} | {t.kind} | {t.unit_type} | "
                f"{_cell(t.description, 80)} | {', '.join(t.evidence)} |"
            )
    if cited:
        lines += ["", "## 引用的证据", ""]
        for eid in cited:
            item = evidence.get(eid)
            if item is None:
                continue
            source = sources.get(item.source_id)
            title = source.title if source else item.source_id
            tier = f"，{source.quality_tier} 级" if source and source.quality_tier else ""
            lines.append(f"- **{eid}** {item.claim}")
            lines.append(f"  > {_cell(item.quote, 300)}")
            lines.append(f"  —— {title}{tier}，{item.location}")
    return "\n".join(lines).rstrip() + "\n"


def _render_card(card: TacticCard, patterns: PatternTable, cited: list[str]) -> list[str]:
    lines = ["", f"### {card.id} {card.name}", ""]
    if card.provenance in INNOVATIVE:
        label = _PROVENANCE[card.provenance]
        lines.append(f"> ⚠ {label}，非条令直接给出。补位缺口：{card.fills_gap}")
        lines.append("")
    lines.append("**战术描述**")
    lines.append("")
    lines.append(card.narrative.strip() or card.summary)
    lines.append("")
    if card.review_flags:
        lines.append("**⚠ 需确认**")
        lines.append("")
        lines += [f"- {flag}" for flag in card.review_flags]
        lines.append("")
    lines += _capability_lines(card)
    pattern_names = "、".join(
        f"{p}（{patterns.get(p).name}）" if patterns.get(p) else p for p in card.patterns
    )
    lines.append(f"- 模式：{pattern_names or '无'}")
    context = card.source_context or "未说明"
    lines.append(
        f"- 证据所述对象：{context}（单位匹配 {card.context_match}，{card.unit_match:.2f}）"
    )
    lines.append(f"- 决策权：{card.decision_authority}")
    lines.append(f"- 实现的需求条款：{'；'.join(card.addresses) or '无'}")
    lines.append(f"- 其中有证据支持的：{'；'.join(card.covers) or '无'}")
    if card.techniques:
        lines.append(f"- 使用的战术动作：{', '.join(card.techniques)}")
    support = "，".join(
        f"{_SECTION_LABELS[s]} {_SUPPORT[card.support.get(s, 'empty')]}" for s in SECTIONS
    )
    lines.append(f"- 依据核对：{support}")
    if card.variants:
        lines.append(f"- 相似变体：{'；'.join(card.variants)}")
    if card.derivation is not None:
        d = card.derivation
        sources = "；".join(
            s.tactic + (f"（{', '.join(s.evidence)}）" if s.evidence else "") for s in d.sources
        )
        lines.append(f"- 推导来源：{sources}")
        if d.operators:
            lines.append(f"- 改编算子：{', '.join(d.operators)}（改动幅度 {card.change_size}）")
        for change in d.changes:
            before = f"{change.before} → " if change.before else ""
            lines.append(f"  - {change.op}：{before}{change.after}。理由：{change.why}")
        if d.combination:
            combo = "，".join(f"{c.dimension}={c.option}" for c in d.combination)
            lines.append(f"- 组合选项：{combo}")
    lines += ["", "<details><summary>结构化要素与依据</summary>"]
    for section in SECTIONS:
        elements = getattr(card, section)
        if not elements:
            continue
        lines += ["", f"**{_SECTION_LABELS[section]}**", ""]
        for element in elements:
            for eid in element.evidence:
                if eid not in cited:
                    cited.append(eid)
            refs = f" [{', '.join(element.evidence)}]" if element.evidence else ""
            lines.append(f"- {_element_line(section, element)}{_BASIS[element.basis]}{refs}")
    lines += ["", "</details>"]
    for title, values in (
        ("假设", card.assumptions),
        ("风险", card.risks),
        ("所需能力", card.required_capabilities),
        ("核对提示", card.notes),
    ):
        if values:
            lines += ["", f"**{title}**：" + "；".join(values)]
    return lines


def _feasibility_cell(card: TacticCard) -> str:
    if card.feasibility is None:
        return "未标注"
    return _FEASIBILITY[card.feasibility.level]


def _capability_lines(card: TacticCard) -> list[str]:
    """One summary line right after the narrative, details folded."""
    if card.feasibility is None:
        return []
    groups: dict[str, list[str]] = {}
    for need in card.capability_needs:
        if need.gap_type and need.necessity == "enhancing":
            key = "enhancing"  # missing, but the tactic still works without it
        else:
            key = need.gap_type or need.status
        groups.setdefault(key, []).append(need.need)
    parts = []
    ok = sum(len(groups.get(k, [])) for k in ("ready", "composable"))
    if ok:
        parts.append(f"可实现 {ok} 项")
    if groups.get("partial"):
        parts.append(f"部分可用 {len(groups['partial'])} 项")
    if groups.get("simplified"):
        names = "、".join(dict.fromkeys(groups["simplified"]))
        parts.append(f"简化处理 {len(groups['simplified'])} 项（{_cell(names, 60)}）")
    for key in ("confirm", "ir_extension", "develop"):
        if groups.get(key):
            names = "、".join(dict.fromkeys(groups[key]))
            parts.append(f"{_GAP_TYPE[key]} {len(groups[key])} 项（{_cell(names, 80)}）")
    if groups.get("enhancing"):
        names = "、".join(dict.fromkeys(groups["enhancing"]))
        parts.append(
            f"增强项未具备 {len(groups['enhancing'])} 项，不影响战术成立（{_cell(names, 60)}）"
        )
    references = [n.reference for n in card.capability_needs if n.reference]
    if references:
        parts.append(f"参考值 {len(references)} 项，可调整（{_cell('；'.join(references), 60)}）")
    level = _FEASIBILITY[card.feasibility.level]
    lines = [f"**平台能力（{level}）**：" + "；".join(parts or ["未列出所需能力"]), ""]
    lines += ["<details><summary>所需能力明细</summary>", ""]
    lines += [
        "| 需要 | 要素 | 必要性 | 对应目录 | 对应理由 | 结论 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for need in card.capability_needs:
        refs = ", ".join(need.maps_to) or "（无）"
        if need.alternatives:
            refs = "；".join(f"{a.via}：{', '.join(a.maps_to)}" for a in need.alternatives)
            if need.chosen:
                refs += f"（采用：{need.chosen}）"
        gap = f" {', '.join(need.gaps)}" if need.gaps else ""
        why = "；".join([need.why] * bool(need.why) + [f"⚠ {n}" for n in need.notes])
        lines.append(
            f"| {_cell(need.need)} | {need.source} | {_NECESSITY[need.necessity]} | "
            f"{_cell(refs)} | {_cell(why or '—', 80)} | {need.label}{gap} |"
        )
    lines += ["", "</details>", ""]
    return lines


def _element_line(section: str, element: Any) -> str:
    if section == "applicability":
        return element.situation
    if section == "roles":
        members = f"：{', '.join(element.members)}" if element.members else ""
        return f"{element.role}{members}"
    if section == "geometry":
        parts = [element.formation, element.when, element.spacing]
        return "，".join(p for p in parts if p)
    if section == "steps":
        return f"{element.phase}：{element.do}；直到 {element.until or '（缺少结束条件）'}"
    return f"{element.situation} → {element.then}"


def _cell(text: str, limit: int | None = None) -> str:
    value = " ".join(text.split()).replace("|", "\\|")
    if limit is not None and len(value) > limit:
        value = value[: limit - 1] + "…"
    return value
