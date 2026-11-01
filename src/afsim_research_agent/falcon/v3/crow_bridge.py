"""Crow → Falcon v3: the tactic the user selected in Crow becomes the v3 drafting input.

Crow (research → candidate tactics → red team → the user's ``tactic_selection.yaml``)
ends with a selected tactic; Falcon v3 starts from a request. This module joins them:

* the request names the selection (``tactic_selection``, a ``tactic_selection.yaml`` or
  the Crow tactics directory holding it, relative to the request file) — the selection
  is loaded and checked by :func:`afsim_research_agent.falcon.tactic.load_selection`
  (missing candidate, revised after selection, unknown phase: refused);
* the requirement every stage sees is the user's text plus the selected tactic in words
  (narrative, steps, formations, exceptions), with the rule that the draft implements the
  tactic rather than designing another;
* the tactic's steps, formations and exceptions become requirement clauses
  (``tc_01_s1`` …) next to the decomposed ones, so ``implements`` has to cover them and
  the obligations are written from them;
* roles come from the mission profile's own forces (or the cards' roles), named by
  ``role_map`` (Crow's role text → role id, e.g. ``排长车: leader``) or by a small
  keyword table; the slices come from the capability needs Crow annotated (C4b);
* Crow's capability annotation (C4b) goes along: for every need of the selected cards,
  the capability-set actions / subtrees Crow mapped it to and how (ready, composable,
  simplified …). Needs Crow found expressible must be built from those, not declared as
  gaps — a gap blocks the AFSIM run; a gap whose closest need Crow mapped is an error
  (``CROW_GAP``) the repair loop sees, with the mapping to use instead;
* the package keeps the selection (``selected_tactics.yaml``), Crow's estimated
  capability gaps (``crow_capability_gaps.yaml``) and a review section.

Nothing here calls a model.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from afsim_research_agent.falcon.v3.draft import RoleHint, V3Request

# Crow role text → role id when the request gives no role_map (first match wins)
ROLE_WORDS: tuple[tuple[str, str], ...] = (
    ("连长", "company_leader"),
    ("排长", "leader"),
    ("长车", "leader"),
    ("长机", "leader"),
    ("指挥", "leader"),
    ("僚车", "member"),
    ("僚机", "member"),
    ("成员", "member"),
    ("跟随", "member"),
    ("侦察", "scout"),
    ("掩护", "cover"),
    # the mission profile writes role hints in English
    ("company", "company_leader"),
    ("leader", "leader"),
    ("command", "leader"),
    ("member", "member"),
    ("wing", "member"),
    ("follower", "member"),
    ("scout", "scout"),
    ("recon", "scout"),
    ("cover", "cover"),
)

TACTIC_RULES = (
    "选定战术（Crow 检索资料、红队评审后由用户确认；草稿要实现这个战术，不要另行设计）：\n"
    "- 每个阶段、队形和例外都要落到具体的状态、转换、条件或子树上，并在 implements 中写出对应的"
    "条款编号（tc_xx_sN / tc_xx_gN / tc_xx_xN）；\n"
    "- 资料没有给出的数值照常定义为参数（basis 写 unknown），由人工复核；\n"
    "- 能力集表达不了的要素写进 gaps 并说明，不要挂在无关的元素上；\n"
    "- 组合了多个候选时，按列出的顺序衔接各阶段；只选了部分阶段的候选，只实现列出的阶段。"
)


# Crow need statuses (CapabilityNeed.status) for which a gap is the honest answer
GAP_STATUSES = frozenset({"partial", "confirm", "ir_extension", "develop"})

CAPABILITY_RULES = (
    "能力映射（Crow 已逐项核对能力集；草稿必须按此实现）：\n"
    "- 标为可直接实现 / 可组合实现 / 简化处理的要素，用列出的动作或子树实现（简化处理按说明的简化"
    "方式落地，例如广播模式改为写共享目标或分配结果、超时自选目标用回退动作），**不要写 gaps**；\n"
    "- 只有标为需确认 / 需扩展 / 需开发 / 部分可实现的要素才可以写 gaps；\n"
    "- 带 gaps 占位的模型不会进入 AFSIM 运行。"
)


def _tokens(ref: Any) -> str:
    return str(ref).split(":")[-1].strip()


def _known(token: str, contracts: Any) -> bool:
    return token in contracts.actions or token in contracts.tactics


def _selected_steps(part: Any) -> set[int] | None:
    return None if part.whole else {i for i, _ in part.steps()}


def capability_map(tactic: Any, contracts: Any) -> list[dict[str, Any]]:
    """Crow's capability needs of the selected cards (phases taken only), with the
    references that exist in this capability set."""
    out: list[dict[str, Any]] = []
    for part in tactic.parts:
        card = part.card
        keep = _selected_steps(part)
        for need in card.capability_needs or []:
            source = str(getattr(need, "source", "") or "")
            m = re.match(r"steps\[(\d+)\]", source)
            if keep is not None and (m is None or int(m.group(1)) not in keep):
                continue
            refs = [t for t in (_tokens(r) for r in need.maps_to) if _known(t, contracts)]
            status = str(getattr(need, "status", "") or "")
            out.append(
                {
                    "card": card.id,
                    "need": str(getattr(need, "need", "") or ""),
                    "source": source,
                    "status": status,
                    "label": str(getattr(need, "label", "") or status),
                    "refs": refs,
                    "why": str(getattr(need, "why", "") or ""),
                    "notes": [str(n) for n in (getattr(need, "notes", None) or [])],
                    "gap_ok": status in GAP_STATUSES or not refs,
                }
            )
    return out


def capability_text(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return ""
    lines = [CAPABILITY_RULES]
    for e in entries:
        refs = "、".join(e["refs"]) or "（能力集中没有对应项）"
        how = f"；简化：{'；'.join(e['notes'])}" if e["notes"] and not e["gap_ok"] else ""
        lines.append(f"- {e['card']} {e['need']}（{e['source']}）：{e['label']} → {refs}{how}")
    return "\n".join(lines)


def _bigrams(text: str) -> set[str]:
    text = re.sub(r"[\s，。、；：（）()\[\]_.:-]+", "", text.lower())
    return {text[i : i + 2] for i in range(len(text) - 1)}


def _closest(text: str, entries: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, float]:
    mine = _bigrams(text)
    best, score = None, 0.0
    for e in entries:
        theirs = _bigrams(e["need"] + e["why"])
        if not mine or not theirs:
            continue
        s = len(mine & theirs) / len(mine)
        if s > score:
            best, score = e, s
    return best, score


def gap_problems(
    gaps: dict[str, Any], entries: list[dict[str, Any]], *, threshold: float = 0.2
) -> list[tuple[str, str]]:
    """(gap id, message) for gaps the selected tactic does not need: the closest Crow need
    was mapped onto the capability set."""
    if not entries:
        return []
    out: list[tuple[str, str]] = []
    for gid, spec in (gaps or {}).items():
        spec = spec if isinstance(spec, dict) else {}
        text = f"{gid} {spec.get('description') or ''}"
        need, score = _closest(text, entries)
        if need is None or need["gap_ok"] or score < threshold:
            continue
        how = f"（简化：{'；'.join(need['notes'])}）" if need["notes"] else ""
        out.append(
            (
                gid,
                f"CROW_GAP 缺口 {gid} 对应 Crow 已核对的要素「{need['need']}」"
                f"（{need['source']}，{need['label']}），能力集可以实现：用 "
                f"{'、'.join(need['refs'])} 实现{how}；删除 gaps.{gid}，并把树中 gap.{gid} "
                "的用法换成这些动作或子树。",
            )
        )
    return out


def resolve_selection(request: V3Request, request_path: Path) -> Path:
    path = Path(request.tactic_selection or "")
    return path if path.is_absolute() else (request_path.parent / path)


def tactic_text(tactic: Any) -> str:
    """The selected tactic in words, appended to the requirement every stage sees."""
    lines = [TACTIC_RULES]
    for part in tactic.parts:
        card = part.card
        taken = "全部" if part.whole else f"仅阶段 {'、'.join(part.phases or [])}"
        lines.append(f"\n战术 {card.id}《{card.name}》（{taken}）：{card.narrative.strip()}")
        if card.roles:
            lines.append(
                "角色："
                + "；".join(
                    r.role + (f"（{'、'.join(r.members)}）" if r.members else "")
                    for r in card.roles
                )
            )
        for index, step in part.steps():
            until = f"，直到{step.until}" if step.until else ""
            lines.append(f"- {card.id}_s{index + 1} 【{step.phase}】{step.do}{until}")
        if part.whole:
            for index, g in enumerate(card.geometry):
                extra = (f"（{g.when}）" if g.when else "") + (
                    f"，间距{g.spacing}" if g.spacing else ""
                )
                lines.append(f"- {card.id}_g{index + 1} 队形：{g.formation}{extra}")
            for index, x in enumerate(card.exceptions):
                lines.append(f"- {card.id}_x{index + 1} 例外：若{x.situation}，{x.then}")
        if card.assumptions:
            lines.append("假设：" + "；".join(card.assumptions))
    if tactic.user_notes:
        lines.append(f"\n用户选择备注：{tactic.user_notes}")
    return "\n".join(lines)


def tactic_clauses(tactic: Any) -> list[dict[str, Any]]:
    """The tactic's steps, formations and exceptions as v3 clauses (``origin: tactic``)."""
    out = []
    for c in tactic.clauses():
        out.append(
            {
                "id": c.id,
                "text": c.text,
                "kind": c.kind,
                "origin": "tactic",
                "evidence": list(c.evidence),
            }
        )
    return out


def role_id(text: str, role_map: dict[str, str]) -> str | None:
    """A role id for Crow's role text: the request's map (exact, then contained word), else
    the keyword table; None when nothing matches."""
    if text in role_map:
        return role_map[text]
    for word, rid in role_map.items():
        if word and word in text:
            return rid
    low = text.lower()
    return next((rid for word, rid in ROLE_WORDS if word in low), None)


def derive_roles(tactic: Any, role_map: dict[str, str]) -> tuple[dict[str, RoleHint], list[str]]:
    """Roles from the mission profile's own forces (count, platform class), else from the
    selected cards' roles; several Crow roles that map to one id are merged."""
    notes: list[str] = []
    entries: list[tuple[str, int, str]] = []  # (Crow text, count, class)
    profile = getattr(tactic, "profile", None)
    if profile is not None and profile.own_forces:
        for f in profile.own_forces:
            entries.append((f.role_hint, max(1, f.count), f.platform_class or "ground_vehicle"))
    else:
        for part in tactic.parts:
            for r in part.card.roles:
                entries.append((r.role, max(1, len(r.members) or 1), "ground_vehicle"))
    roles: dict[str, RoleHint] = {}
    for text, count, cls in entries:
        rid = role_id(text, role_map) or f"role_{len(roles) + 1}"
        if not re.match(r"^[a-z][a-z0-9_]{0,31}$", rid):
            notes.append(f"角色 {text} 的 id {rid} 不合法，已改为 role_{len(roles) + 1}")
            rid = f"role_{len(roles) + 1}"
        if rid in roles:
            prev = roles[rid]
            roles[rid] = RoleHint(
                capability_class=prev.capability_class,
                count=prev.count + count,
                description=f"{prev.description}；{text}",
            )
            continue
        roles[rid] = RoleHint(capability_class=cls, count=count, description=text)
    if roles:
        notes.append(
            "角色由 Crow 任务画像得出："
            + "，".join(f"{k}={v.description}×{v.count}" for k, v in roles.items())
        )
    return roles, notes


def derive_slices(tactic: Any, contracts: Any) -> list[str]:
    """Slices of the capability needs Crow annotated for the selected cards (C4b)."""
    slice_ids = set(getattr(contracts, "slices", {}) or {})
    if not slice_ids:
        slice_ids = {a.slice for a in contracts.actions.values()} | {
            getattr(t, "slice", None) for t in contracts.tactics.values()
        }
        slice_ids.discard(None)
    found: list[str] = []

    def add(s: str | None) -> None:
        if s and s in slice_ids and s not in found:
            found.append(s)

    for part in tactic.parts:
        for need in part.card.capability_needs or []:
            for ref in need.maps_to:
                token = str(ref).split(":")[-1].strip()
                if token in contracts.actions:
                    add(contracts.actions[token].slice)
                elif token in contracts.tactics:
                    add(getattr(contracts.tactics[token], "slice", None))
                elif token in slice_ids:
                    add(token)
                else:
                    head = str(ref).split(":")[0].split(".")[0]
                    add(head)
    return found


def apply_selection(
    request: V3Request, request_path: Path, contracts: Any
) -> tuple[V3Request, Any, list[str]]:
    """The request with the selected tactic folded in; the loaded selection; notes."""
    from afsim_research_agent.falcon.tactic import load_selection

    tactic = load_selection(resolve_selection(request, request_path))
    notes = [f"输入来自 Crow 选定战术 {tactic.source_label}"]
    caps = capability_text(capability_map(tactic, contracts))
    update: dict[str, Any] = {
        "requirement": tactic.requirement(request.requirement)
        + "\n\n"
        + tactic_text(tactic)
        + (f"\n\n{caps}" if caps else "")
    }
    if not request.roles:
        roles, role_notes = derive_roles(tactic, request.role_map)
        if roles:
            update["roles"] = roles
        notes += role_notes
    if request.slices is None:
        slices = derive_slices(tactic, contracts)
        if slices:
            update["slices"] = slices
            notes.append("切片取自 Crow 的能力标注：" + "、".join(slices))
    if not request.display_name:
        update["display_name"] = "+".join(p.card.name for p in tactic.parts)[:60]
    for card_id, text in tactic.reference_assumptions():
        notes.append(f"{card_id} 的参考值假设需人工复核：{text}")
    return request.model_copy(update=update), tactic, notes


def write_crow_files(staging: Path, tactic: Any) -> None:
    (staging / "selected_tactics.yaml").write_text(tactic.selected_document(), encoding="utf-8")
    gaps = tactic.crow_gaps_document()
    if gaps:
        (staging / "crow_capability_gaps.yaml").write_text(gaps, encoding="utf-8")


def review_section(tactic: Any) -> list[str]:
    from afsim_research_agent.falcon.tactic import render_section

    return render_section(tactic, number="Crow")


def make_request(
    tactics_dir: Path,
    model_id: str,
    *,
    requirement: str = "",
    role_map: dict[str, str] | None = None,
    scenario_events: dict[str, str] | None = None,
    candidates: int = 3,
    base: Path | None = None,
) -> dict[str, Any]:
    """A v3 request (as a dict to write next to the Crow run) that names the selection."""
    target = tactics_dir if base is None else Path(_relative(tactics_dir, base))
    out: dict[str, Any] = {
        "model_id": model_id,
        "tactic_selection": target.as_posix(),
        "candidates": candidates,
        "fix_steps": 40,
        "scenario_events": scenario_events or {"cancelled": "外部结束请求"},
    }
    if requirement:
        out["requirement"] = requirement
    if role_map:
        out["role_map"] = dict(role_map)
    return out


def _relative(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()
