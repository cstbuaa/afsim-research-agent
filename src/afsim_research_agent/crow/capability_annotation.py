"""C4b: annotate candidate tactics with the platform capabilities they need.

Runs after C4 has finished the candidates and never changes them, their ranking or the
selection (design crow-tactic-proposal-v1 §18). The model only maps tactic elements to
catalog entries; whether an entry exists and what it can do is decided here from the
catalog (capability.assess).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from afsim_research_agent.capability import (
    LABELS,
    RANK,
    CapabilityCatalog,
    CatalogError,
    Interface,
    Verdict,
    assess,
    assess_ref,
    gap_id,
    summarize,
)
from afsim_research_agent.capability.granularity import CapabilityPolicy, GroupTable
from afsim_research_agent.crow.mission import MissionProfile
from afsim_research_agent.crow.proposal_models import (
    SECTIONS,
    AlternativeResult,
    CapabilityGap,
    CapabilityNeed,
    FeasibilityInfo,
    GapUse,
    MappingReview,
    NeedDraft,
    NeedsDraft,
    ProposalResult,
    TacticCard,
)
from afsim_research_agent.falcon.llm import ModelRunner

ANNOTATION_PROMPT_VERSION = "crow-capability-2026.10.7"
_SKIP_KINDS = {"tool"}  # offline tools are never needed at run time
_GAP_RANK = {"confirm": 0, "ir_extension": 1, "develop": 2}

FORMAT_EXAMPLE = """{
  "needs": [
    {"need": "沿巡逻航线飞行", "source": "steps[0].do", "necessity": "required",
     "group": "maneuver", "maps_to": ["route_navigation:mobility_action.follow_route"],
     "why": "航线跟随就是按航点序列飞行", "alternatives": []},
    {"need": "发现目标后向地面站报告", "source": "steps[1].until", "necessity": "required",
     "group": "comms", "maps_to": ["message_exchange:communication_action.send_message"],
     "why": "报告即向指定对象发送一条类型化消息", "alternatives": []},
    {"need": "保持在僚机右后方", "source": "geometry[0]", "necessity": "substitutable",
     "group": "formation", "maps_to": ["formation_following"], "why": "编队跟随直接按相对领队位置保持站位",
     "alternatives": [{"via": "长机广播位置，僚机自行解算站位",
                       "maps_to": ["message_exchange:communication_message.received_type",
                                   "own_kinematic_state:platform_state.position"]}]},
    {"need": "对来袭导弹实施拖曳诱饵干扰", "source": "exceptions[0].then",
     "necessity": "enhancing", "group": "ew", "maps_to": [], "why": "目录中没有诱饵干扰类条目",
     "missing": "拖曳诱饵干扰", "alternatives": []}
  ]
}"""


def platform_classes(profile: MissionProfile | None) -> list[str]:
    if profile is None:
        return []
    return list(dict.fromkeys(f.platform_class for f in profile.own_forces if f.platform_class))


def _cap_line(cap: Any, interfaces: Iterable[Interface]) -> str:
    uses = "、".join(cap.tactical_uses[:3])
    refs = "；".join(f"{i.ref}（{i.display_name}）" for i in interfaces)
    line = f"- {cap.id}（{cap.display_name}）"
    if cap.summary:
        line += f" 定义：{_clip(cap.summary, 60)}"
    if uses:
        line += f" 用途：{uses}"
    if refs:
        line += f" 接口：{refs}"
    return line


def coarse_digest(
    catalog: CapabilityCatalog, classes: Iterable[str], policy: CapabilityPolicy
) -> str:
    """Listing by tactical capability group, core groups first; detail items left out."""
    wanted = set(classes)
    table = policy.table
    core = (
        set().union(*(policy.precise_groups(c) for c in wanted))
        if wanted
        else set(policy.precise_groups(None))
    )
    lines: list[str] = []
    order = sorted(table.groups.values(), key=lambda g: (g.id not in core, g.id))
    for group in order:
        caps = []
        for cap_id in sorted(group.capabilities | group.detail):
            cap = catalog.get(cap_id)
            if cap is None or cap.kind in _SKIP_KINDS:
                continue
            if wanted and cap.platform_classes and not wanted & set(cap.platform_classes):
                continue
            if policy.hides(group.id, table.is_detail(cap_id)):
                continue
            interfaces = [
                i for i in cap.interfaces if not policy.hides(group.id, table.is_detail(i.ref))
            ]
            caps.append(_cap_line(cap, interfaces))
        if not caps:
            continue
        role = (
            "核心：逐项核对"
            if group.id in core
            else f"辅助：缺失或未确认时简化处理（{group.simplification}）"
        )
        lines.append(f"## {group.display_name}（group: {group.id}）【{role}】")
        lines += caps
    return "\n".join(lines)


def catalog_digest(
    catalog: CapabilityCatalog,
    classes: Iterable[str],
    policy: CapabilityPolicy | None = None,
) -> str:
    """Compact, formal-layer-only listing: by capability group for coarse drafts, otherwise
    by domain (id, name, definition, use, interface refs)."""
    if policy is not None and policy.coarse:
        return coarse_digest(catalog, classes, policy)
    wanted = set(classes)
    lines: list[str] = []
    by_domain: dict[str, list] = {}
    for cap in catalog.capabilities.values():
        if cap.kind in _SKIP_KINDS:
            continue
        if wanted and cap.platform_classes and not wanted & set(cap.platform_classes):
            continue
        by_domain.setdefault(cap.domain, []).append(cap)
    for domain in sorted(by_domain):
        lines.append(f"## {catalog.domains.get(domain, domain)}（{domain}）")
        for cap in sorted(by_domain[domain], key=lambda c: c.id):
            lines.append(_cap_line(cap, cap.interfaces))
    return "\n".join(lines)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _card_elements(card: TacticCard) -> str:
    out = [f"名称：{card.name}", f"战术描述：{card.narrative or card.summary}"]
    for section in SECTIONS:
        for i, element in enumerate(getattr(card, section)):
            data = element.model_dump(exclude={"evidence", "basis"})
            text = "；".join(f"{k}={v}" for k, v in data.items() if v not in ("", [], None))
            out.append(f"{section}[{i}]：{text}")
    out.append(f"decision_authority：{card.decision_authority}")
    return "\n".join(out)


FINE_SCOPE = "1. 逐项检查战术要素：每个步骤的 do 和 until、每个例外的 situation 和 then、需要在运行中判断的适用情况、队形保持（需要自身位置和参照车辆位置）、协同中的报告、命令、确认。静态的编成与角色分配不需要列出。"
COARSE_SCOPE = """1. 粗粒度：这是初步方案，只列出战术成立所需的能力，一般 5 到 10 项。
   - 每项写明所属能力组 group（下面目录中的组 ID）。
   - 同一能力组内的同类要素合并为一项（例如"保持楔形站位"与"保持纵队车距"合并为"保持编队站位"，几处报告合并为"向排长报告"）。
   - 不要列：燃油、弹药余量、设备开关、通信链路细节；状态机、计时、黑板变量等行为模型运行机制；静态的编成与角色分配。
   - 协同与决策逻辑（谁何时跃进、角色轮换、指挥接替、按预案执行）由行为模型实现，不是平台能力，不要写进 missing；只列这些逻辑要用到的平台动作和感知。
   - 辅助能力组的条目缺失时会按目录标注的方式简化处理，照常列出即可。"""


def annotation_prompt(
    card: TacticCard,
    digest: str,
    classes: list[str],
    policy: CapabilityPolicy | None = None,
) -> str:
    platform = "、".join(classes) or "未说明"
    scope = COARSE_SCOPE if policy is not None and policy.coarse else FINE_SCOPE
    return f"""你是作战仿真平台的能力分析员。下面是一个已经定稿的候选战术，请列出执行它需要平台具备的能力，并对应到能力目录条目。

只做对应，不判断平台是否支持；是否支持由程序查目录决定。不要修改、评价或补充战术本身。

## 规则
{scope}
2. need 用中文写清"平台要能做什么"。source 只能写下面"候选战术"中出现的要素路径（如 steps[2].until、exceptions[0].then、geometry[1]、applicability[0].situation、decision_authority），不能写证据编号（如 t3、t6）。
3. maps_to 只能使用下面目录中出现的引用：能力 ID，或"能力ID:接口"。优先写到接口。
4. maps_to 里的条目表示"要同时具备"。如果几个条目是"任选其一"的关系（例如用地形类别或用区域判断都能判断是否进入城区），不要放在一起：选最直接的一个写进 maps_to，其余作为 alternatives，necessity 写 substitutable。
5. 两种情况算对应：(a) 条目本身就是在做这件事；(b) 这件事由几个条目加上简单计算（比较、求距离、计时、计数）就能完成，此时把这些条目都列出，例如"与领车保持 50 米间距" = 自身位置 + 编队相对位置。不算对应：为别的用途设计的条目（例如事后统计的覆盖评估不是运行中的火力扇区判断，毁伤效果不是安全距离），以及名字相近但意思不同的条目。
6. 目录条目只覆盖一部分时，maps_to 写已覆盖的部分，missing 用简短名词写出目录里没有的那部分（例如"敌方火力范围"、"火力扇区几何"）；完全没有对应时 maps_to 留空、missing 写所需的能力。它们会成为待开发缺口，这正是需要的结果。缺少的部分属于另一个能力组时，在 missing 前加组 ID 和冒号（例如 "fires:火力扇区几何"）。如果缺少的只是一个数值或参数（射程、纵深、距离阈值、间隔、区域边界等），不要写 missing，改写 reference："名称≈参考值 单位"（例如 "主炮有效射程≈2000 米"），按参考值处理，不算缺口。maps_to 只写目录引用本身，不要加组 ID 前缀。
7. why 用一句话说明这些条目为什么就是这项需求（或其组成部分）；说不清就说明不该对应。
8. decision_authority 只在战术要求运行中判断权限时才列；编制上的指挥关系不列。
9. necessity：required = 缺了战术不成立；enhancing = 缺了战术仍成立，效果变差；substitutable = 有其他实现路径，此时在 alternatives 中列出其他路径（via 写做法，maps_to 写对应条目）。
10. 同一能力被多个要素用到时，细粒度下每个要素各列一条，粗粒度下合并为一条（source 写最主要的要素）。
11. 只输出 JSON，格式与下例相同（示例取自无关领域，只示范结构）：
{FORMAT_EXAMPLE}

## 平台类别
{platform}

## 候选战术
{_card_elements(card)}

## 能力目录（正式层）
{digest}
"""


def _entry_text(catalog: CapabilityCatalog, ref: str) -> str:
    cap_id, _, ir = ref.partition(":")
    cap = catalog.get(cap_id)
    if cap is None:
        return ref
    text = f"{ref}：{cap.display_name}。定义：{_clip(cap.summary, 120) or '无'}"
    if ir and (iface := cap.interface(ir)) is not None:
        text += f" 接口：{iface.display_name}。"
    if cap.tactical_uses:
        text += f" 典型用途：{'、'.join(cap.tactical_uses[:3])}。"
    if cap.limitations:
        text += f" 限制：{_clip(cap.limitations[0], 60)}"
    return text


def review_prompt(needs: list[NeedDraft], catalog: CapabilityCatalog) -> str | None:
    """Second look at the mapping with full entry definitions (an external check on C4b)."""
    rows = []
    for i, need in enumerate(needs):
        refs = list(need.maps_to) + [r for a in need.alternatives for r in a.maps_to]
        refs = [r for r in dict.fromkeys(refs) if catalog.get(r.partition(":")[0])]
        if not refs:
            continue
        entries = "\n".join(f"   - {_entry_text(catalog, r)}" for r in refs)
        rows.append(f"{i}. 需求：{need.need}（{need.source}）\n   对应条目：\n{entries}")
    if not rows:
        return None
    body = "\n".join(rows)
    return f"""你是作战仿真平台能力目录的审核员。下面每项"需求"被对应到了能力目录条目。请逐条核对：条目的定义是否就是在做这项需求。

判定标准：
- 条目定义就是这件事，或是完成这件事的直接组成部分（由它加上比较、求距离、计时等简单计算即可完成），保留。例如自身位置之于保持间距，定点机动之于缩短跃进距离。
- 为别的用途设计的条目（例如事后统计评估、离线分析、别的武器或平台类型），以及名字相近但意思不同的条目，去掉。
- 保留下来的条目合起来仍缺一块关键能力时，在 missing 中用简短名词写出缺的部分（例如"敌方火力范围"）。
- 不确定时保留。不要新增条目，不要评价战术。

只输出需要去掉条目或仍有缺失的需求，格式：
{{"items": [{{"index": 3, "drop": ["能力ID:接口"], "missing": "", "reason": "一句话说明"}}]}}
全部合适时输出 {{"items": []}}。

## 需求与对应条目
{body}
"""


def apply_review(needs: list[NeedDraft], review: MappingReview) -> dict[int, list[str]]:
    """Drop the rejected references; returns notes per need index."""
    notes: dict[int, list[str]] = {}
    for item in review.items:
        if not 0 <= item.index < len(needs):
            continue
        need = needs[item.index]
        if item.missing.strip() and not need.missing.strip():
            need.missing = item.missing.strip()
            notes.setdefault(item.index, []).append(f"复核指出缺少 {need.missing}")
        drop = {r.strip() for r in item.drop}
        removed = [r for r in need.maps_to if r in drop]
        need.maps_to = [r for r in need.maps_to if r not in drop]
        for alt in need.alternatives:
            removed += [r for r in alt.maps_to if r in drop]
            alt.maps_to = [r for r in alt.maps_to if r not in drop]
        if removed:
            reason = f"：{item.reason}" if item.reason else ""
            notes.setdefault(item.index, []).append(
                f"复核去掉 {', '.join(dict.fromkeys(removed))}{reason}"
            )
    return notes


class CapabilityAnnotator:
    """Map each candidate's needs to the catalog and judge them (C4b).

    ``review`` adds a second model call per candidate that checks each mapping against the
    full entry definitions and drops the ones that are not the same thing."""

    def __init__(
        self,
        runner: ModelRunner,
        catalog: CapabilityCatalog,
        *,
        review: bool = True,
        policy: CapabilityPolicy | None = None,
    ) -> None:
        """``policy``: coarse (default) or fine judgment, see ``capability.granularity``."""
        self.runner = runner
        self.catalog = catalog
        self.review = review
        self.policy = policy if policy is not None else CapabilityPolicy.make()

    async def _review(self, card: TacticCard, needs: list[NeedDraft]) -> dict[int, list[str]]:
        prompt = review_prompt(needs, self.catalog)
        if not self.review or prompt is None:
            return {}
        try:
            review = await self.runner(f"capability_review:{card.id}", prompt, MappingReview)
        except Exception as exc:  # noqa: BLE001 - keep the unreviewed mapping
            card.notes.append(f"能力映射复核失败（{exc.__class__.__name__}），保留原映射")
            return {}
        return apply_review(needs, review)

    async def annotate(
        self, result: ProposalResult, profile: MissionProfile | None
    ) -> list[CapabilityGap]:
        classes = platform_classes(profile)
        policy = self.policy
        digest = catalog_digest(self.catalog, classes, policy)
        for card in result.candidates:
            try:
                draft = await self.runner(
                    f"capability:{card.id}",
                    annotation_prompt(card, digest, classes, policy),
                    NeedsDraft,
                )
            except Exception as exc:  # noqa: BLE001 - annotation must never break C4
                card.notes.append(f"能力标注失败（{exc.__class__.__name__}），未给出可行性")
                continue
            review_notes = await self._review(card, draft.needs)
            judge_card(card, draft.needs, self.catalog, classes, review_notes, policy)
        result.capability_gaps = collect_gaps(result.candidates, self.catalog)
        result.catalog_version = self.catalog.content_hash
        result.capability_granularity = policy.summary()
        scope = "粗粒度" if policy.coarse else "细粒度"
        result.notes.append(
            f"能力标注（{scope}）：{len(result.candidates)} 个候选，"
            f"缺口 {len(result.capability_gaps)} 项（目录 {self.catalog.catalog_id}）"
        )
        return result.capability_gaps


# ---------------------------------------------------------------------- code-side judgment
def _valid_refs(refs: Iterable[str], catalog: CapabilityCatalog, notes: list[str]) -> list[str]:
    out = []
    for ref in refs:
        ref = ref.strip()
        if not ref:
            continue
        try:
            target = catalog.resolve(ref)
        except CatalogError:
            # coarse digests are headed by group IDs; models sometimes prefix them
            head, _, rest = ref.partition(":")
            try:
                if head not in GroupTable.default().groups or not rest:
                    raise CatalogError(ref)
                target = catalog.resolve(rest)
            except CatalogError:
                notes.append(f"目录中没有 {ref}，已忽略")
                continue
            notes.append(f"{ref} 去掉了能力组前缀 {head}")
            ref = rest
        if not isinstance(target, Interface) and target.kind in _SKIP_KINDS:
            notes.append(f"{ref} 是离线工具，不作为运行能力")
            continue
        out.append(ref)
    return list(dict.fromkeys(out))


def _worst_over_classes(
    catalog: CapabilityCatalog,
    uses: list[str],
    any_of: list[list[str]],
    classes: list[str],
    policy: CapabilityPolicy | None = None,
) -> Verdict:
    targets: list[str | None] = list(classes) or [None]
    verdicts = [assess(catalog, uses, any_of, cls, need_ir=True, policy=policy) for cls in targets]
    return max(verdicts, key=lambda v: RANK[v.conclusion])


def _need_group(draft: NeedDraft, refs: list[str], policy: CapabilityPolicy | None) -> str | None:
    table = (policy or CapabilityPolicy.make("fine")).table
    if draft.group.strip() in table.groups:
        return draft.group.strip()
    return next((g for r in refs if (g := table.group_of(r))), None)


# missing pieces that are data, not capabilities (ranges, depths, thresholds, boundaries)
_PARAMETER = re.compile(r"(射程|纵深|距离|间距|间隔|阈值|时长|半径|坐标|范围|参考值|数值|参数)")


def _simplified_missing(
    group: str | None, classes: list[str], policy: CapabilityPolicy | None
) -> bool:
    """A need nothing in the catalog covers is simplified only in a known supporting group."""
    if policy is None or group is None:
        return False
    return all(policy.simplifies(group, cls, False) for cls in (classes or [None]))


def card_sources(card: TacticCard) -> set[str]:
    """Element paths a need may cite: ``steps[0]`` (sub-fields allowed) and decision_authority."""
    paths = {"decision_authority"}
    for section in SECTIONS:
        paths |= {f"{section}[{i}]" for i in range(len(getattr(card, section)))}
    return paths


def _source_root(source: str) -> str:
    return source.strip().split(".", 1)[0]


def judge_need(
    draft: NeedDraft,
    catalog: CapabilityCatalog,
    classes: list[str],
    valid_sources: set[str] | None = None,
    policy: CapabilityPolicy | None = None,
) -> tuple[CapabilityNeed, Verdict]:
    """``policy`` None judges everything precisely (fine)."""
    notes: list[str] = []
    if valid_sources is not None:
        # coarse needs may merge several elements: "geometry[0], geometry[1]"
        paths = [p for p in re.split(r"[,，、;；\s]+", draft.source) if p]
        bad = [p for p in paths if _source_root(p) not in valid_sources]
        if bad or not paths:
            notes.append(f"来源 {draft.source} 不是战术要素路径")
    maps_to = _valid_refs(draft.maps_to, catalog, notes)
    alternatives = [
        (alt.via, _valid_refs(alt.maps_to, catalog, notes)) for alt in draft.alternatives
    ]
    alternatives = [(via, refs) for via, refs in alternatives if refs]
    necessity = draft.necessity
    if necessity == "substitutable" and not alternatives:
        necessity = "required"
        notes.append("标为可替代但没有给出有效的替代路径，按必需处理")
    if alternatives:
        paths = ([("目录对应", maps_to)] if maps_to else []) + alternatives
        verdict = _worst_over_classes(catalog, [], [refs for _, refs in paths], classes, policy)
        chosen = paths[verdict.chosen_alternative or 0][0]
        alt_results = [
            AlternativeResult(
                via=via,
                maps_to=refs,
                status=max(
                    (r.conclusion for r in verdict.alternatives[i]),
                    key=RANK.__getitem__,
                    default="develop",
                ),
            )
            for i, (via, refs) in enumerate(paths)
        ]
    else:
        verdict = _worst_over_classes(catalog, maps_to, [], classes, policy)
        chosen = None
        alt_results = []
    group = _need_group(draft, maps_to + [r for _, refs in alternatives for r in refs], policy)
    simplify_missing = _simplified_missing(group, classes, policy)
    if not maps_to and not alternatives:
        notes.append("目录中没有对应能力")
        notes += [r for r in verdict.reasons if r != "没有对应的目录条目"]
        if simplify_missing:
            assert policy is not None
            text = policy.simplification(group, False)
            notes.append(f"按简化处理：{text}")
            verdict = Verdict("simplified", reasons=[text])
    else:
        notes += verdict.reasons
    missing = draft.missing.strip()
    reference = draft.reference.strip()
    if missing and _PARAMETER.search(missing.partition(":")[2] or missing):
        # a number the platform cannot confirm is a reference value, not a capability gap
        reference = reference or f"{missing.partition(':')[2] or missing}≈（参考值待定）"
        missing = ""
    if reference:
        notes.append(f"按参考值处理：{reference}")
    # "fires:火力扇区几何": the missing part belongs to another group than the need
    head, sep, rest = missing.partition(":")
    table = (policy or CapabilityPolicy.make("fine")).table
    if sep and head.strip() in table.groups and rest.strip():
        missing = rest.strip()
        simplify_missing = _simplified_missing(head.strip(), classes, policy)
        if simplify_missing:
            group = group or head.strip()
            notes.append(f"缺少的部分属于{table.groups[head.strip()].display_name}组")
    if missing and simplify_missing:
        assert policy is not None
        missing_group = head.strip() if sep and head.strip() in table.groups else group
        notes.append(
            f"目录中缺少：{missing}，按简化处理：{policy.simplification(missing_group, False)}"
        )
        if RANK[verdict.conclusion] < RANK["simplified"]:
            verdict = Verdict(
                "simplified",
                refs=verdict.refs,
                alternatives=verdict.alternatives,
                chosen_alternative=verdict.chosen_alternative,
                reasons=verdict.reasons,
            )
        gaps, gap_type = _gaps_of(draft.need, verdict)
    elif missing:
        # part of the need is not in the catalog at all: the tactic needs development
        notes.append(f"目录中缺少：{missing}")
        verdict = Verdict(
            "develop",
            refs=verdict.refs,
            alternatives=verdict.alternatives,
            chosen_alternative=verdict.chosen_alternative,
            reasons=verdict.reasons,
        )
        ref_gaps = [gap_id(r.ref) for r in verdict.blocking_refs()]
        gaps, gap_type = list(dict.fromkeys([*ref_gaps, gap_id(missing)])), "develop"
    else:
        gaps, gap_type = _gaps_of(draft.need, verdict)
    need = CapabilityNeed(
        need=draft.need,
        source=draft.source,
        necessity=necessity,
        maps_to=maps_to,
        why=draft.why,
        missing=missing,
        reference=reference,
        group=group,
        status=verdict.conclusion,
        label=LABELS[verdict.conclusion],
        gaps=gaps,
        gap_type=gap_type,
        alternatives=alt_results,
        chosen=chosen,
        notes=list(dict.fromkeys(notes)),
    )
    return need, verdict


def _gaps_of(need_text: str, verdict: Verdict) -> tuple[list[str], str | None]:
    if verdict.gap_type is None:
        return [], None
    refs = verdict.blocking_refs()
    if not refs:  # nothing in the catalog maps: the gap is the need itself
        return [gap_id(need_text)], verdict.gap_type
    return list(dict.fromkeys(gap_id(r.ref) for r in refs)), verdict.gap_type


def judge_card(
    card: TacticCard,
    drafts: list[NeedDraft],
    catalog: CapabilityCatalog,
    classes: list[str],
    review_notes: dict[int, list[str]] | None = None,
    policy: CapabilityPolicy | None = None,
) -> None:
    needs: list[CapabilityNeed] = []
    rows = []
    sources = card_sources(card)
    for i, draft in enumerate(drafts):
        need, verdict = judge_need(draft, catalog, classes, sources, policy)
        if review_notes and i in review_notes:
            need.notes = review_notes[i] + need.notes
        needs.append(need)
        rows.append((need.necessity, verdict, need.gaps))
    card.capability_needs = needs
    # Crow grades platform capability only: an interface IR 1.0 cannot express yet is
    # recorded as an ir_extension gap but does not make the tactic "needs development";
    # the IR check (K3) decides it on the actual model.
    summary = summarize(rows, development=("develop",))
    card.feasibility = FeasibilityInfo(
        level=summary.level,
        counts={k: v for k, v in summary.counts.items() if v},
        blocking_gaps=list(summary.blocking),
    )
    card.required_capabilities = sorted(
        {ref.split(":", 1)[0] for n in needs for ref in n.maps_to}
        | {ref.split(":", 1)[0] for n in needs for a in n.alternatives for ref in a.maps_to}
    )


def collect_gaps(cards: Iterable[TacticCard], catalog: CapabilityCatalog) -> list[CapabilityGap]:
    """All gaps across candidates. Enhancing needs are listed too (not blocking)."""
    gaps: dict[str, CapabilityGap] = {}
    for card in cards:
        for need in card.capability_needs:
            if not need.gaps:
                continue
            refs = _gap_refs(need)
            for gid in need.gaps:
                ref = next((r for r in refs if gap_id(r) == gid), None)
                text = need.missing if need.missing and gid == gap_id(need.missing) else need.need
                contract = ref or text
                display = _display(catalog, ref) if ref else text
                gap_type = _gap_type(catalog, ref, need)
                gap = gaps.get(gid)
                if gap is None:
                    gap = gaps[gid] = CapabilityGap(
                        id=gid, contract=contract, display_name=display, gap_type=gap_type
                    )
                elif _GAP_RANK[gap_type] > _GAP_RANK[gap.gap_type]:
                    gap.gap_type = gap_type  # type: ignore[assignment]
                use = next((u for u in gap.used_by if u.candidate == card.id), None)
                if use is None:
                    use = GapUse(candidate=card.id)
                    gap.used_by.append(use)
                if need.source not in use.sources:
                    use.sources.append(need.source)
    for gap in gaps.values():
        gap.candidates_count = len(gap.used_by)
    return sorted(gaps.values(), key=lambda g: (-_GAP_RANK[g.gap_type], -g.candidates_count, g.id))


def _gap_refs(need: CapabilityNeed) -> list[str]:
    refs = list(need.maps_to)
    for alt in need.alternatives:
        refs += alt.maps_to
    return list(dict.fromkeys(refs))


def _display(catalog: CapabilityCatalog, ref: str) -> str:
    target = catalog.resolve(ref)
    if isinstance(target, Interface):
        cap = catalog.get(target.capability_id)
        return f"{cap.display_name if cap else target.capability_id} · {target.display_name}"
    return target.display_name


def _gap_type(catalog: CapabilityCatalog, ref: str | None, need: CapabilityNeed) -> str:
    if ref is None:
        return need.gap_type or "develop"
    conclusion = assess_ref(catalog, ref, need_ir=True).conclusion
    return {"confirm": "confirm", "ir_extension": "ir_extension"}.get(conclusion, "develop")


# ---------------------------------------------------------------------- re-judgment (K5)
def _drafts_from(needs: Iterable[CapabilityNeed], resolutions: dict[str, str]) -> list[NeedDraft]:
    """Rebuild the model's mapping from a stored annotation (no model call)."""
    drafts = []
    for need in needs:
        maps_to = list(need.maps_to)
        missing = need.missing
        for gid in need.gaps:
            ref = resolutions.get(gid)
            if ref and ref not in maps_to:
                maps_to.append(ref)  # a need-text gap implemented as a catalog entry
                if missing and gid == gap_id(missing):
                    missing = ""
        drafts.append(
            NeedDraft(
                need=need.need,
                source=need.source,
                necessity=need.necessity,
                group=need.group or "",
                maps_to=maps_to,
                why=need.why,
                missing=missing,
                reference=need.reference,
                alternatives=[
                    {"via": a.via, "maps_to": list(a.maps_to)}
                    for a in need.alternatives
                    if a.via != "目录对应"
                ],
            )
        )
    return drafts


def rejudge(
    result: ProposalResult,
    catalog: CapabilityCatalog,
    profile: MissionProfile | None,
    policy: CapabilityPolicy | None = None,
    resolutions: dict[str, str] | None = None,
) -> list[CapabilityGap]:
    """Judge stored annotations again against the current catalog (after write-back).

    The model's mapping is kept as it was; only the code-side judgment is repeated, so the
    feasibility of every candidate follows confirmed and implemented gaps. Selections of
    gaps are carried over."""
    if policy is None:
        recorded = result.capability_granularity or {}
        policy = CapabilityPolicy.make(
            recorded.get("granularity", "coarse"), recorded.get("refine") or ()
        )
    resolutions = resolutions or {}
    classes = platform_classes(profile)
    selected = {g.id for g in result.capability_gaps if g.selected}
    for card in result.candidates:
        if card.feasibility is None:
            continue  # annotation failed before; nothing to re-judge
        review_notes = {
            i: [n for n in need.notes if n.startswith("复核")]
            for i, need in enumerate(card.capability_needs)
        }
        drafts = _drafts_from(card.capability_needs, resolutions)
        judge_card(card, drafts, catalog, classes, review_notes, policy)
    result.capability_gaps = collect_gaps(result.candidates, catalog)
    for gap in result.capability_gaps:
        gap.selected = gap.id in selected
    result.catalog_version = catalog.content_hash
    result.capability_granularity = policy.summary()
    return result.capability_gaps
