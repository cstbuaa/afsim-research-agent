"""F3: model skeleton (roles, states, coordination choices) with coverage checks."""

from __future__ import annotations

import re
from typing import Any

from afsim_research_agent.falcon.llm import ModelRunner
from afsim_research_agent.falcon.models import (
    EvidencePackage,
    FalconRequest,
    OutOfScope,
    RequirementClause,
    SkeletonDraft,
)
from afsim_research_agent.falcon.prompts import skeleton_prompt
from afsim_research_agent.ir.catalog import Catalog
from afsim_research_agent.ir.validate import _vocabulary_concepts

_ID = re.compile(r"^[a-z][a-z0-9_]*$")


def compact_evidence(
    package: EvidencePackage, topics: set[str] | None = None, limit: int = 80
) -> list[dict]:
    tiers = {s.source_id: s.quality_tier for s in package.sources}
    disputed = {x for pair in package.conflict_pairs() for x in pair}
    out = []
    for item in package.evidence:
        if topics is not None and item.topic not in topics:
            continue
        entry = {"evidence_id": item.evidence_id, "claim": item.claim, "topic": item.topic}
        if item.quantity is not None:
            q = item.quantity
            entry["quantity"] = (
                f"{q.min:g} {q.unit}" if q.min == q.max else f"{q.min:g}–{q.max:g} {q.unit}"
            )
        if tiers.get(item.source_id):
            entry["tier"] = tiers[item.source_id]
        if item.evidence_id in disputed:
            entry["conflict"] = True
        out.append(entry)
    return out[:limit]


def implements_map(skeleton: SkeletonDraft) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for role in skeleton.roles:
        out[role.id] = list(role.implements)
        for state in role.states:
            out[f"{role.id}.{state.id}"] = list(state.implements)
    for group in (skeleton.formations, skeleton.coordination, skeleton.messages):
        for item in group:
            out[item.id] = list(item.implements)
    return out


def clause_data(clauses: list[RequirementClause], fields: set[str]) -> list[dict[str, Any]]:
    """Clauses for prompts; tactic clauses add their origin and evidence."""
    out = []
    for c in clauses:
        entry = c.model_dump(include=fields)
        if c.origin == "tactic":
            entry["origin"] = "tactic"
            if c.evidence:
                entry["evidence"] = list(c.evidence)
            if c.tactic_basis and c.tactic_basis != "evidence":
                entry["tactic_basis"] = c.tactic_basis
        out.append(entry)
    return out


SYNC_PATTERNS = ("leader_follower", "synchronized_transition")


def check_skeleton(
    skeleton: SkeletonDraft,
    clauses: list[RequirementClause],
    catalog: Catalog,
    capabilities: set[str] | None,
) -> tuple[list[str], list[str], list[str]]:
    """Returns (problems, uncovered clause ids, notes). Mutates harmless fixes in place."""
    problems: list[str] = []
    notes: list[str] = []
    clause_ids = {c.id for c in clauses}
    # harmless fixes: drop unknown clause ids and unknown concepts
    for group in (skeleton.roles, skeleton.formations, skeleton.coordination, skeleton.messages):
        for item in group:
            item.implements = [c for c in item.implements if c in clause_ids]
    for role in skeleton.roles:
        for state in role.states:
            state.implements = [c for c in state.implements if c in clause_ids]
    concepts = _vocabulary_concepts()
    if concepts:
        dropped = [c for c in skeleton.domain_concepts if c not in concepts]
        skeleton.domain_concepts = [c for c in skeleton.domain_concepts if c in concepts]
        if dropped:
            notes.append(f"未知领域概念已移除：{dropped}")
    if skeleton.intent not in catalog.vocabularies["intents"]:
        problems.append(f"intent '{skeleton.intent}' 不在意图词表中")

    ids: set[str] = set()
    for role in skeleton.roles:
        if not _ID.match(role.id) or "__" in role.id:
            problems.append(f"角色 id '{role.id}' 不合法（英文蛇形，不含 __）")
        if role.id in ids:
            problems.append(f"角色 id '{role.id}' 重复")
        ids.add(role.id)
        if skeleton.architecture != "behavior_tree":
            states = [s.id for s in role.states]
            if not states:
                problems.append(f"角色 '{role.id}' 没有状态")
            elif role.initial_state not in states:
                if role.initial_state is None:
                    role.initial_state = states[0]
                else:
                    problems.append(
                        f"角色 '{role.id}' 的 initial_state '{role.initial_state}' 不是其状态之一"
                    )
            if len(set(states)) != len(states):
                problems.append(f"角色 '{role.id}' 的状态 id 重复")
        elif not role.root_tree:
            problems.append(f"角色 '{role.id}' 缺少 root_tree")
        if capabilities is not None:
            needed = [f"platform:{role.capability_class}"]
            needed += [role.mover_capability] if role.mover_capability else []
            needed += [c.capability for c in (*role.sensors, *role.comms, *role.weapons)]
            missing = [c for c in needed if c not in capabilities]
            if missing:
                problems.append(f"角色 '{role.id}' 依赖的能力目标引擎不支持：{missing}")
    for role in skeleton.roles:
        if role.superior and role.superior not in ids:
            problems.append(f"角色 '{role.id}' 的上级 '{role.superior}' 未定义")
    for coord in skeleton.coordination:
        if coord.pattern not in catalog.patterns:
            problems.append(
                f"协同 '{coord.id}' 的模式 '{coord.pattern}' 不在目录中（可选：{sorted(catalog.patterns)}）"
            )
    if skeleton.architecture != "behavior_tree" and any(
        c.pattern in SYNC_PATTERNS for c in skeleton.coordination
    ):
        states = {r.id: {s.id for s in r.states} for r in skeleton.roles}
        for role in skeleton.roles:
            if role.superior in states and not (states[role.id] & states[role.superior]):
                problems.append(
                    f"角色 '{role.id}' 与上级 '{role.superior}' 没有同名的阶段状态；使用 leader_follower/"
                    "synchronized_transition 时，同一阶段在两个角色中应使用相同的状态 id"
                )
    covered = {c for refs in implements_map(skeleton).values() for c in refs}
    excused = {o.clause_id for o in skeleton.out_of_scope if o.reason.strip()}
    uncovered = [
        c.id
        for c in clauses
        if c.kind != "out_of_scope" and c.id not in covered and c.id not in excused
    ]
    return problems, uncovered, notes


async def build_skeleton(
    request: FalconRequest,
    clauses: list[RequirementClause],
    package: EvidencePackage,
    catalog: Catalog,
    capabilities: set[str] | None,
    run: ModelRunner,
    tactic_block: str = "",
) -> tuple[SkeletonDraft, list[str], list[str]]:
    clause_rows = clause_data(clauses, {"id", "text", "kind", "roles", "reason"})
    platforms = [p.model_dump() for p in request.platforms]
    terrain = request.terrain_classes or list(catalog.vocabularies["terrain_classes"])
    concepts = sorted(_vocabulary_concepts())
    claims = compact_evidence(package, limit=60)

    def prompt(feedback: str = "") -> str:
        return skeleton_prompt(
            request.requirement,
            clause_rows,
            platforms,
            terrain,
            catalog,
            list(catalog.vocabularies["intents"]),
            concepts,
            claims,
            sorted(capabilities) if capabilities is not None else None,
            feedback,
            tactic_block,
        )

    skeleton = await run("skeleton", prompt(), SkeletonDraft)
    problems, uncovered, notes = check_skeleton(skeleton, clauses, catalog, capabilities)
    if problems or uncovered:
        lines = [f"- {p}" for p in problems]
        lines += [
            f"- 条款 {c} 未被任何元素实现（或写入 out_of_scope 并说明原因）" for c in uncovered
        ]
        skeleton = await run(
            "skeleton",
            prompt("\n上一次骨架存在以下问题，请修正后完整重新输出：\n" + "\n".join(lines)),
            SkeletonDraft,
        )
        problems, uncovered, more = check_skeleton(skeleton, clauses, catalog, capabilities)
        notes += more
        notes += [f"骨架问题未解决：{p}" for p in problems]
    # clauses the decomposition already marked out of scope keep their reason
    for clause in clauses:
        if clause.kind == "out_of_scope" and not any(
            o.clause_id == clause.id for o in skeleton.out_of_scope
        ):
            skeleton.out_of_scope.append(
                OutOfScope(
                    clause_id=clause.id, reason=clause.reason or "需求拆解阶段判定为无法建模"
                )
            )
    return skeleton, uncovered, notes
