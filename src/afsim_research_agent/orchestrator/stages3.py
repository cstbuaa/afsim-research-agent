"""Stages for the v3 flow after drafting: the scenario of a model package, 2D runs, AFSIM
runs and the replays the workbench shows (design ``agent-workbench-v1``).

The model and the scenario are two layers (``scenario-interface-binding-v1``): a model
package names the scenario's points, routes and events; the **scenario** (the platform
project's kit, ``FALCON_AFSIM_KIT``, plus an optional overrides file written by
``scenario_set``) gives them values: start position, enemy count / range / approach speed,
run length, event times. Changing the scenario never changes the model package.

Where things go (never overwriting):

- ``<runs dir>/scenarios/<package>_<name>.yaml`` — scenario overrides;
- ``<runs dir>/replays/<package>_2d_<time>/<scenario>.replay.json`` — 2D runs;
- ``<runs dir>/_local/afsim/<package>_<time>/`` — AFSIM runs: native input, console, the
  ``.aer`` recording. Under ``_local``: the agent cannot read them (native names), the
  workbench serves the recording to the local browser only.

Tool results carry what the sheet itself names (roles, states, actions, obligation ids) and
workspace paths; never native names, coordinates or local paths.
"""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from afsim_research_agent.orchestrator.results import StageResult
from afsim_research_agent.orchestrator.stages import _package_dir, guarded
from afsim_research_agent.orchestrator.workspace import Workspace, WorkspaceError
from afsim_research_agent.progress import report

# scenario overrides: the kit keys a user may change, with sane limits
LIMITS = {
    ("meta", "end_time_s"): (10.0, 3600.0),
    ("enemy", "count"): (0, 12),
    ("enemy", "range_m"): (0.0, 50_000.0),
    ("enemy", "spacing_m"): (0.0, 5_000.0),
    ("enemy", "approach_speed_mps"): (0.0, 40.0),
    ("enemy", "stop_range_m"): (0.0, 50_000.0),
    ("enemy", "approach_start_s"): (0.0, 3600.0),
}


# ============================================================================ helpers
def _kit_path() -> Path | None:
    kit = os.environ.get("FALCON_AFSIM_KIT")
    return Path(kit) if kit else None


def base_kit() -> dict[str, Any]:
    """The platform project's scenario kit (empty: binding defaults)."""
    path = _kit_path()
    if path is None or not path.is_file():
        return {}
    from afsim_research_agent.ir.v2.afsim.scenario import load_kit

    return load_kit(path)


def merge(kit: dict[str, Any], overrides: dict[str, Any] | None) -> dict[str, Any]:
    out = copy.deepcopy(kit)
    for section, values in (overrides or {}).items():
        if section == "points":
            out.setdefault("binding", {}).setdefault("points", {}).update(values or {})
        elif isinstance(values, dict):
            out.setdefault(section, {}).update(values)
    return out


def _runs_base(ws: Workspace, pkg: Path) -> Path:
    """Where a package's runs, scenarios and replays go: next to the ``models`` directory
    (``…/<run>/models/<pkg>`` → ``…/<run>``), or next to the package."""
    parent = pkg.parent
    base = parent.parent if parent.name == "models" else parent
    if ws.root / "runs" not in [base, *base.parents]:
        base = ws.root / "runs" / "workbench"
    return base


def _overrides(ws: Workspace, scenario: str | None) -> dict[str, Any]:
    if not scenario:
        return {}
    path = ws.existing(scenario, kind="想定文件")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("overrides") or {}


class Loaded:
    def __init__(self, pkg: Path) -> None:
        from afsim_research_agent.ir.v2.contracts import Contracts
        from afsim_research_agent.ir.v2.normalize import normalize
        from afsim_research_agent.ir.v2.scenario_interface import ScenarioInterface
        from afsim_research_agent.orchestrator.stages import package_format

        spec_file = pkg / "behavior_spec.yaml"
        if not spec_file.is_file():
            raise WorkspaceError("模型包里没有 behavior_spec.yaml（v3 模型包）")
        if package_format(pkg) == "v2":
            raise WorkspaceError(
                "这是旧流程（v2，IR 1.0）的模型包：v3 工具不能处理；"
                "用户要求旧流程时才用 falcon_check / simulate"
            )
        self.spec = yaml.safe_load(spec_file.read_text(encoding="utf-8")) or {}
        self.contracts = Contracts.load()
        res = normalize(self.spec, self.contracts)
        if not res.ok:
            raise WorkspaceError("behavior_spec.yaml 不能规范化为 IR 2.0：先用 falcon_draft3 修好")
        self.model = res.model
        self.iface = None
        doc_file = pkg / "scenario_interface.yaml"
        if doc_file.is_file():
            doc = yaml.safe_load(doc_file.read_text(encoding="utf-8")) or {}
            if doc.get("scenario_interface"):
                self.iface = ScenarioInterface.model_validate(doc["scenario_interface"])
        ob_file = pkg / "obligations.yaml"
        self.obligations = (
            (yaml.safe_load(ob_file.read_text(encoding="utf-8")) or {}).get("obligations") or []
            if ob_file.is_file()
            else []
        )

    def binding(self, kit: dict[str, Any]) -> Any:
        from afsim_research_agent.ir.v2.scenario_interface import binding_for

        return binding_for(self.model, kit, self.iface)


def _describe(binding: Any, kit: dict[str, Any]) -> dict[str, Any]:
    enemy = kit.get("enemy") or {}
    speed = float(enemy.get("approach_speed_mps", 0.0) or 0.0)
    return {
        "运行时长_s": binding.end_s,
        "敌方数量": len(binding.enemies_plane),
        "敌方起始距离_m": binding.enemy_range_m,
        "敌方开进速度_mps": speed,
        "敌方停止距离_m": binding.enemy_stop_m if speed > 0 else None,
        "敌方开始开进_s": binding.enemy_move_start_s if speed > 0 else None,
        "敌方停下_s": binding.enemy_move_stop_s if speed > 0 else None,
        "点": {k: list(v) for k, v in binding.points_plane.items()},
        "路线": {k: [list(p) for p in r.plane] for k, r in binding.routes.items()},
        "事件": dict(binding.events),
    }


# ============================================================================ scenario
@guarded("scenario_show")
def scenario_show(ws: Workspace, package: str, *, scenario: str | None = None) -> StageResult:
    """The scenario a package runs in: interface items and their bound values (east/north
    metres from the own start), enemy, run length, event times."""
    from afsim_research_agent.ir.v2.scenario_interface import summary

    pkg = _package_dir(ws, package)
    loaded = Loaded(pkg)
    kit = merge(base_kit(), _overrides(ws, scenario))
    binding = loaded.binding(kit)
    data = _describe(binding, kit)
    text = (
        f"运行 {binding.end_s:g} s；敌方 {data['敌方数量']} 辆，起始距离 {binding.enemy_range_m:g} m"
        + (f"，以 {data['敌方开进速度_mps']:g} m/s 开进" if data["敌方开进速度_mps"] else "，静止")
    )
    return StageResult(
        "scenario_show",
        "ok",
        text,
        problems=list(binding.defaults)[:12],
        data=data | {"说明": summary(binding)[:1500]},
    )


@guarded("scenario_set")
def scenario_set(
    ws: Workspace,
    package: str,
    *,
    name: str = "",
    end_time_s: float | None = None,
    enemy_count: int | None = None,
    enemy_range_m: float | None = None,
    enemy_spacing_m: float | None = None,
    enemy_approach_speed_mps: float | None = None,
    enemy_stop_range_m: float | None = None,
    enemy_approach_start_s: float | None = None,
    events: dict[str, float] | None = None,
    points: dict[str, list[float]] | None = None,
    base: str | None = None,
) -> StageResult:
    """Write a new scenario overrides file for a package (the package is not changed)."""
    pkg = _package_dir(ws, package)
    loaded = Loaded(pkg)
    overrides: dict[str, Any] = copy.deepcopy(_overrides(ws, base))
    given = {
        ("meta", "end_time_s"): end_time_s,
        ("enemy", "count"): enemy_count,
        ("enemy", "range_m"): enemy_range_m,
        ("enemy", "spacing_m"): enemy_spacing_m,
        ("enemy", "approach_speed_mps"): enemy_approach_speed_mps,
        ("enemy", "stop_range_m"): enemy_stop_range_m,
        ("enemy", "approach_start_s"): enemy_approach_start_s,
    }
    problems = []
    for (section, key), value in given.items():
        if value is None:
            continue
        lo, hi = LIMITS[(section, key)]
        if not lo <= float(value) <= hi:
            problems.append(f"{section}.{key}={value} 超出范围 [{lo:g}, {hi:g}]")
            continue
        overrides.setdefault(section, {})[key] = int(value) if key == "count" else float(value)
    model_events = set((loaded.model.get("events") or {}).keys())
    for ev, t in (events or {}).items():
        if ev not in model_events:
            problems.append(f"事件 {ev} 不是模型的事件：{sorted(model_events)}")
            continue
        overrides.setdefault("events", {})[ev] = float(t)
    known_points = set(loaded.iface.points) if loaded.iface is not None else set()
    for pid, xy in (points or {}).items():
        if pid not in known_points or len(xy) != 2:
            problems.append(f"点 {pid} 不是想定接口中的点（东、北两个数）：{sorted(known_points)}")
            continue
        overrides.setdefault("points", {})[pid] = [float(xy[0]), float(xy[1])]
    if problems:
        return StageResult("scenario_set", "blocked", "想定参数有误，未写入", problems=problems)
    label = "".join(
        ch for ch in (name or datetime.now().strftime("%m%d_%H%M%S")) if ch.isalnum() or ch in "_-"
    )
    target = _runs_base(ws, pkg) / "scenarios" / f"{pkg.name}_{label}.yaml"
    path = ws.writable_file(ws.show(target))
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "package": ws.show(pkg),
        "created": datetime.now().isoformat(timespec="seconds"),
        "overrides": overrides,
    }
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    binding = loaded.binding(merge(base_kit(), overrides))
    data = _describe(binding, merge(base_kit(), overrides))
    return StageResult(
        "scenario_set",
        "ok",
        f"已写入想定 {ws.show(path)}：运行 {binding.end_s:g} s，敌方起始 {binding.enemy_range_m:g} m",
        artifacts={"scenario": ws.show(path)},
        next_steps=["simulate2d 或 afsim_run 时传 scenario=<这个文件>"],
        data=data,
    )


# ============================================================================ 2D
@guarded("simulate2d")
def simulate2d(
    ws: Workspace, package: str, *, scenario: str | None = None, only: str | None = None
) -> StageResult:
    """Run the package's 2D scenarios plus one baseline run of the scenario itself; write
    a replay per run."""
    from afsim_research_agent.ir.v2.sim import simulate
    from afsim_research_agent.replay.build import from_sim

    pkg = _package_dir(ws, package)
    loaded = Loaded(pkg)
    kit = merge(base_kit(), _overrides(ws, scenario))
    binding = loaded.binding(kit)
    raws = [s for s in loaded.spec.get("scenarios") or [] if isinstance(s, dict)]
    raws.append({"id": "scenario_baseline", "environment": {"duration_s": binding.end_s}})
    if only:
        raws = [r for r in raws if r.get("id") == only]
    out_dir = ws.new_dir(
        ws.show(_runs_base(ws, pkg) / "replays" / f"{pkg.name}_2d_{datetime.now():%m%d_%H%M%S}")
    )
    out_dir.mkdir(parents=True)
    rows, replays = [], {}
    for raw in raws:
        sid = str(raw.get("id") or "scenario")
        report(f"二维仿真：{sid}")
        res = simulate(loaded.model, loaded.contracts, raw, binding)
        replay = from_sim(res, binding, title=f"二维 · {pkg.name} / {sid}")
        target = out_dir / f"{sid}.replay.json"
        target.write_text(json.dumps(replay, ensure_ascii=False), encoding="utf-8")
        replays[sid] = ws.show(target)
        rows.append(
            {
                "id": sid,
                "passed": res.passed,
                "error": res.error,
                "failed": [f"{c.id}: {c.detail}" for c in res.checks if not c.passed][:6],
                "states": {
                    k: " → ".join(f"{s}@{t:g}" for t, s in v[:12]) for k, v in res.states.items()
                },
            }
        )
    passed = sum(r["passed"] for r in rows)
    return StageResult(
        "simulate2d",
        "ok" if passed == len(rows) else "failed",
        f"二维 {passed}/{len(rows)} 通过；回放已生成，可在工作台仿真面板查看",
        artifacts={"replays": ws.show(out_dir)},
        problems=[
            f"{r['id']}：{'; '.join(r['failed']) or r['error']}" for r in rows if not r["passed"]
        ][:12],
        data={"runs": rows, "replays": replays},
    )


# ============================================================================ AFSIM
def _afsim_paths() -> tuple[Path, str] | None:
    binding = os.environ.get("FALCON_AFSIM_BINDING")
    if not binding:
        return None
    return Path(binding), os.environ.get("AFSIM_MISSION") or "mission"


@guarded("afsim_run")
def afsim_run(
    ws: Workspace,
    package: str,
    *,
    scenario: str | None = None,
    acceptance: str | None = None,
    timeout_s: float = 900.0,
) -> StageResult:
    """Run the package once in AFSIM in the given scenario (no repair, the package is not
    changed); judge the package's obligations and an optional acceptance file."""
    from afsim_research_agent.falcon.v3 import triage
    from afsim_research_agent.falcon.v3.afsim_loop import feedback
    from afsim_research_agent.ir.v2.afsim import Binding, CompileError, Compiler
    from afsim_research_agent.ir.v2.afsim.evaluate import run_package
    from afsim_research_agent.ir.v2.afsim.scenario import generate

    paths = _afsim_paths()
    if paths is None or _kit_path() is None:
        return StageResult(
            "afsim_run",
            "not_configured",
            "本地没有配置 AFSIM（FALCON_AFSIM_BINDING / FALCON_AFSIM_KIT）",
        )
    binding_path, mission = paths
    pkg = _package_dir(ws, package)
    loaded = Loaded(pkg)
    kit = merge(base_kit(), _overrides(ws, scenario))
    extra: list[dict[str, Any]] = []
    if acceptance:
        acc = ws.existing(acceptance, kind="验收标准文件")
        extra = (yaml.safe_load(acc.read_text(encoding="utf-8")) or {}).get("obligations") or []
    conditional = {o["id"] for o in extra if o.get("conditional")}
    plain = [{k: v for k, v in o.items() if k != "conditional"} for o in extra]
    # under _local: native input and output, readable by the workbench, not by the agent
    out = _runs_base(ws, pkg) / "_local" / "afsim" / f"{pkg.name}_{datetime.now():%m%d_%H%M%S}"
    if out.exists():
        return StageResult("afsim_run", "blocked", "输出目录已存在，稍后再试")
    out.parent.mkdir(parents=True, exist_ok=True)
    report("AFSIM：生成想定并编译模型")
    run = generate(loaded.model, kit, loaded.contracts)
    try:
        compiler = Compiler(loaded.model, loaded.contracts, Binding.load(binding_path))
        package_obj = compiler.package([run], obligations=loaded.obligations + plain)
        compiler.write(package_obj, out)
    except CompileError:
        return StageResult("afsim_run", "failed", "AFSIM 编译失败（细节只在本地记录）")
    report(f"AFSIM：运行中（仿真 {run.end_time_s:g} s）")
    results = run_package(out, mission, timeout_s)
    report("AFSIM：运行结束，判定结果")
    passed = bool(results) and all(r.passed for r in results)
    missions = {rid: role.get("mission") for rid, role in (loaded.model.get("roles") or {}).items()}
    findings = (
        []
        if passed
        else triage.from_afsim(
            results, [run], loaded.model, loaded.iface.optional_events() if loaded.iface else None
        )
    )
    text = "" if passed else feedback(results, [run], None, missions, None, findings)
    labels = {p.name: f"{p.role} #{p.instance + 1}" for p in run.platforms}
    states = {}
    acc_rows = []
    ids = {o["id"] for o in extra}
    for r in results or []:
        for name, seq in r.states.items():
            if name in labels:
                states[labels[name]] = " → ".join(f"{s}@{t:g}" for t, s in seq[:20])
        for v in r.obligations:
            if v.get("id") in ids:
                row = {
                    "id": v["id"],
                    "passed": bool(v.get("passed")),
                    "judged": v.get("judged", True),
                    "detail": v.get("detail"),
                }
                if not row["judged"] and row["id"] not in conditional:
                    row["passed"] = False  # an acceptance item whose premise never occurred
                acc_rows.append(row)
    accepted = bool(acc_rows) and all(
        r["passed"] or (not r["judged"] and r["id"] in conditional) for r in acc_rows
    )
    aer = sorted((out / "output").glob("*.aer"))
    summary = f"AFSIM {'通过' if passed else '未通过'}" + (
        f"；独立验收 {'通过' if accepted else '未通过'}" if acc_rows else ""
    )
    return StageResult(
        "afsim_run",
        "ok" if passed and (accepted or not acc_rows) else "failed",
        summary + ("；回放已生成，可在工作台仿真面板查看" if aer else ""),
        artifacts={"replay": ws.show(aer[0])} if aer else {},
        problems=[f.render() for f in findings][:8],
        data={
            "states": states,
            "feedback": text[:3000],
            "acceptance": acc_rows,
            "findings": [f.to_dict() for f in findings][:8],
        },
    )


# ============================================================================ branch scenarios
def _baseline(loaded: Loaded, kit: dict[str, Any]) -> dict[str, Any]:
    b = loaded.binding(kit)
    speed = next((r.speed_mps for r in b.routes.values()), None)
    return {
        "enemy_range_m": b.enemy_range_m,
        "sensor_range_m": b.sensor_range_m,
        "end_s": b.end_s,
        "route_speed_mps": speed or float((kit.get("meta") or {}).get("route_speed_mps", 8.0)),
    }


@guarded("scenario_plan")
def scenario_plan(ws: Workspace, package: str, *, scenario: str | None = None) -> StageResult:
    """The branches of a package a scenario has to provoke, and the questions to put to the
    user (with options and a recommended answer) before the variants are written."""
    from afsim_research_agent.orchestrator import scenario_plan as SP

    pkg = _package_dir(ws, package)
    loaded = Loaded(pkg)
    kit = merge(base_kit(), _overrides(ws, scenario))
    found = SP.branches(loaded.model)
    base = _baseline(loaded, kit)
    qs = SP.questions(found, base)
    if not found:
        return StageResult("scenario_plan", "ok", "模型没有需要专门想定触发的分支（只有主流程）")
    return StageResult(
        "scenario_plan",
        "ok",
        f"找到 {len(found)} 个分支；先用 ask_human 逐题问用户（给出选项，用户不答就用推荐值），"
        "再用 scenario_variants 生成想定变体",
        data={
            "branches": [{"id": b.id, "说明": b.text, "期望经过": b.path} for b in found],
            "questions": qs,
            "当前想定": base,
        },
        next_steps=[
            "ask_human（每题一次，选项照抄）",
            "scenario_variants answers={题目 id: 选项 value}",
        ],
    )


@guarded("scenario_variants")
def scenario_variants(
    ws: Workspace,
    package: str,
    *,
    answers: dict[str, Any] | None = None,
    phase_times: dict[str, float] | None = None,
    scenario: str | None = None,
    name: str = "branch",
) -> StageResult:
    """Write one scenario file per branch case from the user's answers (unanswered
    questions take the recommended value). The package is not changed."""
    from afsim_research_agent.orchestrator import scenario_plan as SP

    pkg = _package_dir(ws, package)
    loaded = Loaded(pkg)
    kit = merge(base_kit(), _overrides(ws, scenario))
    found = SP.branches(loaded.model)
    base = _baseline(loaded, kit)
    qs = SP.questions(found, base)
    given = SP.recommended(qs) | {k: v for k, v in (answers or {}).items() if v is not None}
    known = {b.id for b in found}
    bad = [b for b in given.get("branches") or [] if b not in known]
    if bad:
        return StageResult(
            "scenario_variants", "blocked", f"没有这些分支：{bad}；可选 {sorted(known)}"
        )
    rows = SP.variants(found, given, base, phase_times)
    label = "".join(ch for ch in name if ch.isalnum() or ch in "_-") or "branch"
    out_rows, files = [], {}
    for v in rows:
        overrides = copy.deepcopy(_overrides(ws, scenario))
        for section, values in v["overrides"].items():
            overrides.setdefault(section, {}).update(values)
        target = _runs_base(ws, pkg) / "scenarios" / f"{pkg.name}_{label}_{v['name']}.yaml"
        path = ws.writable_file(ws.show(target))
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "package": ws.show(pkg),
            "created": datetime.now().isoformat(timespec="seconds"),
            "branch": v["branch"],
            "expect": v["expect"],
            "answers": given,
            "note": v["note"],
            "overrides": overrides,
        }
        path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
        files[v["branch"]] = ws.show(path)
        out_rows.append({"分支": v["branch"], "想定": ws.show(path), "安排": v["note"]})
    return StageResult(
        "scenario_variants",
        "ok",
        f"已写入 {len(rows)} 个想定变体；用 afsim_branches 逐个运行并核对分支",
        artifacts={"scenarios": files},
        data={"variants": out_rows, "answers": given},
        next_steps=["afsim_branches scenarios=[上面的想定文件]"],
    )


@guarded("afsim_branches")
def afsim_branches(
    ws: Workspace, package: str, *, scenarios: list[str], timeout_s: float = 900.0
) -> StageResult:
    """Run the package in AFSIM once per branch scenario and judge whether the leader went
    through the branch (the state sequence the scenario file expects)."""
    from afsim_research_agent.orchestrator import scenario_plan as SP

    rows = []
    for sc in scenarios:
        path = ws.existing(sc, kind="想定文件")
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        expect = doc.get("expect") or {}
        report(f"AFSIM 分支想定：{doc.get('branch') or path.stem}")
        res = afsim_run(ws, package, scenario=sc, timeout_s=timeout_s)
        states = (res.data or {}).get("states") or {}
        label = f"{expect.get('role')} #1"
        seq = SP.parse_states(states.get(label, ""))
        wanted = list(expect.get("visits_in_order") or [])
        hit = bool(seq) and SP.path_met(seq, wanted)
        rows.append(
            {
                "分支": doc.get("branch"),
                "触发": hit,
                "运行通过": res.status == "ok",
                "期望经过": " → ".join(wanted),
                "实际": states.get(label, ""),
                "回放": (res.artifacts or {}).get("replay"),
                "问题": list(res.problems or [])[:4],
            }
        )
    hit = sum(r["触发"] for r in rows)
    ok = sum(r["触发"] and r["运行通过"] for r in rows)
    return StageResult(
        "afsim_branches",
        "ok" if ok == len(rows) else "failed",
        f"AFSIM 分支：{hit}/{len(rows)} 触发并按期望经过，{ok}/{len(rows)} 同时通过需求约束",
        data={"branches": rows},
    )
