"""F6: confirmations.yaml, falcon_review.md and falcon_trace.json."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from afsim_research_agent.falcon.models import EvidencePackage, FalconTrace

STATUS_ORDER = {"conflicting": 0, "unknown": 1, "inferred": 2, "explicit": 3, "user_specified": 4}
CATEGORIES = ("待确认参数", "推断规则", "冲突证据", "假设")


def _quantity(item) -> str:
    q = item.quantity
    if q is None:
        return ""
    return f"{q.min:g} {q.unit}" if q.min == q.max else f"{q.min:g}–{q.max:g} {q.unit}"


def evidence_summary(package: EvidencePackage, ids: list[str]) -> list[dict[str, Any]]:
    sources = {s.source_id: s for s in package.sources}
    out = []
    for eid in ids:
        item = package.item(eid)
        if item is None:
            continue
        source = sources.get(item.source_id)
        entry: dict[str, Any] = {"id": eid, "claim": item.claim}
        if item.quantity is not None:
            entry["quantity"] = _quantity(item)
        if source is not None:
            entry["tier"] = source.quality_tier
            entry["source"] = (
                "本地资料" if source.classification == "user_private" else source.title[:80]
            )
        entry["location"] = item.location
        out.append(entry)
    return out


def _elements(data: dict[str, Any]) -> list[tuple[str, str, str, dict[str, Any], dict[str, Any]]]:
    """(target path, kind, id, node, basis) for every element carrying a basis."""
    out = []
    for name, param in data.get("parameters", {}).items():
        out.append((f"parameters.{name}", "parameter", name, param, param.get("basis") or {}))
    for key in ("formations", "coordination", "behaviors"):
        for index, item in enumerate(data.get(key, [])):
            if "basis" in item:
                out.append((f"{key}[{index}]", "element", item["id"], item, item["basis"]))
    for r_index, role in enumerate(data.get("roles", [])):
        machine = role.get("state_machine") or {}
        for s_index, state in enumerate(machine.get("states", [])):
            for t_index, tr in enumerate(state.get("transitions", [])):
                if "basis" in tr:
                    path = (
                        f"roles[{r_index}].state_machine.states[{s_index}].transitions[{t_index}]"
                    )
                    out.append((path, "element", tr["transition_id"], tr, tr["basis"]))
    return out


def confirmation_items(data: dict[str, Any], package: EvidencePackage) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {c: [] for c in CATEGORIES}
    for path, kind, element_id, node, basis in _elements(data):
        if basis.get("confirmation") != "pending":
            continue
        status = basis.get("status", "unknown")
        item: dict[str, Any] = {"target": path, "kind": kind, "id": element_id}
        if kind == "parameter":
            item["current"] = {
                "value": node.get("value"),
                "unit": node.get("unit"),
                "status": status,
            }
            if node.get("description"):
                item["description"] = node["description"]
            category = "待确认参数"
        else:
            item["current"] = {"status": status}
            category = "冲突证据" if status == "conflicting" else "推断规则"
        if basis.get("rationale"):
            item["rationale"] = basis["rationale"]
        item["evidence"] = evidence_summary(package, list(basis.get("evidence", [])))
        item["decision"] = "pending"
        item["override"] = None
        item["note"] = None
        item["_order"] = STATUS_ORDER.get(status, 9)
        buckets[category].append(item)
    for index, assumption in enumerate(data.get("assumptions", [])):
        if assumption.get("requires_confirmation") and assumption.get("confirmation") == "pending":
            buckets["假设"].append(
                {
                    "target": f"assumptions[{index}]",
                    "kind": "assumption",
                    "id": assumption["id"],
                    "statement": assumption.get("statement"),
                    "affects": assumption.get("affects", []),
                    "decision": "pending",
                    "note": None,
                    "_order": 0,
                }
            )
    items = []
    for category in CATEGORIES:
        for item in sorted(buckets[category], key=lambda i: (i.pop("_order"), i["target"])):
            items.append({"category": category, **item})
    return items


CONFIRM_HEADER = """\
# Falcon 待确认清单。对每一项填写 decision：
#   confirm  —— 接受当前值/规则
#   reject   —— 驳回（正式构建会被阻塞，需修改后重新生成或覆盖）
#   override —— 仅参数：在 override 中填写 {value: <数值>, unit: <单位>}，状态记为 user_specified
# 填好后运行：python -m afsim_research_agent.falcon confirm <模型包目录> --by <姓名或标识>
"""


def write_confirmations(path: Path, items: list[dict[str, Any]]) -> None:
    body = yaml.safe_dump(
        {"items": items}, allow_unicode=True, sort_keys=False, default_flow_style=False, width=110
    )
    path.write_text(CONFIRM_HEADER + body, encoding="utf-8")


# --------------------------------------------------------------------------- review.md


def _element_evidence(data: dict[str, Any]) -> dict[str, list[str]]:
    return {eid: list(basis.get("evidence", [])) for _, _, eid, _, basis in _elements(data)}


def shallow_clauses(trace: FalconTrace, data: dict[str, Any]) -> list[str]:
    """Clauses whose only implementing elements are roles or role states: nothing
    concrete (transition, behavior, coordination, formation, parameter) carries them."""
    roles = {r.get("id") for r in data.get("roles", [])}
    states = {
        f"{r.get('id')}.{st.get('id')}"
        for r in data.get("roles", [])
        for st in (r.get("state_machine") or {}).get("states", [])
    }
    by_clause: dict[str, set[str]] = {}
    for element, refs in trace.implements.items():
        for ref in refs:
            by_clause.setdefault(ref, set()).add(element)
    oos = {o.clause_id for o in trace.out_of_scope}
    return [
        c.id
        for c in trace.clauses
        if c.id not in oos and by_clause.get(c.id) and by_clause[c.id] <= roles | states
    ]


def render_review(
    trace: FalconTrace,
    data: dict[str, Any],
    package: EvidencePackage,
    items: list[dict[str, Any]],
    problems: list[str],
    l2: dict[str, Any] | None,
    l3: list[dict[str, Any]] | None,
    tactic_lines: list[str] | None = None,
) -> str:
    lines: list[str] = []
    ok = trace.status == "draft_valid"
    lines += [
        f"# Falcon 审查报告：{trace.model_id}",
        "",
        f"- 草稿状态：{'✅ L1/L2 通过（未确认的数值按参考值构建，见审查清单）' if ok else '❌ 仍有错误（draft_invalid）'}",
        f"- Crow 门槛：通过（{package.crow.gate_version}）；证据包 `{package.package_id}`",
        f"- 证据：{len(package.evidence)} 条主张，{len(package.claims)} 个命题，"
        f"{sum(1 for c in package.claims if c.conflicts)} 个有冲突，丢弃 {len(package.dropped)} 条",
        f"- 待确认：{len(items)} 项（见 `confirmations.yaml`）",
        "",
    ]
    if problems:
        lines += ["## 0. 未解决的错误", ""]
        lines += [p for p in problems[:50]]
        lines.append("")

    if tactic_lines:
        lines += tactic_lines

    lines += [
        "## 1. 条款覆盖",
        "",
        *(
            [
                f"⚠ {len(shallow_clauses(trace, data))} 条只由角色或状态覆盖，"
                "模型中没有具体的转换、行为或协同实现它们，请核对。",
                "",
            ]
            if shallow_clauses(trace, data)
            else []
        ),
        "| 条款 | 类型 | 原文 | 实现元素 | 证据 |",
        "| --- | --- | --- | --- | --- |",
    ]
    by_clause: dict[str, list[str]] = {}
    for element, refs in trace.implements.items():
        for ref in refs:
            by_clause.setdefault(ref, []).append(element)
    ev_by_element = _element_evidence(data)
    oos = {o.clause_id: o.reason for o in trace.out_of_scope}
    shallow = shallow_clauses(trace, data)
    for clause in trace.clauses:
        elements = sorted(by_clause.get(clause.id, []))
        evidence = sorted({e for el in elements for e in ev_by_element.get(el, [])})
        if clause.id in oos:
            impl = f"超出范围：{oos[clause.id]}"
        elif elements:
            impl = "、".join(f"`{e}`" for e in elements)
            if clause.id in shallow:
                impl = "⚠ 仅角色/状态级，未落到转换、行为或协同：" + impl
        else:
            impl = "⚠ 未覆盖"
        lines.append(
            f"| {clause.id} | {clause.kind} | {clause.text} | {impl} | {', '.join(evidence) or '—'} |"
        )
    lines.append("")

    lines += ["## 2. 审查清单", ""]
    for category in ("待确认参数", "推断规则", "冲突证据", "假设"):
        chosen = [i for i in items if i["category"] == category]
        if not chosen:
            continue
        title = "参考值：可直接构建，待人工复核调参" if category == "待确认参数" else category
        lines += [f"### {title}（{len(chosen)}）", ""]
        for item in chosen:
            if item["kind"] == "assumption":
                lines.append(
                    f"- `{item['id']}`：{item.get('statement')}（影响 {item.get('affects')}）"
                )
                continue
            current = item["current"]
            value = (
                f"{current.get('value')} {current.get('unit') or ''}".strip()
                if item["kind"] == "parameter"
                else ""
            )
            head = f"- `{item['target']}` {value} — **{current['status']}**"
            if item["kind"] == "parameter" and (
                not item["evidence"] or current["status"] == "unknown"
            ):
                head += " ⚠ 无依据"
            lines.append(head)
            if item.get("rationale"):
                lines.append(f"  - 理由：{item['rationale']}")
            for ev in item["evidence"]:
                qty = f"（{ev['quantity']}）" if ev.get("quantity") else ""
                lines.append(
                    f"  - {ev['id']} [{ev.get('tier') or '-'}] {ev['claim']}{qty} — {ev.get('source', '')} {ev['location']}"
                )
        lines.append("")

    conflicted = [c for c in package.claims if c.conflicts]
    if conflicted:
        lines += ["## 3. 冲突命题", ""]
        for claim in conflicted:
            lines.append(f"- {claim.claim_id} {claim.statement}")
            for c in claim.conflicts:
                lines.append(
                    f"  - {c.a} ↔ {c.b}：{c.reason}（{'数值规则' if c.origin == 'numeric' else '模型判断'}）"
                )
        lines.append("")

    accepted = [
        f"- `{path}`（{eid}）"
        for path, _, eid, _, basis in _elements(data)
        if basis.get("status") == "user_specified"
    ]
    if accepted:
        lines += ["## 4. 按需求直接建模（user_specified，已自动接受）", "", *accepted, ""]

    if trace.enforcement:
        lines += ["## 5. 代码强制调整", ""]
        lines += [f"- `{n.path}` {n.rule}：{n.detail}" for n in trace.enforcement]
        lines.append("")

    if package.dropped:
        lines += ["## 6. 被丢弃的主张", ""]
        for reason, count in Counter(d.reason for d in package.dropped).most_common():
            lines.append(f"- {reason}：{count}")
        lines.append("")

    lines += ["## 7. 校验与修复", ""]
    if trace.repairs:
        for r in trace.repairs:
            unloaded = "（草稿无法加载，其余校验未运行）" if not r.loaded else ""
            lines.append(
                f"- 第 {r.round} 轮：{r.errors} 个错误{unloaded}，重生成 {r.sections}，错误码 {r.codes}"
            )
    else:
        lines.append("- 首次生成即通过，无需修复" if ok else "- 未进行修复")
    if l2 is not None:
        passed = sum(1 for t in l2.get("tests", []) if t.get("passed"))
        lines.append(f"- L2：{passed}/{len(l2.get('tests', []))} 通过")
    if l3 is not None:
        for scenario in l3:
            good = all(e.get("passed") for e in scenario.get("expectations", []))
            lines.append(
                f"- L3（参考，数值未确认）`{scenario.get('id')}`：{'通过' if good else '未通过'}"
            )
    if trace.uncovered_clauses:
        lines.append(f"- ⚠ 骨架未覆盖的条款：{trace.uncovered_clauses}")
    fixed = [(c.stage, f, c.salvaged) for c in trace.calls for f in c.fixes]
    if fixed:
        lines += ["", "模型输出的自动修正（请核对）：", ""]
        lines += [
            f"- {'⚠ ' if salvaged else ''}`{stage}` {note}" for stage, note, salvaged in fixed[:40]
        ]
        if len(fixed) > 40:
            lines.append(f"- …另有 {len(fixed) - 40} 处，见 `falcon_trace.json`")
    lines += [
        "",
        "## 8. 下一步",
        "",
        "1. 在 `confirmations.yaml` 中逐项填写 decision（confirm / reject / override）；",
        "2. 运行 `python -m afsim_research_agent.falcon confirm <模型包> --by <姓名>`；",
        "3. 运行 `python -m afsim_research_agent.ir build <模型包>`，在 `review.md` 中评估 L3 战术效果。",
        "",
    ]
    return "\n".join(lines)


def write_trace(path: Path, trace: FalconTrace) -> None:
    path.write_text(
        json.dumps(trace.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
