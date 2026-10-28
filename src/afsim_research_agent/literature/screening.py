"""Title/abstract screening of candidates before download.

A systematic-review style first pass: the model sees only what a search result
offers (title, venue, year, type, abstract or snippet) and scores relevance to
the research questions on a 0-3 rubric. Downloading, parsing and embedding are
the expensive steps, so filtering here improves both precision and cost.

The screener never decides on its own: its score is recorded on the candidate,
combined with deterministic source quality for ranking, and only a score of 0
(clearly off-topic) removes a candidate.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel, Field

from afsim_research_agent.config import Settings
from afsim_research_agent.literature.models import ScreeningResult, SourceCandidate

SCREENING_PROMPT_VERSION = "crow-screening-2026.10.1"
_ABSTRACT_CHARS = 700

_INSTRUCTIONS = """\
You screen search results for a tactical behavior modeling literature review,
using only the title, source details and abstract shown. Score each candidate:

3 = directly addresses one of the research questions (doctrine for these units
    and tactics, models/algorithms for this exact behaviour, measured parameters)
2 = relevant background for a question (related units, related coordination or
    decision method, simulation of similar behaviour)
1 = tangential (same broad field, little usable content)
0 = off-topic, a product/news/marketing page, a listing, or unrelated meaning
    of the words (e.g. "formation" in geology, "swarm" in insects)

Official manuals for the named unit types or tactics score at least 2 even
without an abstract. Return one item per candidate id with the score, the
number of the best-matching research question (or null) and a reason of at
most 20 words. Do not invent ids.
"""


class ScreeningItem(BaseModel):
    id: str
    score: int = Field(ge=0, le=3)
    question: int | None = None
    reason: str = ""


class ScreeningBatch(BaseModel):
    items: list[ScreeningItem] = Field(default_factory=list)


class BatchModel(Protocol):
    def batch(self, inputs: list[str], config: dict | None = None, **kwargs: object) -> list: ...


class Screener(Protocol):
    name: str

    def screen(
        self,
        requirement: str,
        questions: Sequence[str],
        candidates: Sequence[SourceCandidate],
    ) -> dict[str, ScreeningResult]: ...


class LLMScreener:
    """Batch screening with a structured-output chat model."""

    def __init__(
        self,
        *,
        app_settings: Settings | None = None,
        model: BatchModel | None = None,
        batch_size: int = 8,
        max_concurrency: int = 4,
    ) -> None:
        if model is None:
            from langchain_openai import ChatOpenAI

            runtime = app_settings or Settings.from_environment()
            if not runtime.dashscope_api_key:
                raise ValueError("DASHSCOPE_API_KEY is required for screening")
            model = ChatOpenAI(
                model=runtime.chat_model,
                api_key=runtime.dashscope_api_key,
                base_url=runtime.dashscope_base_url,
                temperature=0,
                extra_body={"enable_thinking": False},
            ).with_structured_output(ScreeningBatch, method="json_schema")
            self.name = f"{runtime.chat_model}/{SCREENING_PROMPT_VERSION}"
        else:
            self.name = f"injected/{SCREENING_PROMPT_VERSION}"
        self._model = model
        self._batch_size = batch_size
        self._max_concurrency = max_concurrency

    def screen(
        self,
        requirement: str,
        questions: Sequence[str],
        candidates: Sequence[SourceCandidate],
    ) -> dict[str, ScreeningResult]:
        if not candidates:
            return {}
        batches = [
            list(candidates[index : index + self._batch_size])
            for index in range(0, len(candidates), self._batch_size)
        ]
        prompts = [build_prompt(requirement, questions, batch) for batch in batches]
        outputs = self._model.batch(
            prompts,
            config={"max_concurrency": self._max_concurrency},
            return_exceptions=True,
        )
        results: dict[str, ScreeningResult] = {}
        for batch, output in zip(batches, outputs, strict=True):
            if isinstance(output, Exception):
                continue  # unscreened candidates keep their deterministic ranking
            parsed = ScreeningBatch.model_validate(
                output.model_dump() if isinstance(output, BaseModel) else output
            )
            valid_ids = {candidate.candidate_id for candidate in batch}
            for item in parsed.items:
                if item.id not in valid_ids:
                    continue
                question = (
                    item.question - 1
                    if item.question is not None and 1 <= item.question <= len(questions)
                    else None
                )
                results[item.id] = ScreeningResult(
                    score=item.score,
                    question_index=question,
                    reason=" ".join(item.reason.split())[:200],
                    screener=self.name,
                )
        return results


def build_prompt(
    requirement: str,
    questions: Sequence[str],
    candidates: Sequence[SourceCandidate],
) -> str:
    lines = [
        _INSTRUCTIONS,
        f"Requirement: {requirement.strip()}",
        "Research questions:",
        *(f"{index}. {question}" for index, question in enumerate(questions, start=1)),
        "",
        "Candidates:",
    ]
    for candidate in candidates:
        meta = candidate.metadata
        details = [
            part
            for part in (
                meta.venue,
                str(meta.year) if meta.year else None,
                meta.document_type,
                candidate.provider,
            )
            if part
        ]
        text = (meta.abstract or candidate.snippet or "").strip()
        lines.append(f"[{candidate.candidate_id}] {candidate.title}")
        lines.append(f"    source: {'; '.join(details)}; {candidate.fetch_url[:120]}")
        if text:
            lines.append(f"    abstract: {' '.join(text.split())[:_ABSTRACT_CHARS]}")
    return "\n".join(lines)
