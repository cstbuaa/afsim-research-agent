"""Pipeline stages as plain functions (no LangChain / Deep Agents imports).

Every function:

- takes paths relative to the workspace root (``Workspace.resolve``);
- returns a ``StageResult`` with a short summary; full outputs stay in files;
- never raises for expected failures (bad input, failing checks) — it returns
  ``blocked`` / ``failed`` so the agent can react; unexpected exceptions are turned
  into ``blocked`` by ``guarded``.

Stages that call a model take an optional ``model`` (a ``falcon.llm.FalconModel``); tests
inject ``ScriptedModel``; by default the DashScope model from settings is used.
"""

from __future__ import annotations

import asyncio
import functools
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from afsim_research_agent.orchestrator.extensions import ExtensionRegistry, get_registry
from afsim_research_agent.orchestrator.results import StageResult
from afsim_research_agent.orchestrator.workspace import Workspace, WorkspaceError

SHEET_FILE = "tactic_sheet.md"
DECISIONS_FILE = "decisions.yaml"


# ============================================================================ helpers
def guarded(stage: str) -> Callable:
    """Turn workspace errors and unexpected exceptions into ``blocked`` results."""

    def wrap(fn: Callable) -> Callable:
        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def run_async(*args: Any, **kwargs: Any) -> StageResult:
                try:
                    return await fn(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    return _blocked(stage, exc)

            return run_async

        @functools.wraps(fn)
        def run(*args: Any, **kwargs: Any) -> StageResult:
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                return _blocked(stage, exc)

        return run

    return wrap


def _blocked(stage: str, exc: Exception) -> StageResult:
    from afsim_research_agent.falcon.errors import FalconError

    if isinstance(exc, WorkspaceError):
        text = str(exc)
    elif isinstance(exc, FalconError):
        text = exc.render() if hasattr(exc, "render") else str(exc)
    else:
        text = f"{type(exc).__name__}: {str(exc)[:300]}"
    return StageResult(stage, "blocked", f"未能执行：{text}")


def _default_model(stage_model: str | None = None):
    from dotenv import load_dotenv

    from afsim_research_agent.falcon.llm import DashScopeModel

    load_dotenv()
    return DashScopeModel(stage_model) if stage_model else DashScopeModel()


def generation_model_name() -> str:
    """The model Falcon generation / repair will use (FALCON_CHAT_MODEL or the chat model)."""
    import os

    from dotenv import load_dotenv

    load_dotenv()
    from afsim_research_agent.config import Settings

    return os.getenv("FALCON_CHAT_MODEL") or Settings.from_environment().chat_model


def _package(ws: Workspace, package: str) -> Path:
    path = ws.existing(package, kind="模型包目录")
    if not path.is_dir():
        raise WorkspaceError(f"不是目录：{ws.show(path)}")
    return path


def _files(ws: Workspace, root: Path, names: list[str]) -> dict[str, str]:
    return {n: ws.show(root / n) for n in names if (root / n).exists()}


# ============================================================================ overview
@guarded("overview")
def overview(ws: Workspace, registry: ExtensionRegistry | None = None) -> StageResult:
    """Stage order, extension status and recent runs."""
    registry = registry or get_registry()
    runs_root = ws.root / "runs"
    recent: list[str] = []
    if runs_root.is_dir():
        dirs = [p for p in runs_root.glob("*/*") if p.is_dir()]
        dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        recent = [ws.show(p) for p in dirs[:8]]
    return StageResult(
        "overview",
        "ok",
        "流程：需求 → crow_research → crow_propose（含红队）→ select_tactic（人工）→ falcon_draft → "
        "falcon_check / falcon_fix → confirmation_status / apply_confirmations（人工）→ ir_build → "
        "simulate → platform_export（AFSIM，扩展）",
        data={
            "extensions": registry.status(),
            "generation_model": generation_model_name(),
            "recent_runs": recent,
        },
        problems=registry.check(),
    )


# ============================================================================ Crow
@guarded("crow_research")
async def crow_research(
    ws: Workspace,
    requirement: str,
    *,
    run_dir: str | None = None,
    max_sources: int | None = None,
    gap_rounds: int | None = None,
    pipeline=None,
    settings=None,
) -> StageResult:
    """C1–C3: mission profile, research plan, retrieval, evidence and the Falcon gate."""
    from afsim_research_agent.config import Settings
    from afsim_research_agent.crow.cli import prepare_mission_profile, run_research_with_result

    if not requirement.strip():
        return StageResult("crow_research", "blocked", "需求为空")
    settings = settings or Settings.from_environment()
    run_dir = run_dir or f"runs/crow-research/{datetime.now():%Y%m%d-%H%M%S}"
    out = ws.new_dir(run_dir)
    out.mkdir(parents=True)
    profile = None
    if pipeline is None:  # tests inject a pipeline and skip the model-backed profile
        profile = prepare_mission_profile(
            requirement, settings=settings, output_dir=out, interactive=False
        )
    (md, js), result = await run_research_with_result(
        requirement,
        output_dir=out,
        max_web_results=3,
        max_total_sources=max_sources,
        gap_rounds=gap_rounds,
        pipeline=pipeline,
        mission_profile=profile,
    )
    col = result.collection
    readiness = result.readiness
    failed = [c.name for c in readiness.checks if not c.passed] if readiness else []
    ready = bool(readiness and readiness.ready)
    return StageResult(
        "crow_research",
        "ok" if ready else "failed",
        f"候选来源 {len(col.candidates)}，采用 {len(col.sources)}；Falcon 输入门槛"
        + ("通过" if ready else f"未通过（{', '.join(failed) or '无门槛结果'}）"),
        artifacts={"run_dir": ws.show(out), "report": ws.show(md), "result": ws.show(js)},
        next_steps=["crow_propose"]
        if ready
        else ["检查报告中的缺口，必要时调整需求或增加检索轮数"],
    )


@guarded("crow_propose")
async def crow_propose(
    ws: Workspace,
    run_dir: str,
    *,
    out_dir: str | None = None,
    redteam: bool = True,
    model=None,
    red_model=None,
    settings=None,
) -> StageResult:
    """C4–C5: candidate tactics with capability annotation, red-team review and response."""
    from afsim_research_agent.config import Settings
    from afsim_research_agent.crow.propose import run_proposal

    settings = settings or Settings.from_environment()
    src = ws.existing(run_dir, kind="Crow 运行目录")
    target = ws.new_dir(out_dir or ws.show(src / f"tactics_agent_{datetime.now():%H%M%S}"))
    run = await run_proposal(
        src,
        settings=settings,
        out_dir=target,
        model=model,
        redteam=redteam,
        red_model=red_model,
    )
    rows = []
    for card in run.result.candidates:
        level = card.feasibility.level if card.feasibility else "?"
        rows.append(
            {
                "id": card.id,
                "name": card.name,
                "provenance": card.provenance,
                "score": round(getattr(card.score, "total", 0.0), 3),
                "feasibility": level,
                "review_flags": len(card.review_flags),
            }
        )
    return StageResult(
        "crow_propose",
        "ok" if rows else "failed",
        f"候选战术 {len(rows)} 个" + ("（含红队评审）" if redteam else ""),
        artifacts={"tactics_dir": ws.show(target), **{k: ws.show(v) for k, v in run.paths.items()}},
        data={"candidates": rows},
        next_steps=["把候选和红队意见交给用户，调用 select_tactic 记录用户的选择"],
    )


@guarded("select_tactic")
def select_tactic(
    ws: Workspace,
    tactics_dir: str,
    choices: list[str],
    *,
    rejected: list[str] | None = None,
    note: str = "",
    by: str,
) -> StageResult:
    """Record the human's tactic choice (tactic_selection.yaml). Must be approved by a person."""
    from afsim_research_agent.crow.annotate import load_result
    from afsim_research_agent.crow.select import SELECTION_FILE, SelectionError, build_selection

    root = ws.existing(tactics_dir, kind="战术目录")
    path = root / SELECTION_FILE
    if path.exists():
        return StageResult("select_tactic", "blocked", f"{ws.show(path)} 已存在，不覆盖")
    try:
        doc, warnings = build_selection(
            load_result(root), choices, rejected=list(rejected or []), note=note, by=by
        )
    except SelectionError as exc:
        return StageResult("select_tactic", "blocked", str(exc))
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return StageResult(
        "select_tactic",
        "ok",
        f"已选择 {', '.join(s['candidate'] for s in doc['selected'])}",
        artifacts={"selection": ws.show(path)},
        problems=warnings,
        next_steps=["falcon_draft"],
    )


# ============================================================================ Falcon
@guarded("falcon_draft")
async def falcon_draft(
    ws: Workspace,
    run_dir: str,
    model_id: str,
    *,
    tactics_dir: str | None = None,
    requirement: str = "",
    platforms: list[dict[str, Any]] | None = None,
    terrain_classes: list[str] | None = None,
    user_parameters: dict[str, Any] | None = None,
    with_l3: bool = False,
    model=None,
) -> StageResult:
    """Write a falcon request next to the Crow run and generate the tactic sheet package."""
    from afsim_research_agent.falcon.pipeline import run_draft

    base = ws.existing(run_dir, kind="Crow 运行目录")
    request: dict[str, Any] = {"model_id": model_id, "generator": "v2"}
    if tactics_dir:
        request["tactic_selection"] = Path(ws.existing(tactics_dir)).relative_to(base).as_posix()
    else:
        request["crow_result"] = "research_result.json"
    if requirement:
        request["requirement"] = requirement
    if platforms:
        request["platforms"] = platforms
    if terrain_classes:
        request["terrain_classes"] = terrain_classes
    if user_parameters:
        request["user_parameters"] = user_parameters
    ws.new_dir(ws.show(base / "models" / model_id))  # refuse before calling the model
    req_path = base / f"falcon_request_{model_id}.yaml"
    if req_path.exists():  # an earlier failed attempt: keep it, write a new one beside it
        req_path = base / f"falcon_request_{model_id}_{datetime.now():%H%M%S}.yaml"
    req_path = ws.writable_file(ws.show(req_path))
    req_path.write_text(yaml.safe_dump(request, allow_unicode=True, sort_keys=False), "utf-8")
    result = await run_draft(req_path, model or _default_model(), with_l3=with_l3)
    pkg = result.package_dir
    return StageResult(
        "falcon_draft",
        "ok" if result.status == "draft_valid" else "failed",
        f"状态 {result.status}；待确认 {result.pending} 项",
        artifacts={"package": ws.show(pkg), "request": ws.show(req_path)}
        | _files(ws, pkg, [SHEET_FILE, "falcon_review.md", "confirmations.yaml"]),
        problems=result.problems,
        next_steps=["falcon_fix"]
        if result.status != "draft_valid"
        else ["simulate", "confirmation_status"],
        data={"generation_model": getattr(model, "name", None) or generation_model_name()},
    )


def _afsim_from_env():
    """AFSIM in the drafting loop when the local settings name a binding and run configs
    (FALCON_AFSIM_BINDING, FALCON_AFSIM_RUNS separated by ';', AFSIM_MISSION). The paths
    stay on the server side and never appear in a tool result."""
    import os

    from afsim_research_agent.falcon.v3.afsim_loop import AfsimConfig

    binding = os.environ.get("FALCON_AFSIM_BINDING")
    runs = [r for r in (os.environ.get("FALCON_AFSIM_RUNS") or "").split(";") if r.strip()]
    kit = os.environ.get("FALCON_AFSIM_KIT")
    if not binding or not (runs or kit):
        return None
    return AfsimConfig(
        binding=Path(binding),
        runs=[Path(r.strip()) for r in runs],
        mission=os.environ.get("AFSIM_MISSION") or "mission",
        kit=Path(kit) if kit else None,
    )


@guarded("falcon_draft3")
async def falcon_draft3(
    ws: Workspace,
    tactics_dir: str,
    model_id: str,
    *,
    role_map: dict[str, str] | None = None,
    requirement: str = "",
    candidates: int = 3,
    afsim: bool = False,
    missing_capabilities: str = "skip",
    model=None,
) -> StageResult:
    """Crow → Falcon v3: write a v3 request naming the user's tactic selection next to the
    Crow run, then draft (tactic sheet v3 → IR 2.0; AFSIM in the loop when configured)."""
    from afsim_research_agent.falcon.v3.crow_bridge import make_request
    from afsim_research_agent.falcon.v3.draft import run_draft3

    tactics = ws.existing(tactics_dir, kind="Crow 战术目录")
    if not (tactics / "tactic_selection.yaml").is_file():
        return StageResult(
            "falcon_draft3", "blocked", "战术目录中没有 tactic_selection.yaml：先 select_tactic"
        )
    base = tactics.parent if tactics.name == "tactics" else tactics
    ws.new_dir(ws.show(base / "models" / model_id))  # refuse before calling the model
    req_path = base / f"falcon3_{model_id}.yaml"
    if req_path.exists():
        req_path = base / f"falcon3_{model_id}_{datetime.now():%H%M%S}.yaml"
    req_path = ws.writable_file(ws.show(req_path))
    doc = make_request(
        tactics,
        model_id,
        requirement=requirement,
        role_map=role_map or {},
        candidates=candidates,
        base=req_path.parent,
        missing_capabilities=missing_capabilities,
    )
    req_path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), "utf-8")
    cfg = _afsim_from_env() if afsim else None
    problems = []
    if afsim and cfg is None:
        problems.append("未配置 AFSIM（FALCON_AFSIM_BINDING / FALCON_AFSIM_RUNS），只做二维检查")
    result = await run_draft3(
        req_path, model or _default_model(), out_dir=base / "models", afsim=cfg
    )
    pkg = result.package_dir
    check = result.check
    problems += [p.render() for p in (check.problems or [])][:8]
    problems += [f"{s['id']} 未通过" for s in (check.l3 or []) if not s.get("passed")][:4]
    return StageResult(
        "falcon_draft3",
        "ok" if result.status == "draft_valid" else "failed",
        f"状态 {result.status}；L1 {'通过' if check.l1_ok else '未通过'}；"
        f"L2 {sum(t['passed'] for t in check.l2)}/{len(check.l2)}；"
        f"L3 {sum(s['passed'] for s in check.l3)}/{len(check.l3)}"
        + ("；AFSIM 已在循环中运行" if cfg else ""),
        artifacts={"package": ws.show(pkg), "request": ws.show(req_path)}
        | _files(
            ws,
            pkg,
            [
                "tactic_sheet.md",
                "falcon_review.md",
                "behavior_spec.yaml",
                "selected_tactics.yaml",
                "afsim_rounds.md",
            ],
        ),
        problems=problems,
        next_steps=["读 falcon_review.md，把结果和未通过项告诉用户"]
        if result.status != "draft_valid"
        else ["把模型包和审阅结果告诉用户"],
        data={"generation_model": getattr(model, "name", None) or generation_model_name()},
    )


@guarded("falcon_check")
def falcon_check(ws: Workspace, package: str, *, with_l3: bool = False) -> StageResult:
    """Run L1/L2/probes/properties (+ L3) on a package without changing it."""
    from afsim_research_agent.falcon.repair import check_package

    pkg = _package(ws, package)
    check = check_package(pkg, [], with_l3=with_l3)
    l2 = check.l2 or {}
    data: dict[str, Any] = {"score": list(check.score), "loaded": check.loaded}
    if l2:
        tests = l2.get("tests") or []
        data["l2"] = {
            "passed": l2.get("passed"),
            "tests": len(tests),
            "failed": [t["id"] for t in tests if not t.get("passed")][:20],
        }
    if check.l3 is not None:
        data["l3"] = [
            {
                "id": s["id"],
                "passed": s["passed"],
                "failed": [e["id"] for e in s["expectations"] if not e["passed"]],
            }
            for s in check.l3
        ]
    l3_ok = check.l3 is None or all(s["passed"] for s in check.l3)
    return StageResult(
        "falcon_check",
        "ok" if check.ok and l3_ok else "failed",
        f"{len(check.problems)} 个问题"
        + ("" if check.l3 is None else f"；L3 {'通过' if l3_ok else '未通过'}"),
        problems=[p.render() for p in check.problems],
        data=data,
        next_steps=[] if check.ok else ["falcon_fix"],
    )


@guarded("falcon_compile")
def falcon_compile(ws: Workspace, package: str) -> StageResult:
    """Recompile tactic_sheet.md → behavior_spec.yaml (no model call)."""
    from afsim_research_agent.falcon.sheet.recompile import recompile_package

    pkg = _package(ws, package)
    report = recompile_package(pkg)
    return StageResult(
        "falcon_compile",
        "ok" if report.status == "draft_valid" else "failed",
        f"状态 {report.status}；待确认 {report.pending} 项",
        problems=report.problems,
        artifacts=_files(ws, pkg, ["behavior_spec.yaml", "falcon_review.md"]),
    )


@guarded("falcon_fix")
async def falcon_fix(
    ws: Workspace,
    package: str,
    *,
    out_dir: str | None = None,
    max_steps: int = 30,
    with_l3: bool = False,
    properties: bool = True,
    model=None,
) -> StageResult:
    """The self-checking repair loop on a copy of the package."""
    from afsim_research_agent.falcon.llm import ModelRunner
    from afsim_research_agent.falcon.sheet.fixer import fix_package

    pkg = _package(ws, package)
    target = ws.new_dir(out_dir or ws.show(pkg.with_name(pkg.name + "_fix")))
    fixed = await fix_package(
        pkg,
        ModelRunner(model or _default_model()),
        out_dir=target,
        max_steps=max_steps,
        with_l3=with_l3,
        properties=properties,
    )
    status = (
        "ok" if fixed.status == "draft_valid" else ("needs_human" if fixed.question else "failed")
    )
    return StageResult(
        "falcon_fix",
        status,
        f"状态 {fixed.status}；{len(fixed.steps)} 步，{len(fixed.edits)} 处修改",
        artifacts={"package": ws.show(fixed.package_dir)}
        | _files(ws, fixed.package_dir, ["fix_log.json", "falcon_review.md"]),
        problems=fixed.problems,
        question=fixed.question,
        data={"metrics": fixed.metrics} if fixed.metrics else {},
        next_steps=["ask_human"]
        if fixed.question
        else ([] if status == "ok" else ["查看 fix_log.json"]),
    )


@guarded("falcon_properties")
async def falcon_properties(
    ws: Workspace, package: str, *, force: bool = False, model=None
) -> StageResult:
    """Write requirement properties independently of the sheet and check the sheet."""
    from afsim_research_agent.falcon.llm import ModelRunner
    from afsim_research_agent.falcon.sheet.fixer import write_properties

    pkg = _package(ws, package)
    lines, failures = await write_properties(
        pkg, ModelRunner(model or _default_model()), force=force
    )
    return StageResult(
        "falcon_properties",
        "ok" if not failures else "failed",
        "；".join(lines[:3]),
        problems=failures,
        artifacts=_files(ws, pkg, ["requirement_properties.yaml"]),
    )


@guarded("defer_tests")
def defer_tests(
    ws: Workspace, package: str, test_ids: list[str], reason: str, *, decision: str = ""
) -> StageResult:
    """Mark single-step tests as deferred (human decision): kept in the sheet, left out of checks.

    The transitions they name stay counted as covered, so the draft can compile, run L2 on
    the remaining tests and run the 2D scenarios while the question stays open."""
    import re

    from afsim_research_agent.falcon.sheet.recompile import recompile_package

    pkg = _package(ws, package)
    sheet = pkg / SHEET_FILE
    text = sheet.read_text(encoding="utf-8")
    m = re.search(r"```yaml verification\n(.*?)\n```", text, re.S)
    if m is None:
        return StageResult("defer_tests", "blocked", "战术稿没有 verification 块")
    block = m.group(1)
    data = yaml.safe_load(block) or {}
    tests = {str(x.get("id")): x for x in data.get("tests") or [] if isinstance(x, dict)}
    missing = [i for i in test_ids if i not in tests]
    if missing or not test_ids:
        return StageResult(
            "defer_tests",
            "blocked",
            f"找不到测试：{', '.join(missing) or '（未给测试 ID）'}",
            data={"tests": sorted(tests)[:60]},
        )
    done, already = [], []
    for tid in test_ids:
        if tests[tid].get("deferred"):
            already.append(tid)
            continue
        pattern = re.compile(rf"^([ \t]*)- id:[ \t]*[\"']?{re.escape(tid)}[\"']?[ \t]*$", re.M)
        hit = pattern.search(block)
        if hit is None:
            return StageResult(
                "defer_tests", "blocked", f"测试 {tid} 不是 `- id: …` 单独一行的写法，无法自动标记"
            )
        pad = hit.group(1) + "  "
        insert = (
            f"\n{pad}deferred: true\n{pad}deferred_reason: {json.dumps(reason, ensure_ascii=False)}"
        )
        block = block[: hit.end()] + insert + block[hit.end() :]
        done.append(tid)
    if done:
        sheet.write_text(text[: m.start(1)] + block + text[m.end(1) :], encoding="utf-8")
        record_decision(ws, ws.show(pkg), f"暂缓测试 {', '.join(done)}", decision or reason)
    report = recompile_package(pkg)
    return StageResult(
        "defer_tests",
        "ok" if report.status == "draft_valid" else "failed",
        f"已暂缓 {len(done)} 个测试"
        + (f"（{len(already)} 个原已暂缓）" if already else "")
        + f"；重新编译：{report.status}",
        problems=[p for p in report.problems if "deferred" not in p],
        artifacts=_files(ws, pkg, [SHEET_FILE, "decisions.yaml", "falcon_review.md"]),
        data={"deferred": done + already},
        next_steps=["simulate"] if report.status == "draft_valid" else ["falcon_fix"],
    )


@guarded("confirmation_status")
def confirmation_status(ws: Workspace, package: str) -> StageResult:
    from afsim_research_agent.falcon.confirm import package_status

    pkg = _package(ws, package)
    st = package_status(pkg)
    return StageResult(
        "confirmation_status",
        "ok" if st["ready_to_build"] else "needs_human",
        "待确认 {pending}，已确认 {confirmed}，已驳回 {rejected}".format(**st["counts"]),
        problems=list(st["blocking"]),
        artifacts=_files(ws, pkg, ["confirmations.yaml"]),
        next_steps=["ir_build"]
        if st["ready_to_build"]
        else ["请用户在 confirmations.yaml 中逐项决定，再调用 apply_confirmations"],
    )


@guarded("apply_confirmations")
def apply_confirmations(ws: Workspace, package: str, *, by: str) -> StageResult:
    """Write the decisions in confirmations.yaml back into the spec. Must be approved by a person."""
    from afsim_research_agent.falcon.confirm import apply_confirmations as apply

    pkg = _package(ws, package)
    report = apply(pkg, by)
    return StageResult("apply_confirmations", "ok", report.render()[:400])


# ============================================================================ IR / simulation
@guarded("ir_build")
def ir_build(
    ws: Workspace,
    package: str,
    *,
    backends: list[str] | None = None,
    draft: bool = False,
    skip_l3: bool = False,
) -> StageResult:
    """Normalize, test and export backends (Mermaid, BT.CPP XML, python_sim) + capability check."""
    from afsim_research_agent.ir.binding import BACKENDS
    from afsim_research_agent.ir.pipeline import build_package

    pkg = _package(ws, package)
    result = build_package(
        pkg, backends=tuple(backends or BACKENDS), draft=draft, run_l3=not skip_l3
    )
    errors = [d for d in result.diagnostics if getattr(d.severity, "value", d.severity) == "error"]
    data: dict[str, Any] = {"written": len(result.written)}
    if result.capability is not None:
        data["capability"] = {
            "level": result.capability.level,
            "gaps": len(result.capability.gaps),
            "unresolved": len(result.capability.unresolved),
        }
    return StageResult(
        "ir_build",
        "ok" if result.ok else "failed",
        f"状态 {result.status}；写出 {len(result.written)} 个文件",
        problems=[f"{d.code} {d.path}: {d.message}" for d in errors],
        artifacts=_files(ws, pkg, ["review.md", "normalized_ir.json", "capability_gaps.yaml"]),
        data=data,
    )


@guarded("simulate")
def simulate(ws: Workspace, package: str, *, scenario: str | None = None) -> StageResult:
    """Run the 2D scenarios (L3) on the current draft and summarize each run."""
    from afsim_research_agent.falcon.repair import _run_l3, check_package

    pkg = _package(ws, package)
    check = check_package(pkg, [], with_l3=False)
    if check.ir is None:
        return StageResult(
            "simulate", "blocked", "稿件不能规范化为 IR，先用 falcon_check / falcon_fix"
        )
    runs = _run_l3(check.ir, pkg) or []
    if scenario:
        runs = [r for r in runs if r["id"] == scenario]
    if not runs:
        return StageResult("simulate", "blocked", "没有可运行的想定（或绑定失败）")
    rows = []
    for r in runs:
        failed = [
            f"{e['id']}: {e.get('detail') or ''} (measured={e.get('measured')})"
            for e in r["expectations"]
            if not e["passed"]
        ]
        rows.append(
            {
                "id": r["id"],
                "passed": r["passed"],
                "failed": failed,
                "final_states": r.get("final_states"),
            }
        )
    ok = all(r["passed"] for r in rows)
    return StageResult(
        "simulate",
        "ok" if ok else "failed",
        f"{sum(r['passed'] for r in rows)}/{len(rows)} 个想定通过",
        data={"scenarios": rows},
        problems=[] if ok else ["静态检查通过但想定失败时，先查是否有状态来回切换（PROBE002）"],
    )


@guarded("capability_gaps")
def capability_gaps(ws: Workspace, package: str) -> StageResult:
    pkg = _package(ws, package)
    path = pkg / "capability_gaps.yaml"
    if not path.is_file():
        return StageResult(
            "capability_gaps", "blocked", "模型包中没有 capability_gaps.yaml（先 ir_build）"
        )
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    gaps = doc.get("gaps") or []
    rows = [
        {k: g.get(k) for k in ("id", "display_name", "gap_type", "status") if g.get(k) is not None}
        for g in gaps
    ]
    return StageResult(
        "capability_gaps",
        "ok",
        f"能力缺口 {len(rows)} 项",
        data={"gaps": rows[:30]},
        artifacts={"gaps": ws.show(path)},
    )


# ============================================================================ human decisions
@guarded("record_decision")
def record_decision(
    ws: Workspace, directory: str, question: str, answer: str, *, options: list[str] | None = None
) -> StageResult:
    """Append a human answer to decisions.yaml in a run or package directory."""
    root = ws.existing(directory)
    if root.relative_to(ws.root).parts[:1] != ("runs",):
        return StageResult("record_decision", "blocked", "只能写 runs/ 下的目录")
    path = root / DECISIONS_FILE
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None
    doc = doc or {"format": "Decisions", "decisions": []}
    doc["decisions"].append(
        {
            "question": question,
            "options": list(options or []),
            "answer": answer,
            "time": datetime.now().isoformat(timespec="seconds"),
        }
    )
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return StageResult(
        "record_decision", "ok", f"已记录：{answer[:80]}", artifacts={"decisions": ws.show(path)}
    )


# ============================================================================ extensions
def _not_configured(stage: str, slot: str, plan: str) -> StageResult:
    return StageResult(
        stage,
        "not_configured",
        f"{slot} 尚未接入（{plan}）",
        next_steps=["告知用户该能力尚未接入，按现有流程继续"],
    )


@guarded("list_actions")
def list_actions(
    ws: Workspace, *, platform_class: str | None = None, registry: ExtensionRegistry | None = None
) -> StageResult:
    lib = (registry or get_registry()).action_library
    if lib is None:
        return _not_configured("list_actions", "动作集 action_library", "开发中")
    items = [a.public() for a in lib.actions(platform_class)]
    return StageResult(
        "list_actions",
        "ok",
        f"{lib.name} {lib.version}：{len(items)} 个动作",
        data={"actions": items[:80]},
    )


@guarded("find_tactics")
def find_tactics(
    ws: Workspace, query: str, *, limit: int = 5, registry: ExtensionRegistry | None = None
) -> StageResult:
    lib = (registry or get_registry()).tactic_library
    if lib is None:
        return _not_configured("find_tactics", "战术集 tactic_library", "开发中")
    hits = [t.public() for t in lib.match(query, limit=limit)]
    return StageResult(
        "find_tactics",
        "ok" if hits else "failed",
        f"匹配 {len(hits)} 个战术模板",
        data={"tactics": hits},
    )


@guarded("plan_format")
def plan_format(ws: Workspace, *, registry: ExtensionRegistry | None = None) -> StageResult:
    comp = (registry or get_registry()).plan_compiler
    if comp is None:
        return _not_configured("plan_format", "行为树生成器 plan_compiler", "开发中")
    return StageResult(
        "plan_format",
        "ok",
        f"{comp.name}：高层描述格式 {comp.plan_format}",
        data={"guide": comp.format_guide()},
    )


@guarded("compile_plan")
def compile_plan(
    ws: Workspace, plan: str, out_dir: str, *, registry: ExtensionRegistry | None = None
) -> StageResult:
    """High-level description (written by the model) → behavior trees, deterministically."""
    reg = registry or get_registry()
    comp = reg.plan_compiler
    if comp is None:
        return _not_configured("compile_plan", "行为树生成器 plan_compiler", "开发中")
    problems = comp.validate(plan, actions=reg.action_library)
    if problems:
        return StageResult(
            "compile_plan", "failed", f"高层描述有 {len(problems)} 处问题", problems=problems
        )
    target = ws.new_dir(out_dir)
    target.mkdir(parents=True)
    (target / f"plan.{comp.plan_format}").write_text(plan, encoding="utf-8")
    out = comp.compile(plan, out_dir=target, actions=reg.action_library)
    return StageResult(
        "compile_plan",
        "ok" if out.ok else "failed",
        f"生成 {len(out.files)} 个文件",
        problems=out.problems,
        artifacts={p.name: ws.show(p) for p in out.files[:20]},
    )


@guarded("platform_export")
def platform_export(
    ws: Workspace,
    package: str,
    out_dir: str,
    *,
    backend: str = "afsim",
    run: bool = True,
    timeout_s: float = 600,
    registry: ExtensionRegistry | None = None,
) -> StageResult:
    """Map a model package onto a simulation platform (template) and optionally run it."""
    impl = (registry or get_registry()).platform_backends.get(backend)
    if impl is None:
        return _not_configured("platform_export", f"平台后端 {backend}", "AFSIM 模板映射开发中")
    pkg = _package(ws, package)
    target = ws.new_dir(out_dir)
    target.mkdir(parents=True)
    exported = impl.export(pkg, target)
    if not exported.ok or not run:
        return StageResult(
            "platform_export",
            "ok" if exported.ok else "failed",
            f"导出 {len(exported.files)} 个文件" + ("" if run else "（未运行）"),
            problems=exported.problems,
            artifacts={p.name: ws.show(p) for p in exported.files[:20]},
        )
    result = impl.run(target, timeout_s=timeout_s)
    return StageResult(
        "platform_export",
        "ok" if result.ok else "failed",
        f"{backend} 运行{'通过' if result.ok else '未通过'}",
        problems=result.problems,
        artifacts={"log": ws.show(result.log)} if result.log else {},
        data={"metrics": result.metrics} if result.metrics else {},
    )


@guarded("draft_capability_plugin")
def draft_capability_plugin(
    ws: Workspace,
    package: str,
    gap_id: str,
    out_dir: str,
    *,
    registry: ExtensionRegistry | None = None,
) -> StageResult:
    builder = (registry or get_registry()).plugin_builder
    if builder is None:
        return _not_configured("draft_capability_plugin", "能力插件开发 plugin_builder", "规划中")
    pkg = _package(ws, package)
    doc = yaml.safe_load((pkg / "capability_gaps.yaml").read_text(encoding="utf-8")) or {}
    gap = next((g for g in doc.get("gaps") or [] if g.get("id") == gap_id), None)
    if gap is None:
        return StageResult("draft_capability_plugin", "blocked", f"没有缺口 {gap_id}")
    target = ws.new_dir(out_dir)
    target.mkdir(parents=True)
    draft = builder.draft(gap, target)
    verify = builder.verify(target) if draft.ok else None
    ok = draft.ok and verify is not None and verify.ok
    return StageResult(
        "draft_capability_plugin",
        "needs_human" if ok else "failed",
        "插件草稿已通过验证，交人审核" if ok else "插件草稿未通过",
        problems=draft.problems + (verify.problems if verify else []),
        artifacts={p.name: ws.show(p) for p in draft.files[:20]},
        question="是否采纳该插件草稿？" if ok else None,
    )


def to_json(result: StageResult) -> str:  # convenience for scripts
    return json.dumps(result.to_dict(), ensure_ascii=False)
