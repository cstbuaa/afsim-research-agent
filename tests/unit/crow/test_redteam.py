"""C5: red-team review, proposer response, report and user selection (design §6.2–6.3)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from afsim_research_agent.capability.registry import GapRegistry
from afsim_research_agent.crow.annotate import load_package, load_result
from afsim_research_agent.crow.capability_annotation import CapabilityAnnotator
from afsim_research_agent.crow.mission import MissionDraft, normalize_profile
from afsim_research_agent.crow.proposal_models import (
    ExceptionItem,
    ProposalResult,
    RoleAssignment,
    Step,
    TacticCard,
)
from afsim_research_agent.crow.redteam import (
    RedTeam,
    independence_note,
    model_family,
    redteam_prompt,
    run_redteam,
    write_c5,
)
from afsim_research_agent.crow.select import SelectionError, build_selection
from afsim_research_agent.crow.select import main as select_main
from afsim_research_agent.crow.selection_report import load_reviews
from afsim_research_agent.falcon.llm import ModelRunner, ScriptedModel

NARRATIVE = (
    "装甲排在开阔地以楔形队形行进，排长车居前，三辆僚车在两翼后方保持间距。接近城区时排长下令转为纵队，"
    "车距五十米依次进入。发现敌情后立即停止前进并向连部报告，随后两组车辆交替跃进：一组静止掩护，"
    "另一组跃进到下一掩蔽位置，到位后角色互换，直至脱离威胁区域或到达目标。"
)
REVISED = NARRATIVE + "进入城区前由一辆僚车先行侦察城区入口，发现伏击迹象时全排改走预备路线。"


def profile():
    draft = MissionDraft.model_validate(
        {
            "domain": "ground",
            "echelon": "platoon",
            "mission": {"type": "formation_transition", "objective": "通过城区"},
            "own_forces": [
                {"role_hint": "排长车", "count": 1, "platform_class": "ground_vehicle"},
                {"role_hint": "僚车", "count": 3, "platform_class": "ground_vehicle"},
            ],
            "enemy": {"known": "敌反装甲小组", "assumed": ["可能在城区边缘设伏"]},
            "user_specified": [],
        }
    )
    return normalize_profile(draft, "装甲排通过城区")


def card(cid: str = "tc_01") -> TacticCard:
    return TacticCard(
        id=cid,
        name="楔形—纵队—交替跃进",
        narrative=NARRATIVE,
        summary="开阔地楔形，城区纵队，接敌交替跃进",
        roles=[RoleAssignment(role="排长车", members=["1 号车"], evidence=["ev_1"])],
        steps=[
            Step(phase="行进", do="楔形行进", until="接近城区", evidence=["ev_1"]),
            Step(phase="转换", do="转为纵队，车距 50 米", until="进入城区", evidence=["ev_2"]),
            Step(phase="接敌", do="两组交替跃进", until="脱离威胁区域", evidence=["ev_3"]),
        ],
        exceptions=[ExceptionItem(situation="通信中断", then="按预案继续", evidence=["ev_3"])],
        assumptions=["排内通信可靠"],
        risks=["城区入口无侦察"],
    )


def result_with(*cards: TacticCard) -> ProposalResult:
    return ProposalResult(
        requirement="装甲排通过城区", max_candidates=5, max_innovative=2, candidates=list(cards)
    )


RED = {
    "critical_assumptions": [
        {"assumption": "排内通信持续可用", "fragility": "high", "why": "敌方可能干扰"},
        {"assumption": "城区道路可通行", "fragility": "low", "why": ""},
    ],
    "counter_tactics": [
        {
            "id": "ct_01",
            "description": "反装甲小组在城区边缘建筑内伏击纵队先导车",
            "enemy_capabilities_needed": ["反坦克导弹"],
            "trigger": "纵队进入城区边缘",
            "expected_effect": "先导车损失",
            "severity": "high",
        }
    ],
    "failure_conditions": ["掩护组与跃进组间距过大，掩护火力覆盖不到"],
    "capability_doubts": [],
    "overall": {"soundness": "medium", "main_weakness": "城区入口缺少侦察"},
}

ANSWER = {
    "responses": [
        {"target": "ct_01", "decision": "revise", "reason": "进入城区前先行侦察"},
        {"target": "fc_1", "decision": "keep", "reason": "跃进距离已限制在掩护火力范围内"},
    ],
    "revisions": [
        {
            "op": "add_step",
            "index": 0,
            "phase": "侦察",
            "do": "一辆僚车先行侦察城区入口",
            "until": "确认无伏击或选定预备路线",
            "responds_to": ["ct_01"],
        },
        {
            "op": "add_exception",
            "situation": "发现伏击迹象",
            "then": "全排改走预备路线",
            "responds_to": ["ct_01"],
        },
    ],
    "narrative": REVISED,
}


def scripted(red=RED, answer=ANSWER) -> tuple[ScriptedModel, ScriptedModel]:
    return (
        ScriptedModel({"redteam": [red]}),
        ScriptedModel({"response": [answer]}),
    )


# ---------------------------------------------------------------------- independence and prompt
def test_model_family_and_independence() -> None:
    assert model_family("qwen3.8-flash") == "qwen" and model_family("deepseek-v4-pro") == "deepseek"
    assert independence_note("deepseek-v4-pro", "qwen3.8-flash") is None
    assert "同一系列" in independence_note("qwen-max", "qwen3.8-flash")


def test_red_team_sees_facts_not_arguments() -> None:
    prompt = redteam_prompt(card(), profile())
    assert "楔形行进" in prompt and "敌反装甲小组" in prompt and "steps[1]" in prompt
    # the proposer's narrative, assumptions and risks are not shown
    assert NARRATIVE[:20] not in prompt
    assert "排内通信可靠" not in prompt and "城区入口无侦察" not in prompt
    assert "ev_1" not in prompt


# ---------------------------------------------------------------------- review and response
def test_review_revises_the_candidate() -> None:
    red, prop = scripted()
    result = result_with(card())
    reviews = asyncio.run(RedTeam(ModelRunner(red), ModelRunner(prop)).review(result, profile()))
    review = reviews[0]
    revised = result.candidates[0]
    assert review.revision_reviewed == 1 and review.revision_after == 2
    assert revised.revision == 2 and revised.previous[0].revision == 1
    assert [s.phase for s in revised.steps] == ["行进", "侦察", "转换", "接敌"]
    assert revised.steps[1].basis == "inferred" and not revised.steps[1].evidence
    assert revised.exceptions[-1].situation == "发现伏击迹象"
    assert revised.narrative == REVISED and revised.support["steps"] == "partial"
    assert any("回应红方意见（ct_01）" in f for f in revised.review_flags)
    # every item to answer is answered: ct_01, fc_1 and the high-fragility assumption ca_1
    assert {r.target for r in review.responses} == {"ct_01", "fc_1"}
    assert "生成方未回应 ca_1" in review.notes
    # the response prompt asks about the high-fragility assumption only
    _, response_prompt_text = prop.calls[0]
    assert "ca_1" in response_prompt_text and "ca_2" not in response_prompt_text


def test_unusable_revisions_keep_the_original() -> None:
    bad = {**ANSWER, "revisions": [{"op": "modify_step", "index": 9, "do": "x"}]}
    red, prop = scripted(answer=bad)
    result = result_with(card())
    review = asyncio.run(RedTeam(ModelRunner(red), ModelRunner(prop)).review(result, profile()))[0]
    assert result.candidates[0].revision == 1 and review.revision_after == 1
    assert any("没有第 9 个步骤" in n for n in review.notes)
    # a revision that leaves the tactic incomplete is not used either
    short = {**ANSWER, "narrative": "太短"}
    red, prop = scripted(answer=short)
    result = result_with(card())
    review = asyncio.run(RedTeam(ModelRunner(red), ModelRunner(prop)).review(result, profile()))[0]
    assert result.candidates[0].revision == 1
    assert any("修订版不完整" in n for n in review.notes)


def test_red_team_failure_is_reported_and_others_continue() -> None:
    red = ScriptedModel({"redteam:tc_01": [RuntimeError("down")], "redteam:tc_02": [RED]})
    prop = ScriptedModel({"response": [{"responses": [], "revisions": []}]})
    result = result_with(card("tc_01"), card("tc_02"))
    reviews = asyncio.run(
        RedTeam(ModelRunner(red, retries=0), ModelRunner(prop)).review(result, profile())
    )
    assert "红方审查失败" in reviews[0].notes[0]
    assert reviews[1].counter_tactics[0].id == "ct_01"
    assert result.candidates[1].revision == 1  # nothing changed
    assert len(result.candidates) == 2  # C5 never removes candidates


def test_revised_candidates_are_annotated_again(capability_set) -> None:
    catalog = capability_set
    red, prop = scripted()
    cap = ScriptedModel(
        {
            "capability": [
                {
                    "needs": [
                        {
                            "need": "先行侦察城区入口",
                            "source": "steps[1].do",
                            "group": "maneuver",
                            "maps_to": ["movement.move_to_point"],
                        }
                    ]
                }
            ],
            "capability_review": [{"items": []}],
        }
    )
    annotator = CapabilityAnnotator(ModelRunner(cap), catalog)
    result = result_with(card())
    asyncio.run(
        RedTeam(ModelRunner(red), ModelRunner(prop), annotator=annotator).review(result, profile())
    )
    revised = result.candidates[0]
    assert revised.feasibility is not None and revised.feasibility.level == "ready"
    assert revised.capability_needs[0].need == "先行侦察城区入口"


# ---------------------------------------------------------------------- files, report, selection
def test_files_report_and_selection(tmp_path: Path) -> None:
    red, prop = scripted()
    result = result_with(card("tc_01"), card("tc_02"))
    reviews, notes = asyncio.run(
        run_redteam(
            result,
            profile(),
            red_runner=ModelRunner(red),
            proposer_runner=ModelRunner(prop),
            red_model="qwen-max",
            proposer_model="qwen3.8-flash",
        )
    )
    assert notes and "同一系列" in notes[0]
    paths = write_c5(
        result,
        reviews,
        load_package(None),
        tmp_path,
        red_model="qwen-max",
        proposer_model="qwen3.8-flash",
        notes=notes,
    )
    report = paths["report"].read_text(encoding="utf-8")
    assert "## tc_01 楔形—纵队—交替跃进（第 2 版）" in report
    assert "| ct_01 | 反装甲小组在城区边缘建筑内伏击纵队先导车" in report
    assert "已修改：进入城区前先行侦察" in report and "不改：跃进距离已限制" in report
    assert "**第 2 版的修改**" in report and "新增步骤（第 0 项）" in report
    assert "⚠ 红方模型 qwen-max 与生成模型" in report
    loaded, meta = load_reviews(paths["redteam"])
    assert loaded[0].counter_tactics[0].severity == "high" and meta["red_model"] == "qwen-max"
    assert load_result(tmp_path).candidates[0].revision == 2

    # selection: one candidate, phases of another; errors are explained
    doc, warnings = build_selection(
        load_result(tmp_path), ["tc_01", "tc_02:接敌"], rejected=[], note="组合", by="u"
    )
    assert doc["selected"] == [
        {"candidate": "tc_01", "revision": 2, "phases": "all"},
        {"candidate": "tc_02", "revision": 2, "phases": ["接敌"]},
    ]
    assert any("需要确认" in w for w in warnings)
    with pytest.raises(SelectionError, match="没有阶段"):
        build_selection(load_result(tmp_path), ["tc_01:冲锋"], rejected=[], note="", by="u")
    with pytest.raises(SelectionError, match="同时被选中和否决"):
        build_selection(load_result(tmp_path), ["tc_01"], rejected=["tc_01"], note="", by="u")


def test_select_command_writes_selection_and_queues_gaps(tmp_path: Path, capability_set) -> None:
    catalog = capability_set
    terrain = "城区判断"  # the lab has no terrain judgment
    cap = ScriptedModel(
        {
            "capability": [
                {
                    "needs": [
                        {
                            "need": "判断是否进入城区",
                            "source": "steps[1].until",
                            "group": "terrain",
                            "missing": terrain,
                        }
                    ]
                }
            ],
            "capability_review": [{"items": []}],
        }
    )
    result = result_with(card("tc_01"), card("tc_02"))
    asyncio.run(CapabilityAnnotator(ModelRunner(cap), catalog).annotate(result, profile()))
    from afsim_research_agent.crow.proposal_report import write_proposal

    write_proposal(result, load_package(None), tmp_path, None)
    registry = tmp_path / "registry.yaml"
    code = select_main(
        [
            str(tmp_path),
            "--candidate",
            "tc_01",
            "--reject",
            "tc_02",
            "--by",
            "u",
            "--registry",
            str(registry),
        ]
    )
    assert code == 0
    selection = yaml.safe_load((tmp_path / "tactic_selection.yaml").read_text(encoding="utf-8"))
    assert selection["selected"][0]["candidate"] == "tc_01" and selection["rejected"] == ["tc_02"]
    queue = GapRegistry.load(registry).queue()
    assert [g.contract for g in queue] == [terrain]
    gaps = yaml.safe_load((tmp_path / "capability_gaps.yaml").read_text(encoding="utf-8"))
    assert gaps["gaps"][0]["selected"] is True


# ---------------------------------------------------------------------- first online run fixes
def test_red_team_does_not_see_capability_verdicts() -> None:
    from afsim_research_agent.crow.proposal_models import CapabilityNeed

    c = card()
    c.capability_needs = [
        CapabilityNeed(
            need="判断是否进入城区", source="steps[1].until", status="confirm", label="需确认"
        )
    ]
    prompt = redteam_prompt(c, profile())
    assert "判断是否进入城区" in prompt and "需确认" not in prompt.split("## 候选战术要素")[1]
    assert "low：核心机制在常见反制下失效" in prompt


def test_capability_doubts_are_not_answered() -> None:
    from afsim_research_agent.crow.proposal_models import RedTeamReview
    from afsim_research_agent.crow.redteam import items_to_answer

    review = RedTeamReview(candidate="tc_01", capability_doubts=["能否判断城区边界"])
    assert items_to_answer(review) == []


def test_self_assessment_and_new_numbers_are_handled() -> None:
    from afsim_research_agent.crow.redteam import strip_self_assessment

    text, removed = strip_self_assessment(NARRATIVE + "此战术强化了侧翼防护，有效应对伏击。")
    assert text == NARRATIVE and removed.startswith("此战术强化了")
    answer = {
        **ANSWER,
        "revisions": [
            {
                "op": "add_exception",
                "situation": "通信中断超过 30 秒",
                "then": "跃进距离限制在射程的 80% 以内",
                "responds_to": ["ct_01"],
            }
        ],
        "narrative": REVISED + "此方案有效弥补了侦察不足。",
    }
    red, prop = scripted(answer=answer)
    result = result_with(card())
    review = asyncio.run(RedTeam(ModelRunner(red), ModelRunner(prop)).review(result, profile()))[0]
    revised = result.candidates[0]
    assert "此方案" not in revised.narrative
    assert any("去掉自我评价" in n for n in review.notes)
    # new numbers are reference values the user can adjust, not items to confirm
    assert not any("数值" in f for f in revised.review_flags)
    ref = next(a for a in revised.assumptions if a.startswith("参考值"))
    assert "30 秒" in ref and "80%" in ref and "50" not in ref


def test_merged_sources_are_accepted(capability_set) -> None:
    from afsim_research_agent.crow.capability_annotation import judge_need
    from afsim_research_agent.crow.proposal_models import NeedDraft

    draft = NeedDraft(
        need="保持编队", source="geometry[0], geometry[1]", maps_to=["formation_change"]
    )
    need, _ = judge_need(draft, capability_set, ["ground_vehicle"], {"geometry[0]", "geometry[1]"})
    assert not any("不是战术要素路径" in n for n in need.notes)


def test_reannotation_keeps_granularity(capability_set) -> None:
    catalog = capability_set
    red, prop = scripted()
    cap = ScriptedModel({"capability": [{"needs": []}], "capability_review": [{"items": []}]})
    result = result_with(card())
    asyncio.run(
        RedTeam(
            ModelRunner(red),
            ModelRunner(prop),
            annotator=CapabilityAnnotator(ModelRunner(cap), catalog),
        ).review(result, profile())
    )
    assert result.capability_granularity == {"granularity": "coarse", "refine": []}
    assert result.catalog_version == catalog.content_hash


# ---------------------------------------------------------------------- second online run fixes
def test_revision_is_checked_against_user_constraints() -> None:
    p = profile()
    p.user_specified = ["接近城区时转纵队", "纵队车距 50 米"]
    red = ScriptedModel(
        {
            "redteam": [RED],
            "constraint": [
                {
                    "violations": [
                        {"constraint": "纵队车距 50 米", "how": "修订改为交错队形，未保持纵队车距"}
                    ]
                }
            ],
        }
    )
    prop = ScriptedModel({"response": [ANSWER]})
    c = card()
    c.addresses = list(p.user_specified)
    result = result_with(c)
    review = asyncio.run(RedTeam(ModelRunner(red), ModelRunner(prop)).review(result, p))[0]
    revised = result.candidates[0]
    assert revised.revision == 2  # flagged for the user, not rejected
    assert any("可能违背用户约束「纵队车距 50 米」" in f for f in revised.review_flags)
    assert any("交错队形" in n for n in review.notes)
    stage, prompt = next((s, q) for s, q in red.calls if s.startswith("constraint:"))
    assert "纵队车距 50 米" in prompt and "侦察" in prompt


def test_self_assessment_variants() -> None:
    from afsim_research_agent.crow.redteam import strip_self_assessment

    text, removed = strip_self_assessment(NARRATIVE + "这种流程确保了在复杂城市环境下的火力协调。")
    assert text == NARRATIVE and removed


def test_group_prefixes_and_missing_groups_in_annotation(capability_set) -> None:
    from afsim_research_agent.capability.granularity import CapabilityPolicy
    from afsim_research_agent.crow.capability_annotation import judge_need
    from afsim_research_agent.crow.proposal_models import NeedDraft

    catalog = capability_set
    policy = CapabilityPolicy.make()
    # "formation:<id>" → <id>
    need, _ = judge_need(
        NeedDraft(
            need="变换队形",
            source="steps[1].do",
            group="formation",
            maps_to=["formation:hierarchical_formation.publish_formation_commands"],
        ),
        catalog,
        ["ground_vehicle"],
        None,
        policy,
    )
    assert need.maps_to == ["hierarchical_formation.publish_formation_commands"]
    assert need.status != "develop"
    # the missing part belongs to fires (a supporting group): simplified, not development
    need, _ = judge_need(
        NeedDraft(
            need="跃进不超出掩护火力纵深",
            source="steps[3].do",
            group="maneuver",
            maps_to=["movement.read_position"],
            missing="fires:火力扇区几何",
        ),
        catalog,
        ["ground_vehicle"],
        None,
        policy,
    )
    assert need.status == "simplified" and need.missing == "火力扇区几何" and not need.gaps
    # in a core group it stays a development gap
    need, _ = judge_need(
        NeedDraft(
            need="识别隐蔽位置",
            source="steps[0].do",
            group="terrain",
            missing="terrain:隐蔽位置识别",
        ),
        catalog,
        ["ground_vehicle"],
        None,
        policy,
    )
    assert need.status == "develop" and need.missing == "隐蔽位置识别"


def test_feasibility_change_from_revision_is_reported(capability_set) -> None:
    from afsim_research_agent.crow.proposal_models import FeasibilityInfo
    from afsim_research_agent.crow.selection_report import render_report

    catalog = capability_set
    red, prop = scripted()
    cap = ScriptedModel(
        {
            "capability": [
                {
                    "needs": [
                        {
                            "need": "识别可隐蔽的位置",
                            "source": "steps[1].do",
                            "group": "terrain",
                            "missing": "隐蔽位置识别",
                        }
                    ]
                }
            ],
            "capability_review": [{"items": []}],
        }
    )
    c = card()
    c.feasibility = FeasibilityInfo(level="needs_confirmation")
    result = result_with(c)
    reviews = asyncio.run(
        RedTeam(
            ModelRunner(red),
            ModelRunner(prop),
            annotator=CapabilityAnnotator(ModelRunner(cap), catalog),
        ).review(result, profile())
    )
    assert (reviews[0].feasibility_before, reviews[0].feasibility_after) == (
        "needs_confirmation",
        "needs_development",
    )
    assert "修订对平台可行性的影响**：需确认 → 需开发" in render_report(result, reviews)


def test_red_team_judges_the_whole_tactic() -> None:
    prompt = redteam_prompt(card(), profile())
    assert "整体上是否合理" in prompt and "不要纠缠细节" in prompt
    assert "最多 3 条" in prompt and "最多 2 条" in prompt
    from afsim_research_agent.crow.proposal_models import RedTeamReview
    from afsim_research_agent.crow.redteam import response_prompt

    text = response_prompt(card(), RedTeamReview(candidate="tc_01"), profile())
    assert "合理的参考值" in text and "保持在战术层面" in text
