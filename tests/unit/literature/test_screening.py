"""Tests for LLM title/abstract screening."""

from __future__ import annotations

from afsim_research_agent.literature.models import SourceCandidate
from afsim_research_agent.literature.screening import (
    LLMScreener,
    ScreeningBatch,
    ScreeningItem,
    build_prompt,
)
from afsim_research_agent.models import SourceMetadata


def candidate(cid: str, title: str = "Tank platoon formations") -> SourceCandidate:
    return SourceCandidate(
        candidate_id=cid, channel="web", provider="tavily", query="q", rank=1, title=title,
        url=f"https://e.org/{cid}", normalized_url=f"https://e.org/{cid}",
        fetch_url=f"https://e.org/{cid}", snippet="snippet text",
        metadata=SourceMetadata(venue="Drones", year=2023, abstract="Abstract text " * 200),
    )


class FakeBatchModel:
    def __init__(self, outputs: list[object]) -> None:
        self.outputs = outputs
        self.prompts: list[str] = []
        self.config: dict | None = None

    def batch(self, inputs, config=None, **kwargs):
        self.prompts.extend(inputs)
        self.config = config
        assert kwargs["return_exceptions"] is True
        return self.outputs[: len(inputs)]


def test_prompt_contains_rubric_questions_and_truncated_abstract() -> None:
    prompt = build_prompt(
        "armored formations", ["Which formation?", "What spacing?"], [candidate("c1")]
    )

    assert "3 = directly addresses" in prompt
    assert "1. Which formation?" in prompt and "2. What spacing?" in prompt
    assert "[c1] Tank platoon formations" in prompt
    assert "Drones; 2023; tavily" in prompt
    abstract_line = next(line for line in prompt.splitlines() if "abstract:" in line)
    assert len(abstract_line) < 760


def test_batches_are_parsed_and_unknown_ids_ignored() -> None:
    model = FakeBatchModel(
        [
            ScreeningBatch(
                items=[
                    ScreeningItem(id="c1", score=3, question=1, reason="doctrine for platoon"),
                    ScreeningItem(id="c2", score=0, question=9, reason="geology"),
                    ScreeningItem(id="zzz", score=3),
                ]
            ),
            RuntimeError("model timeout"),
        ]
    )
    screener = LLMScreener(model=model, batch_size=2)

    results = screener.screen(
        "r", ["q1", "q2"], [candidate("c1"), candidate("c2"), candidate("c3")]
    )

    assert len(model.prompts) == 2
    assert model.config == {"max_concurrency": 4}
    assert results["c1"].score == 3 and results["c1"].question_index == 0
    assert results["c2"].question_index is None  # out-of-range question number
    assert "c3" not in results and "zzz" not in results  # failed batch stays unscreened


def test_dict_outputs_are_accepted() -> None:
    model = FakeBatchModel([{"items": [{"id": "c1", "score": 2, "reason": "  related  "}]}])

    results = LLMScreener(model=model).screen("r", ["q"], [candidate("c1")])

    assert results["c1"].reason == "related"
