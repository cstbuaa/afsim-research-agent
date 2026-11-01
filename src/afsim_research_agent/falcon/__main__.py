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
import os
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
    draft3 = sub.add_parser("draft3", help="v3：需求 → 战术稿 v3 → IR 2.0 模型包")
    draft3.add_argument("request", type=Path)
    draft3.add_argument("--out", type=Path, help="模型包的上级目录（默认请求文件所在目录）")
    draft3.add_argument("--no-mission-ref", action="store_true", help="不允许整体引用战术集任务")
    draft3.add_argument(
        "--fix-steps", type=int, help="自检纠错循环的步数上限（默认取请求文件，缺省 20；0 关闭）"
    )
    draft3.add_argument(
        "--candidates",
        type=int,
        help="独立起草的份数，取检查结果最好的一份（默认取请求文件，缺省 1）",
    )
    draft3.add_argument(
        "--afsim-binding", type=Path, help="AFSIM 绑定：给出时选中的稿在 AFSIM 中运行"
    )
    draft3.add_argument("--afsim-run", type=Path, action="append", help="AFSIM 运行配置（可多个）")
    draft3.add_argument(
        "--afsim-mission", help="mission 可执行文件（默认 AFSIM_MISSION 或 mission）"
    )
    draft3.add_argument(
        "--afsim-rounds", type=int, default=3, help="AFSIM 失败后修复循环内最多复验几次"
    )
    draft3.add_argument("--afsim-steps", type=int, default=15, help="每次复验对应的修复步数")
    crow = sub.add_parser(
        "crow-request",
        help="Crow → v3：由 Crow 战术目录（含 tactic_selection.yaml）写出 draft3 请求文件",
    )
    crow.add_argument("tactics_dir", type=Path, help="Crow 战术目录（含 tactic_selection.yaml）")
    crow.add_argument("--model-id", required=True)
    crow.add_argument("--out", type=Path, help="请求文件路径（默认 <运行目录>/falcon3_<id>.yaml）")
    crow.add_argument(
        "--role", action="append", default=[], help="Crow 角色=角色 ID，如 排长车=leader（可多个）"
    )
    crow.add_argument("--requirement", default="", help="补充的需求原文（默认用 Crow 的需求）")
    crow.add_argument("--candidates", type=int, default=3)
    crow.add_argument(
        "--missing",
        choices=("skip", "wait"),
        default="skip",
        help="能力目录暂缺的战术要素：skip 本次忽略（默认），wait 写成缺口等待能力开发",
    )
    comp3 = sub.add_parser(
        "compile3", help="v3：战术稿 → IR 2.0 并做 L1–L3（不调用模型，结果写到新目录）"
    )
    comp3.add_argument("sheet", type=Path)
    comp3.add_argument("--request", type=Path, required=True)
    comp3.add_argument("--out", type=Path, required=True, help="新的模型包目录（不能已存在）")
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


def _print_v3(result) -> None:
    check = result.check
    print(f"模型包：{result.package_dir}")
    print(
        f"状态：{result.status}；L1 {'通过' if check.l1_ok else '未通过'}；"
        f"L2 {sum(t['passed'] for t in check.l2)}/{len(check.l2)}；"
        f"L3 {sum(s['passed'] for s in check.l3)}/{len(check.l3)}；修复 {len(result.rounds)} 轮"
    )
    for p in check.problems[:20]:
        print(p.render())
    print("审阅：falcon_review.md")


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
        if args.command == "draft3":
            from dotenv import load_dotenv

            from afsim_research_agent.falcon.llm import DashScopeModel
            from afsim_research_agent.falcon.v3.draft import run_draft3

            load_dotenv()
            afsim = None
            if args.afsim_binding is not None:
                from afsim_research_agent.falcon.v3.afsim_loop import AfsimConfig

                if not args.afsim_run:
                    print("--afsim-binding 需要至少一个 --afsim-run", file=sys.stderr)
                    return EXIT_INPUT
                afsim = AfsimConfig(
                    binding=args.afsim_binding,
                    runs=list(args.afsim_run),
                    mission=args.afsim_mission or os.environ.get("AFSIM_MISSION") or "mission",
                    rounds=args.afsim_rounds,
                    steps=args.afsim_steps,
                )
            result3 = asyncio.run(
                run_draft3(
                    args.request,
                    DashScopeModel(),
                    out_dir=args.out,
                    allow_mission_ref=False if args.no_mission_ref else None,
                    fix_steps=args.fix_steps,
                    candidates=args.candidates,
                    afsim=afsim,
                )
            )
            _print_v3(result3)
            return result3.exit_code
        if args.command == "crow-request":
            import yaml

            from afsim_research_agent.falcon.v3.crow_bridge import make_request

            role_map = {}
            for item in args.role:
                crow_role, sep, rid = item.partition("=")
                if not sep or not rid.strip():
                    print(f"--role 写成 Crow角色=角色ID：{item}", file=sys.stderr)
                    return EXIT_INPUT
                role_map[crow_role.strip()] = rid.strip()
            tactics = args.tactics_dir
            if not (tactics / "tactic_selection.yaml").is_file():
                print(
                    f"{tactics} 中没有 tactic_selection.yaml：先在 Crow 中选择战术", file=sys.stderr
                )
                return EXIT_INPUT
            run_dir = tactics.parent if tactics.name == "tactics" else tactics
            out = args.out or run_dir / f"falcon3_{args.model_id}.yaml"
            if out.exists():
                print(f"{out} 已存在，不覆盖", file=sys.stderr)
                return EXIT_INPUT
            doc = make_request(
                tactics,
                args.model_id,
                requirement=args.requirement,
                role_map=role_map,
                candidates=args.candidates,
                missing_capabilities=args.missing,
                base=out.parent,
            )
            out.write_text(
                yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
            print(f"请求文件：{out}")
            print(f"下一步：python -m afsim_research_agent.falcon draft3 {out}")
            return 0
        if args.command == "compile3":
            from afsim_research_agent.falcon.v3.draft import compile_package

            result3 = compile_package(args.sheet, args.request, args.out)
            _print_v3(result3)
            return result3.exit_code
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
        if os.environ.get("FALCON_TRACEBACK"):
            # program frames only (file, line, function); no model output is included
            import traceback

            for frame in traceback.extract_tb(exc.__traceback__)[-8:]:
                print(f"  {frame.filename}:{frame.lineno} {frame.name}", file=sys.stderr)
        print(
            "诊断记录：models/<model_id>.failure.json（调用阶段、耗时，不含模型输出）",
            file=sys.stderr,
        )
        return 1
    return EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
