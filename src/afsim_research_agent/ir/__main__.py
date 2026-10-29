"""Command line: ``python -m afsim_research_agent.ir <command> <package>``.

Commands
  validate  <pkg>                L1 only
  test      <pkg> [--draft]      L1 + gate + L2 (draft skips the confirmation gate)
  build     <pkg> [--draft] [--backend B ...] [--skip-l3] [--crow-gaps F --crow-candidate ID]
            [--capability-catalog DIR] [--no-capability] [--granularity G] [--refine GROUP]
            [--faithful-sim]
                                 full pipeline; writes derived files into the package
  simulate  <pkg> [--scenario ID]
                                 L3 only, from an existing normalized_ir.json
  cosim     <pkg> --engine reference|FILE.py:FACTORY|module:FACTORY
            [--engine-name NAME] [--messaging adapter|engine] [--scenario ID] [--trace-dir DIR]
                                 lock-step co-simulation with an external engine
  serve     [--engine kinematic|reference] [--package PKG] [--host H] [--port P]
                                 reference tbe-step/1 engine host (Python)
  protocol-check --host H --port P
                                 conformance check of a tbe-step/1 engine host
  conformance export <pkg> --out DIR | compare --pack DIR --results FILE.json
                                 single-tick conformance pack for native backends
  judge     <pkg> --scenario ID --trace FILE.jsonl
                                 judge a recorded run (e.g. a native lab-engine run) with L3
  schema    [--out PATH] [--ir-version 2.0]
                                 write the JSON Schema of behavior_spec.yaml
  v2-check  <behavior_spec.yaml> [--capability-set DIR] [--out FILE.json] [-v]
                                 IR 2.0 normalization and L1 against a capability set
  v2-from-tactic (<mission id> ... | --all) [--capability-set DIR] [--out DIR] [-v]
                                 convert tactic-set missions to IR 2.0 (no model) and check them
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from afsim_research_agent.ir.binding import BACKENDS
from afsim_research_agent.ir.diagnostics import Severity


def _print_diagnostics(items, *, verbose: bool) -> None:
    for item in items:
        if item.severity is Severity.INFO and not verbose:
            continue
        print(item.render())


def schema_text() -> str:
    from afsim_research_agent.ir.models import BehaviorSpec

    schema = BehaviorSpec.model_json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://afsim-research-agent.local/schema/behavior_spec-1.0.json"
    schema["title"] = "Tactical Behavior IR 1.0 (behavior_spec.yaml)"
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def schema_text_v2() -> str:
    from afsim_research_agent.ir.v2 import json_schema

    schema = json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://afsim-research-agent.local/schema/behavior_spec-2.0.json"
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _contracts(root: Path | None):
    from afsim_research_agent.capability import CatalogError, default_catalog_root
    from afsim_research_agent.ir.v2 import Contracts

    try:
        return Contracts.load(root or default_catalog_root())
    except CatalogError as exc:
        print(f"capability set: {exc}")
        return None


def _v2_check(args) -> int:
    import yaml

    from afsim_research_agent.ir.v2 import normalize

    contracts = _contracts(args.capability_set)
    if contracts is None:
        return 2
    raw = yaml.safe_load(args.spec.read_text(encoding="utf-8"))
    result = normalize(raw, contracts)
    _print_diagnostics(result.diagnostics, verbose=args.verbose)
    if result.ok and args.out:
        if args.out.exists():
            print(f"{args.out} exists; not overwritten")
            return 1
        args.out.write_text(
            json.dumps(result.model, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print("IR 2.0 L1 passed" if result.ok else "IR 2.0 L1 failed")
    return 0 if result.ok else 1


def _v2_from_tactic(args) -> int:
    import yaml

    from afsim_research_agent.ir.v2 import mission_to_ir, normalize

    contracts = _contracts(args.capability_set)
    if contracts is None:
        return 2
    ids = args.missions or []
    if args.all:
        ids = sorted(e.id for e in contracts.tactics.values() if e.section == "missions")
    if not ids:
        print("name tactic-set missions, or use --all")
        return 2
    if args.out:
        if args.out.exists() and any(args.out.iterdir()):
            print(f"{args.out} is not empty; choose a new directory")
            return 1
        args.out.mkdir(parents=True, exist_ok=True)
    failed = 0
    for mission_id in ids:
        raw = mission_to_ir(contracts, mission_id)
        result = normalize(raw, contracts)
        _print_diagnostics(result.diagnostics, verbose=args.verbose)
        roles = ", ".join(raw["missions"])
        print(f"{'PASS' if result.ok else 'FAIL'} {mission_id} ({roles})")
        failed += not result.ok
        if args.out:
            name = raw["model"]["id"]
            (args.out / f"{name}.behavior_spec.yaml").write_text(
                yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
    print(f"{len(ids) - failed}/{len(ids)} missions pass IR 2.0 L1")
    return 0 if not failed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m afsim_research_agent.ir")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "test", "build", "simulate"):
        cmd = sub.add_parser(name)
        cmd.add_argument("package", type=Path)
        cmd.add_argument("-v", "--verbose", action="store_true")
        if name in ("test", "build"):
            cmd.add_argument("--draft", action="store_true")
        if name == "build":
            cmd.add_argument("--backend", action="append", choices=BACKENDS)
            cmd.add_argument("--skip-l3", action="store_true")
            cmd.add_argument(
                "--capability-catalog", type=Path, help="能力目录根目录（默认仓库副本）"
            )
            cmd.add_argument("--no-capability", action="store_true", help="跳过平台能力核对")
            cmd.add_argument(
                "--crow-gaps", type=Path, help="Crow 的 capability_gaps.yaml，用于对照预估"
            )
            cmd.add_argument("--crow-candidate", help="本模型实现的 Crow 候选 ID（如 tc_02）")
            cmd.add_argument(
                "--granularity",
                choices=("coarse", "fine"),
                help="能力核对粒度（默认读 capability_decisions.yaml，否则 coarse）",
            )
            cmd.add_argument(
                "--refine", action="append", help="精确核对的能力组（可多次），如 comms"
            )
            cmd.add_argument(
                "--strict-parameters",
                action="store_true",
                help="未确认的参数值也阻塞构建（默认按参考值通过，列入 review.md 待复核）",
            )
            cmd.add_argument(
                "--faithful-sim",
                action="store_true",
                help="L3 忠实模式：未处理的能力缺口在仿真中不提供（读取为未知值，动作无效）",
            )
        if name == "simulate":
            cmd.add_argument("--scenario")
    cosim = sub.add_parser("cosim")
    cosim.add_argument("package", type=Path)
    cosim.add_argument("--engine", required=True)
    cosim.add_argument("--engine-name", default="lab_engine")
    cosim.add_argument("--messaging", choices=("adapter", "engine"), default=None)
    cosim.add_argument("--scenario")
    cosim.add_argument("--trace-dir", type=Path)
    cosim.add_argument("-v", "--verbose", action="store_true")
    srv = sub.add_parser("serve")
    srv.add_argument("--engine", choices=("kinematic", "reference"), default="kinematic")
    srv.add_argument("--package", type=Path)
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=47100)
    chk = sub.add_parser("protocol-check")
    chk.add_argument("--host", default="127.0.0.1")
    chk.add_argument("--port", type=int, default=47100)
    conf = sub.add_parser("conformance")
    conf_sub = conf.add_subparsers(dest="conf_command", required=True)
    exp = conf_sub.add_parser("export")
    exp.add_argument("package", type=Path)
    exp.add_argument("--out", type=Path, required=True)
    cmp_ = conf_sub.add_parser("compare")
    cmp_.add_argument("--pack", type=Path, required=True)
    cmp_.add_argument("--results", type=Path, required=True)
    judge = sub.add_parser("judge")
    judge.add_argument("package", type=Path)
    judge.add_argument("--scenario", required=True)
    judge.add_argument("--trace", type=Path, required=True)
    schema = sub.add_parser("schema")
    schema.add_argument("--out", type=Path)
    schema.add_argument("--ir-version", choices=("1.0", "2.0"), default="1.0")
    v2c = sub.add_parser("v2-check")
    v2c.add_argument("spec", type=Path)
    v2c.add_argument("--capability-set", type=Path)
    v2c.add_argument("--out", type=Path)
    v2c.add_argument("-v", "--verbose", action="store_true")
    v2t = sub.add_parser("v2-from-tactic")
    v2t.add_argument("missions", nargs="*")
    v2t.add_argument("--all", action="store_true")
    v2t.add_argument("--capability-set", type=Path)
    v2t.add_argument("--out", type=Path)
    v2t.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "v2-check":
        return _v2_check(args)
    if args.command == "v2-from-tactic":
        return _v2_from_tactic(args)

    if args.command == "cosim":
        return _cosim(args)
    if args.command == "judge":
        return _judge(args)
    if args.command == "serve":
        return _serve(args)
    if args.command == "conformance":
        from afsim_research_agent.ir.conformance import compare_l2, export_pack

        if args.conf_command == "export":
            ir = _load_ir(args.package)
            if ir is None:
                return 1
            counts = export_pack(ir, args.out)
            print(
                f"wrote {counts['l2_cases']} L2 cases and "
                f"{counts['l3_scenarios']} L3 references to {args.out}"
            )
            return 0
        problems = compare_l2(args.pack, args.results)
        for problem in problems:
            print(problem)
        print("conformant" if not problems else f"{len(problems)} differences")
        return 0 if not problems else 1
    if args.command == "protocol-check":
        from afsim_research_agent.ir.cosim.check import run_checks

        results = run_checks(args.host, args.port)
        for r in results:
            print(
                ("PASS " if r.passed else "FAIL ") + r.name + (f": {r.detail}" if r.detail else "")
            )
        passed = sum(r.passed for r in results)
        print(f"{passed}/{len(results)} checks passed")
        return 0 if passed == len(results) else 1

    if args.command == "schema":
        text = schema_text_v2() if args.ir_version == "2.0" else schema_text()
        if args.out:
            args.out.write_text(text, encoding="utf-8")
        else:
            sys.stdout.write(text)
        return 0

    if args.command == "validate":
        from afsim_research_agent.ir.validate import validate_package

        result = validate_package(args.package)
        _print_diagnostics(result.diagnostics, verbose=args.verbose)
        print("L1 passed" if result.ok else "L1 failed")
        return 0 if result.ok else 1

    if args.command == "test":
        from afsim_research_agent.ir.l2 import run_tests
        from afsim_research_agent.ir.normalize import normalize
        from afsim_research_agent.ir.validate import validate_package

        norm = normalize(validate_package(args.package), draft=args.draft)
        _print_diagnostics(norm.diagnostics, verbose=args.verbose)
        if norm.ir is None:
            print("blocked before L2")
            return 1
        report = run_tests(norm.ir)
        for outcome in report.outcomes:
            print(
                ("PASS " if outcome.passed else "FAIL ") + outcome.test_id,
                "; ".join(outcome.failures),
            )
        _print_diagnostics(report.diagnostics, verbose=args.verbose)
        print("L2 passed" if report.passed else "L2 failed")
        return 0 if report.passed else 1

    if args.command == "build":
        from afsim_research_agent.ir.pipeline import build_package

        result = build_package(
            args.package,
            backends=tuple(args.backend or BACKENDS),
            draft=args.draft,
            run_l3=not args.skip_l3,
            capability_catalog=args.capability_catalog,
            check_capabilities=not args.no_capability,
            crow_gaps=args.crow_gaps,
            crow_candidate=args.crow_candidate,
            granularity=args.granularity,
            refine=args.refine,
            faithful_sim=args.faithful_sim,
            strict_parameters=args.strict_parameters,
        )
        _print_diagnostics(result.diagnostics, verbose=args.verbose)
        print(f"status: {result.status}; wrote {len(result.written)} files; see review.md")
        if result.capability is not None:
            cap = result.capability
            level = {
                "ready": "可实现",
                "needs_confirmation": "需确认",
                "needs_development": "需开发",
            }
            print(
                f"platform capability: {level[cap.level]}; {len(cap.gaps)} gaps, "
                f"{len(cap.unresolved)} unresolved (AFSIM backend gated); see capability_gaps.yaml"
            )
        return 0 if result.ok else 1

    if args.command == "simulate":
        from afsim_research_agent.ir.binding import bind, load_overrides
        from afsim_research_agent.ir.normalize import verify_hash
        from afsim_research_agent.ir.sim import run_scenario

        ir = json.loads((args.package / "normalized_ir.json").read_text(encoding="utf-8"))
        if ir.get("draft") or not verify_hash(ir):
            print("normalized_ir.json is a draft or was modified; run build first")
            return 1
        context, items = bind(ir, "python_sim", load_overrides(args.package, "python_sim"))
        _print_diagnostics(items, verbose=args.verbose)
        if context is None:
            return 1
        ok = True
        for scenario in ir["scenarios"]:
            if args.scenario and scenario["id"] != args.scenario:
                continue
            result = run_scenario(ir, context, scenario, package_dir=args.package)
            ok &= result.passed
            print(("PASS " if result.passed else "FAIL ") + result.id)
            for e in result.expectations:
                print(
                    f"  {'ok ' if e.passed else 'BAD'} {e.id}: "
                    f"measured={e.measured} threshold={e.threshold} {e.detail}"
                )
        return 0 if ok else 1
    return 2


def _load_ir(package: Path) -> dict | None:
    from afsim_research_agent.ir.normalize import verify_hash

    path = package / "normalized_ir.json"
    if not path.exists():
        print("normalized_ir.json not found; run build first")
        return None
    ir = json.loads(path.read_text(encoding="utf-8"))
    if ir.get("draft") or not verify_hash(ir):
        print("normalized_ir.json is a draft or was modified; run build first")
        return None
    return ir


def _load_factory(spec: str):
    import importlib
    import importlib.util

    target, _, attr = spec.rpartition(":")
    if not target or not attr:
        raise SystemExit(
            "--engine must look like path/to/adapter.py:make_engine or module:make_engine"
        )
    if target.endswith(".py"):
        module_spec = importlib.util.spec_from_file_location("lab_engine_adapter", target)
        if module_spec is None or module_spec.loader is None:
            raise SystemExit(f"cannot load {target}")
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
    else:
        module = importlib.import_module(target)
    return getattr(module, attr)


def _print_result(result) -> None:
    print(("PASS " if result.passed else "FAIL ") + result.id)
    for e in result.expectations:
        print(
            f"  {'ok ' if e.passed else 'BAD'} {e.id}: "
            f"measured={e.measured} threshold={e.threshold} {e.detail}"
        )


def _cosim(args) -> int:
    from afsim_research_agent.ir.binding import bind, bind_external, load_overrides
    from afsim_research_agent.ir.cosim import CoSimRunner, EngineMapping

    ir = _load_ir(args.package)
    if ir is None:
        return 1
    mapping_path = args.package / "bindings" / f"{args.engine_name}.json"
    mapping = EngineMapping.load(mapping_path)
    if args.engine.startswith("tcp://"):
        from afsim_research_agent.ir.cosim.remote import RemoteEngineAdapter

        host, _, port = args.engine.removeprefix("tcp://").rpartition(":")
        if mapping_path.exists():
            context, items = bind_external(ir, args.engine_name, mapping.capabilities)
        else:
            print(f"[warning] {mapping_path} not found; using python_sim bindings")
            context, items = bind(ir, "python_sim", load_overrides(args.package, "python_sim"))

        def factory(ir_, context_, options):
            return RemoteEngineAdapter(host or "127.0.0.1", int(port))
    elif args.engine == "reference":
        from afsim_research_agent.ir.cosim.reference import ReferenceEngine

        context, items = bind(ir, "python_sim", load_overrides(args.package, "python_sim"))

        def factory(ir_, context_, options):
            return ReferenceEngine(ir_, context_, messaging=options.get("messaging") or "adapter")
    else:
        context, items = bind_external(ir, args.engine_name, mapping.capabilities)
        factory = _load_factory(args.engine)
    _print_diagnostics(items, verbose=args.verbose)
    if context is None:
        return 1
    ok = True
    for scenario in ir["scenarios"]:
        if args.scenario and scenario["id"] != args.scenario:
            continue
        engine = factory(ir, context, {"messaging": args.messaging, "package_dir": args.package})
        if args.messaging and getattr(engine, "messaging", None) != args.messaging:
            engine.messaging = args.messaging
        runner = CoSimRunner(
            ir, engine, scenario, mapping=mapping, bindings=context, package_dir=args.package
        )
        trace = None
        if args.trace_dir:
            args.trace_dir.mkdir(parents=True, exist_ok=True)
            trace = args.trace_dir / f"{scenario['id']}.jsonl"
        result = runner.run(trace_path=trace)
        for warning in runner.warnings:
            print(f"[warning] {warning}")
        _print_result(result)
        ok &= result.passed
    return 0 if ok else 1


def _serve(args) -> int:
    from afsim_research_agent.ir.cosim.server import serve

    if args.engine == "kinematic":
        from afsim_research_agent.ir.cosim.kinematic import KinematicEngine

        factory = KinematicEngine
    else:
        from afsim_research_agent.ir.binding import bind
        from afsim_research_agent.ir.cosim.reference import ReferenceEngine

        if args.package is None:
            print("--engine reference needs --package")
            return 2
        ir = _load_ir(args.package)
        if ir is None:
            return 1
        context, _ = bind(ir, "python_sim")

        def factory():
            return ReferenceEngine(ir, context)

    print(f"tbe-step/1 {args.engine} engine host on {args.host}:{args.port}")
    serve(factory, args.host, args.port, engine_name=args.engine)
    return 0


def _judge(args) -> int:
    from afsim_research_agent.ir.cosim.trace import TraceError, judge_trace

    ir = _load_ir(args.package)
    if ir is None:
        return 1
    scenario = next((s for s in ir["scenarios"] if s["id"] == args.scenario), None)
    if scenario is None:
        print(f"unknown scenario '{args.scenario}'")
        return 1
    try:
        result = judge_trace(ir, scenario, args.trace)
    except TraceError as exc:
        print(f"trace rejected: {exc}")
        return 1
    _print_result(result)
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
