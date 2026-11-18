"""C1 requirement understanding: natural-language requirement → mission profile.

The model structures what the user said and lists ambiguities; it does not
propose tactics. Code then normalises the draft against controlled
vocabularies, checks that user-specified constraints are quoted verbatim, and
records clarifications. Blocking questions are asked on the command line and
the answers are injected into every later prompt through `profile_context`.

Design: docs/design/crow-tactic-proposal-v1.md §2.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Literal, Protocol

import yaml
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from afsim_research_agent.config import Settings as AppSettings
from afsim_research_agent.ir.catalog import load_catalog

MISSION_PROMPT_VERSION = "crow-mission-2026.10.2"
MISSION_FORMAT_VERSION = "0.1.0"
_MAX_QUESTIONS = 5
_MAX_BLOCKING = 3

Domain = Literal["ground", "air", "sea", "joint"]
Comparator = Literal["gte", "lte", "eq"]


class Mission(BaseModel):
    type: str = Field(description="One intent from the allowed list, or 'custom'.")
    objective: str


class ForceElement(BaseModel):
    role_hint: str
    count: int = Field(default=1, ge=1)
    platform_class: str | None = None


class EnemyPicture(BaseModel):
    known: str = "不明"
    assumed: list[str] = Field(default_factory=list)


class Environment(BaseModel):
    terrain: list[str] = Field(default_factory=list)
    other: list[str] = Field(default_factory=list)


class Constraint(BaseModel):
    text: str = Field(description="The user's words, quoted verbatim.")
    kind: str | None = None
    parameter: str | None = None
    value: float | str | None = None
    unit: str | None = None


class SuccessCriterion(BaseModel):
    measure: str
    description: str
    comparator: Comparator | None = None
    threshold: float | str | None = None


class OpenQuestion(BaseModel):
    id: str
    text: str
    blocking: bool = False
    default_assumption: str
    answer: str | None = None
    answered_by: Literal["user", "default"] | None = None


class MissionDraft(BaseModel):
    """What the model is asked to produce (structured output schema)."""

    domain: Domain
    echelon: str
    mission: Mission
    own_forces: list[ForceElement] = Field(default_factory=list)
    enemy: EnemyPicture = Field(default_factory=EnemyPicture)
    environment: Environment = Field(default_factory=Environment)
    constraints: list[Constraint] = Field(default_factory=list)
    success_criteria: list[SuccessCriterion] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    user_specified: list[str] = Field(default_factory=list)


class MissionProfile(MissionDraft):
    format: Literal["MissionProfile"] = "MissionProfile"
    format_version: str = MISSION_FORMAT_VERSION
    requirement: str
    capabilities_ref: str | None = None
    notes: list[str] = Field(default_factory=list)

    @property
    def blocking_questions(self) -> list[OpenQuestion]:
        return [q for q in self.open_questions if q.blocking and q.answered_by is None]


_INSTRUCTIONS = """\
You are the requirement-understanding stage of a tactical behavior research
agent. Structure the user's requirement into a mission profile. Do NOT propose
tactics, formations or parameter values the user did not state.

Fields:
- domain: ground | air | sea | joint.
- echelon: the unit level (e.g. section, platoon, company, flight, four-ship).
- mission.type: exactly one of the allowed intents below; use "custom" if none fits.
- mission.objective: one sentence, in the user's language.
- own_forces: one entry per role (e.g. platoon leader's vehicle, wingman
  vehicles) with count and platform class (e.g. ground_vehicle, air_vehicle).
  Use counts the user stated. If the composition is not stated, give the
  roles the echelon implies with your best count AND add a non-blocking open
  question about the composition whose default_assumption states that count
  (e.g. "按排长车 1 辆 + 僚车 3 辆处理").
- enemy.known: what the user said about the enemy, or "不明".
  enemy.assumed: assumptions needed to plan; each will be shown for confirmation.
- environment.terrain: terrain classes, preferably from: {terrain}.
- constraints: every explicit constraint, `text` quoted VERBATIM from the
  requirement. Fill parameter/value/unit only for numeric constraints.
- success_criteria: how success would be judged; use comparator/threshold only
  if a value or named parameter is given.
- open_questions (at most {max_q}): ambiguities that affect which tactic fits.
  blocking=true ONLY when different answers lead to completely different
  tactics (e.g. attack vs. defend, whether contact is expected at all); at most
  {max_b} blocking questions. Every question needs a default_assumption.
- user_specified: short phrases copied verbatim from the requirement that later
  stages must not change.

Allowed intents: {intents}
"""


class StructuredModel(Protocol):
    async def ainvoke(self, prompt: str) -> object: ...


class MissionProfiler:
    """Build a mission profile from one natural-language requirement."""

    def __init__(
        self,
        *,
        app_settings: AppSettings | None = None,
        model: StructuredModel | None = None,
    ) -> None:
        if model is None:
            if app_settings is None or not app_settings.dashscope_api_key:
                raise ValueError("DASHSCOPE_API_KEY is required")
            model = ChatOpenAI(
                model=app_settings.chat_model,
                api_key=app_settings.dashscope_api_key,
                base_url=app_settings.dashscope_base_url,
                temperature=0,
                extra_body={"enable_thinking": False},
                timeout=180,
                max_retries=2,
            ).with_structured_output(MissionDraft, method="json_schema")
        self._model = model

    async def profile(self, requirement: str) -> MissionProfile:
        text = requirement.strip()
        if not text:
            raise ValueError("requirement must not be blank")
        vocab = load_catalog().vocabularies
        prompt = _INSTRUCTIONS.format(
            terrain=", ".join(vocab["terrain_classes"]),
            intents=", ".join(vocab["intents"]),
            max_q=_MAX_QUESTIONS,
            max_b=_MAX_BLOCKING,
        )
        response = await self._model.ainvoke(f"{prompt}\nUser requirement:\n{text}")
        if isinstance(response, BaseModel):
            response = response.model_dump()
        draft = MissionDraft.model_validate(response)
        return normalize_profile(draft, text)


def normalize_profile(draft: MissionDraft, requirement: str) -> MissionProfile:
    """Deterministic clean-up; every change is recorded in `notes`."""
    profile = MissionProfile(requirement=requirement, **draft.model_dump())
    vocab = load_catalog().vocabularies
    notes = profile.notes

    if profile.mission.type not in vocab["intents"]:
        notes.append(f"任务类型 `{profile.mission.type}` 不在意图词表中，改为 custom，需人工确认")
        profile.mission.type = "custom"

    unknown_terrain = [t for t in profile.environment.terrain if t not in vocab["terrain_classes"]]
    if unknown_terrain:
        notes.append(f"地形 {unknown_terrain} 不在地形词表中，保留原文，后续需映射")

    source = _squash(requirement)
    for phrase in profile.user_specified:
        if _squash(phrase) not in source:
            notes.append(f"user_specified「{phrase}」不是需求原文，需核对")
    for constraint in profile.constraints:
        if _squash(constraint.text) not in source:
            notes.append(f"约束「{constraint.text}」不是需求原文，需核对")

    questions = profile.open_questions[:_MAX_QUESTIONS]
    if len(profile.open_questions) > _MAX_QUESTIONS:
        notes.append(f"待确认问题超过 {_MAX_QUESTIONS} 个，只保留前 {_MAX_QUESTIONS} 个")
    blocking = 0
    for index, question in enumerate(questions, start=1):
        question.id = f"q{index}"
        question.answer = None
        question.answered_by = None
        if question.blocking:
            blocking += 1
            if blocking > _MAX_BLOCKING:
                question.blocking = False
                notes.append(f"{question.id} 超出阻塞问题上限，改为非阻塞，按默认假设处理")
    profile.open_questions = questions
    return profile


Asker = Callable[[OpenQuestion], str | None]


def resolve_open_questions(profile: MissionProfile, ask: Asker | None) -> MissionProfile:
    """Ask blocking questions; everything else takes its default assumption.

    `ask` returns the user's answer; a blank answer means the user accepted the
    default; None means no answer could be read (e.g. end of input). Without an
    `ask` callable (non-interactive runs) all defaults are used.
    """
    for question in profile.open_questions:
        if question.answered_by is not None:
            continue
        if not question.blocking:
            question.answer, question.answered_by = question.default_assumption, "default"
            continue
        if ask is None:
            profile.notes.append(f"非交互运行：阻塞问题 {question.id} 按默认假设处理，需确认")
            answer = None
        else:
            answer = ask(question)
            if answer is None:
                profile.notes.append(f"阻塞问题 {question.id} 未读到回答，按默认假设继续，需确认")
            elif not answer.strip():
                profile.notes.append(f"阻塞问题 {question.id}：用户确认采用默认假设")
        if answer and answer.strip():
            question.answer, question.answered_by = answer.strip(), "user"
        else:
            question.answer, question.answered_by = question.default_assumption, "default"
    return profile


def profile_context(profile: MissionProfile) -> str:
    """Compact text block injected into planner and later prompts."""
    lines = [
        f"Domain: {profile.domain}; echelon: {profile.echelon}; mission: "
        f"{profile.mission.type} — {profile.mission.objective}",
    ]
    if profile.own_forces:
        forces = ", ".join(
            f"{f.count}× {f.role_hint}" + (f" ({f.platform_class})" if f.platform_class else "")
            for f in profile.own_forces
        )
        lines.append(f"Own forces: {forces}")
    lines.append(
        f"Enemy: {profile.enemy.known}"
        + (f"; assumed: {'; '.join(profile.enemy.assumed)}" if profile.enemy.assumed else "")
    )
    if profile.environment.terrain or profile.environment.other:
        lines.append(
            "Environment: " + ", ".join([*profile.environment.terrain, *profile.environment.other])
        )
    if profile.user_specified:
        lines.append("User-specified (do not change): " + "; ".join(profile.user_specified))
    if profile.success_criteria:
        lines.append(
            "Success criteria: " + "; ".join(c.description for c in profile.success_criteria)
        )
    answered = [q for q in profile.open_questions if q.answer]
    if answered:
        lines.append("Clarifications:")
        for q in answered:
            who = "user answer" if q.answered_by == "user" else "default assumption"
            lines.append(f"- {q.text} → {q.answer} ({who})")
    return "\n".join(lines)


def write_profile(profile: MissionProfile, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = profile.model_dump(mode="json", exclude_none=True)
    head = ("format", "format_version", "requirement")
    data = {**{k: data[k] for k in head}, **{k: v for k, v in data.items() if k not in head}}
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100), encoding="utf-8"
    )
    return path


def load_profile(path: Path) -> MissionProfile:
    return MissionProfile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def _squash(text: str) -> str:
    return re.sub(r"[\s，,。．.、；;：:\"'“”「」]", "", text).lower()
