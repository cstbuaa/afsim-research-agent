"""Command line: ``python -m afsim_research_agent.falcon <command>``.

Commands
  draft   <falcon_request.yaml> [--out DIR] [--max-repair N] [--with-l3]
          (the request may name a Crow tactic_selection; see tactical-falcon-v1 §13)
  compile <package>            rebuild behavior_spec.yaml from an edited tactic_sheet.md (v2)
  properties <package> [--force]  write requirement properties independently and check them
  fix     <package> [--out DIR] [--max-steps N] [--with-l3]
          self-checking repair loop on a copy of the package (agent-loop-deepagents-v1 §4)
  confirm <package> --by NAME
  status  <package>
  evaluate <package> [--reference PKG]

Exit codes: 0 ok; 2 Crow gate refused (FAL001); 3 input/package error
(FAL002/FAL003); 4 draft written but still has L1/L2 errors; 1 unexpected
failure (model service, network) — see models/<model_id>.failure.json.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from afsim_research_agent.falcon.errors import EXIT_INPUT, FalconError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m afsim_research_agent.falcon")
    sub = parser.add_subparsers(dest="command", required=True)
    draft = sub.add_parser("draft", help="Crow 证据 + 需求/选定战术 → behavior_spec.yaml 草稿")
    draft.add_argument("request", type=Path)
    draft.add_argument("--out", type=Path)
    draft.add_argument("--max-repair", type=int, default=3)
    draft.add_argument("--with-l3", action="store_true")
    draft.add_argument("--capability-catalog", type=Path, help="能力目录根目录（默认仓库副本）")
    draft.add_argument(
        "--no-capability", action="store_true", help="不按平台能力约束生成，也不做能力核对"
    )
    draft.add_argument(
        "--generator",
        choices=("v1", "v2"),
        help="生成层：v1 分段 JSON；v2 战术稿（默认取请求文件）",
    )
    draft.add_argument(
        "--sheet-calls", type=int, choices=(1, 3), help="v2：三次调用或一次生成整份战术稿"
    )
    draft.add_argument(
        "--tree-format",
        choices=("python", "xml"),
        help="v2：行为树写成 Python 函数或 BehaviorTree.CPP XML（默认取请求文件，缺省 python）",
    )
    comp = sub.add_parser(
        "compile", help="战术稿 tactic_sheet.md → behavior_spec.yaml（不调用模型）"
    )
    comp.add_argument("package", type=Path)
    fix = sub.add_parser(
        "fix", help="自检纠错：模型调用检查工具逐步修正战术稿（在模型包副本上进行）"
    )
    fix.add_argument("package", type=Path)
    fix.add_argument("--out", type=Path, help="输出目录（默认 <模型包>_fix，不能已存在）")
    fix.add_argument("--max-steps", type=int, default=30)
    fix.add_argument("--with-l3", action="store_true", help="L3 二维仿真想定也必须通过")
    fix.add_argument(
        "--no-properties",
        action="store_true",
        help="不生成、不检查需求属性（requirement_properties.yaml）",
    )
    props = sub.add_parser(
        "properties", help="由需求条款独立写出需求属性 requirement_properties.yaml 并检查当前战术稿"
    )
    props.add_argument("package", type=Path)
    props.add_argument("--force", action="store_true", help="覆盖已有的属性文件")
    confirm = sub.add_parser("confirm", help="把 confirmations.yaml 的决定写回 behavior_spec.yaml")
    confirm.add_argument("package", type=Path)
    confirm.add_argument("--by", required=True)
    status = sub.add_parser("status", help="确认进度与能否正式构建")
    status.add_argument("package", type=Path)
    evaluate = sub.add_parser("evaluate", help="计算在线评测指标，写入 falcon_eval.json")
    evaluate.add_argument("package", type=Path)
    evaluate.add_argument("--reference", type=Path, help="对照的参考模型包（如手写示例）")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "draft":
            from dotenv import load_dotenv

            from afsim_research_agent.falcon.llm import DashScopeModel
            from afsim_research_agent.falcon.pipeline import run_draft

            load_dotenv()
            result = asyncio.run(
                run_draft(
                    args.request,
                    DashScopeModel(),
                    out_dir=args.out,
                    max_repair=args.max_repair,
                    with_l3=args.with_l3,
                    capability_catalog=False if args.no_capability else args.capability_catalog,
                    generator=args.generator,
                    sheet_calls=args.sheet_calls,
                    tree_format=args.tree_format,
                )
            )
            print(f"模型包：{result.package_dir}")
            print(f"状态：{result.status}；待确认 {result.pending} 项（confirmations.yaml）")
            for line in result.problems[:20]:
                print(line)
            print("审查报告：falcon_review.md")
            return result.exit_code
        if args.command == "compile":
            from afsim_research_agent.falcon.sheet.recompile import recompile_package

            report = recompile_package(args.package)
            print(report.render())
            print(
                "确认：python -m afsim_research_agent.falcon confirm "
                + str(args.package)
                + " --by <姓名>"
            )
            return 0 if report.status == "draft_valid" else 4
        if args.command == "fix":
            from dotenv import load_dotenv

            from afsim_research_agent.falcon.llm import DashScopeModel, ModelRunner
            from afsim_research_agent.falcon.sheet.fixer import fix_package

            load_dotenv()
            fixed = asyncio.run(
                fix_package(
                    args.package,
                    ModelRunner(DashScopeModel()),
                    out_dir=args.out,
                    max_steps=args.max_steps,
                    with_l3=args.with_l3,
                    properties=not args.no_properties,
                )
            )
            print(f"模型包：{fixed.package_dir}")
            print(
                f"状态：{fixed.status}；{len(fixed.steps)} 步，{len(fixed.edits)} 处修改"
                f"（逐条记录见 falcon_review.md 的“自检修改记录”和 fix_log.json）"
            )
            if fixed.question:
                print(f"需要你决定：{fixed.question}")
            for line in fixed.problems[:20]:
                print(line)
            return fixed.exit_code
        if args.command == "properties":
            from dotenv import load_dotenv

            from afsim_research_agent.falcon.llm import DashScopeModel, ModelRunner
            from afsim_research_agent.falcon.sheet.fixer import write_properties

            load_dotenv()
            lines, failures = asyncio.run(
                write_properties(args.package, ModelRunner(DashScopeModel()), force=args.force)
            )
            for line in lines + failures:
                print(line)
            return 0 if not failures else 4
        if args.command == "confirm":
            from afsim_research_agent.falcon.confirm import apply_confirmations

            report = apply_confirmations(args.package, args.by)
            print(report.render())
            return 0
        if args.command == "evaluate":
            from afsim_research_agent.falcon.evaluate import evaluate_package, render_metrics

            print(render_metrics(evaluate_package(args.package, args.reference)))
            return 0
        if args.command == "status":
            from afsim_research_agent.falcon.confirm import package_status

            status = package_status(args.package)
            counts = status["counts"]
            print(
                f"待确认 {counts['pending']}，已确认 {counts['confirmed']}，已驳回 {counts['rejected']}"
            )
            if status["ready_to_build"]:
                print("可以正式构建：python -m afsim_research_agent.ir build " + str(args.package))
            else:
                print(f"阻塞正式构建的项（{len(status['blocking'])}）：")
                for line in status["blocking"][:30]:
                    print(f"  - {line}")
            return 0
    except FalconError as exc:
        print(exc.render(), file=sys.stderr)
        return exc.exit_code
    except Exception as exc:  # noqa: BLE001 - report model/service failures without a traceback
        print(f"运行失败：{type(exc).__name__}: {str(exc)[:300]}", file=sys.stderr)
        print(
            "诊断记录：models/<model_id>.failure.json（调用阶段、耗时，不含模型输出）",
            file=sys.stderr,
        )
        return 1
    return EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
