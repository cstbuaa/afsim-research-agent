"""C4 tactic proposal: retrieved tactics first, innovation only to fill gaps.

Flow (design crow-tactic-proposal-v1 §5.5–5.7):

1. Extract tactics the evidence directly describes (`retrieved`), each element
   citing evidence IDs from the normalized evidence package (F1).
2. Check every element in code: unknown evidence IDs are removed; elements
   whose numbers are not in the cited quotes, or that cite nothing, are
   downgraded to `inferred`; per-section support is computed.
3. Rank retrieved tactics by requirement coverage, context match and
   evidence strength; near-duplicates become variants of one candidate.
4. Detect gaps: too few retrieved tactics, user-specified clauses no
   retrieved tactic covers, research dimensions without strong evidence.
5. If there are gaps, run one supplementary retrieval round (when a retriever
   is configured) and re-extract.
6. Only gaps that remain are filled by innovation (adapted/combined), at most
   `max_innovative`, preferring the smallest change that fills the gap.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from afsim_research_agent.crow.dimensions import PatternTable
from afsim_research_agent.crow.mission import MissionProfile, profile_context
from afsim_research_agent.crow.models import ReadinessAssessment
from afsim_research_agent.crow.proposal_models import (
    INNOVATIVE,
    SECTIONS,
    Derivation,
    ExtractionResult,
    Gap,
    Idea,
    IdeaDraft,
    InnovationResult,
    ProposalResult,
    Score,
    TacticCard,
    TacticDraft,
    Technique,
)
from afsim_research_agent.evidence.merge import number_present
from afsim_research_agent.falcon.evidence import package_hash, squash
from afsim_research_agent.falcon.llm import ModelRunner
from afsim_research_agent.falcon.models import EvidenceItem, EvidencePackage, EvidenceSource

PROPOSAL_PROMPT_VERSION = "crow-proposal-2026.10.2"

_CATALOG = Path(__file__).with_name("catalog") / "innovation.toml"
_GAP_ORDER = {"uncovered_clause": 0, "too_few_retrieved": 1, "dimension": 2}
_QUANTITY = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:m|米|km|公里|千米|s|秒|min|分钟|km/h|公里/小时|mph|yd|码)(?![a-z])",
    re.IGNORECASE,
)

Retriever = Callable[[list[Gap]], Awaitable[EvidencePackage | None]]


# --------------------------------------------------------------------------- settings


@dataclass(frozen=True)
class ProposalSettings:
    max_candidates: int = 5
    max_innovative: int = 2
    ideas_per_gap: int = 3
    min_retrieved: int = 3
    merge_similarity: float = 0.7
    min_steps: int = 2
    min_narrative_chars: int = 80


@dataclass(frozen=True)
class InnovationCatalog:
    version: str
    operators: dict[str, tuple[str, int, str]]  # id -> (name, rank, description)
    dimensions: dict[str, tuple[str, tuple[str, ...]]]  # id -> (name, options)
    incompatible: tuple[tuple[str, str, str], ...]

    @classmethod
    @cache
    def default(cls) -> InnovationCatalog:
        data = tomllib.loads(_CATALOG.read_text(encoding="utf-8"))
        return cls(
            version=data["meta"]["version"],
            operators={
                o["id"]: (o["name"], int(o["rank"]), o["description"]) for o in data["operators"]
            },
            dimensions={d["id"]: (d["name"], tuple(d["options"])) for d in data["dimensions"]},
            incompatible=tuple((i["a"], i["b"], i["why"]) for i in data["incompatible"]),
        )

    def clash(self, options: set[str]) -> str | None:
        for a, b, why in self.incompatible:
            if a in options and b in options:
                return why
        return None


# --------------------------------------------------------------------------- prompts


def evidence_digest(package: EvidencePackage) -> str:
    sources = {s.source_id: s for s in package.sources}
    lines = []
    for item in package.evidence:
        source = sources.get(item.source_id)
        title = source.title[:70] if source else item.source_id
        tier = f", {source.quality_tier}" if source and source.quality_tier else ""
        subject = f"［主体：{item.subject}］" if item.subject else ""
        extra = f" | 条件：{item.condition}" if item.condition else ""
        lines.append(
            f"- {item.evidence_id} [{item.topic}]{subject} {item.claim}{extra} （{title}{tier}）"
        )
    return "\n".join(lines) or "- （无证据）"


def _card_digest(card: TacticCard) -> str:
    parts = [f"{card.id} {card.name}（{card.provenance}；模式 {', '.join(card.patterns)}）"]
    parts.append(f"  战术描述：{card.narrative}")
    for s in card.steps:
        parts.append(f"  步骤：{s.phase}：{s.do}，直到 {s.until}")
    for e in card.exceptions:
        parts.append(f"  例外：{e.situation} → {e.then}")
    parts.append(f"  决策权：{card.decision_authority}")
    return "\n".join(parts)


def _technique_digest(techniques: list[Technique]) -> str:
    return (
        "\n".join(
            f"- {t.id} {t.name}（{t.kind}）：{t.description} {t.evidence}" for t in techniques
        )
        or "- （无）"
    )


# Format example from an unrelated domain so the model copies the structure, not the content.
FORMAT_EXAMPLE = """\
{
  "techniques": [
    {"id": "a1", "name": "航线搜索", "kind": "movement",
     "description": "沿预定航线以巡航速度搜索目标区域", "evidence": ["ev_1111111111"]},
    {"id": "a2", "name": "发现即报告", "kind": "coordination",
     "description": "发现目标后立即向地面站报告位置并等待确认", "evidence": ["ev_2222222222"]}
  ],
  "tactics": [
    {
      "name": "航线搜索—发现报告—返航",
      "narrative": "侦察无人机起飞后沿预定航线以巡航速度搜索目标区域（a1）。发现疑似目标时，先保持\
跟踪，再向地面站报告目标位置并等待确认（a2）；收到确认后继续搜索剩余航线。若通信中断，无人机继续按\
航线搜索，并在预定时刻返航。航线飞完或剩余燃油降到返航阈值时返航，着陆后任务结束。",
      "techniques": ["a1", "a2"],
      "patterns": ["report_and_wait"],
      "summary": "沿航线搜索，发现即报告待确认，完成后返航。",
      "source_context": "无人侦察机（单机）",
      "context_match": "exact",
      "applicability": [{"situation": "目标区域已知且空域无威胁", "evidence": ["ev_1111111111"],
                         "basis": "evidence"}],
      "roles": [{"role": "侦察机", "members": ["recon_uav"], "evidence": [],
                 "basis": "user_specified"}],
      "geometry": [{"formation": "单机", "when": "全程", "spacing": "", "evidence": [],
                    "basis": "inferred"}],
      "steps": [
        {"phase": "搜索", "do": "沿预定航线以巡航速度搜索", "until": "发现目标或航线飞完",
         "evidence": ["ev_1111111111"], "basis": "evidence"},
        {"phase": "报告", "do": "向地面站报告目标位置", "until": "收到地面站确认",
         "evidence": ["ev_2222222222"], "basis": "evidence"},
        {"phase": "返航", "do": "返回基地", "until": "着陆", "evidence": [],
         "basis": "user_specified"}
      ],
      "exceptions": [{"situation": "通信中断", "then": "继续搜索并在预定时刻返航",
                      "evidence": [], "basis": "inferred"}],
      "decision_authority": "leader_assigns",
      "addresses": ["发现目标后报告", "完成后返航"],
      "covers": ["发现目标后报告"],
      "conflicts_with_user": [],
      "required_capabilities": ["air_mover", "sensor", "radio"],
      "assumptions": ["地面站全程可通信"],
      "risks": ["报告等待期间目标脱离跟踪"]
    }
  ]
}"""

_COMMON_RULES = (
    """\
What a tactic is:
- A tactic is a COMPLETE plan that accomplishes the WHOLE mission from start
  to end, written first as `narrative`: natural Chinese prose (150-500
  characters) that a commander could read aloud — when it applies, who does
  what in which order, how one phase hands over to the next, how units
  coordinate, what happens in exceptions, and how it ends.
- A single technique, a single formation or a single phase is NOT a tactic.
  Those are techniques (building blocks); a tactic combines them.
- `steps` repeat the narrative as ordered phases covering start to end; every
  step has an end condition (`until`); the last step ends the mission.
- A tactic must implement EVERY user-specified constraint. List every one it
  implements in `addresses` (verbatim). If it cannot satisfy one, list that
  one in `conflicts_with_user`.
- `covers`: the subset of `addresses` that the evidence itself supports (not
  only the user's word).

Field rules:
- All text in Chinese; an English doctrine term may follow in parentheses.
- `formation` is a short formation name (楔形、纵队、梯队、横队、两组交替…);
  put conditions in `when` and distances in `spacing`. Do not write sentences
  into `formation`.
- `exceptions` are situations that change or stop the tactic (e.g. 通信中断、
  遭敌反装甲火力、地形严重受限), not preconditions.
- Each element lists the evidence_id values that state it. basis = "evidence"
  when cited; "user_specified" when it comes from the user's requirement;
  "inferred" when derived without a direct statement; "unsupported" for an
  element the evidence does not contain at all.
- Evidence about a different unit type (e.g. dismounted infantry when the
  mission is an armored platoon) may only be used with a note in the element
  text that it needs adaptation; never copy its numbers as if they applied.
- Never invent numbers. A number in an element with basis "evidence" must
  appear in a cited evidence item.
- patterns: ids from the pattern table only.

Format example (structure only; different domain):
"""
    + FORMAT_EXAMPLE
    + "\n"
)


def extraction_prompt(
    requirement: str,
    profile: MissionProfile | None,
    package: EvidencePackage,
    patterns: PatternTable,
) -> str:
    context = profile_context(profile) if profile else "(no mission profile)"
    user = "; ".join(profile.user_specified) if profile and profile.user_specified else "(none)"
    return f"""\
You turn evidence into COMPLETE tactics for the mission below.

Step 1 — techniques: list the techniques (building blocks) the evidence
describes that matter for this mission: formations, movement techniques,
transitions, reporting and command procedures, engagement rules.

Step 2 — tactics: assemble 1-4 complete tactics from those techniques. Every
tactic covers the whole mission (all phases in order, every user-specified
constraint, exceptions, end). Assemble only what the evidence supports; the
order of phases may follow the user's requirement. Produce more than one
tactic only when the evidence offers real alternatives (e.g. a different
movement technique for the contact phase, a different grouping, a different
decision authority); alternatives must differ in substance, not wording. Do
not invent techniques the evidence lacks; filling gaps is a later stage.

{_COMMON_RULES}
Mission profile:
{context}

User-specified constraints (verbatim): {user}

Pattern table:
{patterns.prompt_listing()}

Evidence (id [topic][subject] statement | condition (source, tier)):
{evidence_digest(package)}

User requirement:
{requirement.strip()}
"""


def innovation_prompt(
    requirement: str,
    profile: MissionProfile | None,
    package: EvidencePackage,
    gap: Gap,
    retrieved: list[TacticCard],
    techniques: list[Technique],
    catalog: InnovationCatalog,
    patterns: PatternTable,
    count: int,
) -> str:
    context = profile_context(profile) if profile else "(no mission profile)"
    operators = "\n".join(
        f"- {op} (change size {rank}): {desc}"
        for op, (_, rank, desc) in sorted(catalog.operators.items(), key=lambda kv: kv[1][1])
    )
    dimensions = "\n".join(
        f"- {dim}: {', '.join(options)}" for dim, (_, options) in catalog.dimensions.items()
    )
    tactics = "\n".join(_card_digest(c) for c in retrieved) or "(no complete retrieved tactics)"
    return f"""\
The complete tactics retrieved from the evidence leave a gap. Propose {count}
COMPLETE tactics (whole mission, start to end, with narrative) that fill THIS
gap with the smallest possible change to retrieved material.

Gap: {gap.detail}
{_GAP_DEMAND[gap.kind].format(target=gap.target)}

Methods, smallest change first:
1. provenance "adapted": apply one (or few) adaptation operators to ONE
   retrieved tactic. derivation.sources names that tactic and the evidence it
   rests on; derivation.operators lists operator ids; derivation.changes
   lists each change (op, before, after, why).
2. provenance "combined": assemble a complete tactic from the techniques
   below and/or parts of different tactics. derivation.sources lists every
   source; derivation.combination chooses one option per dimension (listed
   options only); derivation.operators may be empty.

Innovation may only appear in HOW material is adapted or combined. Elements
that keep retrieved content keep its evidence; changed elements are
"inferred"; an element with no basis in the evidence at all is "unsupported".
Each proposal must differ in substance from the retrieved tactics.
Return JSON with an "ideas" list; each idea has the tactic fields of the
format example plus "provenance" and "derivation".

{_COMMON_RULES}
Adaptation operators:
{operators}

Combination dimensions and options:
{dimensions}

Mission profile:
{context}

Techniques found in the evidence:
{_technique_digest(techniques)}

Complete retrieved tactics:
{tactics}

Pattern table:
{patterns.prompt_listing()}

Evidence:
{evidence_digest(package)}

User requirement:
{requirement.strip()}
"""


_GAP_DEMAND = {
    "uncovered_clause": (
        'Every proposal must implement the clause "{target}" with evidence-backed content and '
        "list it verbatim in both addresses and covers."
    ),
    "too_few_retrieved": "Each proposal must differ clearly from every retrieved tactic.",
    "dimension": "Each proposal must give concrete content for the dimension {target}.",
}
# Card sections that carry each research dimension (None: cannot be checked on a card).
_DIMENSION_SECTIONS: dict[str, tuple[str, ...] | None] = {
    "applicability": ("applicability",),
    "roles_geometry": ("roles", "geometry"),
    "sequence_termination": ("steps",),
    "exceptions_abort": ("exceptions",),
    "coordination_authority": ("steps", "roles"),
    "effectiveness": None,
}


# --------------------------------------------------------------------------- unit types

_DISMOUNT = ("下车", "徒步", "dismount")
_INFANTRY = (
    "士兵",
    "步兵",
    "步枪",
    "火力小组",
    "班",
    "infantry",
    "squad",
    "soldier",
    "rifle",
    "fire team",
)
_VEHICLE = ("坦克", "车辆", "装甲", "战车", "车距", "tank", "vehicle", "armor", "armour", "mounted")
_UNIT_LABEL = {"infantry": "徒步步兵", "vehicle": "车辆"}
_MATCH = {"exact": 1.0, "similar": 0.6, "different": 0.2}


def unit_type(text: str) -> str | None:
    """'infantry', 'vehicle' or None (generic / mixed / unknown)."""
    low = text.lower()
    if any(word in low for word in _DISMOUNT):
        return "infantry"
    infantry = any(word in low for word in _INFANTRY)
    vehicle = any(word in low for word in _VEHICLE)
    if infantry == vehicle:
        return None
    return "infantry" if infantry else "vehicle"


def mission_unit(profile: MissionProfile | None, requirement: str) -> str | None:
    parts = [requirement]
    if profile is not None:
        parts += [profile.echelon, profile.mission.objective]
        parts += [f"{f.role_hint} {f.platform_class or ''}" for f in profile.own_forces]
    found = unit_type(" ".join(parts))
    if found is None and profile is not None:
        classes = " ".join(f.platform_class or "" for f in profile.own_forces)
        if "vehicle" in classes or "ground_vehicle" in classes:
            return "vehicle"
    return found


def evidence_match(
    item: EvidenceItem, source: EvidenceSource | None, mission: str | None
) -> tuple[str, str | None]:
    """(match level, evidence unit type). Subject-level mismatch is 'different';
    a mismatch only visible in the source title is 'similar'."""
    if mission is None:
        return "similar", None
    by_subject = unit_type(item.subject) if item.subject else None
    if by_subject is not None:
        return ("exact" if by_subject == mission else "different"), by_subject
    by_title = unit_type(source.title) if source else None
    if by_title is None:
        return "similar", None
    return ("exact" if by_title == mission else "similar"), by_title


# --------------------------------------------------------------------------- checks


def _elements(card: TacticDraft):
    for section in SECTIONS:
        for element in getattr(card, section):
            yield section, element


def _element_text(element) -> str:
    return " ".join(
        str(v) for k, v in element.model_dump().items() if k not in ("evidence", "basis")
    )


def _match_phrase(phrase: str, options: list[str]) -> str | None:
    key = squash(phrase)
    for option in options:
        other = squash(option)
        if key and other and (key == other or key in other or other in key):
            return option
    return None


def check_card(
    draft: TacticDraft,
    *,
    card_id: str,
    provenance: str,
    package: EvidencePackage,
    profile: MissionProfile | None,
    patterns: PatternTable,
    requirement: str,
    derivation: Derivation | None = None,
    catalog: InnovationCatalog | None = None,
) -> tuple[TacticCard, str | None]:
    """Code checks for one card. Returns (card, rejection reason or None)."""
    data = draft.model_dump(exclude={"provenance", "derivation"})
    card = TacticCard(id=card_id, provenance=provenance, derivation=derivation, **data)
    notes = card.notes
    items: dict[str, EvidenceItem] = {e.evidence_id: e for e in package.evidence}

    unknown_patterns = [p for p in card.patterns if p not in patterns.ids]
    if unknown_patterns:
        notes.append(f"模式 {unknown_patterns} 不在模式表中，已移除")
        card.patterns = [p for p in card.patterns if p in patterns.ids]

    for section, element in _elements(card):
        bad = [e for e in element.evidence if e not in items]
        if bad:
            notes.append(f"{section}：引用了不存在的证据 {bad}，已移除")
            element.evidence = [e for e in element.evidence if e in items]
        if element.basis == "evidence" and not element.evidence:
            element.basis = "inferred"
            notes.append(f"{section}「{_element_text(element)[:30]}」无有效引用，改为 inferred")
        numbers = [float(n) for n in _QUANTITY.findall(_element_text(element))]
        if numbers and element.basis == "evidence":
            texts = " ".join(f"{items[e].quote} {items[e].claim}" for e in element.evidence)
            missing = [n for n in numbers if not number_present(n, texts)]
            if missing:
                element.basis = "inferred"
                notes.append(
                    f"{section}：数值 {', '.join(f'{n:g}' for n in missing)} 不在所引证据中，"
                    "改为 inferred"
                )
        elif numbers and element.basis == "user_specified":
            missing = [n for n in numbers if not number_present(n, requirement)]
            if missing:
                element.basis = "inferred"
                notes.append(
                    f"{section}：数值 {', '.join(f'{n:g}' for n in missing)} 不在需求原文中，"
                    "改为 inferred"
                )
        if section == "steps" and not element.until.strip():
            notes.append(f"步骤「{element.phase}」缺少结束条件")

    _check_units(card, items, package, profile, requirement)
    cited_text = " ".join(
        f"{items[e].quote} {items[e].claim}" for _, el in _elements(card) for e in el.evidence
    )
    loose = [
        n
        for n in (float(x) for x in _QUANTITY.findall(card.narrative))
        if not number_present(n, requirement) and not number_present(n, cited_text)
    ]
    if loose:
        notes.append(
            f"战术描述中的数值 {', '.join(f'{n:g}' for n in loose)} "
            "既不在需求中也不在所引证据中，需核对"
        )

    clauses = profile.user_specified if profile else []
    card.covers = _clauses(card.covers, clauses, notes, "证据覆盖条款")
    card.addresses = _clauses([*card.addresses, *card.covers], clauses, notes, "实现条款")
    card.conflicts_with_user = [_match_phrase(c, clauses) or c for c in card.conflicts_with_user]

    card.support = {section: _support(getattr(card, section)) for section in SECTIONS}

    reason = None
    if provenance in INNOVATIVE and catalog is not None and derivation is not None:
        reason = _check_derivation(card, derivation, catalog, items)
    return card, reason


def _clauses(phrases: list[str], clauses: list[str], notes: list[str], label: str) -> list[str]:
    out: list[str] = []
    for phrase in phrases:
        matched = _match_phrase(phrase, clauses)
        if matched is None:
            notes.append(f"{label}「{phrase}」不是需求中的用户指定内容，已忽略")
        elif matched not in out:
            out.append(matched)
    return out


def _check_units(
    card: TacticCard,
    items: dict[str, EvidenceItem],
    package: EvidencePackage,
    profile: MissionProfile | None,
    requirement: str,
) -> None:
    """Compute unit match in code; downgrade elements resting only on evidence
    about a different unit type (e.g. dismounted infantry for an armored platoon)."""
    mission = mission_unit(profile, requirement)
    sources = {s.source_id: s for s in package.sources}
    levels: list[float] = []
    for section, element in _elements(card):
        matches = [
            evidence_match(items[e], sources.get(items[e].source_id), mission)
            for e in element.evidence
        ]
        levels += [_MATCH[level] for level, _ in matches]
        numbers = [float(n) for n in _QUANTITY.findall(_element_text(element))]
        if element.basis == "evidence" and numbers and any(m[0] == "different" for m in matches):
            usable = " ".join(
                f"{items[e].quote} {items[e].claim}"
                for e, m in zip(element.evidence, matches, strict=True)
                if m[0] != "different"
            )
            foreign = [n for n in numbers if not number_present(n, usable)]
            if foreign:
                element.basis = "inferred"
                card.notes.append(
                    f"{section}：数值 {', '.join(f'{n:g}' for n in foreign)} 只出现在其他类型单位的"
                    "资料中，用于本任务需换算或确认，改为 inferred"
                )
                continue
        if element.basis == "evidence" and matches and all(m[0] == "different" for m in matches):
            element.basis = "inferred"
            other = _UNIT_LABEL.get(matches[0][1] or "", "其他单位")
            subjects = "、".join(sorted({items[e].subject for e in element.evidence}))
            card.notes.append(
                f"{section}「{_element_text(element)[:24]}」的依据来自{other}资料（主体：{subjects}），"
                f"用于{_UNIT_LABEL.get(mission or '', '本任务')}需换算或确认，改为 inferred"
            )
    card.unit_match = round(sum(levels) / len(levels), 3) if levels else 0.6
    model_view = card.context_match
    card.context_match = (
        "exact"
        if card.unit_match >= 0.85
        else "similar"
        if card.unit_match >= 0.45
        else "different"
    )
    if model_view != card.context_match:
        card.notes.append(f"单位匹配度由代码计算为 {card.context_match}（模型自评 {model_view}）")


_EVIDENCE_PAREN = re.compile(r"[（(][^（）()]*ev_[0-9a-zA-Z]+[^（）()]*[）)]")
_EVIDENCE_ID = re.compile(r"\s*ev_[0-9a-zA-Z]+")
_INFANTRY_WORDS = re.compile(
    r"单兵|士兵|步兵|徒步|下车|火力小组|[或各每]班|每个班|先导班|班长|步枪"
)


def review_flags(
    card: TacticCard,
    items: dict[str, EvidenceItem],
    profile: MissionProfile | None,
    requirement: str,
    gap: Gap | None = None,
) -> None:
    """Annotate points a human must confirm; nothing is rejected here."""
    flags = card.review_flags
    cleaned = _EVIDENCE_ID.sub("", _EVIDENCE_PAREN.sub("", card.narrative))
    if cleaned != card.narrative:
        card.narrative = cleaned
        flags.append("战术描述中出现了证据编号，已去除；依据请看结构化要素")
    if mission_unit(profile, requirement) == "vehicle":
        words = sorted(set(_INFANTRY_WORDS.findall(card.narrative)))
        if words:
            flags.append(
                f"战术描述中出现步兵用语（{'、'.join(words)}），本任务为车辆编队，需改写或确认"
            )
    if card.provenance in INNOVATIVE:
        if gap is not None and gap.kind == "uncovered_clause":
            flags.append(
                f"条款「{gap.target}」由创新方案实现，证据没有直接给出做法，条款理解需确认"
            )
        for section, element in _elements(card):
            numbers = [
                n
                for n in _QUANTITY.findall(_element_text(element))
                if not number_present(float(n), requirement)
            ]
            if not numbers or element.basis != "evidence":
                continue
            sources = "；".join(
                f"{e}（{items[e].condition or items[e].claim[:20]}）" for e in element.evidence
            )
            flags.append(
                f"{_SECTION_NAMES.get(section, section)}「{_element_text(element)[:30]}」"
                f"沿用证据数值，原适用条件：{sources}；用于新场景，需确认是否适用"
            )


_SECTION_NAMES = {
    "applicability": "适用情况",
    "roles": "角色",
    "geometry": "队形与几何",
    "steps": "步骤",
    "exceptions": "例外",
}


def assess_completeness(
    card: TacticCard, profile: MissionProfile | None, settings: ProposalSettings
) -> list[str]:
    """Why the card is not a complete tactic (empty list: complete)."""
    issues: list[str] = []
    if len(card.narrative.strip()) < settings.min_narrative_chars:
        issues.append("缺少完整的自然语言战术描述")
    if len(card.steps) < settings.min_steps:
        issues.append(f"步骤少于 {settings.min_steps} 个，未覆盖从开始到结束的全过程")
    if card.steps and not card.steps[-1].until.strip():
        issues.append("最后一步没有结束条件")
    clauses = profile.user_specified if profile else []
    missing = [c for c in clauses if c not in card.addresses and c not in card.conflicts_with_user]
    if missing:
        issues.append("未实现需求条款：" + "；".join(missing))
    return issues


def _support(elements: list) -> str:
    if not elements:
        return "empty"
    backed = sum(1 for e in elements if e.basis in ("evidence", "user_specified"))
    if backed == len(elements):
        return "supported"
    if backed:
        return "partial"
    if all(e.basis == "unsupported" for e in elements):
        return "unsupported"
    return "inferred"


def _check_derivation(
    card: TacticCard,
    derivation: Derivation,
    catalog: InnovationCatalog,
    items: dict[str, EvidenceItem],
) -> str | None:
    unknown = [op for op in derivation.operators if op not in catalog.operators]
    if unknown:
        card.notes.append(f"未知改编算子 {unknown}，已移除")
        derivation.operators = [op for op in derivation.operators if op in catalog.operators]
    for source in derivation.sources:
        source.evidence = [e for e in source.evidence if e in items]
    if not derivation.sources:
        return "推导过程没有给出源战术或证据"
    if card.provenance == "adapted" and not derivation.operators:
        return "改编候选没有使用任何改编算子"
    options: set[str] = set()
    for choice in derivation.combination:
        known = catalog.dimensions.get(choice.dimension)
        if known is None or choice.option not in known[1]:
            card.notes.append(f"组合选项 {choice.dimension}={choice.option} 不在维度表中")
        options.add(choice.option)
    if card.provenance == "combined" and not derivation.combination:
        return "组合候选没有给出维度选项"
    clash = catalog.clash(options)
    if clash:
        return f"组合不相容：{clash}"
    rank = sum(catalog.operators[op][1] for op in derivation.operators)
    card.change_size = rank + len(derivation.changes) + (3 if card.provenance == "combined" else 0)
    if any(e.basis == "unsupported" for _, e in _elements(card)):
        card.notes.append("含证据中没有的新要素（unsupported），需补充调研")
    return None


def _misses_gap(card: TacticCard, gap: Gap) -> str | None:
    """Reason an idea does not fill the gap it was generated for, or None."""
    if gap.kind == "uncovered_clause" and gap.target not in card.covers:
        return f"没有覆盖缺口条款「{gap.target}」"
    if gap.kind == "dimension":
        sections = _DIMENSION_SECTIONS.get(gap.target)
        if sections and not any(getattr(card, name) for name in sections):
            return f"没有给出维度「{gap.target}」的内容"
    return None


# --------------------------------------------------------------------------- ranking


def score_card(card: TacticCard, package: EvidencePackage, profile: MissionProfile | None) -> Score:
    clauses = profile.user_specified if profile else []
    coverage = len(card.covers) / len(clauses) if clauses else 1.0
    context = card.unit_match
    elements = [e for _, e in _elements(card)]
    cited = [e for e in elements if e.basis == "evidence"]
    fraction = len(cited) / len(elements) if elements else 0.0
    quality = {e.evidence_id: e.quality_score for e in package.evidence}
    used = [quality[i] for e in cited for i in e.evidence if i in quality]
    mean_quality = sum(used) / len(used) if used else 0.5
    strength = fraction * (0.5 + 0.5 * mean_quality)
    total = 0.45 * coverage + 0.2 * context + 0.35 * strength
    return Score(
        coverage=round(coverage, 3),
        context=round(context, 3),
        evidence_strength=round(strength, 3),
        total=round(total, 3),
    )


def features(card: TacticCard) -> set[str]:
    """What makes two tactics substantively different: the techniques they are
    built from, patterns, formations and decision authority. Step names are
    free text and vary between generations, so they are used only when a card
    lists no techniques."""
    out = {f"pattern:{p}" for p in card.patterns}
    out |= {f"technique:{t}" for t in card.techniques}
    out |= {f"formation:{squash(g.formation)}" for g in card.geometry}
    out.add(f"authority:{card.decision_authority}")
    if not card.techniques:
        out |= {f"phase:{squash(s.phase)}" for s in card.steps}
    return out


def similarity(a: TacticCard, b: TacticCard) -> float:
    fa, fb = features(a), features(b)
    return len(fa & fb) / len(fa | fb) if fa | fb else 1.0


def select_retrieved(
    pool: list[TacticCard], settings: ProposalSettings
) -> tuple[list[TacticCard], list[str]]:
    """Ranked, de-duplicated retrieved candidates (before innovation)."""
    notes: list[str] = []
    selected: list[TacticCard] = []
    for card in sorted(pool, key=lambda c: (-c.score.total, c.id)):
        if card.conflicts_with_user:
            conflicts = "；".join(card.conflicts_with_user)
            notes.append(f"{card.id} 与用户指定约束冲突（{conflicts}），未入选")
            continue
        twin = next((s for s in selected if similarity(s, card) >= settings.merge_similarity), None)
        if twin is not None:
            twin.variants.append(f"{card.id} {card.name}")
            notes.append(f"{card.id} 与 {twin.id} 高度相似，作为其变体")
            continue
        selected.append(card)
    return selected, notes


def find_gaps(
    selected: list[TacticCard],
    profile: MissionProfile | None,
    readiness: ReadinessAssessment | None,
    settings: ProposalSettings,
    fragments: int = 0,
) -> list[Gap]:
    gaps: list[Gap] = []
    covered = {c for card in selected for c in card.covers}
    for clause in profile.user_specified if profile else []:
        if clause in covered:
            continue
        parameter = bool(_QUANTITY.search(clause))
        gaps.append(
            Gap(
                id="",
                kind="uncovered_clause",
                detail=f"需求条款「{clause}」没有被任何完整检索战术的证据内容覆盖"
                + (
                    "（参数类条款：只做补充检索，不做创新补位，按用户指定处理）"
                    if parameter
                    else ""
                ),
                target=clause,
                fills_needed=0 if parameter else 1,
            )
        )
    usable = [c for c in selected if c.context_match != "different"]
    if len(usable) < settings.min_retrieved:
        others = len(selected) - len(usable)
        gaps.append(
            Gap(
                id="",
                kind="too_few_retrieved",
                detail=(
                    f"适用于本任务的完整检索战术只有 {len(usable)} 个（少于 "
                    f"{settings.min_retrieved}"
                    + (f"；另有 {others} 个来自不同类型单位，不计入" if others else "")
                    + (f"；另有 {fragments} 个不完整，未作为候选" if fragments else "")
                    + "），需要与现有战术有明显差异的完整战术"
                ),
                fills_needed=settings.min_retrieved - len(usable),
            )
        )
    for row in readiness.dimensions if readiness else []:
        if not row.covered:
            gaps.append(
                Gap(
                    id="",
                    kind="dimension",
                    detail=f"研究维度「{row.label}」缺少强证据，候选战术的这部分需要补充",
                    target=row.dimension,
                )
            )
    gaps.sort(key=lambda g: _GAP_ORDER[g.kind])
    for index, gap in enumerate(gaps, start=1):
        gap.id = f"gap_{index:02d}"
    return gaps


def merge_packages(base: EvidencePackage, extra: EvidencePackage, tag: str) -> EvidencePackage:
    """Union of two evidence packages; claims from `extra` get prefixed IDs."""
    merged = base.model_copy(deep=True)
    known = {e.evidence_id for e in merged.evidence}
    merged.evidence += [e for e in extra.evidence if e.evidence_id not in known]
    sources = {s.source_id for s in merged.sources}
    merged.sources += [s for s in extra.sources if s.source_id not in sources]
    for claim in extra.claims:
        copy = claim.model_copy(deep=True)
        copy.claim_id = f"{tag}_{claim.claim_id}"
        merged.claims.append(copy)
    merged.dropped += extra.dropped
    merged.content_hash = package_hash(merged)
    return merged


# --------------------------------------------------------------------------- proposer


@dataclass
class TacticProposer:
    runner: ModelRunner
    settings: ProposalSettings = field(default_factory=ProposalSettings)
    patterns: PatternTable = field(default_factory=PatternTable.default)
    catalog: InnovationCatalog = field(default_factory=InnovationCatalog.default)
    retriever: Retriever | None = None

    async def propose(
        self,
        requirement: str,
        package: EvidencePackage,
        *,
        profile: MissionProfile | None = None,
        readiness: ReadinessAssessment | None = None,
        package_ref: str = "",
    ) -> ProposalResult:
        result = ProposalResult(
            requirement=requirement.strip(),
            evidence_package=package_ref,
            max_candidates=self.settings.max_candidates,
            max_innovative=self.settings.max_innovative,
            prompt_version=PROPOSAL_PROMPT_VERSION,
        )
        pool, techniques = await self._extract(requirement, package, profile, "tactic.extract")
        selected, notes = select_retrieved(_complete(pool), self.settings)
        gaps = find_gaps(selected, profile, readiness, self.settings, _fragments(pool))

        if gaps and self.retriever is not None:
            result.notes.append("材料不足，补充检索：" + "；".join(g.detail for g in gaps))
            extra = await self.retriever(gaps)
            result.retrieval_rounds = 1
            if extra is not None and extra.evidence:
                package = merge_packages(package, extra, "r2")
                pool, techniques = await self._extract(
                    requirement, package, profile, "tactic.extract:r2"
                )
                selected, notes = select_retrieved(_complete(pool), self.settings)
                gaps = find_gaps(selected, profile, readiness, self.settings, _fragments(pool))
            else:
                result.notes.append("补充检索没有得到新证据")
        elif gaps:
            result.notes.append("材料不足，但未配置补充检索")
        for card in pool:
            if not card.complete:
                result.notes.append(
                    f"{card.id}「{card.name}」不是完整战术（{'；'.join(card.completeness_issues)}），"
                    "只作为战术动作参考"
                )
        result.notes += notes
        result.retrieved_pool = [c for c in pool if c.complete]
        result.fragments = [c for c in pool if not c.complete]
        result.techniques = techniques
        result.gaps = gaps

        innovative = (
            await self._fill_gaps(requirement, package, profile, gaps, selected, result)
            if gaps
            else []
        )

        keep = max(0, self.settings.max_candidates - len(innovative))
        if len(selected) > keep:
            for card in selected[keep:]:
                result.notes.append(f"{card.id} 因候选数上限让位给创新补位候选")
        result.candidates = selected[:keep] + innovative
        if not gaps:
            result.notes.append("检索战术足够，未生成创新候选")
        return result

    async def _extract(
        self,
        requirement: str,
        package: EvidencePackage,
        profile: MissionProfile | None,
        stage: str,
    ) -> tuple[list[TacticCard], list[Technique]]:
        prompt = extraction_prompt(requirement, profile, package, self.patterns)
        draft = await self.runner(stage, prompt, ExtractionResult)
        techniques = _check_techniques(draft.techniques, package, profile, requirement)
        cards = []
        for index, tactic in enumerate(draft.tactics, start=1):
            card, _ = check_card(
                tactic,
                card_id=f"tc_{index:02d}",
                provenance="retrieved",
                package=package,
                profile=profile,
                patterns=self.patterns,
                requirement=requirement,
            )
            card.completeness_issues = assess_completeness(card, profile, self.settings)
            card.complete = not card.completeness_issues
            card.score = score_card(card, package, profile)
            review_flags(card, _items(package), profile, requirement)
            cards.append(card)
        return cards, techniques

    async def _fill_gaps(
        self,
        requirement: str,
        package: EvidencePackage,
        profile: MissionProfile | None,
        gaps: list[Gap],
        selected: list[TacticCard],
        result: ProposalResult,
    ) -> list[TacticCard]:
        budget = min(self.settings.max_innovative, self.settings.max_candidates)
        chosen: list[TacticCard] = []
        idea_index = 0
        for gap in gaps:
            if gap.fills_needed <= 0:
                continue
            if budget <= 0:
                limit = self.settings.max_innovative
                result.notes.append(f"{gap.id} 未补位：创新候选已达上限 {limit}")
                continue
            wanted = min(gap.fills_needed, budget)
            count = self.settings.ideas_per_gap + wanted - 1
            prompt = innovation_prompt(
                requirement,
                profile,
                package,
                gap,
                selected + chosen,
                result.techniques,
                self.catalog,
                self.patterns,
                count,
            )
            draft = await self.runner(f"tactic.innovate:{gap.id}", prompt, InnovationResult)
            candidates: list[TacticCard] = []
            for idea in draft.ideas[:count]:
                idea_index += 1
                card, reason = self._check_idea(
                    idea, f"idea_{idea_index:02d}", package, profile, requirement
                )
                card.fills_gap = gap.id
                review_flags(card, _items(package), profile, requirement, gap)
                reason = reason or _misses_gap(card, gap)
                if reason is not None:
                    result.ideas.append(
                        Idea(card=card, gap=gap.id, status="rejected", reason=reason)
                    )
                    continue
                candidates.append(card)
            candidates.sort(key=lambda c: (c.change_size, -c.score.total, c.id))
            taken = 0
            for card in candidates:
                twin = next(
                    (
                        s
                        for s in selected + chosen
                        if similarity(s, card) >= self.settings.merge_similarity
                    ),
                    None,
                )
                if taken >= wanted:
                    result.ideas.append(
                        Idea(card=card, gap=gap.id, status="not_selected", reason="本缺口已补足")
                    )
                elif twin is not None:
                    result.ideas.append(
                        Idea(
                            card=card,
                            gap=gap.id,
                            status="not_selected",
                            reason=f"与 {twin.id} 过于相似",
                        )
                    )
                else:
                    taken += 1
                    budget -= 1
                    chosen.append(card)
                    gap.filled_by.append(card.id)
                    result.ideas.append(Idea(card=card, gap=gap.id, status="selected"))
            if taken < wanted:
                result.notes.append(f"{gap.id} 只补上 {taken}/{wanted} 个候选")
        # final IDs continue after the retrieved pool
        start = len(result.retrieved_pool)
        for offset, card in enumerate(chosen, start=1):
            old = card.id
            card.id = f"tc_{start + offset:02d}"
            for gap in gaps:
                gap.filled_by = [card.id if x == old else x for x in gap.filled_by]
            card.notes.insert(0, f"由补位构想 {old} 入选")
        return chosen

    def _check_idea(
        self,
        idea: IdeaDraft,
        card_id: str,
        package: EvidencePackage,
        profile: MissionProfile | None,
        requirement: str,
    ) -> tuple[TacticCard, str | None]:
        card, reason = check_card(
            idea,
            card_id=card_id,
            provenance=idea.provenance,
            package=package,
            profile=profile,
            patterns=self.patterns,
            requirement=requirement,
            derivation=idea.derivation.model_copy(deep=True),
            catalog=self.catalog,
        )
        card.score = score_card(card, package, profile)
        card.completeness_issues = assess_completeness(card, profile, self.settings)
        card.complete = not card.completeness_issues
        if reason is None and not card.complete:
            reason = "不是完整战术：" + "；".join(card.completeness_issues)
        if reason is None and card.conflicts_with_user:
            reason = "与用户指定约束冲突：" + "；".join(card.conflicts_with_user)
        return card, reason


def _complete(pool: list[TacticCard]) -> list[TacticCard]:
    return [card for card in pool if card.complete]


def _fragments(pool: list[TacticCard]) -> int:
    return sum(1 for card in pool if not card.complete)


def _check_techniques(
    techniques: list[Technique],
    package: EvidencePackage,
    profile: MissionProfile | None,
    requirement: str,
) -> list[Technique]:
    items = {e.evidence_id: e for e in package.evidence}
    sources = {s.source_id: s for s in package.sources}
    mission = mission_unit(profile, requirement)
    out = []
    for technique in techniques:
        technique = technique.model_copy(deep=True)
        technique.evidence = [e for e in technique.evidence if e in items]
        kinds = {
            evidence_match(items[e], sources.get(items[e].source_id), mission)[1]
            for e in technique.evidence
        } - {None}
        technique.unit_type = "、".join(sorted(_UNIT_LABEL[k] for k in kinds)) or "通用"
        out.append(technique)
    return out


def _items(package: EvidencePackage) -> dict[str, EvidenceItem]:
    return {e.evidence_id: e for e in package.evidence}
