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
* before drafting, every tactic clause is mapped onto the capability set (Crow's
  annotation, then Falcon's own check of each clause, :func:`blocked_elements`): a *core*
  element (a step or a formation) that the set cannot carry blocks the draft by default
  (``missing_capabilities: block``) — the model would otherwise imitate it with a look-alike
  action and a fitting state name; ``skip`` leaves such elements out (declared, not silent),
  ``wait`` keeps them as gaps; exceptions are peripheral and are left out in ``block`` too;
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

import yaml

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
    "- 组合了多个候选时，按列出的顺序衔接各阶段；只选了部分阶段的候选，只实现列出的阶段。"
)

# Crow need statuses (CapabilityNeed.status) the capability set cannot provide yet; the
# others (ready, composable, simplified, partial, confirm) are built from what Crow mapped
MISSING_STATUSES = frozenset({"ir_extension", "develop"})

CORE_KINDS = ("s", "g")  # steps and formations; exceptions (x) are peripheral


def is_core(clause_id: str) -> bool:
    m = re.search(r"_([sgx])\d+$", clause_id)
    return bool(m) and m.group(1) in CORE_KINDS


_RULES = {
    "block": (
        "能力映射（Crow 已逐项核对能力目录；先按目录匹配实现）：\n"
        "- 列出了动作或子树的要素，用列出的动作或子树实现；简化处理按说明的简化方式落地；\n"
        "- 标为**暂缺能力**的例外处理本次忽略：不实现、不写 gaps、不写进 implements；\n"
        "- 不得用做别的事的动作冒充条款，也不得只用状态名表示实现；本次模型不得有 gaps。"
    ),
    "skip": (
        "能力映射（Crow 已逐项核对能力目录；先按目录匹配实现）：\n"
        "- 列出了动作或子树的要素（可直接实现、可组合实现、简化处理、需确认、部分可实现），用列出的"
        "动作或子树实现；简化处理按说明的简化方式落地（例如广播模式改为写共享目标或分配结果、超时"
        "自选目标用回退动作）；\n"
        "- 标为**暂缺能力**的要素本次忽略：不实现、不写 gaps、不写进 implements，模型不为它设"
        "状态或转换；它们已记入能力开发队列，待能力开发后再补；\n"
        "- 本次模型不得有 gaps。"
    ),
    "wait": (
        "能力映射（Crow 已逐项核对能力目录；先按目录匹配实现）：\n"
        "- 列出了动作或子树的要素，用列出的动作或子树实现，**不要写 gaps**；简化处理按说明的简化"
        "方式落地；\n"
        "- 标为**暂缺能力**的要素写成 gaps（说明需要的能力），留给能力开发；带 gaps 的模型在能力"
        "开发完成前不会进入 AFSIM 运行。"
    ),
}


def _tokens(ref: Any) -> str:
    return str(ref).split(":")[-1].strip()


def _known(token: str, contracts: Any) -> bool:
    return token in contracts.actions or token in contracts.tactics


def _selected_steps(part: Any) -> set[int] | None:
    return None if part.whole else {i for i, _ in part.steps()}


_ELEMENT = {"steps": "s", "geometry": "g", "exceptions": "x"}


def element_ids(card_id: str, source: str) -> list[str]:
    """Clause ids of the card elements a need came from (``steps[0].do, steps[2].until``
    → tc_01_s1, tc_01_s3)."""
    out: list[str] = []
    for kind, index in re.findall(r"(steps|geometry|exceptions)\[(\d+)\]", source):
        cid = f"{card_id}_{_ELEMENT[kind]}{int(index) + 1}"
        if cid not in out:
            out.append(cid)
    return out


def capability_map(tactic: Any, contracts: Any) -> list[dict[str, Any]]:
    """Crow's capability needs of the selected cards (phases taken only), with the
    references that exist in this capability set; ``missing``: nothing in the set provides
    it yet."""
    out: list[dict[str, Any]] = []
    for part in tactic.parts:
        card = part.card
        keep = _selected_steps(part)
        for need in card.capability_needs or []:
            source = str(getattr(need, "source", "") or "")
            steps = {int(i) for i in re.findall(r"steps\[(\d+)\]", source)}
            if keep is not None and not steps & keep:
                continue
            refs = [t for t in (_tokens(r) for r in need.maps_to) if _known(t, contracts)]
            status = str(getattr(need, "status", "") or "")
            out.append(
                {
                    "card": card.id,
                    "elements": element_ids(card.id, source),
                    "need": str(getattr(need, "need", "") or ""),
                    "source": source,
                    "status": status,
                    "label": str(getattr(need, "label", "") or status),
                    "refs": refs,
                    "why": str(getattr(need, "why", "") or ""),
                    "missing_text": str(getattr(need, "missing", "") or ""),
                    "notes": [str(n) for n in (getattr(need, "notes", None) or [])],
                    "missing": status in MISSING_STATUSES or not refs,
                }
            )
    return out


def deferred_elements(entries: list[dict[str, Any]]) -> set[str]:
    """Card elements (clause ids) every capability need of which is missing."""
    by: dict[str, list[bool]] = {}
    for e in entries:
        for element in e["elements"]:
            by.setdefault(element, []).append(e["missing"])
    return {el for el, flags in by.items() if all(flags)}


def blocked_elements(
    clauses: list[dict[str, Any]],
    entries: list[dict[str, Any]],
    clause_map: dict[str, dict[str, Any]],
) -> dict[str, str]:
    """Tactic clauses the capability set cannot carry: clause id → why. A clause is blocked
    when Falcon's check names a missing capability, or Crow's annotation of its element
    has only missing needs."""
    out: dict[str, str] = {}
    crow: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        for el in e["elements"]:
            crow.setdefault(el, []).append(e)
    for c in clauses:
        if c.get("origin") != "tactic":
            continue
        cid = c["id"]
        m = clause_map.get(cid) or {}
        if m.get("missing"):
            out[cid] = str(m["missing"])
        elif crow.get(cid) and all(e["missing"] for e in crow[cid]):
            out[cid] = "；".join(e.get("missing_text") or e["need"] for e in crow[cid])
    return out


def map_text(clause_map: dict[str, dict[str, Any]], blocked: dict[str, str]) -> str:
    """The clause → actions mapping the draft must follow."""
    if not clause_map:
        return ""
    lines = ["条款与能力的对应（起草前已核对；按此实现，不要换成做别的事的动作）："]
    for cid, m in clause_map.items():
        if cid in blocked:
            lines.append(f"- {cid}：能力集暂缺（{blocked[cid]}），本次不实现，不写进 implements")
        elif m.get("actions"):
            how = f"：{m['how']}" if m.get("how") else ""
            lines.append(f"- {cid}：{'、'.join(m['actions'])}{how}")
    return "\n".join(lines)


def capability_text(entries: list[dict[str, Any]], mode: str = "block") -> str:
    if not entries:
        return ""
    lines = [_RULES[mode]]
    for e in entries:
        if e["missing"]:
            lines.append(f"- {e['card']} {e['need']}（{e['source']}）：暂缺能力")
            continue
        how = f"；简化：{'；'.join(e['notes'])}" if e["notes"] else ""
        refs = "、".join(e["refs"])
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
    gaps: dict[str, Any],
    entries: list[dict[str, Any]],
    *,
    mode: str = "skip",
    threshold: float = 0.2,
) -> list[tuple[str, str]]:
    """(gap id, message) for the gaps the selected tactic should not have: with ``skip``
    every gap (the closest Crow need names the actions to use, or the element is left
    out); with ``wait`` the gaps whose closest need Crow mapped onto the capability set."""
    if not entries:
        return []
    out: list[tuple[str, str]] = []
    for gid, spec in (gaps or {}).items():
        spec = spec if isinstance(spec, dict) else {}
        need, score = _closest(f"{gid} {spec.get('description') or ''}", entries)
        mapped = need is not None and not need["missing"] and score >= threshold
        if mapped:
            how = f"（简化：{'；'.join(need['notes'])}）" if need["notes"] else ""
            out.append(
                (
                    gid,
                    f"CROW_GAP 缺口 {gid} 对应 Crow 已在能力目录中匹配的要素「{need['need']}」"
                    f"（{need['source']}，{need['label']}）：用 {'、'.join(need['refs'])} 实现"
                    f"{how}；删除 gaps.{gid}，并把树中 gap.{gid} 的用法换成这些动作或子树。",
                )
            )
        elif mode != "wait":
            what = f"「{need['need']}」" if need is not None and score >= threshold else ""
            out.append(
                (
                    gid,
                    f"CROW_GAP 缺口 {gid}：能力目录暂缺的要素{what}本次忽略（已记入能力开发"
                    f"队列）。删除 gaps.{gid}，删掉只为它设的状态、转换和树节点，implements "
                    "中也不要再写它；其余行为照常实现。",
                )
            )
    return out


def resolve_selection(request: V3Request, request_path: Path) -> Path:
    path = Path(request.tactic_selection or "")
    return path if path.is_absolute() else (request_path.parent / path)


def tactic_text(tactic: Any, deferred: set[str] | None = None) -> str:
    """The selected tactic in words, appended to the requirement every stage sees;
    ``deferred`` elements (no capability yet) are marked as left out."""
    deferred = deferred or set()

    def mark(cid: str) -> str:
        return "（暂缺能力，本次忽略）" if cid in deferred else ""

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
            cid = f"{card.id}_s{index + 1}"
            lines.append(f"- {cid} 【{step.phase}】{step.do}{until}{mark(cid)}")
        if part.whole:
            for index, g in enumerate(card.geometry):
                extra = (f"（{g.when}）" if g.when else "") + (
                    f"，间距{g.spacing}" if g.spacing else ""
                )
                cid = f"{card.id}_g{index + 1}"
                lines.append(f"- {cid} 队形：{g.formation}{extra}{mark(cid)}")
            for index, x in enumerate(card.exceptions):
                cid = f"{card.id}_x{index + 1}"
                lines.append(f"- {cid} 例外：若{x.situation}，{x.then}{mark(cid)}")
        if card.assumptions:
            lines.append("假设：" + "；".join(card.assumptions))
    if tactic.user_notes:
        lines.append(f"\n用户选择备注：{tactic.user_notes}")
    return "\n".join(lines)


def tactic_clauses(tactic: Any, skip: set[str] | None = None) -> list[dict[str, Any]]:
    """The tactic's steps, formations and exceptions as v3 clauses (``origin: tactic``);
    ``skip``: elements left out of this model (no capability yet)."""
    out = []
    for c in tactic.clauses():
        if skip and c.id in skip:
            continue
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
    request: V3Request, request_path: Path, contracts: Any, tactic: Any = None
) -> tuple[V3Request, Any, list[str]]:
    """The request with the selected tactic folded in; the loaded selection; notes.
    ``tactic``: an already loaded (e.g. simplified, see ``simplify``) selection."""
    from afsim_research_agent.falcon.tactic import load_selection

    if tactic is None:
        tactic = load_selection(resolve_selection(request, request_path))
    notes = [f"输入来自 Crow 选定战术 {tactic.source_label}"]
    entries = capability_map(tactic, contracts)
    mode = request.missing_capabilities
    caps = capability_text(entries, mode)
    deferred = deferred_elements(entries) if mode != "wait" else set()
    update: dict[str, Any] = {
        "requirement": tactic.requirement(request.requirement)
        + "\n\n"
        + tactic_text(tactic, deferred=deferred)
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
    missing = [e for e in entries if e["missing"]]
    if missing:
        how = "写成缺口，等待能力开发" if mode == "wait" else "本次不实现，待能力开发"
        notes.append(
            f"能力目录暂缺 {len(missing)} 项（{how}）：" + "；".join(e["need"] for e in missing)
        )
    for card_id, text in tactic.reference_assumptions():
        notes.append(f"{card_id} 的参考值假设需人工复核：{text}")
    return request.model_copy(update=update), tactic, notes


DEFERRED_FILE = "deferred_capabilities.yaml"


def write_crow_files(
    staging: Path,
    tactic: Any,
    entries: list[dict[str, Any]] | None = None,
    mode: str = "block",
    blocked: dict[str, str] | None = None,
    simplified: Any = None,
) -> None:
    (staging / "selected_tactics.yaml").write_text(tactic.selected_document(), encoding="utf-8")
    extra: dict[str, Any] = {}
    if simplified is not None:
        from afsim_research_agent.falcon.v3.simplify import deferred_section, full_document

        (staging / "full_tactic.yaml").write_text(full_document(simplified), encoding="utf-8")
        extra = deferred_section(simplified)
    missing = [e for e in entries or [] if e["missing"]]
    if missing or blocked or extra:
        doc = {
            "mode": mode,
            "meaning": {
                "block": "能力集暂缺；核心要素缺能力时不起草，等能力开发后重新起草",
                "skip": "能力集暂缺；本模型不实现这些要素，能力开发后重新起草",
                "wait": "能力集暂缺；本模型以 gaps 占位，等待能力开发",
            }.get(mode, ""),
            # Falcon's check of each tactic clause: clause → missing atomic capability
            "blocked_clauses": dict(blocked or {}),
            "left_out": sorted(deferred_elements(entries or []) | set(blocked or {}))
            if mode != "wait"
            else [],
            "needs": [
                {
                    k: e[k]
                    for k in ("card", "elements", "need", "source", "status", "missing_text")
                    if e.get(k)
                }
                for e in missing
            ],
            **extra,
        }
        (staging / DEFERRED_FILE).write_text(
            yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
    gaps = tactic.crow_gaps_document()
    if gaps:
        (staging / "crow_capability_gaps.yaml").write_text(gaps, encoding="utf-8")


def review_section(
    tactic: Any,
    entries: list[dict[str, Any]] | None = None,
    mode: str = "block",
    blocked: dict[str, str] | None = None,
    simplified: Any = None,
) -> list[str]:
    from afsim_research_agent.falcon.tactic import render_section

    lines = list(render_section(tactic, number="Crow"))
    if simplified is not None:
        lines += [
            "",
            "### 可执行版与完整战术",
            "",
            "上面是按现有能力集化简后的可执行版（本模型按它起草）。完整战术见 full_tactic.yaml；"
            "以下要素暂不实现，原子能力补齐后按完整战术重新起草：",
            "",
        ]
        lines += [
            f"- {d['element']}：{d['what']}"
            + (f"（缺：{d['missing']}）" if d.get("missing") else "")
            for d in simplified.deferred
        ] or ["- （无）"]
        if simplified.answer.kept_constraints:
            lines += ["", "保留的需求约束：" + "；".join(simplified.answer.kept_constraints)]
    entries = entries or []
    if entries:
        mapped = [e for e in entries if not e["missing"]]
        missing = [e for e in entries if e["missing"]]
        lines += ["", "### 能力目录匹配", ""]
        lines += [f"- {e['need']}（{e['label']}）→ {'、'.join(e['refs'])}" for e in mapped]
        if missing:
            how = (
                "以 gaps 占位，等待能力开发"
                if mode == "wait"
                else "本次不实现，见 deferred_capabilities.yaml，能力开发后重新起草"
            )
            lines += ["", f"暂缺能力（{how}）："]
            lines += [f"- {e['need']}（{e['source']}）" for e in missing]
    if blocked:
        lines += ["", "### 起草前核对：能力集实现不了的条款", ""]
        lines += [f"- {cid}：{why}" for cid, why in blocked.items()]
    return lines


def make_request(
    tactics_dir: Path,
    model_id: str,
    *,
    requirement: str = "",
    role_map: dict[str, str] | None = None,
    scenario_events: dict[str, str] | None = None,
    candidates: int = 3,
    base: Path | None = None,
    missing_capabilities: str = "block",
    drafting: str = "free",
) -> dict[str, Any]:
    """A v3 request (as a dict to write next to the Crow run) that names the selection."""
    target = tactics_dir if base is None else Path(_relative(tactics_dir, base))
    out: dict[str, Any] = {
        "model_id": model_id,
        "tactic_selection": target.as_posix(),
        "candidates": candidates,
        "fix_steps": 40,
        "scenario_events": scenario_events or {"cancelled": "外部结束请求"},
        "missing_capabilities": missing_capabilities,
    }
    if drafting != "free":
        out["drafting"] = drafting
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
