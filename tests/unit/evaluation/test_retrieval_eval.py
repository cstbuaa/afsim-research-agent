"""Tests for the retrieval evaluation harness."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from afsim_research_agent.evaluation.retrieval import (
    GoldSource,
    apply_labels,
    load_gold,
    render_markdown,
    rescore,
    score_case,
    summarize,
)
from afsim_research_agent.literature.models import SourceCandidate, SourceCollection
from afsim_research_agent.models import SourceMetadata

GOLD_FILE = Path(__file__).resolve().parents[3] / "eval" / "retrieval_gold.toml"


def cand(cid: str, title: str, url: str, decision: str, **meta: object) -> SourceCandidate:
    return SourceCandidate(
        candidate_id=cid, channel="web", provider="p", query="q", rank=1, title=title, url=url,
        normalized_url=url, fetch_url=url, decision=decision, metadata=SourceMetadata(**meta),
    )


def test_shipped_gold_set_is_well_formed() -> None:
    cases = load_gold(GOLD_FILE)

    assert len(cases) >= 10
    assert len({c.id for c in cases}) == len(cases)
    for case in cases:
        assert case.gold, case.id
        for gold in case.gold:
            assert gold.importance in {"must", "should"}
            assert any([gold.doi, gold.arxiv, gold.designation, gold.title, gold.url_contains])


def test_matchers() -> None:
    assert GoldSource("x", doi="10.1/ABC").matches(cand("c", "t", "u", "accepted", doi="10.1/abc"))
    assert GoldSource("x", arxiv="1709.00084").matches(
        cand("c", "t", "https://arxiv.org/pdf/1709.00084v5", "accepted")
    )
    assert GoldSource("x", designation="ATP 3-20.15").matches(
        cand("c", "Tank Platoon", "https://x.mil/ATP_3-20.15.pdf", "accepted")
    )
    assert not GoldSource("x", designation="ATP 3-20.15").matches(
        cand("c", "FM 3-20.15 Tank Platoon", "https://x/a", "accepted")
    )
    assert GoldSource("x", title="A survey of Behavior Trees in robotics and AI").matches(
        cand("c", "A Survey of Behavior Trees in Robotics and AI.", "u", "accepted")
    )
    assert GoldSource("x", url_contains="japcc.org").matches(
        cand("c", "t", "https://www.japcc.org/articles/mumt", "accepted")
    )


def collection() -> SourceCollection:
    return SourceCollection(
        requirement="r",
        queries=[],
        candidates=[
            cand("c1", "ATP 3-20.15 Tank Platoon", "https://a.mil/1.pdf", "accepted"),
            cand("c2", "Paper", "https://b.org/2", "screened_out", doi="10.1/s"),
            cand("c3", "Other", "https://c.org/3", "skipped_budget", doi="10.1/k"),
        ],
    )


def test_stage_recall_and_summary() -> None:
    case = load_gold(GOLD_FILE)[0]
    case = type(case)(
        id="t",
        requirement="r",
        gold=(
            GoldSource("doctrine", importance="must", designation="ATP 3-20.15"),
            GoldSource("screened", doi="10.1/s"),
            GoldSource("kept", doi="10.1/k"),
            GoldSource("missing", importance="must", doi="10.9/none"),
        ),
    )

    result = score_case(case, collection())

    assert [(o.discovered, o.kept, o.accepted) for o in result.outcomes] == [
        (True, True, True), (True, False, False), (True, True, False), (False, False, False)
    ]
    assert result.recall("discovered") == 0.75
    assert result.recall("accepted", "must") == 0.5
    summary = summarize([result])
    assert summary["recall_kept"] == 0.5
    markdown = render_markdown([result], summary)
    assert "未找到的 must 金标" in markdown and "missing" in markdown
    assert "被筛除/拒绝" in markdown


def test_labels_give_precision(tmp_path: Path) -> None:
    labels = tmp_path / "labels.csv"
    labels.write_text(
        "case_id,source_id,tier,title,uri,relevant\nt,a,A,x,u,y\nt,b,C,x,u,n\nt,c,C,x,u,\n",
        encoding="utf-8",
    )
    result = score_case(load_gold(GOLD_FILE)[0], collection())
    result.case_id = "t"

    apply_labels([result], labels)

    assert (result.precision, result.labelled) == (0.5, 2)


def test_rescore_reads_saved_collections(tmp_path: Path) -> None:
    gold = tmp_path / "gold.toml"
    gold.write_text(
        'version="t"\n[[case]]\nid="armor"\nrequirement="r"\n'
        '[[case.gold]]\nlabel="ATP"\ndesignation="ATP 3-20.15"\nimportance="must"\n',
        encoding="utf-8",
    )
    (tmp_path / "run" / "armor").mkdir(parents=True)
    (tmp_path / "run" / "armor" / "collection.json").write_text(
        json.dumps({"plan": {}, "collection": collection().model_dump(mode="json")}),
        encoding="utf-8",
    )

    summary = rescore(argparse.Namespace(gold=gold, run_dir=tmp_path / "run", labels=None))

    assert summary["must_recall_accepted"] == 1.0
    assert (tmp_path / "run" / "evaluation.md").is_file()
