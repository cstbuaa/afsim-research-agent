"""C5 red team: adversarial review of candidate tactics and the proposer's response.

Design crow-tactic-proposal-v1 §6.2:

1. **Red team** (``CROW_REDTEAM_MODEL``, a different model family from the proposer, since
   models favour their own output): sees the mission profile and the *facts* of a candidate
   — applicability, roles, geometry, steps, exceptions, decision authority, platform
   capability needs — but not the proposer's narrative, reasoning, assumptions or risks, so
   it is not argued into agreement. It returns critical assumptions, the enemy's most likely
   counter-tactics, failure conditions, capability doubts and an overall judgment.
2. **Response** (the proposer model): for every counter-tactic, failure condition,
   high-fragility assumption and capability doubt, either revise the tactic (with explicit
   change operations) or say why it is kept. Revisions produce a new revision of the card;
   changed elements carry no evidence (basis ``inferred``) and are flagged for review.
3. **No verdict by a model.** Both sides are shown to the user (``tactic_report.md``),
   who selects (``crow.select``).

C5 never removes or re-ranks candidates.
"""

from __future__ import annotations

import json
import re
from typing import Any

from afsim_research_agent.crow.mission import MissionProfile, profile_context
from afsim_research_agent.crow.proposal import (
    ProposalSettings,
    _support,
    assess_completeness,
)
from afsim_research_agent.crow.proposal_models import (
    SECTIONS,
    ConstraintCheck,
    ExceptionItem,
    ProposalResult,
    RedTeamDraft,
    RedTeamReview,
    ResponseDraft,
    ResponseItem,
    RevisionOp,
    Step,
    TacticCard,
    TacticSnapshot,
)
from afsim_research_agent.evidence.merge import number_present
from afsim_research_agent.falcon.llm import ModelRunner

REDTEAM_PROMPT_VERSION = "crow-redteam-2026.10.4"
_LEVEL_CN = {"ready": "可实现", "needs_confirmation": "需确认", "needs_development": "需开发"}
_HIGH = "high"

RED_FORMAT = """{
  "critical_assumptions": [
    {"assumption": "编队内数据链持续可用", "fragility": "high", "why": "对方可能实施通信干扰"}
  ],
  "counter_tactics": [
    {"id": "ct_01", "description": "对方在航线必经的山口预设防空阵地伏击编队长机",
     "enemy_capabilities_needed": ["便携防空导弹", "隐蔽阵地"],
     "trigger": "编队降低高度通过山口", "expected_effect": "长机受损，编队失去指挥",
     "severity": "high"}
  ],
  "failure_conditions": ["僚机与长机间距超过通信距离，长机指令无法送达"],
  "capability_doubts": ["夜间低空飞行时判断地形间隔"],
  "overall": {"soundness": "medium", "main_weakness": "山口段缺少先行侦察"}
}"""

RESPONSE_FORMAT = """{
  "responses": [
    {"target": "ct_01", "decision": "revise", "reason": "增加先行侦察，发现防空阵地即绕行"},
    {"target": "fc_1", "decision": "keep", "reason": "战术已规定间距上限，通信距离远大于该上限"}
  ],
  "revisions": [
    {"op": "add_step", "index": 0, "phase": "先行侦察", "do": "一架僚机先行侦察山口",
     "until": "确认山口无防空阵地或已选定绕行航线", "responds_to": ["ct_01"]},
    {"op": "add_exception", "situation": "侦察发现防空阵地", "then": "全编队改走预备航线",
     "responds_to": ["ct_01"]}
  ],
  "narrative": "（修改后的完整战术描述）"
}"""


# ---------------------------------------------------------------------- models
def model_family(name: str | None) -> str:
    """``qwen3.8-flash`` → ``qwen``; ``deepseek-v4-pro`` → ``deepseek``."""
    base = (name or "").lower().rsplit("/", 1)[-1]
    match = re.match(r"[a-z]+", base)
    return match.group(0) if match else base


def independence_note(red_model: str | None, proposer_model: str | None) -> str | None:
    if red_model and proposer_model and model_family(red_model) == model_family(proposer_model):
        return (
            f"红方模型 {red_model} 与生成模型 {proposer_model} 属于同一系列，"
            "评审可能偏向生成方；建议用 CROW_REDTEAM_MODEL 换成其他系列"
        )
    return None


# ---------------------------------------------------------------------- prompts
def card_facts(card: TacticCard) -> str:
    """The checkable content of a card, without the proposer's argumentation."""
    lines = [f"名称：{card.name}"]
    for section in SECTIONS:
        for i, element in enumerate(getattr(card, section)):
            data = element.model_dump(exclude={"evidence", "basis"})
            text = "；".join(f"{k}={v}" for k, v in data.items() if v not in ("", [], None))
            lines.append(f"{section}[{i}]：{text}")
    lines.append(f"决策权：{card.decision_authority}")
    if card.capability_needs:
        # what the tactic asks of the platform, without the catalog's verdicts: the red team
        # judges tactics, platform support is checked separately (C4b / IR check)
        lines.append("所需平台能力：" + "；".join(n.need for n in card.capability_needs))
    return "\n".join(lines)


def redteam_prompt(card: TacticCard, profile: MissionProfile | None) -> str:
    context = profile_context(profile) if profile else "（无任务画像）"
    return f"""你是红方（对手）指挥员兼战术评审员。下面是我方一个候选战术的要素。请站在对手立场，评审这个战术**整体上是否合理**：阶段之间是否衔接、协同是否成立、面对对手的常见反制是否站得住。

只看战术层面的大问题，不要纠缠细节：具体数值取多少、传感器精度、平台软件如何实现、个别措辞，都不在评审范围内（数值会按合理参考值处理，平台能力另有核对）。

## 要求
1. critical_assumptions：战术整体成立所依赖的关键前提，最多 3 条，按脆弱程度从高到低排列；fragility 取 low / medium / high，why 说明为什么脆弱。
2. counter_tactics：对手最可能采取的 2 到 3 种反制，id 依次为 ct_01、ct_02、ct_03。每种写明对手需要的能力、触发时机（战术中的哪个阶段）、预期效果、严重程度（low / medium / high）。反制要符合任务画像中的对手情况，不要假设对手拥有画像之外的能力，除非画像未说明。
3. failure_conditions：不需要对手行动、战术自身就可能失效的结构性问题（阶段衔接、协同关系、前后矛盾），最多 3 条。
4. capability_doubts：战术层面默认我方能做、但实战中未必做得到的事（如"无通信时仍能协调角色交换"），最多 2 条；没有就留空。
5. overall：soundness 按下面的标准取值，main_weakness 一句话：
   - high：常见反制下核心机制仍然成立，弱点只需局部修补；
   - medium：有明显弱点，但通过增加步骤或例外可以修补；
   - low：核心机制在常见反制下失效，需要重新设计。
6. 只评审，不要给出修改方案；不要评价证据来源。全部用中文，只输出 JSON，格式同下例（示例取自无关领域，只示范结构）：
{RED_FORMAT}

## 任务画像
{context}

## 候选战术要素
{card_facts(card)}
"""


def items_to_answer(review: RedTeamReview) -> list[tuple[str, str]]:
    """(target id, text) the proposer must answer: every counter-tactic and failure
    condition, and the high-fragility assumptions. Capability doubts are shown in the report
    but not answered: platform support is the capability check's job."""
    items = [
        (ct.id, f"{ct.description}（触发：{ct.trigger}；严重程度 {ct.severity}）")
        for ct in review.counter_tactics
    ]
    items += [(f"fc_{i + 1}", text) for i, text in enumerate(review.failure_conditions)]
    items += [
        (f"ca_{i + 1}", f"{a.assumption}（{a.why}）")
        for i, a in enumerate(review.critical_assumptions)
        if a.fragility == _HIGH
    ]
    return items


def response_prompt(card: TacticCard, review: RedTeamReview, profile: MissionProfile | None) -> str:
    items = "\n".join(f"- {target}：{text}" for target, text in items_to_answer(review))
    fixed = "；".join(profile.user_specified) if profile and profile.user_specified else "无"
    tactic = card.model_dump(
        mode="json",
        include={
            "name",
            "narrative",
            "applicability",
            "roles",
            "geometry",
            "steps",
            "exceptions",
            "decision_authority",
        },
    )
    return f"""你是这个战术的提出者。红方对它提出了下列意见。请逐条回应：要么修改战术（decision=revise），要么说明为什么不改（decision=keep，给出具体理由，不要泛泛而谈）。

## 规则
1. responses 必须覆盖下面每一条意见，target 写意见编号。
2. 修改用 revisions 表示，op 取：
   - add_exception（新增例外：situation、then）
   - modify_exception（改第 index 个例外）
   - add_step（在第 index 个步骤之后插入；index=-1 表示插在最前；phase、do、until 都要写）
   - modify_step（改第 index 个步骤，只写要改的字段）
   每个修改的 responds_to 列出它回应的意见编号。
3. 只做回应意见所必需的修改，修改保持在战术层面（调整阶段、协同、例外处置），不要为了细节增加新的感知或判断要求；不得删除或违背用户指定的约束：{fixed}。
4. 有任何修改时，narrative 给出修改后的完整战术描述（中文，150–500 字）：只写战术本身怎么做，不要写"此战术强化了……"之类的自我评价；没有修改时 narrative 留空。
5. 车辆用战术中的角色名称（如排长车、僚车、前锋车），不要用内部编号。
6. 需要具体数值（时间、比例、距离）而没有依据时，直接给出合理的参考值并写明单位，系统会把它记为可调整的参考值，不必回避。
7. 不要编造证据编号。全部用中文，只输出 JSON，格式同下例（示例取自无关领域）：
{RESPONSE_FORMAT}

## 战术（当前版本 {card.revision}）
{json.dumps(tactic, ensure_ascii=False, indent=1)}

## 红方意见
{items}
"""


# ---------------------------------------------------------------------- applying revisions
_SELF_ASSESSMENT = re.compile(
    r"[^。！？]*(?:此|该|本|这一|这种|这样的|上述)(?:战术|方案|修订|流程|做法|安排|部署)[^。！？]*"
    r"(?:强化|提高|提升|增强|弥补|有效|降低了|确保了|保证了|确保|兼顾)[^。！？]*[。！？]?"
)


def strip_self_assessment(text: str) -> tuple[str, str]:
    """Remove sentences praising the tactic ("此战术强化了……"): a narrative describes."""
    removed = [m.group(0).strip() for m in _SELF_ASSESSMENT.finditer(text)]
    cleaned = _SELF_ASSESSMENT.sub("", text).strip()
    return (cleaned, "；".join(removed)) if removed and cleaned else (text, "")


def _card_text(card: TacticCard) -> str:
    parts = [card.narrative]
    for section in SECTIONS:
        for element in getattr(card, section):
            parts += [str(v) for v in element.model_dump(exclude={"evidence", "basis"}).values()]
    return " ".join(parts)


_NUMBER = re.compile(r"(\d+(?:\.\d+)?)\s*(%|％|米|m|千米|公里|km|秒|s|分钟|min|小时|h)(?![a-zA-Z])")


def _new_numbers(old: TacticCard, new: TacticCard, requirement: str) -> list[str]:
    """Quantities in the revision that neither the requirement nor the earlier card has."""
    before = f"{_card_text(old)} {requirement}"
    out = []
    for m in _NUMBER.finditer(_card_text(new)):
        if not number_present(float(m.group(1)), before):
            out.append(m.group(0).strip())
    return list(dict.fromkeys(out))


def apply_revisions(
    card: TacticCard,
    draft: ResponseDraft,
    profile: MissionProfile | None,
    settings: ProposalSettings | None = None,
    requirement: str = "",
) -> tuple[TacticCard | None, list[str]]:
    """The revised card, or None with the reasons when the revision cannot be used."""
    notes: list[str] = []
    ops = [op for op in draft.revisions if op.op]
    if not ops:
        return None, notes
    revised = card.model_copy(deep=True)
    steps = list(revised.steps)
    exceptions = list(revised.exceptions)
    for op in ops:
        tag = "、".join(op.responds_to) or "红方意见"
        problem = _apply(op, steps, exceptions)
        if problem:
            notes.append(f"修改未采用（{op.op}，回应 {tag}）：{problem}")
    if notes and len(notes) == len(ops):
        return None, notes
    revised.steps, revised.exceptions = steps, exceptions
    if draft.narrative.strip():
        narrative, removed = strip_self_assessment(draft.narrative.strip())
        revised.narrative = narrative
        if removed:
            notes.append(f"已从战术描述中去掉自我评价：{removed}")
    else:
        notes.append("修订后没有给出新的战术描述，沿用原描述，可能与要素不一致")
    for section in SECTIONS:
        revised.support[section] = _support(getattr(revised, section))
    issues = assess_completeness(revised, profile, settings or ProposalSettings())
    if issues:
        return None, [*notes, "修订版不完整，未采用：" + "；".join(issues)]
    revised.previous = [
        *card.previous,
        TacticSnapshot(
            revision=card.revision,
            narrative=card.narrative,
            steps=card.steps,
            exceptions=card.exceptions,
        ),
    ]
    revised.revision = card.revision + 1
    new_numbers = _new_numbers(card, revised, requirement)
    if new_numbers:
        # reference values: reasonable defaults the user can adjust, not open items
        revised.assumptions.append(
            f"参考值（第 {card.revision + 1} 版，无依据，可调整）：{'、'.join(new_numbers)}"
        )
    targets = sorted({t for op in ops for t in op.responds_to})
    revised.review_flags.append(
        f"第 {revised.revision} 版为回应红方意见（{'、'.join(targets) or '—'}）所做的修改，"
        "新增或修改的要素没有证据支持，需确认"
    )
    revised.capability_needs, revised.feasibility = [], None  # annotated again by the caller
    return revised, notes


def _apply(op: RevisionOp, steps: list[Step], exceptions: list[ExceptionItem]) -> str | None:
    why = f"回应红方 {'、'.join(op.responds_to)}" if op.responds_to else "回应红方意见"
    if op.op == "add_exception":
        if not (op.situation.strip() and op.then.strip()):
            return "新增例外缺少情况或处置"
        exceptions.append(ExceptionItem(situation=op.situation, then=op.then, basis="inferred"))
        return None
    if op.op == "modify_exception":
        if op.index is None or not 0 <= op.index < len(exceptions):
            return f"没有第 {op.index} 个例外"
        old = exceptions[op.index]
        exceptions[op.index] = ExceptionItem(
            situation=op.situation or old.situation, then=op.then or old.then, basis="inferred"
        )
        return None
    if op.op == "add_step":
        if not (op.phase.strip() and op.do.strip() and op.until.strip()):
            return "新增步骤缺少阶段、动作或结束条件"
        index = -1 if op.index is None else op.index
        if not -1 <= index < len(steps):
            return f"没有第 {index} 个步骤"
        steps.insert(index + 1, Step(phase=op.phase, do=op.do, until=op.until, basis="inferred"))
        return None
    if op.op == "modify_step":
        if op.index is None or not 0 <= op.index < len(steps):
            return f"没有第 {op.index} 个步骤"
        old = steps[op.index]
        steps[op.index] = Step(
            phase=op.phase or old.phase,
            do=op.do or old.do,
            until=op.until or old.until,
            basis="inferred",
        )
        return None
    return f"未知操作 {op.op}（{why}）"


def constraint_prompt(card: TacticCard, constraints: list[str]) -> str:
    tactic = card.model_dump(mode="json", include={"narrative", "geometry", "steps", "exceptions"})
    items = "\n".join(f"- {c}" for c in constraints)
    return f"""下面是一个战术的修订版，以及用户指定、不得改变的约束。请只判断修订版是否违背或削弱了某条约束（例如约束要求"纵队车距 50 米"，修订改为交错队形或不同间距）。没有违背时输出空列表。只输出 JSON：
{{"violations": [{{"constraint": "约束原文", "how": "修订中哪一处与它矛盾"}}]}}

## 用户约束
{items}

## 修订版
{json.dumps(tactic, ensure_ascii=False, indent=1)}
"""


# ---------------------------------------------------------------------- the stage
class RedTeam:
    """C5 adversarial review of every candidate, then the proposer's response."""

    def __init__(
        self,
        red_runner: ModelRunner,
        proposer_runner: ModelRunner | None,
        *,
        annotator: Any | None = None,
        settings: ProposalSettings | None = None,
    ) -> None:
        self.red = red_runner
        self.proposer = proposer_runner
        self.annotator = annotator  # CapabilityAnnotator: re-annotates revised candidates
        self.settings = settings or ProposalSettings()

    async def review(
        self, result: ProposalResult, profile: MissionProfile | None
    ) -> list[RedTeamReview]:
        reviews: list[RedTeamReview] = []
        revised: list[TacticCard] = []
        for i, card in enumerate(result.candidates):
            review = RedTeamReview(
                candidate=card.id,
                revision_reviewed=card.revision,
                revision_after=card.revision,
                feasibility_before=card.feasibility.level if card.feasibility else None,
            )
            reviews.append(review)
            try:
                draft = await self.red(
                    f"redteam:{card.id}", redteam_prompt(card, profile), RedTeamDraft
                )
            except Exception as exc:  # noqa: BLE001 - one failed review never stops C5
                review.notes.append(f"红方审查失败（{exc.__class__.__name__}）")
                continue
            _fill(review, draft)
            if self.proposer is None or not items_to_answer(review):
                continue
            try:
                answer = await self.proposer(
                    f"response:{card.id}", response_prompt(card, review, profile), ResponseDraft
                )
            except Exception as exc:  # noqa: BLE001
                review.notes.append(f"生成方回应失败（{exc.__class__.__name__}）")
                continue
            review.responses = _complete_responses(review, answer.responses)
            review.revisions = list(answer.revisions)
            new_card, notes = apply_revisions(
                card, answer, profile, self.settings, requirement=result.requirement
            )
            review.notes += notes
            if new_card is not None:
                await self._check_constraints(new_card, review, profile)
                result.candidates[i] = new_card
                review.revision_after = new_card.revision
                revised.append(new_card)
        if revised and self.annotator is not None:
            await self._annotate(result, revised, profile)
        cards = {c.id: c for c in result.candidates}
        for review in reviews:
            card = cards[review.candidate]
            review.feasibility_after = card.feasibility.level if card.feasibility else None
        result.notes.append(
            f"红方审查：{len(reviews)} 个候选，修订 {len(revised)} 个（{REDTEAM_PROMPT_VERSION}）"
        )
        return reviews

    async def _check_constraints(
        self, card: TacticCard, review: RedTeamReview, profile: MissionProfile | None
    ) -> None:
        """An independent look (red-team model) at whether the revision broke a user
        constraint; the proposer cannot be trusted to report that itself."""
        constraints = list(profile.user_specified) if profile else []
        if not constraints:
            return
        try:
            check = await self.red(
                f"constraint:{card.id}", constraint_prompt(card, constraints), ConstraintCheck
            )
        except Exception as exc:  # noqa: BLE001
            review.notes.append(f"约束核对失败（{exc.__class__.__name__}），请人工确认")
            return
        for v in check.violations:
            if not v.constraint.strip():
                continue
            text = f"第 {card.revision} 版可能违背用户约束「{v.constraint}」：{v.how}"
            card.review_flags.append(text)
            review.notes.append(text)

    async def _annotate(
        self, result: ProposalResult, cards: list[TacticCard], profile: MissionProfile | None
    ) -> None:
        from afsim_research_agent.crow.capability_annotation import collect_gaps

        selected = {g.id for g in result.capability_gaps if g.selected}
        part = result.model_copy(update={"candidates": cards, "notes": []})
        await self.annotator.annotate(part, profile)
        result.capability_gaps = collect_gaps(result.candidates, self.annotator.catalog)
        result.catalog_version = part.catalog_version
        result.capability_granularity = part.capability_granularity
        for gap in result.capability_gaps:
            gap.selected = gap.id in selected


def _fill(review: RedTeamReview, draft: RedTeamDraft) -> None:
    review.critical_assumptions = draft.critical_assumptions
    seen: set[str] = set()
    for i, ct in enumerate(draft.counter_tactics):
        if not ct.id or ct.id in seen:
            ct.id = f"ct_{i + 1:02d}"
        seen.add(ct.id)
    review.counter_tactics = draft.counter_tactics
    review.failure_conditions = [f for f in draft.failure_conditions if f.strip()]
    review.capability_doubts = [d for d in draft.capability_doubts if d.strip()]
    review.overall = draft.overall


def _complete_responses(review: RedTeamReview, answers: list[ResponseItem]) -> list[ResponseItem]:
    """Keep answers to known items; record items the proposer left unanswered."""
    known = dict(items_to_answer(review))
    out = [a for a in answers if a.target in known]
    answered = {a.target for a in out}
    for target in known:
        if target not in answered:
            review.notes.append(f"生成方未回应 {target}")
    return out


# ---------------------------------------------------------------------- running C5
async def run_redteam(
    result: ProposalResult,
    profile: MissionProfile | None,
    *,
    red_runner: ModelRunner,
    proposer_runner: ModelRunner | None,
    red_model: str | None,
    proposer_model: str | None,
    annotator: Any | None = None,
) -> tuple[list[RedTeamReview], list[str]]:
    notes = []
    warning = independence_note(red_model, proposer_model)
    if warning:
        notes.append(warning)
        result.notes.append(warning)
    stage = RedTeam(red_runner, proposer_runner, annotator=annotator)
    reviews = await stage.review(result, profile)
    return reviews, notes


def write_c5(
    result: ProposalResult,
    reviews: list[RedTeamReview],
    package: Any,
    out_dir: Any,
    *,
    red_model: str | None,
    proposer_model: str | None,
    notes: list[str],
    trace: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Candidates (with revisions), ``redteam_review.yaml`` and ``tactic_report.md``."""
    from pathlib import Path

    from afsim_research_agent.crow.proposal_report import write_proposal
    from afsim_research_agent.crow.selection_report import (
        REPORT_FILE,
        REVIEW_FILE,
        render_report,
        write_review,
    )

    out = Path(out_dir)
    paths = write_proposal(result, package, out, None)
    paths["redteam"] = write_review(
        reviews,
        out / REVIEW_FILE,
        red_model=red_model,
        proposer_model=proposer_model,
        notes=notes,
    )
    report = out / REPORT_FILE
    report.write_text(
        render_report(result, reviews, red_model=red_model, notes=notes, tactics_dir=str(out)),
        encoding="utf-8",
    )
    paths["report"] = report
    if trace is not None:
        path = out / "redteam_trace.json"
        path.write_text(json.dumps(trace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        paths["redteam_trace"] = path
    return paths


def main(argv: list[str] | None = None) -> None:
    """``python -m afsim_research_agent.crow.redteam <tactics_dir>``: C5 on an existing run."""
    import argparse
    import asyncio
    from pathlib import Path

    from dotenv import load_dotenv

    from afsim_research_agent.config import Settings
    from afsim_research_agent.crow.annotate import load_package, load_result
    from afsim_research_agent.crow.capability_annotation import CapabilityAnnotator
    from afsim_research_agent.crow.mission import load_profile
    from afsim_research_agent.crow.propose import (
        add_granularity_arguments,
        load_catalog,
        policy_from_args,
    )
    from afsim_research_agent.falcon.llm import DashScopeModel

    parser = argparse.ArgumentParser(prog="python -m afsim_research_agent.crow.redteam")
    parser.add_argument("tactics_dir", type=Path, help="含 tactic_candidates.yaml 的目录")
    parser.add_argument("--out", type=Path, help="输出目录，默认原目录")
    parser.add_argument("--mission-profile", type=Path)
    parser.add_argument("--evidence-package", type=Path, help="evidence_package.json（报告引文用）")
    parser.add_argument("--no-response", action="store_true", help="只做红方审查，不让生成方回应")
    parser.add_argument("--capability-catalog", type=Path, help="修订后重新做能力标注用的目录")
    parser.add_argument("--no-capability", action="store_true", help="修订后不重新做能力标注")
    add_granularity_arguments(parser)
    args = parser.parse_args(argv)
    policy = policy_from_args(parser, args)

    load_dotenv()
    settings = Settings.from_environment()
    result = load_result(args.tactics_dir)
    profile_path = args.mission_profile or args.tactics_dir / "mission_profile.yaml"
    profile = load_profile(profile_path) if profile_path.is_file() else None
    package = load_package(
        args.evidence_package or args.tactics_dir.parent / "evidence_package.json"
    )
    red_model = settings.crow_redteam_model
    proposer_model = settings.crow_proposer_model or settings.chat_model
    red_runner = ModelRunner(DashScopeModel(red_model))
    proposer_runner = None if args.no_response else ModelRunner(DashScopeModel(proposer_model))
    annotator = None
    if not args.no_capability and result.catalog_version:
        catalog = load_catalog(args.capability_catalog, settings)
        if catalog is not None:
            cap_model = settings.crow_capability_model or proposer_model
            annotator = CapabilityAnnotator(
                ModelRunner(DashScopeModel(cap_model)), catalog, policy=policy
            )
    reviews, notes = asyncio.run(
        run_redteam(
            result,
            profile,
            red_runner=red_runner,
            proposer_runner=proposer_runner,
            red_model=red_model,
            proposer_model=proposer_model,
            annotator=annotator,
        )
    )
    trace = [r.model_dump(mode="json") for r in red_runner.records]
    if proposer_runner is not None:
        trace += [r.model_dump(mode="json") for r in proposer_runner.records]
    if annotator is not None:
        trace += [r.model_dump(mode="json") for r in annotator.runner.records]
    paths = write_c5(
        result,
        reviews,
        package,
        args.out or args.tactics_dir,
        red_model=red_model,
        proposer_model=proposer_model,
        notes=notes,
        trace=trace,
    )
    for note in notes:
        print(f"⚠ {note}")
    by_id = {r.candidate: r for r in reviews}
    for card in result.candidates:
        review = by_id[card.id]
        overall = review.overall.soundness if review.overall else "未审查"
        changed = (
            f"，修订为第 {card.revision} 版"
            if review.revision_after > review.revision_reviewed
            else ""
        )
        feas = ""
        if review.feasibility_before and review.feasibility_after != review.feasibility_before:
            feas = (
                f"，平台可行性 {_LEVEL_CN.get(review.feasibility_before, '—')}"
                f" → {_LEVEL_CN.get(review.feasibility_after or '', '未标注')}"
            )
        print(
            f"  {card.id} {card.name}：红方评价 {overall}，反制 {len(review.counter_tactics)} 条，"
            f"回应 {len(review.responses)} 条{changed}{feas}"
        )
    print(f"- 评审报告：{paths['report']}")
    print(f"- 红方意见与回应：{paths['redteam']}")


if __name__ == "__main__":
    main()
