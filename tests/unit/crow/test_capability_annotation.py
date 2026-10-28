"""Tests for C4b: capability needs of candidate tactics (crow-tactic-proposal-v1 §18)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from afsim_research_agent.capability import CapabilityCatalog, gap_id
from afsim_research_agent.capability.granularity import FINE, CapabilityPolicy
from afsim_research_agent.crow.annotate import annotate_dir, load_result
from afsim_research_agent.crow.capability_annotation import (
    CapabilityAnnotator,
    annotation_prompt,
    catalog_digest,
    judge_need,
)
from afsim_research_agent.crow.mission import MissionDraft, normalize_profile
from afsim_research_agent.crow.proposal_models import (
    NeedDraft,
    ProposalResult,
    Step,
    TacticCard,
)
from afsim_research_agent.crow.proposal_report import render_markdown, write_proposal
from afsim_research_agent.falcon.llm import ModelRunner, ScriptedModel
from afsim_research_agent.falcon.models import CrowRef, EvidencePackage

SEND = "message_exchange:communication_action.send_message"
RECEIVE = "message_exchange:communication_message.received_type"
GO = "point_navigation:mobility_action.go_to_location"
POSITION = "own_kinematic_state:platform_state.position"
URBAN = "terrain_classification:environment_state.terrain_class"


@pytest.fixture(scope="module")
def catalog() -> CapabilityCatalog:
    return CapabilityCatalog.load()


def profile():
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
    return normalize_profile(draft, "装甲排交替跃进")


def card(card_id: str, name: str) -> TacticCard:
    return TacticCard(
        id=card_id,
        name=name,
        narrative="排长车与僚车交替跃进，跃进到位后报告，接近城区时转纵队。" * 3,
        summary=name,
        steps=[
            Step(phase="跃进", do="跃进到下一掩护位置", until="到位并报告"),
            Step(phase="转换", do="转纵队", until="进入城区"),
        ],
    )


def need(text, source, maps_to=(), necessity="required", alternatives=(), missing=""):
    return NeedDraft(
        need=text,
        source=source,
        necessity=necessity,
        maps_to=list(maps_to),
        alternatives=[{"via": v, "maps_to": list(m)} for v, m in alternatives],
        missing=missing,
    )


# ---------------------------------------------------------------------- code-side judgment
def test_judge_need_statuses(catalog: CapabilityCatalog) -> None:
    ready, _ = judge_need(need("跃进到位置", "steps[0].do", [GO]), catalog, ["ground_vehicle"])
    assert ready.status == "ready" and not ready.gaps

    confirm, _ = judge_need(need("报告到位", "steps[0].until", [SEND]), catalog, ["ground_vehicle"])
    assert confirm.status == "confirm" and confirm.gap_type == "confirm"
    assert confirm.gaps == [gap_id(SEND)]

    # nothing in the catalog fits: the need itself is the gap
    missing, _ = judge_need(need("投放烟幕", "exceptions[0].then"), catalog, ["ground_vehicle"])
    assert missing.status == "develop" and missing.gaps == [gap_id("投放烟幕")]
    assert "目录中没有对应能力" in missing.notes


def test_invalid_refs_are_dropped_not_trusted(catalog: CapabilityCatalog) -> None:
    result, _ = judge_need(
        need("跃进", "steps[0].do", ["made_up_capability", GO, "point_navigation:nope"]),
        catalog,
        ["ground_vehicle"],
    )
    assert result.maps_to == [GO]
    assert any("made_up_capability" in n for n in result.notes)
    assert result.status == "ready"


def test_substitutable_takes_best_alternative(catalog: CapabilityCatalog) -> None:
    result, _ = judge_need(
        need(
            "读取领车位置",
            "geometry[0]",
            [SEND, RECEIVE],
            necessity="substitutable",
            alternatives=[("固定偏移跟随", ["formation_following"])],
        ),
        catalog,
        ["ground_vehicle"],
    )
    assert result.chosen == "固定偏移跟随" and result.status == "partial"
    assert [a.status for a in result.alternatives] == ["confirm", "partial"]
    # substitutable without a valid alternative is treated as required
    lone, _ = judge_need(
        need("读取领车位置", "geometry[0]", [SEND], necessity="substitutable"),
        catalog,
        ["ground_vehicle"],
    )
    assert lone.necessity == "required" and lone.status == "confirm"


def test_platform_class_is_respected(catalog: CapabilityCatalog) -> None:
    # altitude control does not apply to ground vehicles
    result, _ = judge_need(
        need("爬升到指定高度", "steps[0].do", ["altitude_control:mobility_action.go_to_altitude"]),
        catalog,
        ["ground_vehicle"],
    )
    assert result.status in {"ready", "develop"}
    cap = catalog.get("altitude_control")
    if cap.platform_classes and "ground_vehicle" not in cap.platform_classes:
        assert result.status == "develop"


# ---------------------------------------------------------------------- annotator
def scripted(answers: dict[str, list[NeedDraft]], review=None) -> ScriptedModel:
    script = {
        f"capability:{cid}": [{"needs": [n.model_dump() for n in needs]}]
        for cid, needs in answers.items()
    }
    script["capability_review"] = [review or {"items": []}]
    return ScriptedModel(script)


def two_candidates() -> ProposalResult:
    return ProposalResult(
        requirement="装甲排交替跃进",
        max_candidates=5,
        max_innovative=2,
        candidates=[card("tc_01", "交替跃进"), card("tc_02", "前锋侦察")],
    )


def test_annotator_builds_feasibility_and_shared_gaps(catalog: CapabilityCatalog) -> None:
    result = two_candidates()
    model = scripted(
        {
            "tc_01": [
                need("跃进到位置", "steps[0].do", [GO]),
                need("报告到位", "steps[0].until", [SEND]),
                need("判断是否在城区", "steps[1].do", [URBAN]),
            ],
            "tc_02": [
                need("前锋报告敌情", "steps[0].until", [SEND]),
                need("投放烟幕", "exceptions[0].then", necessity="enhancing"),
            ],
        }
    )
    before = [(c.id, c.score.total) for c in result.candidates]
    gaps = asyncio.run(
        CapabilityAnnotator(ModelRunner(model), catalog, policy=FINE).annotate(result, profile())
    )
    # C4b never reorders or rescores candidates
    assert [(c.id, c.score.total) for c in result.candidates] == before
    first, second = result.candidates
    assert first.feasibility.level == "needs_confirmation"
    assert set(first.feasibility.blocking_gaps) == {gap_id(SEND), gap_id(URBAN)}
    assert first.required_capabilities == [
        "message_exchange",
        "point_navigation",
        "terrain_classification",
    ]
    # an enhancing need never blocks, but is still listed as a gap
    assert second.feasibility.level == "needs_confirmation"
    assert gap_id("投放烟幕") not in second.feasibility.blocking_gaps
    by_id = {g.id: g for g in gaps}
    send = by_id[gap_id(SEND)]
    assert send.candidates_count == 2 and send.gap_type == "confirm"
    assert [u.candidate for u in send.used_by] == ["tc_01", "tc_02"]
    assert by_id[gap_id("投放烟幕")].gap_type == "develop"
    assert gaps[0].gap_type == "develop"  # develop first
    assert result.catalog_version == catalog.content_hash
    # prompts carry the formal layer only
    _, prompt = model.calls[0]
    assert "_local/" not in prompt and ".local." not in prompt and SEND in prompt


def test_annotation_failure_is_reported_not_raised(catalog: CapabilityCatalog) -> None:
    result = two_candidates()
    model = ScriptedModel(
        {"capability:tc_01": [RuntimeError("down")], "capability:tc_02": [{"needs": []}]}
    )
    asyncio.run(
        CapabilityAnnotator(ModelRunner(model, retries=0), catalog, policy=FINE).annotate(
            result, None
        )
    )
    assert result.candidates[0].feasibility is None
    assert any("能力标注失败" in n for n in result.candidates[0].notes)
    assert result.candidates[1].feasibility.level == "ready"


def test_prompt_and_digest(catalog: CapabilityCatalog) -> None:
    digest = catalog_digest(catalog, ["ground_vehicle"])
    assert "weapon_tools" not in digest and "launch_envelope_generation" not in digest
    assert "comm_device_control" in digest
    prompt = annotation_prompt(card("tc_01", "交替跃进"), digest, ["ground_vehicle"])
    assert "steps[0]：phase=跃进" in prompt and "maps_to 留空" in prompt


# ---------------------------------------------------------------------- report and files
def annotated(catalog: CapabilityCatalog) -> ProposalResult:
    result = two_candidates()
    model = scripted(
        {
            "tc_01": [need("报告到位", "steps[0].until", [SEND])],
            "tc_02": [need("跃进到位置", "steps[0].do", [GO])],
        }
    )
    asyncio.run(
        CapabilityAnnotator(ModelRunner(model), catalog, policy=FINE).annotate(result, profile())
    )
    return result


def empty_package() -> EvidencePackage:
    return EvidencePackage(package_id="p", crow=CrowRef(gate_version="g"))


def test_report_shows_feasibility_separately(catalog: CapabilityCatalog) -> None:
    text = render_markdown(annotated(catalog), empty_package())
    assert "| 平台可行性 |" in text and "| 需确认 |" in text and "| 可实现 |" in text
    assert "**平台能力（需确认）**：需确认 1 项（报告到位）" in text
    assert "## 平台能力缺口" in text and gap_id(SEND) in text
    assert "不影响候选排序" in text


def test_files_round_trip_and_annotate_command(catalog: CapabilityCatalog, tmp_path: Path) -> None:
    result = annotated(catalog)
    paths = write_proposal(result, empty_package(), tmp_path, None)
    gaps = yaml.safe_load(paths["capability_gaps"].read_text(encoding="utf-8"))
    assert gaps["catalog_version"] == catalog.content_hash
    assert gaps["gaps"][0]["id"] == gap_id(SEND) and gaps["gaps"][0]["selected"] is False
    reloaded = load_result(tmp_path)
    assert reloaded.candidates[0].feasibility.level == "needs_confirmation"

    # re-annotating an existing run (offline replay path) overwrites the annotation only
    model = scripted({"tc_01": [need("跃进到位置", "steps[0].do", [GO])], "tc_02": []})
    again, again_paths = asyncio.run(
        annotate_dir(
            tmp_path,
            catalog=catalog,
            profile=profile(),
            package=empty_package(),
            model=model,
            policy=FINE,
        )
    )
    assert again.candidates[0].feasibility.level == "ready"
    assert again.capability_gaps == []
    assert (tmp_path / "capability_trace.json").is_file()
    assert again_paths["markdown"].read_text(encoding="utf-8").count("平台能力（可实现）") == 2


# ---------------------------------------------------------------------- online-run fixes
ZONE = "zone_membership:environment_state.in_zone"


def test_ir_extension_does_not_make_crow_level_development(catalog: CapabilityCatalog) -> None:
    # zone membership exists on the platform but IR 1.0 cannot express it yet
    result = two_candidates()
    model = scripted({"tc_01": [need("判断是否进入城区", "steps[1].until", [ZONE])], "tc_02": []})
    gaps = asyncio.run(
        CapabilityAnnotator(ModelRunner(model), catalog, policy=FINE).annotate(result, profile())
    )
    first = result.candidates[0]
    assert first.capability_needs[0].status == "ir_extension"
    assert first.feasibility.level == "needs_confirmation"
    assert gaps[0].gap_type == "ir_extension"


def test_invalid_source_and_why_are_kept_for_review(catalog: CapabilityCatalog) -> None:
    draft = need("跃进到位置", "t6", [GO])
    draft.why = "定点机动就是到达指定位置"
    sources = {"steps[0]", "decision_authority"}
    result, _ = judge_need(draft, catalog, ["ground_vehicle"], sources)
    assert "来源 t6 不是战术要素路径" in result.notes
    assert result.why == "定点机动就是到达指定位置"
    ok, _ = judge_need(
        need("跃进到位置", "steps[0].do", [GO]), catalog, ["ground_vehicle"], sources
    )
    assert not ok.notes


def test_report_shows_mapping_reason(catalog: CapabilityCatalog) -> None:
    result = two_candidates()
    draft = need("报告到位", "steps[0].until", [SEND])
    draft.why = "报告即发送消息"
    model = scripted({"tc_01": [draft], "tc_02": []})
    asyncio.run(
        CapabilityAnnotator(ModelRunner(model), catalog, policy=FINE).annotate(result, profile())
    )
    text = render_markdown(result, empty_package())
    assert "| 对应理由 |" in text and "报告即发送消息" in text


def test_prompt_separates_alternatives_and_forbids_evidence_ids(catalog) -> None:
    prompt = annotation_prompt(card("tc_01", "交替跃进"), "", ["ground_vehicle"])
    assert "任选其一" in prompt and "不能写证据编号" in prompt and '"why"' in prompt


# ---------------------------------------------------------------------- mapping review
COVERAGE = "sensor_coverage_assessment:sensing_coverage.total_covered_time"


def test_review_drops_cross_purpose_mapping(catalog: CapabilityCatalog) -> None:
    result = two_candidates()
    model = scripted(
        {
            "tc_01": [
                need("跃进到位置", "steps[0].do", [GO]),
                need("确认火力已覆盖前方威胁区域", "steps[0].do", [COVERAGE]),
            ],
            "tc_02": [],
        },
        review={
            "items": [{"index": 1, "drop": [COVERAGE], "reason": "事后覆盖统计，不是火力扇区"}]
        },
    )
    gaps = asyncio.run(
        CapabilityAnnotator(ModelRunner(model), catalog, policy=FINE).annotate(result, profile())
    )
    kept, dropped = result.candidates[0].capability_needs
    assert kept.status == "ready" and not kept.notes
    assert dropped.maps_to == [] and dropped.status == "develop"
    assert dropped.notes[0].startswith(f"复核去掉 {COVERAGE}：事后覆盖统计")
    assert result.candidates[0].feasibility.level == "needs_development"
    assert [g.contract for g in gaps] == ["确认火力已覆盖前方威胁区域"]
    review_prompt_text = next(p for s, p in model.calls if s == "capability_review:tc_01")
    assert "定义：" in review_prompt_text and COVERAGE in review_prompt_text


def test_review_failure_keeps_mapping(catalog: CapabilityCatalog) -> None:
    result = two_candidates()
    model = ScriptedModel(
        {
            "capability:tc_01": [
                {"needs": [need("报告到位", "steps[0].until", [SEND]).model_dump()]}
            ],
            "capability:tc_02": [{"needs": []}],
            "capability_review": [RuntimeError("down")],
        }
    )
    asyncio.run(
        CapabilityAnnotator(ModelRunner(model, retries=0), catalog, policy=FINE).annotate(
            result, None
        )
    )
    first = result.candidates[0]
    assert first.capability_needs[0].maps_to == [SEND]
    assert any("映射复核失败" in n for n in first.notes)


def test_review_can_be_disabled_and_digest_has_definitions(catalog: CapabilityCatalog) -> None:
    result = two_candidates()
    model = scripted({"tc_01": [need("跃进", "steps[0].do", [GO])], "tc_02": []})
    annotator = CapabilityAnnotator(ModelRunner(model), catalog, review=False)
    asyncio.run(annotator.annotate(result, profile()))
    assert not any(s.startswith("capability_review") for s, _ in model.calls)
    assert "定义：" in catalog_digest(catalog, ["ground_vehicle"])


def test_missing_part_keeps_mapping_and_becomes_development_gap(catalog: CapabilityCatalog) -> None:
    draft = need("判断是否已脱离敌方火力范围", "steps[3].until", [POSITION])
    draft.missing = "敌方火力态势判断"
    result, verdict = judge_need(draft, catalog, ["ground_vehicle"])
    assert result.maps_to == [POSITION] and result.status == "develop"
    assert result.gaps == [gap_id("敌方火力态势判断")]
    assert "目录中缺少：敌方火力态势判断" in result.notes
    # nothing mapped at all: the note is not repeated by the verdict reason
    empty, _ = judge_need(need("投放烟幕", "exceptions[0].then"), catalog, ["ground_vehicle"])
    assert empty.notes == ["目录中没有对应能力"]


def test_review_can_keep_building_blocks_and_name_missing_part(catalog) -> None:
    result = two_candidates()
    model = scripted(
        {"tc_01": [need("判断是否已脱离敌方火力范围", "steps[0].until", [POSITION])], "tc_02": []},
        review={"items": [{"index": 0, "drop": [], "missing": "敌方火力态势判断", "reason": "缺"}]},
    )
    gaps = asyncio.run(
        CapabilityAnnotator(ModelRunner(model), catalog, policy=FINE).annotate(result, profile())
    )
    first = result.candidates[0].capability_needs[0]
    assert first.maps_to == [POSITION] and first.status == "develop"
    assert first.notes[0] == "复核指出缺少 敌方火力态势判断"
    assert [(g.contract, g.gap_type) for g in gaps] == [("敌方火力态势判断", "develop")]
    review_text = next(p for s, p in model.calls if s.startswith("capability_review"))
    assert "自身位置之于保持间距" in review_text


# ---------------------------------------------------------------------- coarse first drafts
def test_coarse_judges_core_groups_and_simplifies_the_rest(catalog: CapabilityCatalog) -> None:
    result = two_candidates()
    model = scripted(
        {
            "tc_01": [
                need("跃进到位置", "steps[0].do", [GO]),
                need("向排长报告", "steps[0].until", [SEND]),
                need("判断是否进入城区", "steps[1].until", [URBAN]),
            ],
            "tc_02": [need("判断火力扇区覆盖", "steps[0].do", missing="火力扇区几何")],
        }
    )
    # the model is asked for coarse, grouped needs
    model.script["capability:tc_01"][0]["needs"][1]["group"] = "comms"
    model.script["capability:tc_02"][0]["needs"][0]["group"] = "fires"
    annotator = CapabilityAnnotator(ModelRunner(model), catalog)  # coarse by default
    gaps = asyncio.run(annotator.annotate(result, profile()))
    first, second = result.candidates
    by_need = {n.need: n for n in first.capability_needs}
    assert by_need["向排长报告"].status == "simplified" and not by_need["向排长报告"].gaps
    assert by_need["判断是否进入城区"].status == "confirm"  # terrain is a core group
    assert first.feasibility.blocking_gaps == [gap_id(URBAN)]
    # a supporting group's missing piece is simplified, not a development gap
    assert second.feasibility.level == "ready"
    assert second.capability_needs[0].status == "simplified"
    assert [g.contract for g in gaps] == [URBAN]
    assert result.capability_granularity == {"granularity": "coarse", "refine": []}
    prompt = next(p for s, p in model.calls if s == "capability:tc_01")
    assert "粗粒度" in prompt and "group: maneuver" in prompt and "【核心：逐项核对】" in prompt
    assert "fuel_status_monitoring" not in prompt
    text = render_markdown(result, empty_package())
    assert "简化处理" in text and "按粗粒度判定" in text


def test_refined_group_is_judged_precisely(catalog: CapabilityCatalog) -> None:
    result = two_candidates()
    model = scripted({"tc_01": [need("向排长报告", "steps[0].until", [SEND])], "tc_02": []})
    policy = CapabilityPolicy.make("coarse", ["comms"])
    asyncio.run(
        CapabilityAnnotator(ModelRunner(model), catalog, policy=policy).annotate(result, profile())
    )
    assert result.candidates[0].capability_needs[0].status == "confirm"
    assert result.candidates[0].feasibility.blocking_gaps == [gap_id(SEND)]


def test_unconfirmable_numbers_are_reference_values(catalog: CapabilityCatalog) -> None:
    policy = CapabilityPolicy.make()
    # a missing number (a range, a depth, a boundary) is a reference value, not a gap
    draft = need("跃进不超出掩护火力有效纵深", "steps[0].do", [POSITION])
    draft.group, draft.missing = "terrain", "掩护火力有效纵深"
    result, _ = judge_need(draft, catalog, ["ground_vehicle"], None, policy)
    assert result.status == "ready" and not result.gaps and not result.missing
    assert result.reference.startswith("掩护火力有效纵深≈")
    # the model may give the value itself
    draft = need("与敌保持安全距离", "steps[0].do", [POSITION])
    draft.reference = "安全距离≈1500 米"
    result, _ = judge_need(draft, catalog, ["ground_vehicle"], None, policy)
    assert (
        result.reference == "安全距离≈1500 米" and "按参考值处理：安全距离≈1500 米" in result.notes
    )
