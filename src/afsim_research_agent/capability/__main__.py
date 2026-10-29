"""Command line: ``python -m afsim_research_agent.capability <command>``.

Gap registry and development queue (K5, design capability-catalog-integration-v1 §3.6):

  ingest   FILE...                      add Crow / IR capability_gaps.yaml files
  select   FILE --candidate tc_02       the chosen tactic: its gaps enter the queue
  list     [--status S] [--all]         registry contents
  queue    [--out dev_queue.md]         development queue with delivery contents
  confirm  ID --by NAME [--as REF]      the platform supports it → written back to the catalog
  start    ID --by NAME                 development started
  implement ID --by NAME --plugin P [--as REF]
                                        a plugin provides it → written back to the catalog
  reject   ID --by NAME --note TEXT     will not be implemented
  reopen   ID --by NAME                 undo a decision (removes the write-back)
  merge    ID... --into ID --by NAME    same capability under different wording
  recheck  DIR [--out DIR]              judge a Crow tactics dir or a model package again
                                        against the current catalog (no model call)
  export-lab --lab DIR [--out DIR] [--check]
                                        export the public capability set (动作集 / 战术集)
                                        from the vehicle lab project; --check only compares
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from afsim_research_agent.capability.catalog import CapabilityCatalog, CatalogError
from afsim_research_agent.capability.registry import (
    GAP_LABELS,
    STATUS_LABELS,
    GapRegistry,
    RegistryError,
    registry_path,
    render_queue,
)

_LEVEL = {"ready": "可实现", "needs_confirmation": "需确认", "needs_development": "需开发"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m afsim_research_agent.capability")
    parser.add_argument(
        "--registry", type=Path, help="缺口登记表（默认 data/capability/gap_registry.yaml）"
    )
    parser.add_argument("--capability-catalog", type=Path, help="能力目录根目录（默认仓库副本）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ingest").add_argument("files", type=Path, nargs="+")
    sel = sub.add_parser("select")
    sel.add_argument("file", type=Path)
    sel.add_argument("--candidate", required=True)
    sel.add_argument("--by", default="")
    lst = sub.add_parser("list")
    lst.add_argument("--status", choices=sorted(STATUS_LABELS))
    lst.add_argument("--all", action="store_true", help="包括未选定战术的缺口")
    sub.add_parser("queue").add_argument("--out", type=Path)
    for name in ("confirm", "start", "implement", "reject", "reopen"):
        cmd = sub.add_parser(name)
        cmd.add_argument("id")
        cmd.add_argument("--by", required=True)
        cmd.add_argument("--note", default="", required=name == "reject")
        if name in ("confirm", "implement"):
            cmd.add_argument("--as", dest="as_ref", help="提供该能力的目录引用（能力ID:接口）")
        if name == "implement":
            cmd.add_argument("--plugin", required=True)
    mrg = sub.add_parser("merge")
    mrg.add_argument("ids", nargs="+")
    mrg.add_argument("--into", required=True)
    mrg.add_argument("--by", required=True)
    mrg.add_argument("--note", default="")
    exp = sub.add_parser("export-lab")
    exp.add_argument("--lab", type=Path, required=True, help="装甲车工程目录（主版本）")
    exp.add_argument("--out", type=Path, help="输出目录（默认仓库内能力集）")
    exp.add_argument("--check", action="store_true", help="只比较，不写入；有差异时返回 1")
    chk = sub.add_parser("recheck")
    chk.add_argument("directory", type=Path)
    chk.add_argument("--out", type=Path, help="Crow 目录的输出目录（默认原目录）")
    chk.add_argument("--evidence-package", type=Path)
    chk.add_argument("--granularity", choices=("coarse", "fine"))
    chk.add_argument("--refine", action="append")
    return parser


def _export_lab(args: argparse.Namespace) -> int:
    from afsim_research_agent.capability.catalog import default_catalog_root
    from afsim_research_agent.capability.lab_export import LabExporter, scan_tree, write_export

    out = args.out or args.capability_catalog or default_catalog_root()
    exporter = LabExporter(args.lab)
    result = exporter.export()
    print(exporter.report.render())
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "set"
        write_export(result, staged)
        hits = scan_tree(staged)
        if hits:
            for name, reason, line in hits:
                print(f"[{reason}] {name}: {line[:100]}", file=sys.stderr)
            print("导出结果仍含受限内容，未写入", file=sys.stderr)
            return 2
        changed = []
        for path in sorted(staged.rglob("*.yaml")):
            rel = path.relative_to(staged)
            old = out / rel
            new_text = path.read_text(encoding="utf-8")

            # the export date alone is not a change
            def body(text: str) -> str:
                return "\n".join(x for x in text.splitlines() if "exported_on:" not in x)

            if not old.exists() or body(old.read_text(encoding="utf-8")) != body(new_text):
                changed.append(rel.as_posix())
        if args.check:
            print("与仓库能力集一致" if not changed else f"有差异：{', '.join(changed)}")
            return 1 if changed else 0
        if not changed:
            print("能力集无变化")
            return 0
        for rel in changed:
            target = out / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(staged / rel, target)
        print(f"已更新 {len(changed)} 个文件：{out}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "export-lab":
        return _export_lab(args)
    path = registry_path(args.registry)
    registry = GapRegistry.load(path)
    try:
        if args.command == "ingest":
            for file in args.files:
                ids = registry.ingest(file)
                print(f"{file}: {len(ids)} 项缺口")
        elif args.command == "select":
            ids = registry.select(args.file, args.candidate, by=args.by)
            print(f"{args.candidate}：{len(ids)} 项缺口进入开发队列")
        elif args.command == "list":
            _print_list(registry, args.status, args.all)
            return 0
        elif args.command == "queue":
            text = render_queue(registry)
            if args.out:
                args.out.write_text(text, encoding="utf-8")
                print(f"开发队列：{args.out}（{len(registry.queue())} 项）")
            else:
                print(text)
            return 0
        elif args.command == "merge":
            target = registry.merge(args.ids, args.into, by=args.by, note=args.note)
            print(f"已合并到 {target.id}：{', '.join(target.aliases)}")
        elif args.command == "recheck":
            return _recheck(args, registry)
        else:
            record = registry.transition(
                args.id,
                args.command,
                by=args.by,
                note=args.note,
                plugin=getattr(args, "plugin", None),
                as_ref=getattr(args, "as_ref", None),
                catalog_root=args.capability_catalog,
            )
            print(f"{record.id} {record.display_name}：{STATUS_LABELS[record.status]}")
            if record.resolved_as and args.command in ("confirm", "implement"):
                print(f"已回写能力目录：{record.resolved_as}；用 recheck 重新判定已有结果")
    except (RegistryError, CatalogError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    registry.save(path)
    return 0


def _print_list(registry: GapRegistry, status: str | None, show_all: bool) -> None:
    rows = [
        g
        for g in registry.active()
        if (show_all or g.selected) and (status is None or g.status == status)
    ]
    if not rows:
        print("（无）")
    for g in rows:
        mark = "选定" if g.selected else "未选定"
        print(
            f"{g.id}  {GAP_LABELS[g.gap_type]}  {STATUS_LABELS[g.status]}  {mark}  "
            f"{g.display_name}  战术 {len(g.tactics)}  运行 {len(g.runs)}"
        )


def _recheck(args: argparse.Namespace, registry: GapRegistry) -> int:
    from afsim_research_agent.capability.granularity import CapabilityPolicy

    catalog = CapabilityCatalog.load(args.capability_catalog)
    directory: Path = args.directory
    policy = (
        CapabilityPolicy.make(args.granularity, args.refine or ())
        if args.granularity or args.refine
        else None
    )
    if (directory / "tactic_candidates.yaml").is_file():
        return _recheck_crow(directory, args, catalog, policy, registry)
    if (directory / "normalized_ir.json").is_file():
        return _recheck_model(directory, args, catalog, policy)
    print(
        f"错误：{directory} 中既没有 tactic_candidates.yaml，也没有 normalized_ir.json",
        file=sys.stderr,
    )
    return 2


def _recheck_crow(directory, args, catalog, policy, registry) -> int:
    from afsim_research_agent.crow.annotate import load_package, load_result
    from afsim_research_agent.crow.capability_annotation import rejudge
    from afsim_research_agent.crow.mission import load_profile
    from afsim_research_agent.crow.proposal_report import write_proposal

    result = load_result(directory)
    before = {c.id: c.feasibility.level if c.feasibility else None for c in result.candidates}
    profile_path = directory / "mission_profile.yaml"
    profile = load_profile(profile_path) if profile_path.is_file() else None
    gaps = rejudge(result, catalog, profile, policy, registry.resolutions())
    out = args.out or directory
    out.mkdir(parents=True, exist_ok=True)
    package_path = args.evidence_package or directory.parent / "evidence_package.json"
    if package_path.is_file():
        write_proposal(result, load_package(package_path), out, None)
    else:
        # without the evidence package the report's citations would be lost: data files only
        with tempfile.TemporaryDirectory() as tmp:
            write_proposal(result, load_package(None), Path(tmp), None)
            for name in ("tactic_candidates.yaml", "capability_gaps.yaml"):
                shutil.copy(Path(tmp) / name, out / name)
        print("未找到证据包，只更新了 tactic_candidates.yaml 与 capability_gaps.yaml")
    for card in result.candidates:
        old = _LEVEL.get(before[card.id] or "", "未标注")
        new = _LEVEL[card.feasibility.level] if card.feasibility else "未标注"
        print(f"  {card.id} {card.name}：{old} → {new}")
    print(f"- 缺口 {len(gaps)} 项：{out / 'capability_gaps.yaml'}")
    return 0


def _recheck_model(directory, args, catalog, policy) -> int:
    from afsim_research_agent.capability.ir_check import (
        GAPS_FILE,
        check_ir,
        load_decisions,
        load_policy,
    )

    ir = json.loads((directory / "normalized_ir.json").read_text(encoding="utf-8"))
    old_path = directory / GAPS_FILE
    old = None
    if old_path.is_file():
        import yaml

        old = (yaml.safe_load(old_path.read_text(encoding="utf-8")) or {}).get("level")
    check = check_ir(
        ir,
        catalog,
        decisions=load_decisions(directory),
        policy=policy or load_policy(directory),
    )
    old_path.write_text(check.to_yaml(), encoding="utf-8")
    print(
        f"  {directory.name}：{_LEVEL.get(old or '', '未核对')} → {_LEVEL[check.level]}；"
        f"缺口 {len(check.gaps)} 项，未处理 {len(check.unresolved)} 项"
    )
    print("  review.md 与行为树标注在下次 ir build 时更新")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
