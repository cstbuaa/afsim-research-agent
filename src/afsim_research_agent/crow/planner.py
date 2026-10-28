"""Turn one tactical requirement into a research plan.

The language model drafts questions and queries with a domain glossary in its
prompt; deterministic vocabulary expansion then adds professional terms,
current doctrine editions and Chinese search strings, so coverage does not
depend on what the model happens to remember.

Questions are tagged with the six research dimensions (design
crow-tactic-proposal-v1 §3.1). Untagged questions get keyword-inferred tags;
when fewer than five dimensions are covered and there is room, template
questions are added for the missing ones. Pattern hypotheses are restricted to
the human-maintained pattern table.
"""

from __future__ import annotations

from typing import Protocol

from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from afsim_research_agent.config import Settings as AppSettings
from afsim_research_agent.config import settings as default_app_settings
from afsim_research_agent.crow.dimensions import (
    DIMENSION_GUIDE,
    DIMENSIONS,
    PatternTable,
    dimension_label,
    infer_dimensions,
    template_question,
)
from afsim_research_agent.crow.mission import MissionProfile, profile_context
from afsim_research_agent.crow.models import (
    FollowupDraft,
    PlannedQuestion,
    PlannerDraft,
    TacticalResearchPlan,
)
from afsim_research_agent.domain.vocabulary import TacticalVocabulary

PLANNER_PROMPT_VERSION = "crow-planner-2026.10.4"
_MAX_QUESTIONS = 6
_MIN_DIMENSIONS = 5
_MAX_PATTERNS = 3
_MAX_ANALOGY = 3
_MAX_WEB = 6
_MAX_FAMILY = 5
_MAX_KEY_TERMS = 12

_PLANNER_INSTRUCTIONS = """\
You are the planning stage of a tactical behavior research agent whose output
will later be turned into AFSIM behavior models.
Preserve the user's scenario, actors, objectives, and constraints. You may
hypothesise abstract tactic patterns, but do not propose concrete tactics or
parameter values.

Produce:
- requirement_summary: one or two sentences restating the requirement.
- research_questions (4-6): questions answerable from literature that a
  behavior modeler needs. Each item has `question` and `dimensions` (one or
  more of the dimension ids below). Together the questions must cover at least
  five of the six dimensions. Ask for quantitative values (spacing, ranges,
  speeds, timings) and the conditions under which they apply.
- pattern_hypotheses (1-3): ids from the pattern table below that may fit.
  Use only listed ids.
- analogy_queries (1-3): English web queries that look for the hypothesised
  patterns at other echelons or in other domains (air, ground, sea, unmanned
  teams), e.g. "leapfrog movement naval escort".
- search_queries (2-6): precise English web search queries. Mix doctrine
  terms, engineering terms and synonyms.
- scholarly_queries (2-4): short keyword queries (3-7 words, no quotes or
  boolean operators) for academic indexes (OpenAlex, Semantic Scholar, arXiv).
  Use the terms researchers use, e.g. "multi-vehicle formation control terrain".
- doctrine_queries (1-4): queries aimed at official manuals, naming the
  publication when you are confident it exists. Never invent a publication
  number; use descriptive words instead.
- technical_report_queries (1-3): queries for defense technical reports and
  theses (DTIC, Naval Postgraduate School, RAND, AFIT), e.g. "agent-based
  simulation armored platoon formation thesis".
- chinese_queries (1-3): Chinese keyword strings a researcher would type into
  CNKI, using standard Chinese military and engineering terminology.
- key_terms (up to 12).

Prefer the professional terms in the glossary below where they fit.

Dimensions:
{dimensions}

Pattern table:
{patterns}
"""

_FOLLOWUP_INSTRUCTIONS = """\
You are refining a literature search. The first search round did not find
enough evidence for the research questions listed below. Write NEW queries that
approach them differently: other terminology, adjacent fields (robotics,
operations research, simulation), parameter-focused wording, or specific
document types. Do not repeat earlier queries.

Produce search_queries (1-3), scholarly_queries (1-3) and doctrine_queries
(0-2). Never invent publication numbers.
"""


class StructuredPlannerModel(Protocol):
    """Structured-output model interface used by the planner."""

    async def ainvoke(self, prompt: str) -> object: ...


class TacticalResearchPlanner:
    """Preserve user intent while generating bounded evidence queries."""

    def __init__(
        self,
        *,
        app_settings: AppSettings = default_app_settings,
        model: StructuredPlannerModel | None = None,
        followup_model: StructuredPlannerModel | None = None,
        vocabulary: TacticalVocabulary | None = None,
        patterns: PatternTable | None = None,
    ) -> None:
        if model is None or (followup_model is None and app_settings.dashscope_api_key):
            if not app_settings.dashscope_api_key:
                raise ValueError("DASHSCOPE_API_KEY is required")
            chat_model = ChatOpenAI(
                model=app_settings.chat_model,
                api_key=app_settings.dashscope_api_key,
                base_url=app_settings.dashscope_base_url,
                temperature=0,
                extra_body={"enable_thinking": False},
            )
            model = model or chat_model.with_structured_output(
                PlannerDraft, method="json_schema"
            )
            followup_model = followup_model or chat_model.with_structured_output(
                FollowupDraft, method="json_schema"
            )
        # followup_model may stay None when a custom planning model is injected
        # without credentials; plan_followup then returns no queries.
        self._model = model
        self._followup_model = followup_model
        self._vocabulary = vocabulary or TacticalVocabulary.default()
        self._patterns = patterns or PatternTable.default()

    async def plan(
        self, requirement: str, *, mission_profile: MissionProfile | None = None
    ) -> TacticalResearchPlan:
        """Create research questions and search queries for one requirement."""
        normalized_requirement = requirement.strip()
        if not normalized_requirement:
            raise ValueError("requirement must not be blank")

        concepts = self._vocabulary.match([normalized_requirement])
        glossary = self._vocabulary.glossary(concepts) or "- (no glossary terms matched)"
        instructions = _PLANNER_INSTRUCTIONS.format(
            dimensions="\n".join(f"- {d}: {DIMENSION_GUIDE[d]}" for d in DIMENSIONS),
            patterns=self._patterns.prompt_listing(),
        )
        context = (
            f"\nMission profile (confirmed with the user):\n{profile_context(mission_profile)}\n"
            if mission_profile is not None
            else ""
        )
        response = await self._model.ainvoke(
            f"{instructions}\nGlossary:\n{glossary}\n{context}\n"
            f"User requirement:\n{normalized_requirement}"
        )
        draft = PlannerDraft.model_validate(_as_dict(response))

        notes: list[str] = []
        questions = self._shape_questions(draft, notes)
        patterns = self._check_patterns(draft.pattern_hypotheses, notes)
        question_texts = [q.question for q in questions]
        expansion = self._vocabulary.expand(
            [normalized_requirement, *question_texts, *draft.key_terms],
            existing_doctrine=draft.doctrine_queries,
        )
        return TacticalResearchPlan(
            requirement_summary=draft.requirement_summary.strip(),
            research_questions=question_texts,
            question_dimensions=questions,
            pattern_hypotheses=patterns,
            analogy_queries=_dedupe(draft.analogy_queries)[:_MAX_ANALOGY],
            search_queries=(_dedupe(draft.search_queries) or draft.search_queries)[:_MAX_WEB],
            scholarly_queries=_dedupe(
                [*draft.scholarly_queries[:4], *expansion.scholarly_queries]
            )[:_MAX_FAMILY],
            doctrine_queries=_dedupe(
                [*expansion.doctrine_queries[:2], *draft.doctrine_queries]
            )[:_MAX_FAMILY],
            technical_report_queries=_dedupe(draft.technical_report_queries)[:3],
            chinese_queries=_dedupe([*draft.chinese_queries, *expansion.chinese_queries])[
                :_MAX_FAMILY
            ],
            key_terms=_dedupe(draft.key_terms)[:_MAX_KEY_TERMS],
            matched_concepts=[concept.id for concept in expansion.concepts],
            planning_notes=[*expansion.notes, *notes],
        )

    def _shape_questions(self, draft: PlannerDraft, notes: list[str]) -> list[PlannedQuestion]:
        seen: set[str] = set()
        questions: list[PlannedQuestion] = []
        for item in draft.research_questions:
            text = " ".join(item.question.split())
            if not text or text.lower() in seen:
                continue
            seen.add(text.lower())
            dims = list(dict.fromkeys(item.dimensions))
            if not dims:
                dims = infer_dimensions(text)
                if dims:
                    notes.append(f"研究问题「{text[:40]}」未标维度，按关键词推断为 {dims}")
                else:
                    notes.append(f"研究问题「{text[:40]}」未标维度，且无法推断")
            questions.append(PlannedQuestion(question=text, dimensions=dims))
        if len(questions) > _MAX_QUESTIONS:
            notes.append(f"研究问题超过 {_MAX_QUESTIONS} 个，截断")
            questions = questions[:_MAX_QUESTIONS]

        covered = {d for q in questions for d in q.dimensions}
        missing = [d for d in DIMENSIONS if d not in covered]
        while len(DIMENSIONS) - len(missing) < _MIN_DIMENSIONS and missing:
            if len(questions) >= _MAX_QUESTIONS:
                notes.append(
                    "维度覆盖不足且问题数已达上限，未覆盖："
                    + "、".join(dimension_label(d) for d in missing)
                )
                break
            dimension = missing.pop(0)
            questions.append(
                PlannedQuestion(
                    question=template_question(dimension, draft.requirement_summary),
                    dimensions=[dimension],
                )
            )
            notes.append(f"补充模板问题以覆盖维度：{dimension_label(dimension)}")
        return questions

    def _check_patterns(self, hypotheses: list[str], notes: list[str]) -> list[str]:
        known = self._patterns.ids
        kept: list[str] = []
        for raw in hypotheses:
            pattern = raw.strip()
            if pattern in known:
                if pattern not in kept:
                    kept.append(pattern)
            elif pattern:
                notes.append(f"模式假设 `{pattern}` 不在模式表中，已忽略")
        return kept[:_MAX_PATTERNS]

    async def plan_followup(
        self,
        requirement: str,
        plan: TacticalResearchPlan,
        weak_questions: list[str],
        found_titles: list[str],
    ) -> FollowupDraft:
        """Targeted queries for under-covered questions; earlier queries are excluded."""
        if self._followup_model is None:
            return FollowupDraft()
        previous = [
            *plan.search_queries,
            *plan.scholarly_queries,
            *plan.doctrine_queries,
            *(q for r in plan.followup_rounds for q in r.search_queries + r.scholarly_queries),
        ]
        prompt = (
            f"{_FOLLOWUP_INSTRUCTIONS}\nRequirement:\n{requirement.strip()}\n\n"
            "Under-covered research questions:\n"
            + "\n".join(_with_dimensions(q, plan) for q in weak_questions)
            + "\n\nEarlier queries (do not repeat):\n"
            + "\n".join(f"- {q}" for q in previous)
            + "\n\nSources already found:\n"
            + "\n".join(f"- {t}" for t in found_titles[:15])
        )
        draft = FollowupDraft.model_validate(_as_dict(await self._followup_model.ainvoke(prompt)))
        seen = {q.lower() for q in previous}
        return FollowupDraft(
            search_queries=[q for q in _dedupe(draft.search_queries) if q.lower() not in seen][:3],
            scholarly_queries=[
                q for q in _dedupe(draft.scholarly_queries) if q.lower() not in seen
            ][:3],
            doctrine_queries=[
                q for q in _dedupe(draft.doctrine_queries) if q.lower() not in seen
            ][:2],
        )


def _with_dimensions(question: str, plan: TacticalResearchPlan) -> str:
    dims = plan.dimensions_of(question)
    return f"- {question}" + (f" [dimensions: {', '.join(dims)}]" if dims else "")


def _as_dict(response: object) -> object:
    return response.model_dump() if isinstance(response, BaseModel) else response


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for value in values:
        normalized = " ".join(value.split())
        key = normalized.lower()
        if normalized and key not in seen:
            seen.add(key)
            cleaned.append(normalized)
    return cleaned
