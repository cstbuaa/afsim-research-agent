"""C4 entry point: propose candidate tactics for one Crow run.

    python -m afsim_research_agent.crow.propose <crow_run_dir> [options]

Inputs, in order of precedence:
- evidence: --evidence-package, else <run>/evidence_package.json, else F1
  normalization of <run>/research_result.json (written to the run dir; this
  is "F1 moved into Crow");
- mission profile: --mission-profile, else <run>/mission_profile.yaml, else
  C1 is run now (blocking questions asked on the command line).

Outputs go to --out (default: the run dir): tactic_candidates.yaml/.md,
tactic_ideas.yaml, tactic_trace.json, and capability_gaps.yaml when a capability
catalog is available (C4b, design crow-tactic-proposal-v1 §18).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from afsim_research_agent.capability import CapabilityCatalog, CatalogError
from afsim_research_agent.capability.granularity import CapabilityPolicy
from afsim_research_agent.config import Settings
from afsim_research_agent.crow.capability_annotation import CapabilityAnnotator
from afsim_research_agent.crow.mission import MissionProfile, load_profile
from afsim_research_agent.crow.models import CrowResearchResult
from afsim_research_agent.crow.proposal import ProposalSettings, TacticProposer
from afsim_research_agent.crow.proposal_models import Gap, ProposalResult
from afsim_research_agent.crow.proposal_report import write_proposal
from afsim_research_agent.falcon.evidence import normalize_evidence, write_package
from afsim_research_agent.falcon.llm import DashScopeModel, FalconModel, ModelRunner
from afsim_research_agent.falcon.models import EvidencePackage


@dataclass
class ProposalRun:
    result: ProposalResult
    package: EvidencePackage
    paths: dict[str, Path]


def load_crow_result(run_dir: Path) -> CrowResearchResult:
    path = run_dir / "research_result.json"
    return CrowResearchResult.model_validate_json(path.read_text(encoding="utf-8"))


class GapRetriever:
    """One supplementary Crow round aimed at the gaps, then F1 on its result."""

    def __init__(
        self,
        *,
        settings: Settings,
        requirement: str,
        profile: MissionProfile | None,
        output_dir: Path,
        runner: ModelRunner,
    ) -> None:
        self._settings = settings
        self._requirement = requirement
        self._profile = profile
        self._output_dir = output_dir
        self._runner = runner

    async def __call__(self, gaps: list[Gap]) -> EvidencePackage | None:
        from afsim_research_agent.crow.report import write_research_bundle
        from afsim_research_agent.crow.research import CrowResearchPipeline

        focus = (
            self._requirement.strip()
            + "\n补充研究，重点针对以下缺口：\n"
            + "\n".join(f"- {gap.detail}" for gap in gaps)
        )
        print("材料不足，正在补充检索……")
        result = await CrowResearchPipeline(app_settings=self._settings).run(
            focus,
            output_dir=self._output_dir,
            max_web_results=3,
            max_total_sources=max(1, self._settings.crow_gap_round_sources),
            gap_rounds=0,
            **({"mission_profile": self._profile} if self._profile else {}),
        )
        write_research_bundle(result, self._output_dir)
        if not result.reports:
            return None
        package = await normalize_evidence(result, focus, self._runner)
        write_package(package, self._output_dir / "evidence_package.json")
        return package


async def run_proposal(
    run_dir: Path,
    *,
    settings: Settings,
    out_dir: Path | None = None,
    evidence_package: Path | None = None,
    profile: MissionProfile | None = None,
    requirement: str | None = None,
    retrieval: bool = True,
    model: FalconModel | None = None,
    proposal_settings: ProposalSettings | None = None,
    catalog: CapabilityCatalog | None = None,
    capability_policy: CapabilityPolicy | None = None,
    redteam: bool = False,
    red_model: FalconModel | None = None,
) -> ProposalRun:
    """``redteam``: run C5 (red-team review and the proposer's response) after C4b.
    ``red_model`` overrides ``CROW_REDTEAM_MODEL`` (tests)."""
    out_dir = out_dir or run_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    runner = ModelRunner(
        model or DashScopeModel(settings.crow_proposer_model or settings.chat_model)
    )
    crow = load_crow_result(run_dir)
    requirement = requirement or (profile.requirement if profile else crow.collection.requirement)

    package_path = evidence_package or run_dir / "evidence_package.json"
    if package_path.is_file():
        package = EvidencePackage.model_validate_json(package_path.read_text(encoding="utf-8"))
    else:
        print("正在规范化证据（F1）……")
        package = await normalize_evidence(crow, requirement, runner)
        package_path = run_dir / "evidence_package.json"
        write_package(package, package_path)

    retriever = (
        GapRetriever(
            settings=settings,
            requirement=requirement,
            profile=profile,
            output_dir=out_dir / "gap_retrieval",
            runner=runner,
        )
        if retrieval
        else None
    )
    proposer = TacticProposer(
        runner=runner,
        settings=proposal_settings
        or ProposalSettings(
            max_candidates=settings.crow_max_candidates,
            max_innovative=settings.crow_max_innovative,
            ideas_per_gap=settings.crow_ideas_per_gap,
        ),
        retriever=retriever,
    )
    print("正在提出候选战术……")
    result = await proposer.propose(
        requirement,
        package,
        profile=profile,
        readiness=crow.readiness,
        package_ref=str(package_path),
    )
    annotator = None
    cap_runner = runner
    if catalog is not None:
        print("正在标注候选战术所需的平台能力（C4b）……")
        cap_runner = (
            ModelRunner(DashScopeModel(settings.crow_capability_model))
            if settings.crow_capability_model and model is None
            else runner
        )
        annotator = CapabilityAnnotator(cap_runner, catalog, policy=capability_policy)
        await annotator.annotate(result, profile)
    paths: dict[str, Path] = {}
    if redteam and result.candidates:
        from afsim_research_agent.crow.redteam import run_redteam, write_c5

        print("正在进行红方审查与回应（C5）……")
        red_name = settings.crow_redteam_model
        proposer_name = settings.crow_proposer_model or settings.chat_model
        red_runner = ModelRunner(red_model or DashScopeModel(red_name))
        reviews, notes = await run_redteam(
            result,
            profile,
            red_runner=red_runner,
            proposer_runner=runner,
            red_model=red_name if red_model is None else getattr(red_model, "name", None),
            proposer_model=proposer_name if model is None else getattr(model, "name", None),
            annotator=annotator,
        )
        runner.records.extend(red_runner.records)
        if cap_runner is not runner:
            runner.records.extend(cap_runner.records)
        trace = [record.model_dump(mode="json") for record in runner.records]
        paths = write_c5(
            result,
            reviews,
            package,
            out_dir,
            red_model=red_name,
            proposer_model=proposer_name,
            notes=notes,
        )
        (out_dir / "tactic_trace.json").write_text(
            json.dumps(trace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        paths["trace"] = out_dir / "tactic_trace.json"
    else:
        if cap_runner is not runner:
            runner.records.extend(cap_runner.records)
        trace = [record.model_dump(mode="json") for record in runner.records]
        paths = write_proposal(result, package, out_dir, trace)
    return ProposalRun(result=result, package=package, paths=paths)


def print_summary(run: ProposalRun) -> None:
    result = run.result
    retrieved = sum(1 for c in result.candidates if c.provenance == "retrieved")
    print(
        f"\n候选战术 {len(result.candidates)} 个（检索 {retrieved}，"
        f"创新补位 {len(result.candidates) - retrieved}）："
    )
    for card in result.candidates:
        tag = "" if card.provenance == "retrieved" else f" [{card.provenance}，补 {card.fills_gap}]"
        print(f"  {card.id} {card.name}（综合分 {card.score.total:.2f}）{tag}")
        if card.feasibility is not None:
            level = {
                "ready": "可实现",
                "needs_confirmation": "需确认",
                "needs_development": "需开发",
            }
            print(f"     平台可行性：{level[card.feasibility.level]}")
    for gap in result.gaps:
        print(f"  缺口 {gap.id}：{gap.detail}")
    if result.capability_gaps:
        print(f"- 平台能力缺口 {len(result.capability_gaps)} 项：{run.paths['capability_gaps']}")
    print(f"- 候选战术报告：{run.paths['markdown']}")
    if "report" in run.paths:
        print(f"- 评审报告（红方意见与回应，供选择）：{run.paths['report']}")
    print(f"- 候选战术文件：{run.paths['candidates']}")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="为一次 Crow 运行提出候选战术（C4）。")
    parser.add_argument("run_dir", type=Path, help="Crow 运行目录（含 research_result.json）")
    parser.add_argument("--out", type=Path, help="输出目录，默认为运行目录")
    parser.add_argument("--evidence-package", type=Path, help="已有的 evidence_package.json")
    parser.add_argument("--mission-profile", type=Path, help="已有的 mission_profile.yaml")
    parser.add_argument("--requirement", help="覆盖需求原文（默认取任务画像或 Crow 结果）")
    parser.add_argument("--no-retrieval", action="store_true", help="材料不足时不做补充检索")
    parser.add_argument("--no-input", action="store_true", help="不在命令行提问")
    parser.add_argument("--max-candidates", type=int, choices=range(1, 11), metavar="1-10")
    parser.add_argument("--max-innovative", type=int, choices=range(0, 6), metavar="0-5")
    parser.add_argument(
        "--capability-catalog", type=Path, help="能力目录根目录（默认取配置或仓库内副本）"
    )
    parser.add_argument("--no-capability", action="store_true", help="不做平台能力标注（C4b）")
    parser.add_argument(
        "--no-redteam", action="store_true", help="不做红方审查与回应（C5，默认进行）"
    )
    add_granularity_arguments(parser)
    args = parser.parse_args(argv)
    policy = policy_from_args(parser, args)

    load_dotenv()
    settings = Settings.from_environment()
    out_dir = args.out or args.run_dir
    crow = load_crow_result(args.run_dir)

    candidates = (
        [args.mission_profile]
        if args.mission_profile
        else [
            args.run_dir / "mission_profile.yaml",
            out_dir / "mission_profile.yaml",
        ]
    )
    profile_path = next((path for path in candidates if path.is_file()), None)
    if profile_path is not None:
        print(f"使用任务画像：{profile_path}")
        profile = load_profile(profile_path)
    else:
        from afsim_research_agent.crow.cli import prepare_mission_profile

        profile = prepare_mission_profile(
            args.requirement or crow.collection.requirement,
            settings=settings,
            output_dir=out_dir,
            interactive=not args.no_input and sys.stdin.isatty(),
        )

    proposal_settings = ProposalSettings(
        max_candidates=args.max_candidates or settings.crow_max_candidates,
        max_innovative=(
            settings.crow_max_innovative if args.max_innovative is None else args.max_innovative
        ),
        ideas_per_gap=settings.crow_ideas_per_gap,
    )
    catalog = None if args.no_capability else load_catalog(args.capability_catalog, settings)
    run = asyncio.run(
        run_proposal(
            args.run_dir,
            settings=settings,
            out_dir=out_dir,
            evidence_package=args.evidence_package,
            profile=profile,
            requirement=args.requirement,
            retrieval=not args.no_retrieval,
            proposal_settings=proposal_settings,
            catalog=catalog,
            capability_policy=policy,
            redteam=not args.no_redteam,
        )
    )
    print_summary(run)


def load_catalog(path: Path | None, settings: Settings) -> CapabilityCatalog | None:
    """Explicit path, else CROW_CAPABILITY_CATALOG, else the repository copy; None if absent."""
    root = path or (
        Path(settings.crow_capability_catalog) if settings.crow_capability_catalog else None
    )
    try:
        catalog = CapabilityCatalog.load(root)
    except CatalogError as exc:
        print(f"未加载能力目录，跳过平台能力标注：{exc}")
        return None
    print(f"使用能力目录：{catalog.catalog_id}（{len(catalog)} 项）")
    return catalog


def add_granularity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--granularity",
        choices=("coarse", "fine"),
        default="coarse",
        help="能力判定粒度：coarse 只精确核对核心能力组（默认），fine 全部精确核对",
    )
    parser.add_argument(
        "--refine",
        action="append",
        default=[],
        help="需要精确核对的能力组（可多次），如 comms、sensing、fires",
    )


def policy_from_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> CapabilityPolicy:
    try:
        return CapabilityPolicy.make(args.granularity, args.refine)
    except ValueError as exc:
        parser.error(str(exc))
        raise  # unreachable: parser.error exits


if __name__ == "__main__":
    main()
