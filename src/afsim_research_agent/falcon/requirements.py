"""F2: requirement decomposition into verbatim clauses and user-stated values."""

from __future__ import annotations

from afsim_research_agent.evidence.merge import canonical_unit, number_present
from afsim_research_agent.falcon.errors import FalconError
from afsim_research_agent.falcon.llm import ModelRunner
from afsim_research_agent.falcon.models import (
    FalconRequest,
    RequirementClause,
    RequirementDraft,
    UserValueDraft,
    UserValueRecord,
)
from afsim_research_agent.falcon.prompts import requirements_prompt
from afsim_research_agent.falcon.text import squash
from afsim_research_agent.ir.units import to_canonical


def _check(draft: RequirementDraft, requirement: str) -> tuple[list, list[str]]:
    good, bad = [], []
    body = squash(requirement)
    for clause in draft.clauses:
        text = clause.text.strip()
        if text and squash(text) in body:
            good.append(clause)
        else:
            bad.append(text)
    return good, bad


def user_value_record(
    name: str, value: float | int | bool | str, unit: str | None, origin: str
) -> UserValueRecord:
    dimension = canonical = None
    if unit:
        known = canonical_unit(unit)
        if known is not None and isinstance(value, int | float) and not isinstance(value, bool):
            unit, dimension = known
            canonical = to_canonical(float(value), unit, dimension)
    return UserValueRecord(
        name=name, value=value, unit=unit, dimension=dimension, canonical=canonical, origin=origin
    )


async def decompose(
    request: FalconRequest, run: ModelRunner
) -> tuple[list[RequirementClause], list[UserValueRecord], list[str]]:
    notes: list[str] = []
    platforms = [p.model_dump() for p in request.platforms]
    draft = await run(
        "requirements", requirements_prompt(request.requirement, platforms), RequirementDraft
    )
    good, bad = _check(draft, request.requirement)
    if bad:
        feedback = "\n上一次输出中以下 text 不是需求原文的连续片段，请改为逐字摘录：\n" + "\n".join(
            f"- {t}" for t in bad
        )
        draft = await run(
            "requirements",
            requirements_prompt(request.requirement, platforms, feedback),
            RequirementDraft,
        )
        good, bad = _check(draft, request.requirement)
        notes += [f"条款被丢弃（不是需求原文片段）：{t}" for t in bad]
    if not good:
        raise FalconError("FAL004", "requirement decomposition produced no verbatim clauses")

    clauses: list[RequirementClause] = []
    values: list[UserValueRecord] = []
    for index, clause in enumerate(good, start=1):
        cid = f"rq_{index:02d}"
        kept: list[UserValueDraft] = []
        for uv in clause.user_values:
            if not number_present(uv.value, clause.text):
                notes.append(f"{cid}：用户数值 {uv.name}={uv.value:g} 不在条款原文中，已忽略")
                continue
            if uv.unit and canonical_unit(uv.unit) is None:
                notes.append(f"{cid}：用户数值 {uv.name} 的单位 '{uv.unit}' 无法识别，已忽略")
                continue
            kept.append(uv)
            values.append(user_value_record(uv.name, uv.value, uv.unit, cid))
        clauses.append(
            RequirementClause(
                id=cid,
                text=clause.text.strip(),
                kind=clause.kind,
                roles=clause.roles,
                user_values=kept,
                reason=clause.reason,
            )
        )
    for name, item in request.user_parameters.items():
        values.append(user_value_record(name, item.value, item.unit, "falcon_request"))
    return clauses, values, notes
