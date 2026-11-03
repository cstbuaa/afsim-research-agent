"""Executable version of a Crow tactic: reduced to the existing actions and subtrees, the
full tactic kept, the left-out parts listed (falcon.v3.simplify)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import yaml

from afsim_research_agent.crow.proposal_models import (
    ExceptionItem,
    GeometryItem,
    RoleAssignment,
    Step,
    TacticCard,
)
from afsim_research_agent.falcon.llm import ModelRunner, ScriptedModel
from afsim_research_agent.falcon.tactic import SelectedPart, SelectedTactic
from afsim_research_agent.falcon.v3 import crow_bridge as B
from afsim_research_agent.falcon.v3 import simplify as S


def _tactic() -> SelectedTactic:
    card = TacticCard(
        id="tc_01",
        name="侧坡隐蔽观察",
        narrative="前出后建立有线通信，静默观察，暴露时释放烟幕撤离。",
        summary="隐蔽观察",
        roles=[RoleAssignment(role="观察车")],
        geometry=[GeometryItem(formation="侧坡隐蔽位", evidence=["ev_1"])],
        steps=[
            Step(phase="渗透", do="沿道路低速前出", until="到达观察位置", evidence=["ev_2"]),
            Step(phase="构筑", do="铺设有线通信并伪装", until="通信就绪", basis="inferred"),
            Step(phase="观察", do="雷达与光学设备静默监视前方", until="任务结束"),
        ],
        exceptions=[ExceptionItem(situation="遭敌火力", then="释放烟幕撤离")],
    )
    return SelectedTactic(
        directory=Path("tactics"),
        selection_path=Path("tactics/tactic_selection.yaml"),
        doc={"selected": [{"candidate": "tc_01"}]},
        result=SimpleNamespace(requirement="前出建立观察哨"),
        parts=[SelectedPart(card=card, phases=None)],
    )


def _contracts():
    actions = {
        a: SimpleNamespace(slice=a.split(".")[0])
        for a in ("movement.follow_route", "movement.stop", "perception.start_search")
    }
    tactics = {
        "perception.search_for_target": SimpleNamespace(
            section="subtrees",
            id="perception.search_for_target",
            raw={"description": "搜索"},
            parameters={},
            slice="perception",
        )
    }
    return SimpleNamespace(actions=actions, tactics=tactics)


GOOD = {
    "summary": "沿道路前出到观察位置停车，雷达搜索监视前方，未经命令不开火。",
    "steps": [
        {
            "phase": "前出",
            "do": "沿道路低速前出",
            "until": "路线走完",
            "uses": ["movement.follow_route"],
            "derived_from": ["tc_01_s1"],
        },
        {
            "phase": "观察",
            "do": "停车后雷达搜索监视前方",
            "until": "任务结束",
            "uses": ["movement.stop", "perception.search_for_target"],
            "derived_from": ["tc_01_s3"],
        },
    ],
    "deferred": [
        {"element": "tc_01_s2", "what": "铺设有线通信并伪装", "missing": "有线通信、伪装"},
        {"element": "tc_01_s3", "what": "光学设备监视", "missing": "光学设备"},
    ],
    "kept_constraints": ["未经命令不得开火"],
}


def test_elements_and_checks() -> None:
    t = _tactic()
    assert list(S.full_elements(t)) == ["tc_01_s1", "tc_01_s2", "tc_01_s3", "tc_01_g1", "tc_01_x1"]
    bad = S.SimplifyAnswer.model_validate(
        {
            "summary": "x",
            "steps": [
                {"phase": "a", "do": "b", "uses": ["smoke.launch"], "derived_from": ["tc_09_s1"]}
            ],
            "deferred": [{"element": "tc_01_s7", "what": "?"}],
        }
    )
    problems = S.check(bad, _contracts(), S.full_elements(t))
    assert any("smoke.launch" in p for p in problems)
    assert any("没有写用到的动作" in p for p in problems)
    assert any("tc_09_s1" in p for p in problems) and any("tc_01_s7" in p for p in problems)
    assert S.check(S.SimplifyAnswer.model_validate(GOOD), _contracts(), S.full_elements(t)) == []


def test_executable_card_keeps_ids_evidence_and_lists_what_is_left_out() -> None:
    out = S.apply(_tactic(), S.SimplifyAnswer.model_validate(GOOD))
    card = out.tactic.parts[0].card
    assert card.id == "tc_01" and card.name.endswith("（可执行版）")
    assert [s.phase for s in card.steps] == ["前出", "观察"] and card.exceptions == []
    assert card.steps[0].evidence == ["ev_2"] and card.steps[0].basis == "evidence"
    assert [n.maps_to for n in card.capability_needs] == [
        ["movement.follow_route"],
        ["movement.stop", "perception.search_for_target"],
    ]
    assert card.feasibility.level == "ready"
    # the full tactic is untouched; elements neither carried nor listed are deferred too
    assert len(out.full.parts[0].card.steps) == 3
    assert {d["element"] for d in out.deferred} == {"tc_01_s2", "tc_01_s3", "tc_01_g1", "tc_01_x1"}
    assert "未说明原因" in next(d["missing"] for d in out.deferred if d["element"] == "tc_01_x1")
    # nothing in the executable version is missing for the capability check
    entries = B.capability_map(out.tactic, _contracts())
    assert entries and not any(e["missing"] for e in entries)
    assert [c["id"] for c in B.tactic_clauses(out.tactic)] == ["tc_01_s1", "tc_01_s2"]


def test_a_wrong_answer_is_corrected_once() -> None:
    wrong = {**GOOD, "steps": [{**GOOD["steps"][0], "uses": ["movement.drive_fast"]}]}
    model = ScriptedModel({S.STAGE: [wrong, GOOD]})
    out = asyncio.run(
        S.simplify(ModelRunner(model), _tactic(), _contracts(), "前出建立观察哨", "- x")
    )
    assert len(model.calls) == 2 and "movement.drive_fast" in model.calls[1][1]
    assert [s.phase for s in out.tactic.parts[0].card.steps] == ["前出", "观察"]


def test_package_keeps_the_full_tactic_and_the_deferred_parts(tmp_path: Path) -> None:
    out = S.apply(_tactic(), S.SimplifyAnswer.model_validate(GOOD))
    B.write_crow_files(tmp_path, out.tactic, [], "block", {}, out)
    full = yaml.safe_load((tmp_path / "full_tactic.yaml").read_text(encoding="utf-8"))
    assert len(full["candidates"][0]["card"]["steps"]) == 3
    deferred = yaml.safe_load((tmp_path / B.DEFERRED_FILE).read_text(encoding="utf-8"))
    elements = deferred["executable_version"]["deferred_elements"]
    assert {
        "element": "tc_01_s2",
        "what": "铺设有线通信并伪装",
        "missing": "有线通信、伪装",
    } in elements
    review = "\n".join(B.review_section(out.tactic, [], "block", {}, out))
    assert "可执行版与完整战术" in review and "tc_01_s2" in review and "未经命令不得开火" in review
