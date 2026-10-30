"""F1: evidence normalization (design §4 F1).

PaperQA evidence is organized by research question and has no stable IDs.
This stage turns it into claims with content-derived IDs, verifies every
quote against the source excerpt, groups claims that state the same
proposition and records conflicts, then writes ``evidence_package.json``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict

from afsim_research_agent.crow.models import CrowResearchResult
from afsim_research_agent.evidence.merge import (
    canonical_unit,
    disjoint_pairs,
    interval,
    number_present,
)
from afsim_research_agent.falcon.llm import ModelRunner
from afsim_research_agent.falcon.models import (
    ClaimDraft,
    ClaimRecord,
    ConflictRecord,
    CrowRef,
    DroppedClaim,
    EvidenceItem,
    EvidencePackage,
    EvidenceSource,
    Excerpt,
    ExtractionDraft,
    GroupingDraft,
    Quantity,
)
from afsim_research_agent.falcon.prompts import extraction_prompt, grouping_prompt
from afsim_research_agent.falcon.text import _DASHES, squash  # noqa: F401 - re-exported

PRIVATE_KINDS = {"local_document", "local_code"}
TIER_SCORE = {"A": 0.85, "B": 0.65, "C": 0.45, "D": 0.2}
TOPIC_ORDER = (
    "formation",
    "spacing",
    "transition",
    "contact",
    "bounding",
    "communication",
    "speed",
    "terrain",
    "sensing",
    "other",
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _snake(name: str) -> str:
    out = re.sub(r"[^0-9a-zA-Z]+", "_", name.strip()).strip("_").lower()
    return out or "value"


# --------------------------------------------------------------------------- excerpts


def collect_excerpts(crow: CrowResearchResult, min_score: int) -> list[Excerpt]:
    questions = list(crow.plan.research_questions)
    merged: dict[str, Excerpt] = {}
    for r_index, report in enumerate(crow.reports):
        try:
            q_index = questions.index(report.question)
        except ValueError:
            q_index = r_index
        qid = f"q{q_index + 1}"
        for ev in report.evidence:
            if ev.score < min_score or not ev.excerpt.strip():
                continue
            source_id = ev.source_id or f"citation:{_sha(ev.citation)[:10]}"
            key = _sha(f"{source_id}|{ev.location}|{ev.excerpt}")
            excerpt_id = f"ex_{key[:10]}"
            item = merged.get(excerpt_id)
            if item is None:
                item = Excerpt(
                    excerpt_id=excerpt_id,
                    source_id=source_id,
                    location=ev.location,
                    excerpt=ev.excerpt,
                    citation=ev.citation,
                    score=ev.score,
                )
                merged[excerpt_id] = item
            item.score = max(item.score, ev.score)
            if qid not in item.questions:
                item.questions.append(qid)
    return sorted(merged.values(), key=lambda e: e.excerpt_id)


def collect_sources(crow: CrowResearchResult, excerpts: list[Excerpt]) -> dict[str, EvidenceSource]:
    known = {s.source_id: s for s in crow.collection.sources}
    out: dict[str, EvidenceSource] = {}
    for ex in excerpts:
        if ex.source_id in out:
            continue
        ref = known.get(ex.source_id)
        if ref is None:
            out[ex.source_id] = EvidenceSource(
                source_id=ex.source_id, title=ex.citation[:200] or ex.source_id
            )
            continue
        private = ref.kind in PRIVATE_KINDS
        tier = ref.quality.tier if ref.quality else None
        out[ex.source_id] = EvidenceSource(
            source_id=ref.source_id,
            title=ref.title,
            # never write local paths for private material
            locator="" if private else ref.uri,
            kind=ref.kind,
            quality_tier=tier if tier in ("A", "B", "C", "D") else None,
            quality_score=ref.quality.score if ref.quality else None,
            classification="user_private" if private else "public",
        )
    return out


# --------------------------------------------------------------------------- claim checks


def verify_claim(claim: ClaimDraft, excerpt: Excerpt) -> tuple[Quantity | None, str | None]:
    """Mechanical checks; returns (quantity, reason-if-rejected)."""
    quote = claim.quote.strip()
    if len(squash(quote)) < 6:
        return None, "quote too short"
    if squash(quote) not in squash(excerpt.excerpt):
        return None, "quote not found in excerpt"
    if not claim.statement.strip():
        return None, "empty statement"
    if claim.quantity is None:
        return None, None
    q = claim.quantity
    unit = canonical_unit(q.unit)
    if unit is None:
        return None, f"unit '{q.unit}' not recognized"
    low, high = sorted((q.min, q.max))
    for number in {low, high}:
        if not number_present(number, quote):
            return None, f"number {number:g} not in quote"
    symbol, dimension = unit
    return Quantity(name=_snake(q.name), min=low, max=high, unit=symbol, dimension=dimension), None


def evidence_id(source_id: str, location: str, quote: str, statement: str) -> str:
    return "ev_" + _sha(f"{source_id}|{location}|{squash(quote)}|{statement.strip()}")[:10]


def quality_of(source: EvidenceSource | None) -> float:
    if source is None:
        return 0.5
    if source.quality_score is not None:
        return round(float(source.quality_score), 3)
    return TIER_SCORE.get(source.quality_tier or "", 0.5)


# --------------------------------------------------------------------------- grouping


def _group_topic(
    topic: str, items: list[EvidenceItem], draft: GroupingDraft | None
) -> list[tuple[str, list[str], list[ConflictRecord]]]:
    ids = {item.evidence_id for item in items}
    by_id = {item.evidence_id: item for item in items}
    assigned: dict[str, int] = {}
    groups: list[tuple[str, list[str]]] = []
    if draft is not None:
        for group in draft.groups:
            members = [m for m in group.members if m in ids and m not in assigned]
            if not members:
                continue
            for m in members:
                assigned[m] = len(groups)
            groups.append((group.statement.strip() or by_id[members[0]].claim, members))
    for item in items:
        if item.evidence_id not in assigned:
            assigned[item.evidence_id] = len(groups)
            groups.append((item.claim, [item.evidence_id]))
    conflicts: dict[int, list[ConflictRecord]] = defaultdict(list)
    seen: set[frozenset[str]] = set()
    # deterministic rule: non-overlapping numeric ranges inside one proposition
    for index, (_, members) in enumerate(groups):
        ranged = []
        for m in members:
            q = by_id[m].quantity
            if q is not None:
                ranged.append((m, interval(q.min, q.max, q.unit, q.dimension)))
        for a, b in disjoint_pairs(ranged):
            qa, qb = by_id[a].quantity, by_id[b].quantity
            assert qa is not None and qb is not None
            reason = f"数值区间不重叠：{qa.min:g}–{qa.max:g} {qa.unit} 与 {qb.min:g}–{qb.max:g} {qb.unit}"
            conflicts[index].append(ConflictRecord(a=a, b=b, reason=reason, origin="numeric"))
            seen.add(frozenset((a, b)))
    if draft is not None:
        for c in draft.conflicts:
            pair = frozenset((c.a, c.b))
            if c.a == c.b or c.a not in ids or c.b not in ids or pair in seen:
                continue
            seen.add(pair)
            record = ConflictRecord(a=c.a, b=c.b, reason=c.reason.strip(), origin="model")
            for index in {assigned[c.a], assigned[c.b]}:
                conflicts[index].append(record)
    return [(stmt, sorted(members), conflicts[i]) for i, (stmt, members) in enumerate(groups)]


# --------------------------------------------------------------------------- main


async def normalize_evidence(
    crow: CrowResearchResult,
    requirement: str,
    run: ModelRunner,
    *,
    batch_size: int | None = None,
    min_score: int | None = None,
) -> EvidencePackage:
    batch_size = batch_size or int(os.getenv("FALCON_EXTRACT_BATCH", "6"))
    min_score = min_score if min_score is not None else int(os.getenv("FALCON_MIN_SCORE", "5"))
    excerpts = collect_excerpts(crow, min_score)
    sources = collect_sources(crow, excerpts)
    by_excerpt = {e.excerpt_id: e for e in excerpts}
    questions = list(crow.plan.research_questions)

    evidence: dict[str, EvidenceItem] = {}
    dropped: list[DroppedClaim] = []
    for start in range(0, len(excerpts), batch_size):
        batch = excerpts[start : start + batch_size]
        prompt = extraction_prompt(
            requirement,
            questions,
            [
                e.model_dump(include={"excerpt_id", "source_id", "location", "excerpt"})
                for e in batch
            ],
        )
        draft = await run("evidence.extract", prompt, ExtractionDraft)
        answered = set()
        for entry in draft.items:
            excerpt = by_excerpt.get(entry.excerpt_id)
            if excerpt is None or excerpt not in batch:
                continue
            answered.add(entry.excerpt_id)
            for claim in entry.claims:
                quantity, reason = verify_claim(claim, excerpt)
                if reason is not None:
                    dropped.append(
                        DroppedClaim(
                            excerpt_id=excerpt.excerpt_id,
                            statement=claim.statement[:200],
                            reason=reason,
                        )
                    )
                    continue
                eid = evidence_id(excerpt.source_id, excerpt.location, claim.quote, claim.statement)
                if eid in evidence:
                    continue
                source = sources.get(excerpt.source_id)
                evidence[eid] = EvidenceItem(
                    evidence_id=eid,
                    claim=claim.statement.strip(),
                    topic=claim.topic,
                    subject=claim.subject.strip(),
                    condition=claim.condition.strip(),
                    quantity=quantity,
                    quote=claim.quote.strip(),
                    excerpt_id=excerpt.excerpt_id,
                    excerpt=excerpt.excerpt,
                    source_id=excerpt.source_id,
                    location=excerpt.location,
                    relevance_score=round(excerpt.score / 10, 2),
                    quality_score=quality_of(source),
                    questions=list(excerpt.questions),
                )
        for excerpt in batch:
            if excerpt.excerpt_id not in answered:
                dropped.append(
                    DroppedClaim(excerpt_id=excerpt.excerpt_id, reason="excerpt not answered")
                )

    items = sorted(evidence.values(), key=lambda e: e.evidence_id)
    claims: list[ClaimRecord] = []
    grouped: list[tuple[str, str, list[str], list[ConflictRecord]]] = []
    for topic in TOPIC_ORDER:
        topic_items = [e for e in items if e.topic == topic]
        if not topic_items:
            continue
        draft = None
        if len(topic_items) > 1:
            prompt = grouping_prompt(
                topic,
                [
                    e.model_dump(
                        include={"evidence_id", "claim", "subject", "condition", "quantity"},
                        exclude_none=True,
                    )
                    for e in topic_items
                ],
            )
            draft = await run(f"evidence.group:{topic}", prompt, GroupingDraft)
        for statement, members, conflicts in _group_topic(topic, topic_items, draft):
            grouped.append((topic, statement, members, conflicts))
    grouped.sort(key=lambda g: (TOPIC_ORDER.index(g[0]), g[2][0]))
    for index, (topic, statement, members, conflicts) in enumerate(grouped, start=1):
        disputed = sorted({x for c in conflicts for x in (c.a, c.b) if x in members})
        claims.append(
            ClaimRecord(
                claim_id=f"cl_{index:03d}",
                statement=statement,
                topic=topic,
                members=members,
                supporting=[m for m in members if m not in disputed],
                conflicting=disputed,
                conflicts=conflicts,
            )
        )

    used_sources = {e.source_id for e in items}
    run_id = crow.manifest.run_id if crow.manifest else None
    readiness = crow.readiness
    package = EvidencePackage(
        package_id=f"falcon_{run_id or _sha(requirement)[:12]}",
        crow=CrowRef(
            run_id=run_id,
            gate_version=readiness.gate_version if readiness else "unknown",
            research_questions=questions,
        ),
        sources=sorted(
            (s for sid, s in sources.items() if sid in used_sources), key=lambda s: s.source_id
        ),
        evidence=items,
        claims=claims,
        dropped=sorted(dropped, key=lambda d: (d.excerpt_id, d.reason, d.statement)),
    )
    package.content_hash = package_hash(package)
    return package


def package_hash(package: EvidencePackage) -> str:
    data = package.model_dump(mode="json", exclude={"content_hash"})
    text = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + _sha(text)


def write_package(package: EvidencePackage, path) -> None:
    path.write_text(
        json.dumps(package.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
