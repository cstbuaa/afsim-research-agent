"""Tests for C1 requirement understanding (mission profile)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from afsim_research_agent.config import Settings
from afsim_research_agent.crow.cli import prepare_mission_profile
from afsim_research_agent.crow.mission import (
    MissionDraft,
    MissionProfiler,
    load_profile,
    profile_context,
    resolve_open_questions,
)

REQUIREMENT = (
    "研究装甲排在开阔地和城区的编队方式：开阔地用楔形，接近城区时转纵队，纵队车距 50 米；"
    "接敌后报告并交替跃进，与敌保持安全距离。"
)


def draft(**overrides: object) -> dict:
    data = {
        "domain": "ground",
        "echelon": "platoon",
        "mission": {"type": "movement_to_contact", "objective": "沿路线机动至城区边缘"},
        "own_forces": [
            {"role_hint": "platoon_leader", "count": 1, "platform_class": "ground_vehicle"},
            {"role_hint": "wingman", "count": 3},
        ],
        "enemy": {"known": "不明", "assumed": ["可能存在反装甲火力"]},
        "environment": {"terrain": ["open", "urban"]},
        "constraints": [
            {"text": "纵队车距 50 米", "parameter": "column_interval", "value": 50, "unit": "m"}
        ],
        "success_criteria": [{"measure": "reach_objective", "description": "到达城区边缘"}],
        "open_questions": [
            {"id": "x", "text": "进攻还是防御？", "blocking": True, "default_assumption": "进攻"},
            {"id": "y", "text": "是否有上级支援火力？", "default_assumption": "无"},
        ],
        "user_specified": ["开阔地用楔形", "纵队车距 50 米"],
    }
    data.update(overrides)
    return data


class FakeModel:
    def __init__(self, response: object) -> None:
        self.response = response
        self.prompts: list[str] = []

    async def ainvoke(self, prompt: str) -> object:
        self.prompts.append(prompt)
        return self.response


def profile_of(**overrides: object):
    model = FakeModel(MissionDraft.model_validate(draft(**overrides)))
    return asyncio.run(MissionProfiler(model=model).profile(REQUIREMENT)), model


def test_profiler_prompt_lists_controlled_vocabularies() -> None:
    profile, model = profile_of()

    assert "movement_to_contact" in model.prompts[0]
    assert REQUIREMENT in model.prompts[0]
    assert profile.requirement == REQUIREMENT
    assert [q.id for q in profile.open_questions] == ["q1", "q2"]
    assert profile.notes == []


def test_normalisation_records_every_change() -> None:
    questions = [
        {"id": "a", "text": f"问题{i}", "blocking": True, "default_assumption": "d"}
        for i in range(7)
    ]
    profile, _ = profile_of(
        mission={"type": "urban_movement", "objective": "o"},
        environment={"terrain": ["open", "jungle"]},
        user_specified=["开阔地用楔形", "城区用菱形"],
        open_questions=questions,
    )

    assert profile.mission.type == "custom"
    assert len(profile.open_questions) == 5
    assert [q.blocking for q in profile.open_questions] == [True, True, True, False, False]
    joined = "\n".join(profile.notes)
    assert "urban_movement" in joined and "jungle" in joined
    assert "城区用菱形" in joined and "开阔地用楔形" not in joined
    assert "只保留前 5 个" in joined


def test_blocking_questions_are_asked_and_answers_recorded() -> None:
    profile, _ = profile_of()
    asked: list[str] = []

    resolve_open_questions(profile, lambda q: asked.append(q.id) or "防御")

    assert asked == ["q1"]  # non-blocking questions are not asked
    q1, q2 = profile.open_questions
    assert (q1.answer, q1.answered_by) == ("防御", "user")
    assert (q2.answer, q2.answered_by) == ("无", "default")
    context = profile_context(profile)
    assert "进攻还是防御？ → 防御 (user answer)" in context
    assert "是否有上级支援火力？ → 无 (default assumption)" in context
    assert "纵队车距 50 米" in context


def test_blank_answer_and_non_interactive_runs_take_defaults() -> None:
    profile, _ = profile_of()
    resolve_open_questions(profile, lambda q: "  ")
    assert profile.open_questions[0].answered_by == "default"
    assert profile.notes == ["阻塞问题 q1：用户确认采用默认假设"]

    profile, _ = profile_of()
    resolve_open_questions(profile, lambda q: None)  # end of input
    assert "未读到回答" in profile.notes[0]

    profile, _ = profile_of()
    resolve_open_questions(profile, None)
    assert {q.answered_by for q in profile.open_questions} == {"default"}
    assert "非交互运行" in profile.notes[0]


def test_blank_requirement_is_rejected() -> None:
    with pytest.raises(ValueError, match="requirement"):
        asyncio.run(MissionProfiler(model=FakeModel(draft())).profile("  "))


def test_cli_step_writes_profile_and_reloads_it(tmp_path: Path) -> None:
    model = FakeModel(draft())
    profile = prepare_mission_profile(
        REQUIREMENT,
        settings=Settings.from_environment(),
        output_dir=tmp_path,
        interactive=False,
        profiler=MissionProfiler(model=model),
    )

    path = tmp_path / "mission_profile.yaml"
    assert profile is not None and path.is_file()
    text = path.read_text(encoding="utf-8")
    assert text.startswith("format: MissionProfile\nformat_version:")
    assert "纵队车距 50 米" in text
    reloaded = load_profile(path)
    assert reloaded == profile

    # Re-using a saved profile skips the model entirely.
    again = prepare_mission_profile(
        REQUIREMENT, settings=Settings.from_environment(), output_dir=tmp_path / "b", existing=path,
        interactive=False,
    )
    assert again == profile


def test_cli_step_survives_model_failure(tmp_path: Path) -> None:
    class Broken:
        async def ainvoke(self, prompt: str) -> object:
            raise RuntimeError("upstream timeout")

    profile = prepare_mission_profile(
        REQUIREMENT, settings=Settings.from_environment(), output_dir=tmp_path, interactive=False,
        profiler=MissionProfiler(model=Broken()),
    )

    assert profile is None
    assert not (tmp_path / "mission_profile.yaml").exists()
    assert prepare_mission_profile(
        REQUIREMENT, settings=Settings.from_environment(), output_dir=tmp_path, skip=True
    ) is None
