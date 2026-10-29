"""P4: Falcon drafts from the tactic selected in Crow C5 (tactic_selection.yaml)."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest
import yaml
from falcon_fixtures import REQUIREMENT, crow_result, ev, gold_script, skeleton_answer

from afsim_research_agent.crow.mission import MissionDraft, normalize_profile, write_profile
from afsim_research_agent.crow.proposal_models import (
    CounterTactic,
    ExceptionItem,
    GeometryItem,
    OverallJudgment,
    ProposalResult,
    RedTeamReview,
    RoleAssignment,
    Step,
    TacticCard,
)
from afsim_research_agent.crow.proposal_report import write_proposal
from afsim_research_agent.crow.select import build_selection
from afsim_research_agent.crow.selection_report import REVIEW_FILE, write_review
from afsim_research_agent.falcon.errors import FalconError
from afsim_research_agent.falcon.gate import run_gate
from afsim_research_agent.falcon.llm import ScriptedModel
from afsim_research_agent.falcon.models import EvidencePackage
from afsim_research_agent.falcon.pipeline import run_draft
from afsim_research_agent.falcon.tactic import CROW_GAPS_FILE, SELECTED_FILE, load_selection
from afsim_research_agent.ir.pipeline import build_package

URBAN = "ir:terrain_value.class"
SEND = "ir:send_message"
REFERENCE = "参考值（第 2 版，无依据，可调整）：跃进距离 300 m"


@pytest.fixture(scope="module")
def evidence(tmp_path_factory) -> Path:
    """A Crow evidence package (built once with the scripted F1)."""
    from falcon_fixtures import write_request

    tmp = tmp_path_factory.mktemp("f1")
    request = write_request(tmp, crow_result())
    result = asyncio.run(run_draft(request, ScriptedModel(gold_script())))
    return result.package_dir / "evidence_package.json"


def _cards() -> list[TacticCard]:
    tc1 = TacticCard(
        id="tc_01",
        name="楔形转纵队",
        summary="开阔地楔形，近城区转纵队",
        narrative="排在开阔地以楔形行进，接近城区前转为纵队通过。" * 3,
        roles=[RoleAssignment(role="排长车"), RoleAssignment(role="僚车", members=["2号", "3号"])],
        geometry=[
            GeometryItem(formation="楔形", when="开阔地", evidence=[ev("wedge", 0)]),
            GeometryItem(formation="纵队", when="城区", spacing="50 米", evidence=["ev_unknown"]),
        ],
        steps=[
            Step(phase="行进", do="以楔形沿路线行进", until="接近城区", evidence=[ev("wedge", 0)]),
            Step(phase="转换", do="转为纵队", until="进入城区", evidence=[ev("column", 0)]),
        ],
        exceptions=[ExceptionItem(situation="通信中断", then="按最后队形继续", basis="inferred")],
        assumptions=[REFERENCE, "排内通信良好"],
        revision=2,
    )
    tc2 = TacticCard(
        id="tc_02",
        name="交替跃进",
        summary="接敌后交替跃进",
        narrative="接敌后先报告，再由两组交替跃进，与敌保持距离。" * 3,
        geometry=[GeometryItem(formation="横队")],
        steps=[
            Step(phase="行进", do="纵队行进", until="接敌"),
            Step(phase="接敌", do="报告并交替跃进", until="脱离接触", evidence=[ev("contact", 0)]),
        ],
        exceptions=[ExceptionItem(situation="掩护组受压制", then="停止跃进")],
    )
    tc3 = TacticCard(id="tc_03", name="其他", summary="x", narrative="其他战术。" * 10)
    return [tc1, tc2, tc3]


def _gaps() -> dict:
    def gap(gid, contract, candidates):
        return {
            "id": gid,
            "contract": contract,
            "display_name": contract,
            "gap_type": "confirm",
            "used_by": [{"candidate": c, "sources": ["steps[0].until"]} for c in candidates],
        }

    return {
        "granularity": {"granularity": "coarse", "refine": ["comms"]},
        "gaps": [
            gap("cap-gap-urban", URBAN, ["tc_01", "tc_03"]),
            gap("cap-gap-send", SEND, ["tc_02"]),
            gap("cap-gap-only3", "只有三号需要的能力", ["tc_03"]),
        ],
    }


@pytest.fixture
def tactics(tmp_path: Path, evidence: Path) -> Path:
    """data/runs/<run>/{research_result.json, evidence_package.json, tactics/...}"""
    run = tmp_path / "run"
    tactics = run / "tactics"
    tactics.mkdir(parents=True)
    (run / "research_result.json").write_text(crow_result().model_dump_json(), encoding="utf-8")
    shutil.copy(evidence, run / "evidence_package.json")
    package = EvidencePackage.model_validate_json(evidence.read_text(encoding="utf-8"))
    result = ProposalResult(
        requirement=REQUIREMENT,
        evidence_package="data/runs/elsewhere/evidence_package.json",  # resolved by file name
        max_candidates=5,
        max_innovative=2,
        candidates=_cards(),
    )
    write_proposal(result, package, tactics, None)
    (tactics / "capability_gaps.yaml").write_text(
        yaml.safe_dump(_gaps(), allow_unicode=True), encoding="utf-8"
    )
    review = RedTeamReview(
        candidate="tc_01",
        revision_reviewed=1,
        revision_after=2,
        counter_tactics=[CounterTactic(id="ct_01", description="在城区入口设伏")],
        overall=OverallJudgment(soundness="medium", main_weakness="城区入口暴露"),
    )
    write_review([review], tactics / REVIEW_FILE, red_model="red", proposer_model="blue")
    draft = MissionDraft.model_validate(
        {
            "domain": "ground",
            "echelon": "platoon",
            "mission": {"type": "formation_transition", "objective": "x"},
            "own_forces": [
                {"role_hint": "排长车", "count": 1, "platform_class": "ground_vehicle"},
                {"role_hint": "僚车", "count": 3, "platform_class": "ground_vehicle"},
            ],
            "user_specified": [],
        }
    )
    write_profile(normalize_profile(draft, REQUIREMENT), tactics / "mission_profile.yaml")
    _select(tactics, ["tc_01", "tc_02:接敌"], rejected=["tc_03"])
    return tactics


def _select(tactics: Path, choices, rejected=(), note="城区入口增加侦察") -> None:
    from afsim_research_agent.crow.annotate import load_result

    doc, _ = build_selection(
        load_result(tactics), list(choices), rejected=list(rejected), note=note, by="u"
    )
    (tactics / "tactic_selection.yaml").write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def _request(tmp_path: Path, **fields) -> Path:
    data = {"model_id": "armor_tactic", "tactic_selection": "run/tactics", **fields}
    path = tmp_path / "falcon_request.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def _script() -> dict:
    script = gold_script()

    def no_f1(prompt, schema, index):
        raise AssertionError("the Crow evidence package must be reused")

    def skeleton(prompt, schema, index):
        answer = skeleton_answer()
        tactic_ids = ["tc_01_s1", "tc_01_s2", "tc_01_g1", "tc_01_g2", "tc_01_x1", "tc_02_s2"]
        answer["roles"][0]["implements"] += tactic_ids
        return answer

    script.update({"evidence.extract": no_f1, "evidence.group": no_f1, "skeleton": skeleton})
    return script


# ---------------------------------------------------------------------- loading
def test_selection_becomes_clauses_with_card_evidence(tactics: Path) -> None:
    tactic = load_selection(tactics)
    assert tactic.candidates == ["tc_01", "tc_02"]
    ids = {ev("wedge", 0), ev("column", 0), ev("contact", 0)}
    clauses = {c.id: c for c in tactic.clauses(ids)}
    # tc_01 whole: steps, formations, exceptions; tc_02 only the 接敌 phase
    assert list(clauses) == [
        "tc_01_s1",
        "tc_01_s2",
        "tc_01_g1",
        "tc_01_g2",
        "tc_01_x1",
        "tc_02_s2",
    ]
    assert clauses["tc_01_s1"].text == "【行进】以楔形沿路线行进，直到接近城区"
    assert (
        clauses["tc_01_s1"].evidence == [ev("wedge", 0)] and clauses["tc_01_s1"].kind == "behavior"
    )
    assert clauses["tc_01_g2"].evidence == []  # unknown evidence id dropped
    assert clauses["tc_01_x1"].tactic_basis == "inferred"
    assert all(c.origin == "tactic" for c in clauses.values())
    assert any("1 处证据引用" in n for n in tactic.notes)
    brief = tactic.brief()
    assert brief["parts"][1]["taken"] == "仅阶段 接敌" and len(brief["parts"][1]["steps"]) == 1
    assert "combination" in brief and brief["user_notes"] == "城区入口增加侦察"
    assert tactic.requirement("").endswith("用户选择备注：城区入口增加侦察")
    assert [p.role_hint for p in tactic.platforms()] == ["排长车", "僚车"]
    assert tactic.refine() == ["comms"]
    gaps = {g["id"] for g in tactic.crow_gaps()}
    assert gaps == {"cap-gap-urban", "cap-gap-send"}  # tc_03 only gap left out


def test_stale_or_wrong_selection_is_refused(tmp_path: Path, tactics: Path) -> None:
    sel = tactics / "tactic_selection.yaml"
    doc = yaml.safe_load(sel.read_text(encoding="utf-8"))
    doc["selected"][0]["revision"] = 1
    sel.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    with pytest.raises(FalconError, match="does not match") as err:
        run_gate(_request(tmp_path))
    assert "第 1 版" in " ".join(err.value.details) and err.value.code == "FAL003"
    doc["selected"] = [{"candidate": "tc_02", "revision": 1, "phases": ["撤离"]}]
    sel.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    with pytest.raises(FalconError) as err:
        run_gate(_request(tmp_path))
    assert "没有阶段 撤离" in " ".join(err.value.details)
    sel.unlink()
    with pytest.raises(FalconError, match="cannot read tactic selection"):
        run_gate(_request(tmp_path))
    # without a selection the requirement and Crow result are required
    path = tmp_path / "plain.yaml"
    path.write_text(yaml.safe_dump({"model_id": "x"}), encoding="utf-8")
    with pytest.raises(FalconError, match="invalid"):
        run_gate(path)
    assert not (tmp_path / "models").exists()


# ---------------------------------------------------------------------- end to end
def test_draft_implements_the_selected_tactic(
    tmp_path: Path, tactics: Path, capability_set
) -> None:
    model = ScriptedModel(_script())
    result = asyncio.run(run_draft(_request(tmp_path), model))
    pkg = result.package_dir
    assert result.status == "draft_valid", result.problems

    trace = json.loads((pkg / "falcon_trace.json").read_text(encoding="utf-8"))
    origins = {c["id"]: c["origin"] for c in trace["clauses"]}
    assert origins["rq_01"] == "requirement" and origins["tc_02_s2"] == "tactic"
    assert trace["uncovered_clauses"] == []
    assert trace["tactic"]["parts"][0]["revision"] == 2
    assert trace["tactic"]["counter_tactics"] == ["tc_01/ct_01: 在城区入口设伏"]

    # every generation stage sees the tactic; the requirement carries the selection note
    stages = {stage: prompt for stage, prompt in model.calls}
    for stage in ("skeleton", "section:parameters", "section:control"):
        assert "选定战术" in stages[stage] and "tc_01_s1" in stages[stage]
    assert "用户选择备注：城区入口增加侦察" in stages["requirements"]
    assert ev("wedge", 0) in stages["skeleton"]  # card evidence offered as basis

    request = yaml.safe_load((pkg / "falcon_request.yaml").read_text(encoding="utf-8"))
    assert "requirement" not in request or not request["requirement"]  # the user's file as given
    selected = yaml.safe_load((pkg / SELECTED_FILE).read_text(encoding="utf-8"))
    assert [c["card"]["id"] for c in selected["candidates"]] == ["tc_01", "tc_02"]
    assert selected["candidates"][0]["counter_tactics"][0]["id"] == "ct_01"

    crow = yaml.safe_load((pkg / CROW_GAPS_FILE).read_text(encoding="utf-8"))
    assert crow["source"] == "tactics#tc_01+tc_02"
    assert {g["id"] for g in crow["gaps"]} == {"cap-gap-urban", "cap-gap-send"}
    gaps = yaml.safe_load((pkg / "capability_gaps.yaml").read_text(encoding="utf-8"))
    assert gaps["crow_source"] == "tactics#tc_01+tc_02"
    assert {g["id"] for g in gaps["crow_estimate"]} == {"cap-gap-urban", "cap-gap-send"}
    assert trace["capability_constraints"]["refine"] == ["comms"]  # Crow's refinement kept

    review = (pkg / "falcon_review.md").read_text(encoding="utf-8")
    assert "## 0b. 选定战术（Crow C5）" in review
    assert "| tc_02 | 交替跃进 | 第 1 版 | 接敌 |" in review
    assert REFERENCE in review and "在城区入口设伏" in review
    assert "| tc_01_s1 | behavior | 【行进】以楔形沿路线行进，直到接近城区 |" in review
    assert "复用 Crow 证据包" in review

    # ir build keeps the comparison with Crow's estimate without extra flags
    build_package(pkg, draft=True)
    report = json.loads((pkg / "validation_report.json").read_text(encoding="utf-8"))
    assert report["capability"]["crow_source"] == "tactics#tc_01+tc_02"


def test_user_requirement_and_platforms_take_precedence(
    tmp_path: Path, tactics: Path, capability_set
) -> None:
    _select(tactics, ["tc_01"], note="")
    platforms = [{"role_hint": "leader", "capability_class": "ground_vehicle", "count": 1}]
    path = _request(
        tmp_path,
        requirement=REQUIREMENT,
        crow_result="run/research_result.json",
        platforms=platforms,
        capability_refine=[],
    )
    gate, _ = run_gate(path)
    tactic = gate.tactic
    assert tactic.requirement(gate.request.requirement) == REQUIREMENT
    model = ScriptedModel(_script())
    result = asyncio.run(run_draft(path, model))
    trace = json.loads((result.package_dir / "falcon_trace.json").read_text(encoding="utf-8"))
    assert "tc_02_s2" not in {c["id"] for c in trace["clauses"]}
    requirements_prompt = next(p for s, p in model.calls if s == "requirements")
    assert '"role_hint":"leader"' in requirements_prompt and "排长车" not in requirements_prompt
    assert trace["capability_constraints"]["refine"] == []
