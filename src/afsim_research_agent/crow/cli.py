"""Interactive command-line entry point for internet-only Tactical Crow research."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol

from dotenv import load_dotenv

from afsim_research_agent.config import Settings
from afsim_research_agent.crow.mission import (
    MissionProfile,
    MissionProfiler,
    OpenQuestion,
    load_profile,
    resolve_open_questions,
    write_profile,
)
from afsim_research_agent.crow.models import CrowResearchResult
from afsim_research_agent.crow.planner import TacticalResearchPlanner
from afsim_research_agent.crow.report import collection_diagnostics, write_research_bundle
from afsim_research_agent.crow.research import CrowResearchPipeline
from afsim_research_agent.literature.paperqa_backend import PaperQABackend


class ResearchPipeline(Protocol):
    async def run(
        self,
        requirement: str,
        *,
        local_roots: Sequence[Path],
        output_dir: Path,
        max_web_results: int,
        max_total_sources: int | None,
        library_roots: Sequence[Path] | None,
        gap_rounds: int | None,
    ) -> CrowResearchResult: ...


async def run_research(
    requirement: str,
    *,
    output_dir: Path,
    max_web_results: int,
    max_total_sources: int | None = None,
    library_roots: Sequence[Path] | None = None,
    gap_rounds: int | None = None,
    pipeline: ResearchPipeline | None = None,
    mission_profile: MissionProfile | None = None,
) -> tuple[Path, Path]:
    """Run internet research (plus the optional local library) and write the bundle."""
    paths, _ = await run_research_with_result(
        requirement,
        output_dir=output_dir,
        max_web_results=max_web_results,
        max_total_sources=max_total_sources,
        library_roots=library_roots,
        gap_rounds=gap_rounds,
        pipeline=pipeline,
        mission_profile=mission_profile,
    )
    return paths


async def run_research_with_result(
    requirement: str,
    *,
    output_dir: Path,
    max_web_results: int,
    max_total_sources: int | None = None,
    library_roots: Sequence[Path] | None = None,
    gap_rounds: int | None = None,
    pipeline: ResearchPipeline | None = None,
    mission_profile: MissionProfile | None = None,
) -> tuple[tuple[Path, Path], CrowResearchResult]:
    if pipeline is None:
        runtime_settings = Settings.from_environment()
        pipeline = CrowResearchPipeline(
            planner=TacticalResearchPlanner(app_settings=runtime_settings),
            literature_backend=PaperQABackend(app_settings=runtime_settings),
            app_settings=runtime_settings,
        )
    extra: dict[str, object] = {}
    if mission_profile is not None:
        extra["mission_profile"] = mission_profile
    result = await pipeline.run(
        requirement,
        local_roots=(),
        output_dir=output_dir,
        max_web_results=max_web_results,
        max_total_sources=max_total_sources,
        library_roots=library_roots,
        gap_rounds=gap_rounds,
        **extra,
    )
    return write_research_bundle(result, output_dir), result


def main(argv: Sequence[str] | None = None) -> None:
    """Read one requirement and run the complete internet research flow."""
    parser = argparse.ArgumentParser(
        description="输入战术需求，下载互联网资料并生成带证据的研究报告。"
    )
    parser.add_argument("--requirement", help="自然语言战术或行为建模需求")
    parser.add_argument("--output-dir", type=Path, help="本次运行的输出目录")
    parser.add_argument(
        "--max-web-results",
        type=int,
        default=3,
        choices=range(1, 11),
        metavar="1-10",
        help="每个检索式返回的候选结果数，默认 3",
    )
    parser.add_argument(
        "--max-sources",
        type=int,
        default=None,
        choices=range(1, 61),
        metavar="1-60",
        help="整次运行最多采用的来源数，默认读取 CROW_MAX_SOURCES（12）",
    )
    parser.add_argument(
        "--library",
        type=Path,
        action="append",
        help="本地文献库目录（可重复）；默认读取 CROW_LIBRARY_DIRS",
    )
    parser.add_argument(
        "--gap-rounds",
        type=int,
        default=None,
        choices=range(0, 4),
        metavar="0-3",
        help="证据不足时追加检索的轮数，默认读取 CROW_GAP_ROUNDS（1）",
    )
    parser.add_argument(
        "--mission-profile",
        type=Path,
        help="使用已有的 mission_profile.yaml（跳过需求理解步骤，仍会询问未回答的阻塞问题）",
    )
    parser.add_argument(
        "--skip-profile", action="store_true", help="不生成任务画像，直接规划研究问题"
    )
    parser.add_argument(
        "--no-propose",
        action="store_true",
        help="门槛通过后不自动提出候选战术（可之后用 crow.propose 单独运行）",
    )
    parser.add_argument(
        "--no-input",
        action="store_true",
        help="不在命令行提问；所有待确认问题按默认假设处理",
    )
    args = parser.parse_args(argv)

    load_dotenv()
    requirement = (args.requirement or input("请输入自然语言研究需求：\n> ")).strip()
    if not requirement:
        parser.error("研究需求不能为空")

    runtime_settings = Settings.from_environment()
    output_dir = args.output_dir or (
        runtime_settings.data_dir
        / "runs"
        / "crow-research"
        / datetime.now().strftime("%Y%m%d-%H%M%S")
    )
    print(f"\n运行目录：{output_dir}")
    profile = prepare_mission_profile(
        requirement,
        settings=runtime_settings,
        output_dir=output_dir,
        existing=args.mission_profile,
        skip=args.skip_profile,
        interactive=not args.no_input and sys.stdin.isatty(),
    )
    print("正在检索互联网资料并生成证据报告，请耐心等待……")
    (markdown_path, json_path), result = asyncio.run(
        run_research_with_result(
            requirement,
            output_dir=output_dir,
            max_web_results=args.max_web_results,
            max_total_sources=args.max_sources,
            library_roots=args.library,
            gap_rounds=args.gap_rounds,
            mission_profile=profile,
        )
    )
    collection = result.collection
    print("\n研究完成：")
    print(
        f"- 候选 {len(collection.candidates)} 个，采用 {len(collection.sources)} 个，"
        f"失败或拒绝 {len(collection.failures)} 个"
    )
    for line in collection_diagnostics(collection):
        print(line)
    if result.readiness is not None:
        failed = [check.name for check in result.readiness.checks if not check.passed]
        verdict = "通过" if result.readiness.ready else f"未通过（{', '.join(failed)}）"
        print(f"- Falcon 输入门槛：{verdict}")
    print(f"- Markdown 报告：{markdown_path}")
    print(f"- JSON 结果：{json_path}")

    if args.no_propose:
        return
    if result.readiness is None or not result.readiness.ready:
        print(
            "\n门槛未通过，未自动提出候选战术。确认资料后可运行：\n"
            f"  python -m afsim_research_agent.crow.propose {output_dir}"
        )
        return
    from afsim_research_agent.crow.propose import print_summary, run_proposal

    print_summary(
        asyncio.run(run_proposal(output_dir, settings=runtime_settings, profile=profile))
    )


def prepare_mission_profile(
    requirement: str,
    *,
    settings: Settings,
    output_dir: Path,
    existing: Path | None = None,
    skip: bool = False,
    interactive: bool = True,
    profiler: MissionProfiler | None = None,
) -> MissionProfile | None:
    """C1: build (or load) the mission profile and settle open questions."""
    if skip:
        return None
    if existing is not None:
        profile = load_profile(existing)
    else:
        print("正在理解需求、整理任务画像……")
        try:
            profiler = profiler or MissionProfiler(app_settings=settings)
            profile = asyncio.run(profiler.profile(requirement))
        except Exception as exc:  # noqa: BLE001 - the research run can continue without it
            print(f"任务画像生成失败（{type(exc).__name__}: {str(exc)[:200]}），跳过此步。")
            return None
    _print_profile(profile, interactive)
    resolve_open_questions(profile, _ask if interactive else None)
    path = write_profile(profile, output_dir / "mission_profile.yaml")
    print(f"任务画像已保存：{path}\n")
    return profile


def _print_profile(profile: MissionProfile, interactive: bool) -> None:
    print(f"\n任务画像：{profile.domain} / {profile.echelon} / {profile.mission.type}")
    print(f"  目标：{profile.mission.objective}")
    if profile.user_specified:
        print("  用户指定：" + "；".join(profile.user_specified))
    for note in profile.notes:
        print(f"  提示：{note}")
    defaults = [q for q in profile.open_questions if not q.blocking and q.answered_by is None]
    if defaults:
        print(
            "  以下问题按默认假设处理"
            "（可在 mission_profile.yaml 中修改后用 --mission-profile 重跑）："
        )
        for q in defaults:
            print(f"   - {q.text} → {q.default_assumption}")
    if profile.blocking_questions and interactive:
        print("\n以下问题会影响战术选择，请回答（直接回车采用默认假设）：")
    elif profile.blocking_questions:
        print("  非交互运行：阻塞问题也按默认假设处理，见 mission_profile.yaml")


def _ask(question: OpenQuestion) -> str | None:
    try:
        return input(
            f"\n[{question.id}] {question.text}\n  默认：{question.default_assumption}\n> "
        )
    except EOFError:
        return None


if __name__ == "__main__":
    main()
