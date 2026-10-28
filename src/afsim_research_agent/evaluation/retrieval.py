"""Retrieval evaluation against a gold set of expected sources.

    python -m afsim_research_agent.evaluation.retrieval run    --gold eval/retrieval_gold.toml
    python -m afsim_research_agent.evaluation.retrieval rescore --run-dir data/eval/<run> \
        --labels data/eval/<run>/precision_labels.csv
    python -m afsim_research_agent.evaluation.retrieval verify --gold eval/retrieval_gold.toml

``run`` plans and collects sources for every case (round 1 only, no PaperQA)
and measures, per gold source, whether Crow

* discovered it (any search result matches),
* kept it after triage and screening (not rejected or screened out),
* accepted it (downloaded into the evidence corpus).

It writes ``evaluation.json`` (machine-readable, for comparing runs),
``evaluation.md`` and ``precision_labels.csv``. Fill the ``relevant`` column
(y/n) of the CSV and pass it back with ``--labels`` to also get precision of
the accepted sources (``rescore`` re-scores a finished run without searching
again).

``verify`` checks every DOI / arXiv gold entry against OpenAlex (free single
lookups) so the gold set itself stays trustworthy.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import tomllib
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from afsim_research_agent.domain.doctrine import find_designations, normalize_designation
from afsim_research_agent.evidence.deduplicate import normalize_doi
from afsim_research_agent.literature.models import SourceCandidate, SourceCollection
from afsim_research_agent.literature.verification import title_similarity

_TITLE_MATCH = 0.85
_KEPT = {"accepted", "skipped_budget", "failed", "duplicate_content"}


@dataclass(frozen=True)
class GoldSource:
    label: str
    importance: str = "should"
    doi: str | None = None
    arxiv: str | None = None
    designation: str | None = None
    title: str | None = None
    url_contains: str | None = None
    verified: bool = False

    def matches(self, candidate: SourceCandidate) -> bool:
        meta = candidate.metadata
        if self.doi and normalize_doi(meta.doi) == normalize_doi(self.doi):
            return True
        if self.arxiv and (
            meta.external_ids.get("arxiv", "").split("v")[0] == self.arxiv
            or f"/{self.arxiv}" in candidate.url
        ):
            return True
        if self.designation:
            wanted = normalize_designation(self.designation)
            found = find_designations(f"{candidate.title} {candidate.url}")
            if wanted in found:
                return True
        if self.title and title_similarity(self.title, candidate.title) >= _TITLE_MATCH:
            return True
        return bool(
            self.url_contains
            and self.url_contains.lower() in (candidate.final_url or candidate.url).lower()
        )


@dataclass(frozen=True)
class EvaluationCase:
    id: str
    requirement: str
    gold: tuple[GoldSource, ...]


@dataclass
class GoldOutcome:
    label: str
    importance: str
    discovered: bool
    kept: bool
    accepted: bool
    candidate_ids: list[str] = field(default_factory=list)
    best_decision: str | None = None


@dataclass
class CaseResult:
    case_id: str
    requirement: str
    candidates: int
    accepted: int
    outcomes: list[GoldOutcome]
    precision: float | None = None
    labelled: int = 0

    def recall(self, stage: str, importance: str | None = None) -> float | None:
        items = [o for o in self.outcomes if importance is None or o.importance == importance]
        if not items:
            return None
        return sum(1 for o in items if getattr(o, stage)) / len(items)


def load_gold(path: Path) -> list[EvaluationCase]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    cases = []
    for item in data.get("case", []):
        gold = tuple(
            GoldSource(
                label=g["label"],
                importance=g.get("importance", "should"),
                doi=g.get("doi"),
                arxiv=g.get("arxiv"),
                designation=g.get("designation"),
                title=g.get("title"),
                url_contains=g.get("url_contains"),
                verified=bool(g.get("verified", False)),
            )
            for g in item.get("gold", [])
        )
        cases.append(EvaluationCase(id=item["id"], requirement=item["requirement"], gold=gold))
    return cases


def score_case(case: EvaluationCase, collection: SourceCollection) -> CaseResult:
    outcomes = []
    rank = {"accepted": 0, "duplicate_content": 1, "skipped_budget": 2, "failed": 3}
    for gold in case.gold:
        matched = [c for c in collection.candidates if gold.matches(c)]
        decisions = sorted((c.decision for c in matched), key=lambda d: rank.get(d, 9))
        outcomes.append(
            GoldOutcome(
                label=gold.label,
                importance=gold.importance,
                discovered=bool(matched),
                kept=any(c.decision in _KEPT for c in matched),
                accepted=any(c.decision in {"accepted", "duplicate_content"} for c in matched),
                candidate_ids=[c.candidate_id for c in matched],
                best_decision=decisions[0] if decisions else None,
            )
        )
    return CaseResult(
        case_id=case.id,
        requirement=case.requirement,
        candidates=len(collection.candidates),
        accepted=len(collection.sources),
        outcomes=outcomes,
    )


def apply_labels(results: list[CaseResult], labels_path: Path) -> None:
    labels: dict[str, list[bool]] = {}
    with labels_path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            value = (row.get("relevant") or "").strip().lower()
            if value in {"y", "yes", "1", "true", "n", "no", "0", "false"}:
                labels.setdefault(row["case_id"], []).append(value in {"y", "yes", "1", "true"})
    for result in results:
        judged = labels.get(result.case_id, [])
        result.labelled = len(judged)
        result.precision = sum(judged) / len(judged) if judged else None


def summarize(results: Sequence[CaseResult]) -> dict[str, float | None]:
    def mean(values: list[float | None]) -> float | None:
        present = [v for v in values if v is not None]
        return round(sum(present) / len(present), 3) if present else None

    summary: dict[str, float | None] = {}
    for stage in ("discovered", "kept", "accepted"):
        summary[f"recall_{stage}"] = mean([r.recall(stage) for r in results])
        summary[f"must_recall_{stage}"] = mean([r.recall(stage, "must") for r in results])
    summary["precision_accepted"] = mean([r.precision for r in results])
    summary["mean_candidates"] = mean([float(r.candidates) for r in results])
    summary["mean_accepted"] = mean([float(r.accepted) for r in results])
    return summary


def render_markdown(results: Sequence[CaseResult], summary: dict[str, float | None]) -> str:
    def pct(value: float | None) -> str:
        return "—" if value is None else f"{value * 100:.0f}%"

    lines = [
        "# Crow 检索评测",
        "",
        "| 指标 | 全部金标 | must 金标 |",
        "| --- | ---: | ---: |",
        f"| 发现召回率 | {pct(summary['recall_discovered'])} | "
        f"{pct(summary['must_recall_discovered'])} |",
        f"| 筛选后保留召回率 | {pct(summary['recall_kept'])} | "
        f"{pct(summary['must_recall_kept'])} |",
        f"| 采用召回率 | {pct(summary['recall_accepted'])} | "
        f"{pct(summary['must_recall_accepted'])} |",
        f"| 采用精确率（人工标注） | {pct(summary['precision_accepted'])} | |",
        "",
        "| 用例 | 候选 | 采用 | 发现 | 采用 | 未找到的 must 金标 |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for result in results:
        missing = [
            o.label for o in result.outcomes if o.importance == "must" and not o.accepted
        ]
        lines.append(
            f"| {result.case_id} | {result.candidates} | {result.accepted} | "
            f"{pct(result.recall('discovered'))} | {pct(result.recall('accepted'))} | "
            f"{'；'.join(missing) or '—'} |"
        )
    lines.extend(["", "## 金标明细", ""])
    for result in results:
        lines.append(f"### {result.case_id}：{result.requirement}")
        lines.append("")
        for outcome in result.outcomes:
            status = (
                "采用" if outcome.accepted else "保留未采用" if outcome.kept
                else "被筛除/拒绝" if outcome.discovered else "未发现"
            )
            ids = ", ".join(outcome.candidate_ids) or "—"
            lines.append(
                f"- [{outcome.importance}] {outcome.label} → **{status}**"
                f"（候选 {ids}；{outcome.best_decision or '—'}）"
            )
        lines.append("")
    return "\n".join(lines)


def write_label_sheet(path: Path, collections: dict[str, SourceCollection]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["case_id", "source_id", "tier", "title", "uri", "relevant"])
        for case_id, collection in collections.items():
            for source in collection.sources:
                writer.writerow(
                    [
                        case_id,
                        source.source_id,
                        source.quality.tier if source.quality else "",
                        source.title,
                        source.uri,
                        "",
                    ]
                )


async def _collect_case(case: EvaluationCase, output_dir: Path, max_sources: int):
    from afsim_research_agent.config import Settings
    from afsim_research_agent.crow.planner import TacticalResearchPlanner
    from afsim_research_agent.literature.collection import SourceCollector, build_default_services
    from afsim_research_agent.literature.models import SourceCollectionRequest

    settings = Settings.from_environment()
    plan = await TacticalResearchPlanner(app_settings=settings).plan(case.requirement)
    collector = SourceCollector(
        output_dir=output_dir / case.id,
        app_settings=settings,
        services=build_default_services(settings),
    )
    request = SourceCollectionRequest(
        requirement=case.requirement,
        search_queries=plan.search_queries,
        scholarly_queries=plan.scholarly_queries,
        doctrine_queries=plan.doctrine_queries,
        technical_report_queries=plan.technical_report_queries,
        chinese_queries=plan.chinese_queries,
        research_questions=plan.research_questions,
        max_web_results=3,
        max_total_sources=max_sources,
    )
    await asyncio.to_thread(collector.collect, request)
    return plan, collector.snapshot(case.requirement)


def run(args: argparse.Namespace) -> Path:
    cases = load_gold(args.gold)
    if args.cases:
        wanted = set(args.cases.split(","))
        cases = [case for case in cases if case.id in wanted]
    output_dir = args.output_dir or (
        Path("data/eval") / datetime.now().strftime("%Y%m%d-%H%M%S")
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[CaseResult] = []
    collections: dict[str, SourceCollection] = {}
    for case in cases:
        print(f"[{case.id}] {case.requirement}")
        plan, collection = asyncio.run(_collect_case(case, output_dir, args.max_sources))
        collections[case.id] = collection
        (output_dir / case.id).mkdir(parents=True, exist_ok=True)
        (output_dir / case.id / "collection.json").write_text(
            json.dumps(
                {"plan": plan.model_dump(), "collection": collection.model_dump(mode="json")},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        results.append(score_case(case, collection))
        result = results[-1]
        print(
            f"    candidates {result.candidates}, accepted {result.accepted}, "
            f"gold discovered {result.recall('discovered')}, accepted {result.recall('accepted')}"
        )
        from afsim_research_agent.crow.report import collection_diagnostics

        for line in collection_diagnostics(collection):
            print(line)
    if args.labels:
        apply_labels(results, args.labels)
    summary = summarize(results)
    (output_dir / "evaluation.json").write_text(
        json.dumps(
            {"gold": str(args.gold), "summary": summary, "cases": [asdict(r) for r in results]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (output_dir / "evaluation.md").write_text(render_markdown(results, summary), encoding="utf-8")
    write_label_sheet(output_dir / "precision_labels.csv", collections)
    print(json.dumps(summary, indent=2))
    print(f"Results: {output_dir}")
    return output_dir


def rescore(args: argparse.Namespace) -> dict[str, float | None]:
    """Re-score a finished run (e.g. after editing the gold set or filling labels)."""
    results: list[CaseResult] = []
    for case in load_gold(args.gold):
        path = args.run_dir / case.id / "collection.json"
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        results.append(score_case(case, SourceCollection.model_validate(payload["collection"])))
    if args.labels:
        apply_labels(results, args.labels)
    summary = summarize(results)
    (args.run_dir / "evaluation.md").write_text(
        render_markdown(results, summary), encoding="utf-8"
    )
    (args.run_dir / "evaluation.json").write_text(
        json.dumps(
            {"gold": str(args.gold), "summary": summary, "cases": [asdict(r) for r in results]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2))
    return summary


def verify(args: argparse.Namespace) -> int:
    from afsim_research_agent.web.scholarly import OpenAlexSearchProvider

    provider = OpenAlexSearchProvider.from_environment()
    problems = 0
    for case in load_gold(args.gold):
        for gold in case.gold:
            if not gold.doi:
                status = "skip (no DOI; verify manually)" if not gold.verified else "ok (manual)"
                print(f"[{case.id}] {gold.label}: {status}")
                continue
            try:
                record = provider.lookup_doi(normalize_doi(gold.doi) or gold.doi)
            except Exception as exc:  # noqa: BLE001
                print(f"[{case.id}] {gold.label}: lookup failed ({exc})")
                problems += 1
                continue
            if record is None:
                print(f"[{case.id}] {gold.label}: DOI NOT FOUND")
                problems += 1
                continue
            similarity = title_similarity(gold.label, record.title)
            flag = "ok" if similarity >= 0.4 or (gold.title and title_similarity(
                gold.title, record.title) >= _TITLE_MATCH) else "CHECK TITLE"
            if flag != "ok":
                problems += 1
            retracted = " RETRACTED" if record.metadata.is_retracted else ""
            print(f"[{case.id}] {gold.label}: {flag} -> {record.title!r}{retracted}")
    return problems


def main(argv: Sequence[str] | None = None) -> None:
    from dotenv import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(description="Evaluate Crow retrieval against a gold set.")
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--gold", type=Path, default=Path("eval/retrieval_gold.toml"))
    run_parser.add_argument("--cases", help="comma-separated case ids")
    run_parser.add_argument("--max-sources", type=int, default=12)
    run_parser.add_argument("--labels", type=Path, help="filled precision_labels.csv")
    run_parser.add_argument("--output-dir", type=Path)
    rescore_parser = sub.add_parser("rescore")
    rescore_parser.add_argument("--gold", type=Path, default=Path("eval/retrieval_gold.toml"))
    rescore_parser.add_argument("--run-dir", type=Path, required=True)
    rescore_parser.add_argument("--labels", type=Path)
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--gold", type=Path, default=Path("eval/retrieval_gold.toml"))
    args = parser.parse_args(argv)
    if args.command == "run":
        run(args)
    elif args.command == "rescore":
        rescore(args)
    else:
        raise SystemExit(1 if verify(args) else 0)


if __name__ == "__main__":
    main()
