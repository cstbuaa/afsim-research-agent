# ruff: noqa: F811 - pytest fixtures imported from test_falcon_v3
"""Workbench stages (scenario, 2D runs, AFSIM run) and the replay API."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from test_falcon_v3 import SHEET, _fix_drafter, contracts, set_root  # noqa: F401 - fixtures

from afsim_research_agent.ir.v2.contracts import Contracts
from afsim_research_agent.orchestrator import stages3 as st3
from afsim_research_agent.orchestrator import workbench
from afsim_research_agent.orchestrator.workspace import Workspace


@pytest.fixture
def ws(tmp_path: Path, contracts: Contracts, set_root: Path, monkeypatch) -> Workspace:
    monkeypatch.setenv("CROW_CAPABILITY_CATALOG", str(set_root))
    monkeypatch.delenv("FALCON_AFSIM_KIT", raising=False)
    monkeypatch.delenv("FALCON_AFSIM_BINDING", raising=False)
    ws = Workspace((tmp_path / "data").resolve())
    pkg = ws.root / "runs" / "r1" / "models" / "scout_demo"
    pkg.mkdir(parents=True)
    spec = _fix_drafter(contracts, SHEET).check(verify=False).compiled.spec
    (pkg / "behavior_spec.yaml").write_text(yaml.safe_dump(spec, allow_unicode=True), "utf-8")
    return ws


PKG = "/runs/r1/models/scout_demo"


def test_scenario_is_shown_and_changed_without_touching_the_package(ws: Workspace) -> None:
    before = (ws.root / PKG.lstrip("/") / "behavior_spec.yaml").read_bytes()
    shown = st3.scenario_show(ws, PKG)
    assert shown.status == "ok", shown.summary
    bad = st3.scenario_set(ws, PKG, enemy_count=99)
    assert bad.status == "blocked" and "超出范围" in bad.problems[0]
    made = st3.scenario_set(
        ws, PKG, name="far7k", end_time_s=240, enemy_range_m=7000, enemy_approach_speed_mps=10
    )
    assert made.status == "ok", made.summary
    path = made.artifacts["scenario"]
    assert path == "/runs/r1/scenarios/scout_demo_far7k.yaml"
    doc = yaml.safe_load((ws.root / path.lstrip("/")).read_text(encoding="utf-8"))
    assert doc["overrides"]["enemy"] == {"range_m": 7000.0, "approach_speed_mps": 10.0}
    assert st3.scenario_set(ws, PKG, name="far7k").status == "blocked"  # never overwritten
    shown = st3.scenario_show(ws, PKG, scenario=path)
    assert shown.data["敌方起始距离_m"] == 7000 and shown.data["运行时长_s"] == 240
    assert (ws.root / PKG.lstrip("/") / "behavior_spec.yaml").read_bytes() == before


def test_2d_runs_write_replays_the_workbench_serves(ws: Workspace) -> None:
    sc = st3.scenario_set(
        ws, PKG, name="far", end_time_s=60, enemy_range_m=3000, enemy_approach_speed_mps=10
    )
    res = st3.simulate2d(ws, PKG, scenario=sc.artifacts["scenario"])
    assert res.status in ("ok", "failed") and "scenario_baseline" in res.data["replays"]
    base = ws.root / "runs"
    listed = workbench.list_replays([base])
    assert listed and all(r["source"] == "2d" for r in listed)
    rid = next(r["id"] for r in listed if r["id"].endswith("scenario_baseline.replay.json"))
    replay = workbench.build_replay(workbench._resolve(rid, [base]))
    assert replay["format"] == "replay/1" and replay["source"] == "2d"
    hostile = next(v for k, v in replay["samples"].items() if k.startswith("e"))
    assert hostile[-1][1] < hostile[0][1] - 100  # the enemy drove towards the own start
    with pytest.raises(ValueError):
        workbench._resolve("0:../../etc/passwd", [base])
    with pytest.raises(ValueError):
        workbench._resolve("0:r1/models/scout_demo/behavior_spec.yaml", [base])
    json.dumps(replay)  # serialisable


def test_afsim_run_needs_the_local_configuration(ws: Workspace) -> None:
    res = st3.afsim_run(ws, PKG)
    assert res.status == "not_configured"


def test_server_info_names_the_orchestrator_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCHESTRATOR_MODEL", "qwen-test-model")
    info = workbench.server_info()
    assert info["orchestrator_model"] == "qwen-test-model"
    assert info["pid"] > 0 and info["started"] > 0
