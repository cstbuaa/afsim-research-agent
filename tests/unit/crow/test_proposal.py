"""Tests for C4 tactic proposal: retrieval first, innovation only for gaps."""

from __future__ import annotations

import asyncio
from pathlib import Path

import yaml

from afsim_research_agent.crow.dimensions import PatternTable
from afsim_research_agent.crow.mission import MissionDraft, normalize_profile
from afsim_research_agent.crow.models import DimensionCoverage, ReadinessAssessment
from afsim_research_agent.crow.proposal import (
    ProposalSettings,
    TacticProposer,
    check_card,
    merge_packages,
)
from afsim_research_agent.crow.proposal_models import TacticDraft
from afsim_research_agent.crow.proposal_report import render_markdown, write_proposal
from afsim_research_agent.falcon.llm import ModelRunner, ScriptedModel
from afsim_research_agent.falcon.models import (
    CrowRef,
    EvidenceItem,
    EvidencePackage,
    EvidenceSource,
)

REQUIREMENT = "装甲排开阔地用楔形，接近城区时转纵队，纵队车距 50 米；接敌后报告并交替跃进。"
CLAUSES = ["开阔地用楔形", "纵队车距 50 米", "接敌后报告并交替跃进"]


def item(
    eid: str, claim: str, quote: str, topic: str = "formation", q: float = 0.85, subject: str = ""
):
    return EvidenceItem(
        subject=subject,
        evidence_id=eid,
        claim=claim,
        topic=topic,
        quote=quote,
        excerpt_id="ex_" + eid,
        excerpt=quote,
        source_id="pdf:atp",
        location="p1",
        relevance_score=0.8,
        quality_score=q,
    )


def package() -> EvidencePackage:
    return EvidencePackage(
        package_id="p",
        crow=CrowRef(gate_version="g"),
        sources=[EvidenceSource(source_id="pdf:atp", title="ATP 3-20.15", quality_tier="A")],
        evidence=[
            item("ev_wedge", "楔形用于开阔地", "The wedge is used in open terrain."),
            item("ev_column", "纵队用于受限地形", "Column is used in restrictive terrain."),
            item(
                "ev_gap",
                "开放纵队车距50到100米",
                "Vehicle distance varies from 50 to 100 meters",
                "spacing",
            ),
            item(
                "ev_bound",
                "预计接敌时交替跃进",
                "bounding overwatch when contact is expected",
                "transition",
            ),
            item(
                "ev_report",
                "跃进前排长下达命令",
                "the platoon leader must give clear orders",
                "communication",
            ),
            item(
                "ev_rush",
                "每次冲刺3到5秒",
                "Each rush should last from 3 to 5 seconds",
                "speed",
                subject="士兵",
            ),
            item(
                "ev_tank",
                "坦克排预计接敌时交替跃进",
                "The tank platoon bounds when contact is expected",
                "transition",
                subject="坦克排",
            ),
        ],
    )


def profile():
    return normalize_profile(
        MissionDraft.model_validate(
            {
                "domain": "ground",
                "echelon": "platoon",
                "mission": {"type": "movement_to_contact", "objective": "到达城区"},
                "user_specified": CLAUSES,
            }
        ),
        REQUIREMENT,
    )


def tactic(
    name: str,
    *,
    covers=(),
    patterns=("formation_by_terrain",),
    formation="wedge",
    phase="travel",
    authority="leader_assigns",
    **extra,
) -> dict:
    data = {
        "name": name,
        "patterns": list(patterns),
        "summary": f"{name}概要",
        "narrative": f"{name}：开阔地以楔形行进，接近城区时转为纵队并保持车距 50 米；"
        "接敌后先向排长报告，再分两组交替跃进，掩护组占位观察，跃进组前出至新掩护位置，"
        "两组轮换直至到达目标；通信中断时按预定信号继续。",
        "source_context": "tank platoon",
        "context_match": "exact",
        "applicability": [{"situation": "开阔地", "evidence": ["ev_wedge"]}],
        "geometry": [{"formation": formation, "when": "开阔地", "evidence": ["ev_wedge"]}],
        "steps": [
            {"phase": phase, "do": "行进", "until": "接敌", "evidence": ["ev_bound"]},
            {
                "phase": f"{phase}收尾",
                "do": "到达目标",
                "until": "任务结束",
                "basis": "user_specified",
            },
        ],
        "decision_authority": authority,
        "addresses": list(CLAUSES),
        "covers": list(covers),
    }
    data.update(extra)
    return data


def idea(
    name: str,
    *,
    provenance="adapted",
    operators=("change_tempo",),
    combination=(),
    formation="echelon",
    phase="halt",
    **extra,
) -> dict:
    data = tactic(
        name,
        formation=formation,
        phase=phase,
        patterns=("bounding_overwatch",),
        covers=["接敌后报告并交替跃进"],
        **extra,
    )
    data["provenance"] = provenance
    data["derivation"] = {
        "sources": [{"tactic": "tc_01", "evidence": ["ev_bound", "ev_missing"]}],
        "operators": list(operators),
        "changes": [{"op": op, "after": "改动", "why": "理由"} for op in operators],
        "combination": [{"dimension": d, "option": o} for d, o in combination],
    }
    return data


def proposer(script, *, retriever=None, settings=None):
    model = ScriptedModel(script)
    return TacticProposer(
        runner=ModelRunner(model), settings=settings or ProposalSettings(), retriever=retriever
    ), model


def run(p, **kwargs):
    return asyncio.run(p.propose(REQUIREMENT, package(), profile=profile(), **kwargs))


def test_enough_retrieved_tactics_means_no_innovation() -> None:
    tactics = [
        tactic("地形编队", covers=["开阔地用楔形", "纵队车距 50 米"]),
        tactic(
            "交替跃进",
            covers=["接敌后报告并交替跃进"],
            patterns=["bounding_overwatch"],
            formation="column",
            phase="bound",
        ),
        tactic(
            "行进间掩护",
            patterns=["traveling_overwatch"],
            formation="vee",
            phase="overwatch",
            authority="hybrid",
        ),
    ]
    called = []

    async def retriever(gaps):
        called.append(gaps)

    p, model = proposer({"tactic.extract": [{"tactics": tactics}]}, retriever=retriever)
    result = run(p)

    assert [c.provenance for c in result.candidates] == ["retrieved"] * 3
    assert result.gaps == [] and called == [] and result.ideas == []
    assert "检索战术足够，未生成创新候选" in result.notes
    assert [stage for stage, _ in model.calls] == ["tactic.extract"]
    assert result.candidates[0].score.coverage > result.candidates[2].score.coverage


def test_element_checks_downgrade_unsupported_content() -> None:
    draft = TacticDraft.model_validate(
        tactic(
            "检查",
            covers=["开阔地用楔形", "城区用菱形"],
            patterns=["formation_by_terrain", "made_up"],
            geometry=[
                {"formation": "column", "spacing": "50 m", "evidence": ["ev_gap"]},
                {"formation": "column", "spacing": "80 米", "evidence": ["ev_column"]},
                {"formation": "column", "spacing": "50 米", "basis": "user_specified"},
                {"formation": "column", "spacing": "30 米", "basis": "user_specified"},
                {"formation": "line", "evidence": ["ev_nope"]},
            ],
            exceptions=[{"situation": "通信中断", "then": "停止跃进", "basis": "inferred"}],
        )
    )
    card, reason = check_card(
        draft,
        card_id="tc_01",
        provenance="retrieved",
        package=package(),
        profile=profile(),
        patterns=PatternTable.default(),
        requirement=REQUIREMENT,
    )

    assert reason is None
    assert card.patterns == ["formation_by_terrain"]
    assert card.covers == ["开阔地用楔形"]
    bases = [g.basis for g in card.geometry]
    assert bases == ["evidence", "inferred", "user_specified", "inferred", "inferred"]
    assert card.geometry[4].evidence == []
    assert card.support == {
        "applicability": "supported",
        "roles": "empty",
        "geometry": "partial",
        "steps": "supported",
        "exceptions": "inferred",
    }
    joined = "\n".join(card.notes)
    assert "80" in joined and "30" in joined and "ev_nope" in joined and "城区用菱形" in joined


def test_gaps_trigger_one_retrieval_then_smallest_innovation() -> None:
    retrieved = [tactic("地形编队", covers=["开阔地用楔形", "纵队车距 50 米"])]
    ideas = [
        idea("大改动", operators=["regroup", "add_deception"], formation="vee", phase="big"),
        idea("小改动", operators=["change_tempo"]),
        idea(
            "不相容组合",
            provenance="combined",
            operators=[],
            formation="line",
            phase="x",
            combination=[("fires", "hold_fire_concealed"), ("fires", "suppress_then_move")],
        ),
        idea("无算子", operators=[], formation="vee", phase="y"),
    ]
    gaps_seen = []

    async def retriever(gaps):
        gaps_seen.append([g.kind for g in gaps])
        return None

    p, model = proposer(
        {"tactic.extract": [{"tactics": retrieved}], "tactic.innovate": [{"ideas": ideas}]},
        retriever=retriever,
        settings=ProposalSettings(ideas_per_gap=4),
    )
    result = run(p)

    assert gaps_seen == [["uncovered_clause", "too_few_retrieved"]]
    assert result.retrieval_rounds == 1
    assert "补充检索没有得到新证据" in result.notes
    kinds = [c.provenance for c in result.candidates]
    assert kinds.count("retrieved") == 1 and len(kinds) == 3  # 1 retrieved + 2 innovative
    innovative = result.candidates[1:]
    assert [c.name for c in innovative][0] == "小改动"
    assert [c.id for c in innovative] == ["tc_02", "tc_03"]
    assert innovative[0].fills_gap == "gap_01"
    assert innovative[0].derivation.sources[0].evidence == ["ev_bound"]
    statuses = {i.card.name: (i.status, i.reason) for i in result.ideas if i.gap == "gap_01"}
    assert statuses["不相容组合"][0] == "rejected" and "不相容" in statuses["不相容组合"][1]
    assert statuses["无算子"][0] == "rejected"
    assert result.gaps[0].filled_by == ["tc_02"]
    assert [s for s, _ in model.calls] == [
        "tactic.extract",
        "tactic.innovate:gap_01",
        "tactic.innovate:gap_02",
    ]


def test_innovation_respects_the_cap_and_can_be_disabled() -> None:
    retrieved = [tactic("地形编队")]
    p, model = proposer(
        {
            "tactic.extract": [{"tactics": retrieved}],
            "tactic.innovate": [{"ideas": [idea("补位")]}],
        },
        settings=ProposalSettings(max_innovative=0),
    )
    result = run(p)

    assert [c.provenance for c in result.candidates] == ["retrieved"]
    assert all(s == "tactic.extract" for s, _ in model.calls)
    assert any("未补位" in n for n in result.notes)
    assert "未配置补充检索" in result.notes[0]


def test_supplementary_evidence_is_merged_and_re_extracted() -> None:
    extra = EvidencePackage(
        package_id="x",
        crow=CrowRef(gate_version="g"),
        evidence=[item("ev_new", "城区纵队", "column in urban terrain")],
    )

    async def retriever(gaps):
        return extra

    first = [tactic("A", covers=CLAUSES[:1])]
    second = [
        tactic("A", covers=CLAUSES[:2]),
        tactic(
            "B",
            covers=CLAUSES[2:],
            patterns=["bounding_overwatch"],
            formation="column",
            phase="bound",
            geometry=[{"formation": "column", "evidence": ["ev_new"]}],
        ),
        tactic(
            "C",
            patterns=["traveling_overwatch"],
            formation="vee",
            phase="move",
            authority="autonomous",
        ),
    ]
    p, model = proposer(
        {"tactic.extract": [{"tactics": first}], "tactic.extract:r2": [{"tactics": second}]},
        retriever=retriever,
    )
    result = run(p)

    assert [s for s, _ in model.calls] == ["tactic.extract", "tactic.extract:r2"]
    assert len(result.candidates) == 3 and result.gaps == []
    b = next(c for c in result.candidates if c.name == "B")
    assert b.geometry[0].basis == "evidence"  # ev_new is known after the merge


def test_duplicates_become_variants_and_conflicts_are_excluded() -> None:
    tactics = [
        tactic("A", covers=CLAUSES),
        tactic("A 的同义写法", covers=CLAUSES[:1]),
        tactic(
            "冲突",
            patterns=["ambush"],
            formation="line",
            phase="wait",
            conflicts_with_user=["纵队车距 50 米"],
        ),
    ]
    p, _ = proposer({"tactic.extract": [{"tactics": tactics}], "tactic.innovate": [{"ideas": []}]})
    result = run(p)

    assert [c.name for c in result.candidates] == ["A"]
    assert result.candidates[0].variants == ["tc_02 A 的同义写法"]
    assert any("冲突" in n and "未入选" in n for n in result.notes)


def test_innovation_takes_the_lowest_ranked_retrieved_slot_when_full() -> None:
    tactics = [
        tactic(f"T{i}", patterns=[p], formation=f, phase=f"ph{i}", covers=CLAUSES[:2])
        for i, (p, f) in enumerate(
            [
                ("formation_by_terrain", "wedge"),
                ("traveling_overwatch", "vee"),
                ("dispersion", "line"),
                ("massing_fires", "echelon"),
                ("ambush", "column"),
            ]
        )
    ]
    p, _ = proposer(
        {
            "tactic.extract": [{"tactics": tactics}],
            "tactic.innovate": [{"ideas": [idea("补交替跃进", formation="staggered")]}],
        }
    )
    result = run(p)

    assert len(result.candidates) == 5
    assert result.candidates[-1].provenance == "adapted"
    assert any("让位" in n for n in result.notes)


def test_dimension_gaps_come_from_the_readiness_assessment() -> None:
    tactics = [
        tactic("A", covers=CLAUSES),
        tactic("B", patterns=["bounding_overwatch"], formation="column", phase="bound"),
        tactic(
            "C", patterns=["traveling_overwatch"], formation="vee", phase="move", authority="hybrid"
        ),
    ]
    readiness = ReadinessAssessment(
        ready=True,
        gate_version="g",
        dimensions=[
            DimensionCoverage(dimension="exceptions_abort", label="例外与中止", covered=False),
            DimensionCoverage(dimension="applicability", label="适用情况", covered=True),
        ],
    )
    p, model = proposer(
        {
            "tactic.extract": [{"tactics": tactics}],
            "tactic.innovate": [
                {
                    "ideas": [
                        idea("没有例外", operators=["perturb_situation"]),
                        idea(
                            "应变",
                            operators=["perturb_situation"],
                            formation="line",
                            exceptions=[
                                {"situation": "通信中断", "then": "按预定信号", "basis": "inferred"}
                            ],
                        ),
                    ]
                }
            ],
        }
    )
    result = run(p, readiness=readiness)

    assert [g.kind for g in result.gaps] == ["dimension"]
    assert "例外与中止" in result.gaps[0].detail
    assert result.candidates[-1].name == "应变"
    assert result.candidates[-1].fills_gap == "gap_01"
    rejected = next(i for i in result.ideas if i.card.name == "没有例外")
    assert rejected.status == "rejected" and "exceptions_abort" in rejected.reason


def test_clause_gap_rejects_ideas_that_do_not_cover_the_clause() -> None:
    tactics = [
        tactic(f"T{i}", patterns=[p], formation=f, phase=f"ph{i}", covers=CLAUSES[:2])
        for i, (p, f) in enumerate(
            [
                ("formation_by_terrain", "wedge"),
                ("traveling_overwatch", "vee"),
                ("dispersion", "line"),
            ]
        )
    ]
    off_target = idea("跑题", formation="column")
    off_target["covers"] = []
    p, _ = proposer(
        {"tactic.extract": [{"tactics": tactics}], "tactic.innovate": [{"ideas": [off_target]}]}
    )
    result = run(p)

    assert [c.provenance for c in result.candidates] == ["retrieved"] * 3
    assert result.ideas[0].status == "rejected"
    assert "接敌后报告并交替跃进" in result.ideas[0].reason
    assert any("只补上 0/1" in n for n in result.notes)


def test_merge_packages_prefixes_claim_ids() -> None:
    from afsim_research_agent.falcon.models import ClaimRecord

    base = package()
    extra = EvidencePackage(
        package_id="x",
        crow=CrowRef(gate_version="g"),
        evidence=[item("ev_wedge", "dup", "dup quote"), item("ev_new", "n", "new quote")],
        claims=[
            ClaimRecord(
                claim_id="cl_001",
                statement="s",
                topic="t",
                members=["ev_new"],
                supporting=["ev_new"],
                conflicting=[],
            )
        ],
    )
    merged = merge_packages(base, extra, "r2")

    assert [e.evidence_id for e in merged.evidence][-1] == "ev_new"
    assert len(merged.evidence) == 8
    assert merged.claims[-1].claim_id == "r2_cl_001"
    assert merged.content_hash.startswith("sha256:")


def test_outputs_are_written_and_innovation_is_flagged(tmp_path: Path) -> None:
    p, _ = proposer(
        {
            "tactic.extract": [{"tactics": [tactic("地形编队", covers=CLAUSES[:2])]}],
            "tactic.innovate": [
                {"ideas": [idea("补位A"), idea("补位B", formation="line", phase="p2")]}
            ],
        },
    )
    result = run(p)
    paths = write_proposal(result, package(), tmp_path, trace=[{"stage": "x"}])

    data = yaml.safe_load(paths["candidates"].read_text(encoding="utf-8"))
    assert data["format"] == "TacticCandidates"
    assert [c["provenance"] for c in data["candidates"]][0] == "retrieved"
    assert yaml.safe_load(paths["ideas"].read_text(encoding="utf-8"))["format"] == "TacticIdeas"
    text = paths["markdown"].read_text(encoding="utf-8")
    assert "⚠ 改编（创新补位），非条令直接给出" in text
    assert "## 引用的证据" in text and "The wedge is used in open terrain." in text
    assert paths["trace"].is_file()


def test_run_proposal_reads_a_run_directory(tmp_path: Path) -> None:
    from crow_fixtures import result as crow_result

    from afsim_research_agent.config import Settings
    from afsim_research_agent.crow.propose import run_proposal

    (tmp_path / "research_result.json").write_text(
        crow_result().model_dump_json(), encoding="utf-8"
    )
    (tmp_path / "evidence_package.json").write_text(package().model_dump_json(), "utf-8")
    model = ScriptedModel(
        {
            "tactic.extract": [{"tactics": [tactic("A", covers=CLAUSES)]}],
            "tactic.innovate": [
                {"ideas": [idea("补位"), idea("补位二", formation="line", phase="p")]}
            ],
        }
    )

    done = asyncio.run(
        run_proposal(
            tmp_path,
            settings=Settings.from_environment(),
            out_dir=tmp_path / "tactics",
            profile=profile(),
            retrieval=False,
            model=model,
            proposal_settings=ProposalSettings(),
        )
    )

    assert done.result.requirement == REQUIREMENT
    assert done.result.evidence_package.endswith("evidence_package.json")
    assert {p.name for p in (tmp_path / "tactics").iterdir()} == {
        "tactic_candidates.yaml",
        "tactic_candidates.md",
        "tactic_ideas.yaml",
        "tactic_trace.json",
    }
    assert len(done.result.candidates) == 3


def test_parameter_clauses_trigger_retrieval_but_not_innovation() -> None:
    tactics = [
        tactic(
            f"T{i}",
            patterns=[p],
            formation=f,
            phase=f"ph{i}",
            covers=["开阔地用楔形", "接敌后报告并交替跃进"],
        )
        for i, (p, f) in enumerate(
            [
                ("formation_by_terrain", "wedge"),
                ("traveling_overwatch", "vee"),
                ("dispersion", "line"),
            ]
        )
    ]
    seen = []

    async def retriever(gaps):
        seen.append([g.target for g in gaps])

    p, model = proposer({"tactic.extract": [{"tactics": tactics}]}, retriever=retriever)
    result = run(p)

    assert seen == [["纵队车距 50 米"]]
    assert result.gaps[0].fills_needed == 0 and "参数类条款" in result.gaps[0].detail
    assert [s for s, _ in model.calls] == ["tactic.extract"]
    assert len(result.candidates) == 3


def test_tactics_for_a_different_unit_type_do_not_count_as_enough() -> None:
    infantry_only = dict(
        applicability=[{"situation": "徒步冲刺", "evidence": ["ev_rush"]}],
        geometry=[{"formation": "楔形", "evidence": ["ev_rush"]}],
        steps=[
            {
                "phase": "冲刺",
                "do": "每次冲刺 3 到 5 秒",
                "until": "到达掩蔽物",
                "evidence": ["ev_rush"],
            },
            {"phase": "收尾", "do": "到达目标", "until": "任务结束", "evidence": ["ev_rush"]},
        ],
    )
    tactics = [
        tactic("A", covers=CLAUSES),
        tactic("B", patterns=["bounding_overwatch"], formation="column", phase="bound"),
        tactic("徒步", patterns=["traveling_overwatch"], context_match="exact", **infantry_only),
    ]
    p, _ = proposer(
        {
            "tactic.extract": [{"tactics": tactics}],
            "tactic.innovate": [{"ideas": [idea("补位", formation="line")]}],
        }
    )
    result = run(p)

    foot = next(c for c in result.retrieved_pool if c.name == "徒步")
    assert foot.context_match == "different" and foot.unit_match == 0.2
    assert all(e.basis == "inferred" for e in foot.steps)
    assert any("徒步步兵资料" in n and "模型自评 exact" not in n for n in foot.notes)
    assert any("模型自评 exact" in n for n in foot.notes)
    assert [g.kind for g in result.gaps] == ["too_few_retrieved"]
    assert "另有 1 个来自不同类型单位" in result.gaps[0].detail
    assert [c.provenance for c in result.candidates] == ["retrieved"] * 3 + ["adapted"]


def test_fragments_are_not_candidates_and_trigger_the_shortage_gap() -> None:
    fragment = tactic(
        "开阔地楔形行进",
        covers=CLAUSES[:1],
        addresses=CLAUSES[:1],
        narrative="楔形行进",
        steps=[{"phase": "行进", "do": "楔形", "until": "接近城区", "evidence": ["ev_wedge"]}],
    )
    complete = tactic("完整", covers=CLAUSES, patterns=["bounding_overwatch"], formation="column")
    p, model = proposer(
        {
            "tactic.extract": [
                {
                    "techniques": [
                        {
                            "id": "a1",
                            "name": "楔形行进",
                            "kind": "formation",
                            "description": "开阔地楔形",
                            "evidence": ["ev_wedge", "ev_nope"],
                        },
                        {
                            "id": "a2",
                            "name": "冲刺",
                            "kind": "movement",
                            "description": "个人冲刺",
                            "evidence": ["ev_rush"],
                        },
                    ],
                    "tactics": [fragment, complete],
                }
            ],
            "tactic.innovate": [
                {
                    "ideas": [
                        idea("不完整补位", narrative="太短"),
                        idea("完整补位", formation="line"),
                    ]
                }
            ],
        }
    )
    result = run(p)

    assert [c.name for c in result.fragments] == ["开阔地楔形行进"]
    issues = result.fragments[0].completeness_issues
    assert any("自然语言" in i for i in issues) and any("步骤少于" in i for i in issues)
    assert any("未实现需求条款" in i and "纵队车距 50 米" in i for i in issues)
    assert [c.name for c in result.candidates] == ["完整", "完整补位"]
    assert "另有 1 个不完整" in result.gaps[0].detail
    assert result.techniques[0].evidence == ["ev_wedge"]
    assert result.techniques[1].unit_type == "徒步步兵"
    rejected = next(i for i in result.ideas if i.card.name == "不完整补位")
    assert rejected.status == "rejected" and "不是完整战术" in rejected.reason
    prompt = model.calls[0][1]
    assert "COMPLETE plan" in prompt and "Format example" in prompt and "［主体：士兵］" in prompt


def test_numbers_must_come_from_evidence_about_the_same_unit_type() -> None:
    mixed = tactic(
        "混合引用",
        covers=CLAUSES,
        steps=[
            {
                "phase": "跃进",
                "do": "每次跃进冲刺 3 到 5 秒",
                "until": "到达掩护位置",
                "evidence": ["ev_tank", "ev_rush"],
            },
            {"phase": "收尾", "do": "到达目标", "until": "任务结束", "evidence": ["ev_tank"]},
        ],
    )
    card, _ = check_card(
        TacticDraft.model_validate(mixed),
        card_id="tc_01",
        provenance="retrieved",
        package=package(),
        profile=profile(),
        patterns=PatternTable.default(),
        requirement=REQUIREMENT,
    )

    assert [s.basis for s in card.steps] == ["inferred", "evidence"]
    assert any("只出现在其他类型单位" in n for n in card.notes)


def test_numbers_in_the_narrative_are_checked() -> None:
    draft = tactic("描述", covers=CLAUSES)
    draft["narrative"] += "跃进距离约 300 米。"
    card, _ = check_card(
        TacticDraft.model_validate(draft),
        card_id="tc_01",
        provenance="retrieved",
        package=package(),
        profile=profile(),
        patterns=PatternTable.default(),
        requirement=REQUIREMENT,
    )

    assert any("战术描述中的数值 300" in n for n in card.notes)
    assert not any("数值 50" in n for n in card.notes)  # 50 m is in the requirement


def test_ideas_built_from_the_same_techniques_are_variants_not_new_candidates() -> None:
    base = tactic("A", covers=CLAUSES, techniques=["t1", "t2", "t3", "t4", "t5"])
    same = idea(
        "换个说法",
        techniques=["t1", "t2", "t3", "t4", "t5"],
        formation="wedge",
        phase="other words",
    )
    same["patterns"] = ["formation_by_terrain"]
    different = idea("3+1", techniques=["t1", "t2", "t6"], formation="3+1纵队", phase="scout")
    p, _ = proposer(
        {
            "tactic.extract": [{"tactics": [base]}],
            "tactic.innovate": [{"ideas": [same, different]}],
        },
        settings=ProposalSettings(max_innovative=1),
    )
    result = run(p)

    assert [c.name for c in result.candidates] == ["A", "3+1"]
    skipped = next(i for i in result.ideas if i.card.name == "换个说法")
    assert skipped.status == "not_selected" and "过于相似" in skipped.reason


def test_review_flags_mark_what_a_human_must_confirm() -> None:
    clause_gap_idea = idea(
        "前锋侦察",
        narrative=(
            "装甲排开阔地楔形行进，接近城区时转纵队，车距 50 米；1 辆僚车前出 50 到 100 米侦察"
            "（ev_gap 上限），单兵观察后报告，其余各班交替跃进，先导车辆（或班）警戒并与敌保持安全距离，直至到达目标。"
        ),
        geometry=[{"formation": "3+1", "spacing": "50 到 100 米", "evidence": ["ev_gap"]}],
    )
    clause_gap_idea["covers"] = CLAUSES
    p, _ = proposer(
        {
            "tactic.extract": [{"tactics": [tactic("A", covers=CLAUSES[:2])]}],
            "tactic.innovate": [{"ideas": [clause_gap_idea]}],
        }
    )
    result = run(p)

    card = next(c for c in result.candidates if c.name == "前锋侦察")
    assert "ev_gap" not in card.narrative and "（）" not in card.narrative
    flags = "\n".join(card.review_flags)
    assert "证据编号，已去除" in flags
    assert "步兵用语（单兵、各班、或班）" in flags
    assert "条款「接敌后报告并交替跃进」由创新方案实现" in flags
    assert "沿用证据数值" in flags and "ev_gap" in flags
    text = render_markdown(result, package())
    assert "**⚠ 需确认**" in text
