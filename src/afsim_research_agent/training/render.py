"""S4 (design ``training-data-construction-v1`` §3): a (requirement → tactic sheet) pair as
the three Falcon v3 drafting prompts the online pipeline sends, each with the block(s) the
model should answer. The prompts come from the same functions and the same Drafter state as
``run_draft3``, without any model call:

- clauses are the generated model's facts (online they are the model's decomposition of the
  requirement text, so these are the clean version of the same thing);
- structure: requirement, clauses, capability index, roles, scenario interface; no
  obligations yet (online they are written after the structure stage) → ``yaml model`` +
  ``mermaid <role>``;
- control: the structure blocks of the gold sheet, the capability details of its slices, the
  obligations → ``python conditions`` + ``python <role>``;
- verification: the gold sheet without verification and its compile summary →
  ``yaml verification``.

Each target is checked by merging it back the way ``Drafter.ask`` does: the merged sheet
must equal the gold one, block for block. The scenario binding is the interface default (no
kit), so no local binding value reaches the data."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import yaml

from afsim_research_agent.falcon.llm import ModelRunner
from afsim_research_agent.falcon.sheet.blocks import Block, split_sheet
from afsim_research_agent.falcon.sheet.generate import merge
from afsim_research_agent.falcon.v3 import catalog as K
from afsim_research_agent.falcon.v3 import prompts as P
from afsim_research_agent.falcon.v3.draft import (
    CONTROL_KINDS,
    STRUCTURE_KINDS,
    Drafter,
    V3Request,
    _NoModel,
    with_interface,
)
from afsim_research_agent.ir.v2 import Contracts

TASKS = ("structure", "control", "verification")


@dataclass
class Sample:
    task: str  # structure | control | verification
    prompt: str
    target: str
    ok: bool
    problems: list[str]


def _fences(blocks: list[Block], kinds: tuple[str, ...]) -> str:
    return "\n\n".join(b.fence() for b in blocks if b.kind in kinds)


def _same(a: list[Block], b: list[Block]) -> list[str]:
    ka = {x.key: x.text.strip() for x in a}
    kb = {x.key: x.text.strip() for x in b}
    out = [f"缺少块 {k}" for k in kb if k not in ka]
    out += [f"多出块 {k}" for k in ka if k not in kb]
    out += [f"块 {k} 内容不同" for k in ka if k in kb and ka[k] != kb[k]]
    return out


def drafter(request: dict[str, Any], requirement: str, facts: list[str]) -> Drafter:
    """A Drafter in the state ``run_draft3`` reaches before the structure stage."""
    req = V3Request.model_validate({**request, "requirement": requirement})
    req, _notes = with_interface(req)
    d = Drafter(req, Contracts.load(req.capability_set), ModelRunner(_NoModel()))
    d.clauses = [
        {"id": f"rq_{i:02d}", "text": f, "kind": "behavior"} for i, f in enumerate(facts, 1)
    ]
    return d


def render(sheet: str, request: dict[str, Any], requirement: str, facts: list[str]) -> list[Sample]:
    gold = split_sheet(sheet)[0]
    d = drafter(request, requirement, facts)
    out: list[Sample] = []

    # structure: before the obligations exist
    classes = {v.capability_class for v in d.r.roles.values()} or None
    prompt = P.structure_prompt(
        d.r.requirement,
        d.clauses,
        K.index(d.c, classes),
        {k: v.model_dump() for k, v in d.r.roles.items()},
        {k: v.model_dump() for k, v in d.r.scenario_parameters.items()},
        d.r.allow_mission_ref,
        state_names=d.r.state_names,
        scenario_events=d.r.scenario_events,
        policy=d.r.reference_policy,
        obligations=d.obligations_text,
        interface=d.scenario_text(),
    )
    target = _fences(gold, STRUCTURE_KINDS)
    d.blocks = []
    merge(d.blocks, target, STRUCTURE_KINDS)
    probs = _same(d.blocks, [b for b in gold if b.kind in STRUCTURE_KINDS])
    out.append(Sample("structure", prompt, target, not probs, probs))

    # control: the gold structure, its slices, the obligations
    d.blocks = [b for b in gold if b.kind in STRUCTURE_KINDS]
    structure = d.check(pending=True)
    d.slices = d._slices(structure.compiled)
    asyncio.run(d.obligations_stage())
    spec = structure.compiled.spec or {}
    prompt = P.control_prompt(
        d.r.requirement,
        d.clauses,
        d.sheet,
        K.details(d.c, d.slices, d.r.reference_policy),
        [r["id"] for r in spec.get("roles", [])],
        d._states(structure.compiled),
        pending=d._pending(structure.compiled),
        policy=d.r.reference_policy,
        obligations=d.obligations_text,
        interface=d.scenario_text(),
        mission_refs=[r for r, m in (spec.get("missions") or {}).items() if "tactic" in m],
    )
    target = _fences(gold, CONTROL_KINDS)
    merge(d.blocks, target, CONTROL_KINDS)
    probs = _same(d.blocks, [b for b in gold if b.kind != "verification"])
    control = d.check()
    if not control.l1_ok:
        probs.append("控制稿合入后 L1 未通过")
    out.append(Sample("control", prompt, target, not probs, probs))

    # verification: the gold sheet without it, and its compile summary
    prompt = P.verification_prompt(
        d.r.requirement,
        d.sheet,
        d._summary(control),
        d.obligations_text,
        interface=d.scenario_text(control.model),
    )
    target = _fences(gold, ("verification",))
    merge(d.blocks, target, ("verification",), target=Block("verification", None, "", 0))
    probs = _same(d.blocks, gold)
    final = d.check(verify=True)
    if not final.verified:
        probs.append("验证块合入后 L2/L3/约束未全部通过")
    out.append(Sample("verification", prompt, target, not probs, probs))
    return out


def render_files(sheet_text: str, request_yaml: str, requirement: str, facts: list[str]):
    return render(sheet_text, yaml.safe_load(request_yaml), requirement, facts)
