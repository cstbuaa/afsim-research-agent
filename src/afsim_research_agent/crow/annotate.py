"""Add C4b capability annotation to candidates that C4 already wrote.

    python -m afsim_research_agent.crow.annotate <tactics_dir> [options]

Reads tactic_candidates.yaml and tactic_ideas.yaml from the directory, annotates every
candidate (crow-tactic-proposal-v1 §18), and rewrites tactic_candidates.yaml/.md together
with capability_gaps.yaml. Candidates, scores and selection are left unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

import yaml
from dotenv import load_dotenv

from afsim_research_agent.capability import CapabilityCatalog
from afsim_research_agent.capability.granularity import CapabilityPolicy
from afsim_research_agent.config import Settings
from afsim_research_agent.crow.capability_annotation import CapabilityAnnotator
from afsim_research_agent.crow.mission import MissionProfile, load_profile
from afsim_research_agent.crow.proposal_models import ProposalResult
from afsim_research_agent.crow.proposal_report import write_proposal
from afsim_research_agent.crow.propose import (
    add_granularity_arguments,
    load_catalog,
    policy_from_args,
)
from afsim_research_agent.falcon.llm import DashScopeModel, FalconModel, ModelRunner
from afsim_research_agent.falcon.models import CrowRef, EvidencePackage

_IDEA_FIELDS = ("techniques", "fragments", "ideas", "retrieved_pool")


def load_result(tactics_dir: Path) -> ProposalResult:
    data = yaml.safe_load((tactics_dir / "tactic_candidates.yaml").read_text(encoding="utf-8"))
    ideas_path = tactics_dir / "tactic_ideas.yaml"
    ideas = yaml.safe_load(ideas_path.read_text(encoding="utf-8")) if ideas_path.is_file() else {}
    data.update({k: ideas.get(k, []) for k in _IDEA_FIELDS})
    return ProposalResult.model_validate(data)


def load_package(path: Path | None) -> EvidencePackage:
    if path is not None and path.is_file():
        return EvidencePackage.model_validate_json(path.read_text(encoding="utf-8"))
    return EvidencePackage(package_id="unavailable", crow=CrowRef(gate_version=""))


async def annotate_dir(
    tactics_dir: Path,
    *,
    catalog: CapabilityCatalog,
    profile: MissionProfile | None,
    package: EvidencePackage,
    model: FalconModel,
    out_dir: Path | None = None,
    policy: CapabilityPolicy | None = None,
) -> tuple[ProposalResult, dict[str, Path]]:
    result = load_result(tactics_dir)
    runner = ModelRunner(model)
    await CapabilityAnnotator(runner, catalog, policy=policy).annotate(result, profile)
    trace = [record.model_dump(mode="json") for record in runner.records]
    out = out_dir or tactics_dir
    paths = write_proposal(result, package, out, None)
    (out / "capability_trace.json").write_text(
        json.dumps(trace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result, paths


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="为已有候选战术补充平台能力标注（C4b）。")
    parser.add_argument("tactics_dir", type=Path, help="含 tactic_candidates.yaml 的目录")
    parser.add_argument("--out", type=Path, help="输出目录，默认原目录（覆盖候选文件）")
    parser.add_argument("--mission-profile", type=Path, help="mission_profile.yaml")
    parser.add_argument("--evidence-package", type=Path, help="evidence_package.json（报告引文用）")
    parser.add_argument("--capability-catalog", type=Path, help="能力目录根目录")
    add_granularity_arguments(parser)
    args = parser.parse_args(argv)
    policy = policy_from_args(parser, args)

    load_dotenv()
    settings = Settings.from_environment()
    catalog = load_catalog(args.capability_catalog, settings)
    if catalog is None:
        raise SystemExit(3)
    profile_path = args.mission_profile or args.tactics_dir / "mission_profile.yaml"
    profile = load_profile(profile_path) if profile_path.is_file() else None
    package_path = args.evidence_package or args.tactics_dir.parent / "evidence_package.json"
    model = DashScopeModel(
        settings.crow_capability_model or settings.crow_proposer_model or settings.chat_model
    )
    result, paths = asyncio.run(
        annotate_dir(
            args.tactics_dir,
            catalog=catalog,
            profile=profile,
            package=load_package(package_path),
            model=model,
            out_dir=args.out,
            policy=policy,
        )
    )
    level = {"ready": "可实现", "needs_confirmation": "需确认", "needs_development": "需开发"}
    for card in result.candidates:
        mark = level[card.feasibility.level] if card.feasibility else "标注失败"
        print(f"  {card.id} {card.name}：{mark}")
    print(f"- 平台能力缺口 {len(result.capability_gaps)} 项：{paths.get('capability_gaps')}")
    print(f"- 候选战术报告：{paths['markdown']}")


if __name__ == "__main__":
    main()
