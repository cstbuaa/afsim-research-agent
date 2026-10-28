"""Tests for tactical requirement understanding and research planning."""

from __future__ import annotations

import asyncio

import pytest

from afsim_research_agent.crow import TacticalResearchPlanner
from afsim_research_agent.crow import planner as planner_module
from afsim_research_agent.crow.mission import MissionDraft, normalize_profile
from afsim_research_agent.crow.models import (
    FollowupDraft,
    PlannedQuestion,
    PlannerDraft,
    TacticalResearchPlan,
)


class FakeStructuredModel:
    def __init__(self, response: object | None = None) -> None:
        self.prompts: list[str] = []
        self.response = response or PlannerDraft(
            requirement_summary="Study four-aircraft defensive formation transitions.",
            research_questions=[
                "What threat conditions should trigger a defensive transition?",
                "How should leader and wingmen coordinate the transition?",
            ],
            search_queries=[
                "cooperative aircraft defensive formation transition paper",
                "leader follower formation switching UAV",
                "Leader  follower formation switching UAV",
            ],
            scholarly_queries=["formation switching threat"],
            doctrine_queries=["FM 3-20.15 tank platoon"] + [f"d{i}" for i in range(6)],
            technical_report_queries=["formation transition thesis"],
            chinese_queries=["编队 队形转换"],
            key_terms=["formation transition", "leader follower"],
        )

    async def ainvoke(self, prompt: str) -> object:
        self.prompts.append(prompt)
        return self.response


class FakeChatModel:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.schemas: list[object] = []

    def with_structured_output(self, schema: object, *, method: str) -> FakeStructuredModel:
        self.schemas.append((schema, method))
        return FakeStructuredModel()


def test_default_planner_uses_dashscope_openai_compatibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[FakeChatModel] = []

    def build(**kwargs: object) -> FakeChatModel:
        created.append(FakeChatModel(**kwargs))
        return created[-1]

    monkeypatch.setattr(planner_module, "ChatOpenAI", build, raising=False)
    settings = type(
        "S",
        (),
        {
            "chat_model": "qwen3.8-flash",
            "dashscope_api_key": "test-key",
            "dashscope_base_url": "https://example.test/v1",
        },
    )()

    TacticalResearchPlanner(app_settings=settings)

    assert created[0].kwargs == {
        "model": "qwen3.8-flash",
        "api_key": "test-key",
        "base_url": "https://example.test/v1",
        "temperature": 0,
        "extra_body": {"enable_thinking": False},
    }
    assert created[0].schemas == [(PlannerDraft, "json_schema"), (FollowupDraft, "json_schema")]


def test_plan_includes_glossary_and_merges_vocabulary_expansion() -> None:
    model = FakeStructuredModel()

    planner = TacticalResearchPlanner(model=model)

    plan = asyncio.run(planner.plan("四机有人/无人协同编队何时进入防御队形？"))

    prompt = model.prompts[0]
    assert "四机有人/无人协同编队何时进入防御队形？" in prompt
    assert "Glossary:" in prompt and "manned-unmanned teaming" in prompt
    assert "technical_report_queries" in prompt and "chinese_queries" in prompt
    assert "coop.mumt" in plan.matched_concepts
    # FM 3-20.15 in a model query is upgraded to its successor first.
    assert plan.doctrine_queries[0] == "ATP 3-20.15"
    assert "FM 3-20.15 is superseded by ATP 3-20.15" in plan.planning_notes
    assert len(plan.doctrine_queries) <= 5
    assert plan.search_queries == [
        "cooperative aircraft defensive formation transition paper",
        "leader follower formation switching UAV",
    ]
    assert plan.scholarly_queries[0] == "formation switching threat"
    assert any("manned-unmanned teaming" in q for q in plan.scholarly_queries)
    assert plan.chinese_queries[0] == "编队 队形转换"
    assert plan.technical_report_queries == ["formation transition thesis"]


def test_overlong_model_output_is_truncated_not_rejected() -> None:
    draft = PlannerDraft(
        requirement_summary="s",
        research_questions=[f"q{i}" for i in range(7)],
        search_queries=[f"s{i}" for i in range(9)],
    )

    planner = TacticalResearchPlanner(
        app_settings=type("S", (), {"dashscope_api_key": None})(),
        model=FakeStructuredModel(draft),
    )
    plan = asyncio.run(planner.plan("x"))

    assert len(plan.research_questions) == 6
    assert len(plan.search_queries) == 6


class FollowupModel:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def ainvoke(self, prompt: str) -> FollowupDraft:
        self.prompts.append(prompt)
        return FollowupDraft(
            search_queries=["old query", "terrain spacing armored platoon meters"],
            scholarly_queries=["ground vehicle formation spacing terrain"],
        )


def test_followup_targets_weak_questions_and_drops_repeats() -> None:
    followup_model = FollowupModel()
    planner = TacticalResearchPlanner(model=FakeStructuredModel(), followup_model=followup_model)
    plan = TacticalResearchPlan(
        requirement_summary="s", research_questions=["q1", "q2"], search_queries=["old query"]
    )

    draft = asyncio.run(
        planner.plan_followup("requirement", plan, ["q2"], ["ATP 3-20.15 Tank Platoon"])
    )

    assert draft.search_queries == ["terrain spacing armored platoon meters"]
    assert "- q2" in followup_model.prompts[0]
    assert "ATP 3-20.15 Tank Platoon" in followup_model.prompts[0]


def test_followup_is_empty_without_a_followup_model() -> None:
    no_key = type("S", (), {"dashscope_api_key": None})()
    planner = TacticalResearchPlanner(app_settings=no_key, model=FakeStructuredModel())
    plan = TacticalResearchPlan(
        requirement_summary="s", research_questions=["q"], search_queries=["x"]
    )

    assert asyncio.run(planner.plan_followup("r", plan, ["q"], [])) == FollowupDraft()


@pytest.mark.parametrize("requirement", ["", "   "])
def test_planner_rejects_blank_requirement(requirement: str) -> None:
    with pytest.raises(ValueError, match="requirement"):
        asyncio.run(TacticalResearchPlanner(model=FakeStructuredModel()).plan(requirement))


def test_plan_accepts_legacy_payload_without_new_fields() -> None:
    plan = TacticalResearchPlan.model_validate(
        {"requirement_summary": "s", "research_questions": ["q"], "search_queries": ["x"]}
    )

    assert plan.scholarly_queries == [] and plan.followup_rounds == []


def tagged_draft(**overrides: object) -> PlannerDraft:
    data = {
        "requirement_summary": "Armored platoon moves to contact across open and urban terrain.",
        "research_questions": [
            {"question": "When do tank platoons use wedge vs column?",
             "dimensions": ["applicability", "roles_geometry"]},
            {"question": "What is the bounding overwatch sequence and when does a bound end?",
             "dimensions": ["sequence_termination"]},
            {"question": "When is bounding abandoned?", "dimensions": ["exceptions_abort"]},
            {"question": "How are contact reports passed to the platoon leader?",
             "dimensions": ["coordination_authority"]},
        ],
        "pattern_hypotheses": ["bounding_overwatch", "made_up_pattern", "formation_by_terrain"],
        "analogy_queries": ["leapfrog naval escort", "uav bounding overwatch", "a", "b"],
        "search_queries": ["tank platoon formations"],
    }
    data.update(overrides)
    return PlannerDraft.model_validate(data)


def test_dimensions_patterns_and_analogies_are_kept_and_checked() -> None:
    model = FakeStructuredModel(tagged_draft())
    plan = asyncio.run(TacticalResearchPlanner(model=model).plan("装甲排接敌运动"))

    prompt = model.prompts[0]
    assert "sequence_termination:" in prompt and "bounding_overwatch:" in prompt
    assert plan.pattern_hypotheses == ["bounding_overwatch", "formation_by_terrain"]
    assert any("made_up_pattern" in note for note in plan.planning_notes)
    assert plan.analogy_queries == ["leapfrog naval escort", "uav bounding overwatch", "a"]
    assert plan.dimensions_of("When is bounding abandoned?") == ["exceptions_abort"]
    # Five dimensions covered by the model; effectiveness is missing but 5 is enough.
    assert len(plan.research_questions) == 4


def test_missing_dimensions_get_template_questions() -> None:
    draft = tagged_draft(
        research_questions=[
            {"question": "When do tank platoons use wedge vs column?",
             "dimensions": ["applicability"]},
            "What spacing do vehicles keep in a column formation?",  # untagged
        ]
    )
    plan = asyncio.run(TacticalResearchPlanner(model=FakeStructuredModel(draft)).plan("x"))

    assert plan.dimensions_of("What spacing do vehicles keep in a column formation?") == [
        "roles_geometry"
    ]
    covered = {d for q in plan.question_dimensions for d in q.dimensions}
    assert len(covered) == 5
    assert len(plan.research_questions) == 5
    added = plan.question_dimensions[2:]
    assert [q.dimensions for q in added] == [
        ["sequence_termination"], ["exceptions_abort"], ["coordination_authority"]
    ]
    assert "Armored platoon moves to contact" in added[0].question
    assert sum("补充模板问题" in n for n in plan.planning_notes) == 3


def test_full_question_list_reports_uncovered_dimensions() -> None:
    draft = tagged_draft(
        research_questions=[
            {"question": f"formation question {i}", "dimensions": ["roles_geometry"]}
            for i in range(6)
        ]
    )
    plan = asyncio.run(TacticalResearchPlanner(model=FakeStructuredModel(draft)).plan("x"))

    assert len(plan.research_questions) == 6
    assert any("维度覆盖不足" in note for note in plan.planning_notes)


def test_mission_profile_is_injected_into_the_prompt() -> None:
    profile = normalize_profile(
        MissionDraft.model_validate(
            {
                "domain": "ground",
                "echelon": "platoon",
                "mission": {"type": "movement_to_contact", "objective": "reach the town"},
                "open_questions": [
                    {"id": "q", "text": "Attack or defend?", "blocking": True,
                     "default_assumption": "attack"}
                ],
            }
        ),
        "requirement",
    )
    profile.open_questions[0].answer = "defend"
    profile.open_questions[0].answered_by = "user"
    model = FakeStructuredModel(tagged_draft())

    asyncio.run(TacticalResearchPlanner(model=model).plan("requirement", mission_profile=profile))

    assert "Mission profile (confirmed with the user)" in model.prompts[0]
    assert "Attack or defend? → defend (user answer)" in model.prompts[0]


def test_followup_prompt_names_the_weak_dimensions() -> None:
    followup_model = FollowupModel()
    planner = TacticalResearchPlanner(model=FakeStructuredModel(), followup_model=followup_model)
    plan = TacticalResearchPlan(
        requirement_summary="s",
        research_questions=["q1"],
        search_queries=["old query"],
        question_dimensions=[PlannedQuestion(question="q1", dimensions=["exceptions_abort"])],
    )

    asyncio.run(planner.plan_followup("r", plan, ["q1"], []))

    assert "- q1 [dimensions: exceptions_abort]" in followup_model.prompts[0]
