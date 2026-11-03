"""Executable version of a Crow tactic (Falcon v3 stage ``v3:simplify``).

Crow's tactics come from doctrine and red-team revisions and often name means the platform
does not have (wired links, smoke, optics, read-back confirmation, …). Drafting the whole
tactic then fails on the capability check. Here the selected tactic is reduced to what the
existing actions and tactic-set subtrees can carry:

- every step / exception / formation of the executable version names the actions or
  subtrees it uses (checked against the capability set) and the full-tactic elements it
  comes from;
- the parts of the full tactic left out are listed with what is missing; the full tactic
  is kept in the model package (``full_tactic.yaml``) and its deferred parts in
  ``deferred_capabilities.yaml``, to be drafted again once the capability work exists;
- the user's requirement and constraints stay whole: the reduction may leave out means,
  not the task.

The executable version replaces the cards of the selection, so the clauses, the
capability check and the drafting downstream all work on it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic import BaseModel, Field

STAGE = "v3:simplify"
ATTEMPTS = 2


class SimpleStep(BaseModel):
    phase: str
    do: str
    until: str = ""
    uses: list[str] = Field(default_factory=list, description="动作或战术集子树 ID")
    derived_from: list[str] = Field(default_factory=list, description="完整战术的要素编号")


class SimpleException(BaseModel):
    situation: str
    then: str
    uses: list[str] = Field(default_factory=list)
    derived_from: list[str] = Field(default_factory=list)


class SimpleGeometry(BaseModel):
    formation: str
    when: str = ""
    spacing: str = ""
    uses: list[str] = Field(default_factory=list)
    derived_from: list[str] = Field(default_factory=list)


class DeferredItem(BaseModel):
    element: str = Field(description="完整战术的要素编号，如 tc_01_s2")
    what: str = Field(description="暂不实现的内容")
    missing: str = Field(default="", description="缺少的原子能力")


class SimplifyAnswer(BaseModel):
    summary: str = Field(description="可执行版本的一段话概述")
    geometry: list[SimpleGeometry] = Field(default_factory=list)
    steps: list[SimpleStep] = Field(default_factory=list)
    exceptions: list[SimpleException] = Field(default_factory=list)
    deferred: list[DeferredItem] = Field(default_factory=list)
    kept_constraints: list[str] = Field(default_factory=list, description="需求中的约束，逐条原文")


GUIDE = """把选定的完整战术化简成"现有能力集就能执行"的版本。

原则：
- 保留需求要完成的任务和全部约束（如"未经命令不得开火""与敌保持安全距离"）；化简去掉的是手段，
  不是任务；
- 每个步骤、例外、队形都写出用到的动作或战术集子树（uses，只能用下面列出的 ID），以及来自完整
  战术的哪些要素（derived_from，写要素编号）；
- 完整战术里需要能力集没有的手段（如有线通信、烟幕、光学设备、伪装、复诵确认、特定协同消息、
  角色交换等）的部分，不写进可执行版本，列入 deferred：写要素编号、暂不实现的内容、缺少的原子能力；
  一个要素只有部分能做时，能做的部分写进可执行版本，做不了的部分列入 deferred；
- 步骤、例外的文字只写 uses 中的动作能做到的事：做不到的细节（如"被动接收模式""光学设备"
  "向指挥所报告""伪装""烟幕"）从文字里删掉，写进 deferred，不要留在步骤里；
- 例外只保留触发情况能用能力集判断的（如目标丢失、外部命令、距离过近）；"遭直射火力""位置暴露"
  这类能力集判断不了的情况列入 deferred；需求本身没有要求的撤离、返回等后续阶段也不必保留；
- 不引入完整战术和需求里都没有的新手段；
- 尽量简单：步骤宜少（通常 2–5 步）；能用一个动作完成的不用组合子树；
- 看能力标签选：[需≥N车] 的（编队、协同类）只能给车辆数够的角色用，单车不能用；
  "直到到达/走完/停稳"这类靠完成进入下一步的步骤，只用 [自行结束] 的动作或子树
  （如前出用 movement.follow_route，不用边走边搜的编队子树）；
- 步骤的 until 写可观察的结束条件，优先用"动作完成""路线走完""目标被选定"这类由动作给出的
  结果，不要写需要额外感知能力才能判断的条件；
- 战术中的数值（距离、间距、速度、时间）原样保留在文字里，供起草时设成参数；
- kept_constraints 逐条抄写需求中的约束。"""


def full_elements(tactic: Any) -> dict[str, str]:
    """Element id → text of the selected parts of the full tactic."""
    out: dict[str, str] = {}
    for part in tactic.parts:
        card = part.card
        for index, step in part.steps():
            until = f"，直到{step.until}" if getattr(step, "until", "") else ""
            out[f"{card.id}_s{index + 1}"] = f"【{step.phase}】{step.do}{until}"
        if not part.whole:
            continue
        for index, g in enumerate(card.geometry):
            when = f"（{g.when}）" if g.when else ""
            out[f"{card.id}_g{index + 1}"] = f"队形：{g.formation}{when}" + (
                f"，间距{g.spacing}" if g.spacing else ""
            )
        for index, x in enumerate(card.exceptions):
            out[f"{card.id}_x{index + 1}"] = f"例外：若{x.situation}：{x.then}"
    return out


def capability_text(contracts: Any, action_list: str) -> str:
    from afsim_research_agent.falcon.v3.catalog import card_text

    """Actions (with descriptions) and tactic-set subtrees (with their public parameters)."""
    subs = []
    for t in sorted(contracts.tactics.values(), key=lambda x: x.id):
        if t.section != "subtrees":
            continue
        desc = str(t.raw.get("description") or "")[:80]
        params = ", ".join(t.parameters)
        subs.append(
            f"- {t.id} {card_text(contracts, t.id)} — {desc}"
            + (f"（参数 {params}）" if params else "")
        )
    return (
        "## 动作\n"
        + action_list
        + "\n\n## 战术集子树（[单车可用/需≥N车][是否自行结束]）\n"
        + ("\n".join(subs) or "（无）")
    )


def prompt(requirement: str, tactic: Any, capabilities: str, problems: list[str]) -> str:
    elements = "\n".join(f"- {k}：{v}" for k, v in full_elements(tactic).items())
    names = "、".join(p.card.name for p in tactic.parts)
    roles = "；".join(
        f"{r.role}" + (f"（{'、'.join(r.members)}）" if r.members else "")
        for p in tactic.parts
        for r in p.card.roles
    )
    fix = ""
    if problems:
        fix = "\n\n## 上一次输出的问题（修正后完整重新输出）\n" + "\n".join(
            f"- {p}" for p in problems
        )
    return f"""{GUIDE}

## 需求
{requirement}

## 选定的完整战术：{names}
角色：{roles or "（未给出）"}
{elements}

## 现有能力集
{capabilities}{fix}

按给定的结构输出。"""


def check(answer: SimplifyAnswer, contracts: Any, elements: dict[str, str]) -> list[str]:
    """Problems that make the executable version unusable (fed back once)."""
    known = set(contracts.actions) | set(contracts.tactics)
    problems: list[str] = []
    if not answer.steps:
        problems.append("可执行版本至少要有一个步骤")
    items: list[tuple[str, Any]] = [(f"步骤 {i + 1}", s) for i, s in enumerate(answer.steps)]
    items += [(f"例外 {i + 1}", x) for i, x in enumerate(answer.exceptions)]
    items += [(f"队形 {i + 1}", g) for i, g in enumerate(answer.geometry)]
    for label, item in items:
        unknown = [u for u in item.uses if u not in known]
        if unknown:
            problems.append(f"{label} 用到能力集中没有的 ID：{', '.join(unknown)}")
        if not [u for u in item.uses if u in known] and label[:2] != "队形":
            problems.append(f"{label} 没有写用到的动作或子树（uses）")
        bad = [e for e in item.derived_from if e not in elements]
        if bad:
            problems.append(f"{label} 的 derived_from 不是完整战术的要素编号：{', '.join(bad)}")
    for d in answer.deferred:
        if d.element not in elements:
            problems.append(f"deferred 的要素编号 {d.element} 不在完整战术中")
    return problems


@dataclass
class Simplified:
    tactic: Any  # the selection with the executable cards
    full: Any  # the selection as Crow wrote it
    answer: SimplifyAnswer
    deferred: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _evidence(card: Any, refs: list[str]) -> tuple[list[str], str]:
    """Evidence and basis of an executable element: those of the full elements it comes
    from (inferred when it comes from none)."""
    evidence: list[str] = []
    kinds = {"s": "steps", "g": "geometry", "x": "exceptions"}
    basis = "inferred"
    for ref in refs:
        m = re.fullmatch(rf"{re.escape(card.id)}_([sgx])(\d+)", ref)
        if not m:
            continue
        items = getattr(card, kinds[m.group(1)])
        index = int(m.group(2)) - 1
        if 0 <= index < len(items):
            item = items[index]
            evidence += [e for e in item.evidence if e not in evidence]
            if item.basis == "evidence":
                basis = "evidence"
    return evidence, basis


def build_card(card: Any, answer: SimplifyAnswer) -> Any:
    """The executable card: same id and roles, the executable elements, capability needs
    that point at the actions each element uses (all available)."""
    from afsim_research_agent.crow.proposal_models import (
        CapabilityNeed,
        ExceptionItem,
        FeasibilityInfo,
        GeometryItem,
        Step,
    )

    def mine(refs: list[str]) -> list[str]:
        return [r for r in refs if r.startswith(card.id + "_")]

    steps, exceptions, geometry, needs = [], [], [], []
    for i, s in enumerate(answer.steps):
        ev, basis = _evidence(card, mine(s.derived_from))
        steps.append(Step(phase=s.phase, do=s.do, until=s.until, evidence=ev, basis=basis))
        needs.append(
            CapabilityNeed(
                need=s.do[:60], source=f"steps[{i}].do", maps_to=list(s.uses), status="ready"
            )
        )
    for i, x in enumerate(answer.exceptions):
        ev, basis = _evidence(card, mine(x.derived_from))
        exceptions.append(
            ExceptionItem(situation=x.situation, then=x.then, evidence=ev, basis=basis)
        )
        if x.uses:
            needs.append(
                CapabilityNeed(
                    need=x.then[:60],
                    source=f"exceptions[{i}].then",
                    maps_to=list(x.uses),
                    status="ready",
                )
            )
    for i, g in enumerate(answer.geometry):
        ev, basis = _evidence(card, mine(g.derived_from))
        geometry.append(
            GeometryItem(
                formation=g.formation, when=g.when, spacing=g.spacing, evidence=ev, basis=basis
            )
        )
        if g.uses:
            needs.append(
                CapabilityNeed(
                    need=g.formation[:60],
                    source=f"geometry[{i}].formation",
                    maps_to=list(g.uses),
                    status="ready",
                )
            )
    return card.model_copy(
        update={
            "name": f"{card.name}（可执行版）",
            "narrative": answer.summary,
            "summary": answer.summary,
            "steps": steps,
            "exceptions": exceptions,
            "geometry": geometry,
            "capability_needs": needs,
            "feasibility": FeasibilityInfo(level="ready", counts={"ready": len(needs)}),
            "review_flags": [
                *card.review_flags,
                "可执行版：由完整战术按现有能力集化简，暂不实现的部分见 deferred_capabilities.yaml",
            ],
        }
    )


def apply(tactic: Any, answer: SimplifyAnswer) -> Simplified:
    """The selection with every part replaced by its executable card (one answer covers
    all parts; elements are attributed by their card id). Full elements neither carried
    nor listed are deferred with an empty reason, so nothing is dropped silently."""
    elements = full_elements(tactic)
    parts = []
    for part in tactic.parts:
        card = part.card
        sub = SimplifyAnswer(
            summary=answer.summary,
            steps=[s for s in answer.steps if _belongs(s.derived_from, card.id, tactic)],
            exceptions=[x for x in answer.exceptions if _belongs(x.derived_from, card.id, tactic)],
            geometry=[g for g in answer.geometry if _belongs(g.derived_from, card.id, tactic)],
        )
        parts.append(replace(part, card=build_card(card, sub), phases=None))
    used = {
        e for s in [*answer.steps, *answer.exceptions, *answer.geometry] for e in s.derived_from
    }
    deferred = [d.model_dump() for d in answer.deferred]
    listed = {d["element"] for d in deferred}
    for eid in elements:
        if eid not in used and eid not in listed:
            deferred.append(
                {"element": eid, "what": elements[eid], "missing": "（化简时未保留，未说明原因）"}
            )
    notes = [
        f"选定战术已化简为可执行版：{len(answer.steps)} 个步骤、{len(answer.exceptions)} 个例外、"
        f"{len(answer.geometry)} 个队形；完整战术的 {len(deferred)} 个要素（或其部分）暂不实现，"
        "待原子能力补齐后按完整战术重新起草"
    ]
    simple = replace(tactic, parts=parts)
    return Simplified(simple, tactic, answer, deferred, notes)


def _belongs(refs: list[str], card_id: str, tactic: Any) -> bool:
    """An executable element goes to the card its sources name (the first card when it
    names none)."""
    owners = {r.rsplit("_", 1)[0] for r in refs}
    if owners:
        return card_id in owners
    return tactic.parts[0].card.id == card_id


async def simplify(
    run: Any, tactic: Any, contracts: Any, requirement: str, actions: str
) -> Simplified:
    """One model call (plus one corrected retry) for the executable version."""
    elements = full_elements(tactic)
    capabilities = capability_text(contracts, actions)
    problems: list[str] = []
    answer: SimplifyAnswer | None = None
    for _ in range(ATTEMPTS):
        answer = await run(
            STAGE, prompt(requirement, tactic, capabilities, problems), SimplifyAnswer
        )
        problems = check(answer, contracts, elements)
        if not problems:
            break
    assert answer is not None
    known = set(contracts.actions) | set(contracts.tactics)
    for item in [*answer.steps, *answer.exceptions, *answer.geometry]:
        item.uses = [u for u in item.uses if u in known]
        item.derived_from = [e for e in item.derived_from if e in elements]
    answer.deferred = [d for d in answer.deferred if d.element in elements]
    out = apply(tactic, answer)
    if problems:
        out.notes.append("化简结果仍有问题（已去掉无效引用后继续）：" + "；".join(problems[:5]))
    return out


def full_document(simplified: Simplified) -> str:
    """The full tactic as Crow wrote it (kept for drafting it again later)."""
    return simplified.full.selected_document()


def deferred_section(simplified: Simplified) -> dict[str, Any]:
    return {
        "executable_version": {
            "meaning": "可执行版按现有能力集化简；以下完整战术要素暂不实现，原子能力补齐后按完整战术重新起草",
            "full_tactic": "full_tactic.yaml",
            "deferred_elements": simplified.deferred,
        }
    }
