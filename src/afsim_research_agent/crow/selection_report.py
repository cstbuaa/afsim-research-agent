"""C5 outputs: ``redteam_review.yaml`` and ``tactic_report.md`` (design §6.2–6.3).

The report puts each candidate on one page: what it is, how well it is backed, what the
platform can do, what the red team found and how the proposer answered, and what must be
confirmed. No model verdict: the user decides (``python -m afsim_research_agent.crow.select``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from afsim_research_agent.crow.proposal_models import (
    INNOVATIVE,
    SECTIONS,
    ProposalResult,
    RedTeamReview,
    TacticCard,
)
from afsim_research_agent.crow.proposal_report import (
    _PROVENANCE,
    _SECTION_LABELS,
    _SUPPORT,
    _capability_lines,
    _cell,
    _feasibility_cell,
)
from afsim_research_agent.crow.redteam import REDTEAM_PROMPT_VERSION, items_to_answer

REVIEW_FILE = "redteam_review.yaml"
REPORT_FILE = "tactic_report.md"
_LEVEL = {"low": "低", "medium": "中", "high": "高"}
_FEAS = {"ready": "可实现", "needs_confirmation": "需确认", "needs_development": "需开发"}
_DECISION = {"revise": "已修改", "keep": "不改"}
_OPS = {
    "add_exception": "新增例外",
    "modify_exception": "修改例外",
    "add_step": "新增步骤",
    "modify_step": "修改步骤",
}


def write_review(
    reviews: list[RedTeamReview],
    path: Path,
    *,
    red_model: str | None,
    proposer_model: str | None,
    notes: list[str] | None = None,
) -> Path:
    data = {
        "format": "RedTeamReview",
        "format_version": "0.1.0",
        "prompt_version": REDTEAM_PROMPT_VERSION,
        "red_model": red_model,
        "proposer_model": proposer_model,
        "notes": list(notes or []),
        "reviews": [r.model_dump(mode="json", exclude_defaults=False) for r in reviews],
    }
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100), encoding="utf-8"
    )
    return path


def load_reviews(path: Path) -> tuple[list[RedTeamReview], dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    reviews = [RedTeamReview.model_validate(r) for r in data.get("reviews") or []]
    return reviews, data


def render_report(
    result: ProposalResult,
    reviews: list[RedTeamReview],
    *,
    red_model: str | None = None,
    notes: list[str] | None = None,
    tactics_dir: str = "<战术目录>",
) -> str:
    by_id = {r.candidate: r for r in reviews}
    lines = ["# 候选战术评审报告", "", f"需求：{result.requirement}", ""]
    if red_model:
        lines.append(f"红方模型：`{red_model}`。红方只看到战术要素，看不到生成方的论证。")
    for note in notes or []:
        lines.append(f"> ⚠ {note}")
    lines += [
        "",
        "裁决不由模型做出：红方意见与生成方回应并列，请据此选择、组合或要求修改。",
        "",
        "| 编号 | 名称 | 来源 | 综合分 | 平台可行性 | 红方评价 | 主要弱点 | 版本 |",
        "| --- | --- | --- | ---: | --- | --- | --- | --- |",
    ]
    for card in result.candidates:
        review = by_id.get(card.id)
        overall = review.overall if review else None
        lines.append(
            f"| {card.id} | {_cell(card.name, 30)} | {_source(card)} | {card.score.total:.2f} | "
            f"{_feasibility_cell(card)} | "
            f"{_LEVEL.get(overall.soundness, '—') if overall else '未审查'} | "
            f"{_cell(overall.main_weakness, 40) if overall and overall.main_weakness else '—'} | "
            f"第 {card.revision} 版 |"
        )
    for card in result.candidates:
        lines += _page(card, by_id.get(card.id))
    lines += [
        "",
        "## 选择",
        "",
        "选择一个候选，或组合多个候选的不同阶段：",
        "",
        "```",
        f"python -m afsim_research_agent.crow.select {tactics_dir} --candidate tc_01 --by <姓名>",
        f"python -m afsim_research_agent.crow.select {tactics_dir} "
        '--candidate tc_01 --candidate tc_02:接敌 --reject tc_03 --note "城区入口增加侦察" --by <姓名>',
        "```",
        "",
        "选择写入 `tactic_selection.yaml`；所选战术的平台能力缺口同时进入开发队列。"
        "之后在 Falcon 请求文件中写 `tactic_selection: <战术目录>` 生成行为模型。",
        "",
    ]
    return "\n".join(lines)


def _source(card: TacticCard) -> str:
    label = _PROVENANCE[card.provenance]
    return f"⚠ {label}" if card.provenance in INNOVATIVE else label


def _page(card: TacticCard, review: RedTeamReview | None) -> list[str]:
    lines = ["", "---", "", f"## {card.id} {card.name}（第 {card.revision} 版）", ""]
    lines.append(f"> {card.summary}")
    lines.append("")
    lines.append(
        f"- 来源：{_source(card)}" + ("（非条令直接给出）" if card.provenance in INNOVATIVE else "")
    )
    if card.patterns:
        lines.append(f"- 模式：{', '.join(card.patterns)}")
    support = "，".join(
        f"{_SECTION_LABELS[s]} {_SUPPORT[card.support.get(s, 'empty')]}" for s in SECTIONS
    )
    lines.append(f"- 依据：{support}")
    lines.append("")
    lines += _capability_lines(card)
    lines += ["### 战术描述", "", card.narrative, ""]
    if review is None:
        lines += ["### 红方意见", "", "未审查。", ""]
    else:
        lines += _review_lines(review)
    flags = [*card.review_flags]
    if flags or card.assumptions or card.risks:
        lines += ["### 风险与需要确认的地方", ""]
        lines += [f"- ⚠ {f}" for f in flags]
        lines += [f"- 假设：{a}" for a in card.assumptions]
        lines += [f"- 风险：{r}" for r in card.risks]
        lines.append("")
    return lines


def _review_lines(review: RedTeamReview) -> list[str]:
    lines = ["### 红方意见与生成方回应", ""]
    if review.overall:
        lines.append(
            f"总体评价：{_LEVEL.get(review.overall.soundness, review.overall.soundness)}；"
            f"主要弱点：{review.overall.main_weakness or '—'}"
        )
        lines.append("")
    answers = {a.target: a for a in review.responses}
    texts = dict(items_to_answer(review))

    def answer(target: str) -> str:
        a = answers.get(target)
        if a is None:
            return "—" if target not in texts else "未回应"
        return f"{_DECISION[a.decision]}：{_cell(a.reason, 80)}"

    if review.critical_assumptions:
        lines += [
            "**关键前提**",
            "",
            "| 前提 | 脆弱程度 | 原因 | 回应 |",
            "| --- | --- | --- | --- |",
        ]
        for i, a in enumerate(review.critical_assumptions):
            lines.append(
                f"| {_cell(a.assumption)} | {_LEVEL.get(a.fragility, a.fragility)} | "
                f"{_cell(a.why) or '—'} | {answer(f'ca_{i + 1}')} |"
            )
        lines.append("")
    if review.counter_tactics:
        lines += [
            "**对手反制**",
            "",
            "| 编号 | 反制 | 需要的对手能力 | 触发 | 预期效果 | 严重程度 | 回应 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for ct in review.counter_tactics:
            lines.append(
                f"| {ct.id} | {_cell(ct.description)} | "
                f"{_cell('、'.join(ct.enemy_capabilities_needed)) or '—'} | {_cell(ct.trigger) or '—'} | "
                f"{_cell(ct.expected_effect) or '—'} | {_LEVEL.get(ct.severity, ct.severity)} | "
                f"{answer(ct.id)} |"
            )
        lines.append("")
    if review.failure_conditions:
        lines += ["**失效条件**", "", "| 编号 | 条件 | 回应 |", "| --- | --- | --- |"]
        for i, text in enumerate(review.failure_conditions):
            lines.append(f"| fc_{i + 1} | {_cell(text)} | {answer(f'fc_{i + 1}')} |")
        lines.append("")
    if review.capability_doubts:
        lines += ["**能力疑问**", "", "| 编号 | 疑问 | 回应 |", "| --- | --- | --- |"]
        for i, text in enumerate(review.capability_doubts):
            lines.append(f"| cd_{i + 1} | {_cell(text)} | {answer(f'cd_{i + 1}')} |")
        lines.append("")
    if review.feasibility_before and review.feasibility_after != review.feasibility_before:
        lines += [
            f"**修订对平台可行性的影响**：{_FEAS.get(review.feasibility_before, '—')} → "
            f"{_FEAS.get(review.feasibility_after or '', '未标注')}"
            "（修订改变了所需的平台能力，见上方平台能力明细）",
            "",
        ]
    if review.revision_after > review.revision_reviewed and review.revisions:
        lines += [f"**第 {review.revision_after} 版的修改**", ""]
        for op in review.revisions:
            what = "；".join(
                f"{k}={v}"
                for k, v in (
                    ("阶段", op.phase),
                    ("动作", op.do),
                    ("直到", op.until),
                    ("情况", op.situation),
                    ("处置", op.then),
                )
                if v
            )
            where = f"（第 {op.index} 项）" if op.index is not None else ""
            lines.append(
                f"- {_OPS.get(op.op, op.op)}{where}：{what}（回应 {'、'.join(op.responds_to) or '—'}）"
            )
        lines.append("")
    for note in review.notes:
        lines.append(f"- 说明：{note}")
    if review.notes:
        lines.append("")
    return lines
