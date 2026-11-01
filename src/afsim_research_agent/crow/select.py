"""C5 user selection: ``tactic_selection.yaml`` (design crow-tactic-proposal-v1 §6.3).

    python -m afsim_research_agent.crow.select <tactics_dir> --candidate tc_01 --by NAME
    python -m afsim_research_agent.crow.select <tactics_dir> \\
        --candidate tc_01 --candidate tc_02:接敌,跃进 --reject tc_03 --note "..." --by NAME

``tc_02:接敌`` takes only the named phases (step ``phase`` values) of a candidate, for
combining tactics. The selected candidates' capability gaps are marked ``selected`` in
``capability_gaps.yaml`` and enter the development queue of the gap registry.
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from afsim_research_agent.crow.proposal_models import ProposalResult

SELECTION_FILE = "tactic_selection.yaml"


class SelectionError(ValueError):
    pass


def parse_choice(text: str) -> tuple[str, list[str] | None]:
    candidate, _, phases = text.partition(":")
    names = [p.strip() for p in phases.replace("，", ",").split(",") if p.strip()]
    return candidate.strip(), names or None


def build_selection(
    result: ProposalResult,
    choices: list[str],
    *,
    rejected: list[str],
    note: str,
    by: str,
) -> tuple[dict[str, Any], list[str]]:
    """The selection document and warnings (e.g. a phase name the card does not have)."""
    if not by:
        raise SelectionError("请用 --by 写明选择人")
    cards = {c.id: c for c in result.candidates}
    warnings: list[str] = []
    selected = []
    for choice in choices:
        cid, phases = parse_choice(choice)
        card = cards.get(cid)
        if card is None:
            raise SelectionError(f"没有候选 {cid}；可选：{', '.join(cards)}")
        if phases:
            known = [s.phase for s in card.steps]
            unknown = [p for p in phases if p not in known]
            if unknown:
                raise SelectionError(
                    f"{cid} 没有阶段 {'、'.join(unknown)}；它的阶段：{'、'.join(known)}"
                )
        selected.append({"candidate": cid, "revision": card.revision, "phases": phases or "all"})
        if card.review_flags:
            warnings.append(f"{cid} 有 {len(card.review_flags)} 处需要确认的地方，见报告")
    if not selected:
        raise SelectionError("至少选择一个候选（--candidate）")
    for cid in rejected:
        if cid not in cards:
            raise SelectionError(f"没有候选 {cid}")
    overlap = {s["candidate"] for s in selected} & set(rejected)
    if overlap:
        raise SelectionError(f"{', '.join(sorted(overlap))} 同时被选中和否决")
    if len(selected) > 1 and all(s["phases"] == "all" for s in selected):
        warnings.append("选择了多个完整候选；组合时建议用 tc_xx:阶段 指明各取哪些阶段")
    doc = {
        "format": "TacticSelection",
        "format_version": "0.1.0",
        "requirement": result.requirement,
        "selected": selected,
        "rejected": list(rejected),
        "user_notes": note,
        "confirmed_by": by,
        "date": date.today().isoformat(),
    }
    return doc, warnings


def main(argv: list[str] | None = None) -> int:
    from afsim_research_agent.capability.registry import GapRegistry, RegistryError, registry_path
    from afsim_research_agent.crow.annotate import load_result

    parser = argparse.ArgumentParser(prog="python -m afsim_research_agent.crow.select")
    parser.add_argument("tactics_dir", type=Path)
    parser.add_argument("--candidate", action="append", default=[], help="tc_01 或 tc_02:阶段,阶段")
    parser.add_argument("--reject", action="append", default=[])
    parser.add_argument("--note", default="")
    parser.add_argument("--by", required=True)
    parser.add_argument(
        "--registry", type=Path, help="缺口登记表（默认 data/capability/gap_registry.yaml）"
    )
    parser.add_argument("--no-registry", action="store_true", help="不把缺口登记到开发队列")
    args = parser.parse_args(argv)

    result = load_result(args.tactics_dir)
    try:
        doc, warnings = build_selection(
            result, args.candidate, rejected=args.reject, note=args.note, by=args.by
        )
    except SelectionError as exc:
        print(f"错误：{exc}")
        return 2
    path = args.tactics_dir / SELECTION_FILE
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    for w in warnings:
        print(f"⚠ {w}")
    print(f"已选择：{', '.join(s['candidate'] for s in doc['selected'])} → {path}")
    print(
        "下一步（Falcon v3）：python -m afsim_research_agent.falcon crow-request "
        f"{args.tactics_dir} --model-id <模型名> [--role 排长车=leader]，"
        "再按提示运行 falcon draft3"
    )

    gaps_file = args.tactics_dir / "capability_gaps.yaml"
    if gaps_file.is_file() and not args.no_registry:
        reg_path = registry_path(args.registry)
        registry = GapRegistry.load(reg_path)
        count = 0
        for item in doc["selected"]:
            try:
                count += len(registry.select(gaps_file, item["candidate"], by=args.by))
            except RegistryError:
                pass  # the candidate has no capability gaps
        registry.save(reg_path)
        print(f"平台能力缺口 {count} 项进入开发队列（{reg_path}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
