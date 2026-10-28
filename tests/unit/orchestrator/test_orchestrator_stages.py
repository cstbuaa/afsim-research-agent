"""Orchestrator stages and extension registry (no Deep Agents needed)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
import yaml

from afsim_research_agent.falcon.llm import ScriptedModel
from afsim_research_agent.orchestrator import stages as st
from afsim_research_agent.orchestrator.extensions import (
    ActionSpec,
    CatalogActionLibrary,
    CompileOutput,
    ExtensionRegistry,
    PlatformRun,
    TacticSpec,
)
from afsim_research_agent.orchestrator.tools import TOOL_SPECS, approval_tools
from afsim_research_agent.orchestrator.workspace import Workspace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "falcon"))  # test_falcon_sheet


@pytest.fixture
def ws(tmp_path) -> Workspace:
    root = tmp_path / "data"
    (root / "runs").mkdir(parents=True)
    return Workspace(root.resolve())


def _drafted(ws: Workspace) -> str:
    from falcon_fixtures import REQUIREMENT, crow_result
    from test_falcon_sheet import _v2_script

    run = ws.root / "runs" / "r1"
    run.mkdir()
    (run / "research_result.json").write_text(crow_result().model_dump_json(), encoding="utf-8")
    result = asyncio.run(
        st.falcon_draft(
            ws,
            "runs/r1",
            "armor_platoon_v2",
            requirement=REQUIREMENT,
            platforms=[
                {"role_hint": "platoon_leader", "capability_class": "ground_vehicle", "count": 1},
                {"role_hint": "wingman", "capability_class": "ground_vehicle", "count": 3},
            ],
            terrain_classes=["open", "urban"],
            model=ScriptedModel(_v2_script()),
        )
    )
    assert result.status == "ok", result.render()
    assert result.artifacts["package"] == "/runs/r1/models/armor_platoon_v2"
    assert "tactic_sheet.md" in result.artifacts
    return result.artifacts["package"]


# ------------------------------------------------------------------ workspace
def test_workspace_refuses_escapes_local_and_overwrites(ws: Workspace) -> None:
    for bad in ("../x", "runs/_local/a", "/_local"):
        with pytest.raises(ValueError):
            ws.resolve(bad)
    (ws.root / "runs" / "exists").mkdir()
    with pytest.raises(ValueError, match="已存在"):
        ws.new_dir("runs/exists")
    with pytest.raises(ValueError, match="runs/"):
        ws.new_dir("models/new")
    assert ws.resolve("/runs/a") == ws.root / "runs" / "a"
    assert ws.show(ws.root / "runs" / "a") == "/runs/a"


# ------------------------------------------------------------------ falcon stages
def test_draft_check_compile_simulate_and_status(ws: Workspace) -> None:
    pkg = _drafted(ws)
    check = st.falcon_check(ws, pkg, with_l3=True)
    assert check.status in ("ok", "failed")
    assert check.data["l2"]["tests"] > 0
    assert "l3" in check.data
    assert st.falcon_compile(ws, pkg).status in ("ok", "failed")
    sim = st.simulate(ws, pkg)
    assert sim.data["scenarios"] and {"id", "passed", "failed"} <= set(sim.data["scenarios"][0])
    status = st.confirmation_status(ws, pkg)
    assert status.status == "needs_human" and "待确认" in status.summary
    # every result renders as JSON with paths relative to the workspace
    for r in (check, sim, status):
        d = json.loads(r.render())
        assert str(ws.root) not in r.render() and d["stage"]


def test_draft_refuses_existing_package(ws: Workspace) -> None:
    _drafted(ws)
    again = asyncio.run(st.falcon_draft(ws, "runs/r1", "armor_platoon_v2", requirement="x"))
    assert again.status == "blocked"


def test_stage_errors_become_blocked_results(ws: Workspace) -> None:
    assert st.falcon_check(ws, "runs/missing").status == "blocked"
    assert st.falcon_check(ws, "../outside").status == "blocked"
    assert st.simulate(ws, "runs/_local/x").status == "blocked"


def test_record_decision_appends(ws: Workspace) -> None:
    (ws.root / "runs" / "r").mkdir()
    st.record_decision(ws, "runs/r", "受损时？", "撤出", options=["继续", "撤出"])
    st.record_decision(ws, "runs/r", "再问", "好")
    doc = yaml.safe_load((ws.root / "runs" / "r" / "decisions.yaml").read_text(encoding="utf-8"))
    assert [d["answer"] for d in doc["decisions"]] == ["撤出", "好"]


# ------------------------------------------------------------------ extensions
class _Actions:
    name, version = "test-actions", "0.1"

    def actions(self, platform_class=None):
        return [ActionSpec("bound", "跃进到下一位置", native={"afsim": "SECRET"})]

    def get(self, name):
        return self.actions()[0] if name == "bound" else None


class _Tactics:
    name, version = "test-tactics", "0.1"

    def tactics(self):
        return [TacticSpec("bounding_overwatch", "交替跃进", actions=["bound"])]

    def get(self, name):
        return self.tactics()[0]

    def match(self, query, *, limit=5):
        return self.tactics()[:limit]


class _Compiler:
    name, plan_format = "test-compiler", "yaml"

    def format_guide(self):
        return "steps: [...]"

    def validate(self, plan, *, actions=None):
        return [] if "steps" in plan else ["缺少 steps"]

    def compile(self, plan, *, out_dir: Path, actions=None):
        out = out_dir / "tree.xml"
        out.write_text("<root/>", encoding="utf-8")
        return CompileOutput(files=[out])


class _Afsim:
    name = "afsim"

    def export(self, package_dir, out_dir):
        f = out_dir / "scenario.txt"
        f.write_text("platform ...", encoding="utf-8")
        return CompileOutput(files=[f])

    def run(self, export_dir, *, timeout_s=600):
        log = export_dir / "run.log"
        log.write_text("ERROR line 3", encoding="utf-8")
        return PlatformRun(ok=False, problems=["line 3: unknown command"], log=log)


def test_unconfigured_extensions_say_so(ws: Workspace) -> None:
    reg = ExtensionRegistry()
    for result in (
        st.list_actions(ws, registry=reg),
        st.find_tactics(ws, "x", registry=reg),
        st.plan_format(ws, registry=reg),
        st.compile_plan(ws, "steps: []", "runs/p", registry=reg),
        st.platform_export(ws, "runs", "runs/e", registry=reg),
        st.draft_capability_plugin(ws, "runs", "g1", "runs/g", registry=reg),
    ):
        assert result.status == "not_configured", result.render()
    assert set(reg.status().values()) == {"未接入"}


def test_registered_extensions_are_used_and_native_names_hidden(ws: Workspace) -> None:
    reg = ExtensionRegistry(
        action_library=_Actions(), tactic_library=_Tactics(), plan_compiler=_Compiler()
    )
    reg.platform_backends["afsim"] = _Afsim()
    assert reg.check() == []
    actions = st.list_actions(ws, registry=reg)
    assert actions.ok and "SECRET" not in actions.render()
    assert (
        st.find_tactics(ws, "交替跃进", registry=reg).data["tactics"][0]["name"]
        == "bounding_overwatch"
    )
    assert st.compile_plan(ws, "oops", "runs/p0", registry=reg).status == "failed"
    compiled = st.compile_plan(ws, "steps: [bound]", "runs/p1", registry=reg)
    assert compiled.ok and (ws.root / "runs/p1/plan.yaml").is_file()
    (ws.root / "runs" / "pkg").mkdir()
    ran = st.platform_export(ws, "runs/pkg", "runs/e1", registry=reg)
    assert ran.status == "failed" and ran.problems == ["line 3: unknown command"]
    assert ran.artifacts["log"] == "/runs/e1/run.log"


def test_registry_reports_nonconforming_and_broken_hooks() -> None:
    reg = ExtensionRegistry(action_library=object())  # type: ignore[arg-type]
    reg.load(["no_such_module:setup"], entry_points=False)
    problems = reg.check()
    assert any("action_library" in p for p in problems)
    assert any("no_such_module" in p for p in problems)


def test_registry_loads_setup_hook(monkeypatch) -> None:
    import sys
    import types

    mod = types.ModuleType("fake_ext")
    mod.setup = lambda r: setattr(r, "tactic_library", _Tactics())
    monkeypatch.setitem(sys.modules, "fake_ext", mod)
    reg = ExtensionRegistry()
    reg.load(["fake_ext:setup"], entry_points=False)
    assert reg.tactic_library is not None and reg.loaded_from == ["fake_ext:setup"]


def test_catalog_adapter_implements_action_library() -> None:
    lib = CatalogActionLibrary()
    assert lib.actions() and lib.get(lib.actions()[0].name) is not None
    assert ExtensionRegistry(action_library=lib).check() == []


def test_tool_specs_are_consistent() -> None:
    names = [s.name for s in TOOL_SPECS]
    assert len(names) == len(set(names))
    assert {"select_tactic", "apply_confirmations", "platform_export"} <= set(approval_tools())
    for spec in TOOL_SPECS:
        params = __import__("inspect").signature(spec.stage).parameters
        assert list(params)[0] == "ws", spec.name
        unknown = set(spec.args.model_fields) - set(params)
        assert not unknown, f"{spec.name}: schema fields not accepted by the stage: {unknown}"
        assert spec.description


# ------------------------------------------------------------------ deferred tests (option D)
def _first_tests(pkg_dir: Path, n: int = 2) -> list[str]:
    import re as _re

    sheet = (pkg_dir / "tactic_sheet.md").read_text(encoding="utf-8")
    block = _re.search(r"```yaml verification\n(.*?)\n```", sheet, _re.S).group(1)
    return [str(t["id"]) for t in yaml.safe_load(block)["tests"][:n]]


def test_defer_tests_keeps_them_in_sheet_and_out_of_the_ir(ws: Workspace) -> None:
    pkg = _drafted(ws)
    pkg_dir = ws.resolve(pkg)
    before = len(yaml.safe_load((pkg_dir / "behavior_spec.yaml").read_text("utf-8"))["tests"])
    ids = _first_tests(pkg_dir, 1)
    result = st.defer_tests(ws, pkg, ids, "等待用户决定", decision="D")
    assert result.data["deferred"] == ids and result.status == "ok", result.render()
    sheet = (pkg_dir / "tactic_sheet.md").read_text(encoding="utf-8")
    assert f"- id: {ids[0]}\n" in sheet and "\n\n    deferred: true" not in sheet
    assert 'deferred_reason: "等待用户决定"' in sheet
    after = yaml.safe_load((pkg_dir / "behavior_spec.yaml").read_text("utf-8"))["tests"]
    assert len(after) == before - 1 and ids[0] not in {t["id"] for t in after}
    decisions = yaml.safe_load((pkg_dir / "decisions.yaml").read_text("utf-8"))["decisions"]
    assert decisions[-1]["answer"] == "D"
    # idempotent; unknown ids are refused
    assert st.defer_tests(ws, pkg, ids, "x").summary.startswith("已暂缓 0 个测试（1 个原已暂缓）")
    assert st.defer_tests(ws, pkg, ["no_such_test"], "x").status == "blocked"


def test_repair_loop_cannot_defer_tests(ws: Workspace) -> None:
    from afsim_research_agent.falcon.sheet.fixer import SheetFixer
    from afsim_research_agent.falcon.sheet.recompile import open_workspace

    pkg_dir = ws.resolve(_drafted(ws))
    tid = _first_tests(pkg_dir, 1)[0]
    fixer = SheetFixer(open_workspace(pkg_dir))
    block = fixer.block("verification")
    message = fixer.patch("verification", f"- id: {tid}\n", f"- id: {tid}\n    deferred: true\n")
    assert "deferred" in message and "拒绝" in message
    assert fixer.block("verification") == block
