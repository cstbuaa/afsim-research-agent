"""What the v3 prompts show of the capability set.

Only the public formal layer is shown: action-set ids, parameters, contract summaries,
blackboard field names and tactic-set entries. Native names, bindings, scripts and paths
are not part of the capability set and never reach a prompt.

Retrieval is two-stage: the structure call sees an index of every slice (ids only) and
names the slices it needs (``slices:`` in ``yaml model``); the control call sees those
slices in full.
"""

from __future__ import annotations

from typing import Any

from afsim_research_agent.ir.v2.contracts import Contracts

MAX_DESC = 160


def _short(text: Any, limit: int = MAX_DESC) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def slices(contracts: Contracts) -> list[str]:
    found = {a.slice for a in contracts.actions.values()}
    found |= {t.slice for t in contracts.tactics.values()}
    return sorted(found)


def index(contracts: Contracts, classes: set[str] | None = None) -> str:
    """One paragraph per slice: its actions, tactic-set trees and missions (ids only)."""
    lines = []
    for sid in slices(contracts):
        actions = sorted(
            a.id
            for a in contracts.actions.values()
            if a.slice == sid
            and (not classes or not a.platform_classes or classes & set(a.platform_classes))
        )
        trees = sorted(
            t.id for t in contracts.tactics.values() if t.slice == sid and t.section != "missions"
        )
        missions = [
            t for t in contracts.tactics.values() if t.slice == sid and t.section == "missions"
        ]
        lines.append(f"- 切片 `{sid}`")
        if actions:
            lines.append(f"  - 动作：{', '.join(actions)}")
        if trees:
            lines.append(f"  - 战术集子树 / 状态树：{', '.join(trees)}")
        for m in missions:
            desc = _short(m.raw.get("description"), 100)
            lines.append(
                f"  - 战术集任务 `{m.id}`：状态 {', '.join(map(str, m.raw.get('states') or []))}"
                + (f"（{desc}）" if desc else "")
            )
    return "\n".join(lines)


def _param(spec: dict[str, Any]) -> str:
    parts = [str(spec.get("type", "any"))]
    if spec.get("units"):
        parts.append(str(spec["units"]))
    if spec.get("required"):
        parts.append("必填")
    if "default" in spec:
        parts.append(f"默认 {spec['default']}")
    for key, sym in (("minimum", ">="), ("maximum", "<=")):
        if key in spec:
            parts.append(f"{sym}{spec[key]}")
    if spec.get("enum"):
        parts.append("取值 " + "/".join(map(str, spec["enum"])))
    return " ".join(parts)


def details(contracts: Contracts, wanted: list[str]) -> str:
    """Actions (contract summary), tactic-set entries and blackboard fields of the slices."""
    out: list[str] = []
    for sid in wanted:
        actions = [a for a in contracts.actions.values() if a.slice == sid]
        tactics = [t for t in contracts.tactics.values() if t.slice == sid]
        if not actions and not tactics:
            continue
        out.append(f"### 切片 `{sid}`")
        if actions:
            out.append("动作（叶节点写 `切片.动作(参数=值)`）：")
            for a in sorted(actions, key=lambda x: x.id):
                cap = contracts.catalog.capabilities.get(a.id)
                desc = _short(cap.raw.get("description") if cap else "")
                params = ", ".join(f"{k}: {_param(v)}" for k, v in a.parameters.items()) or "无参数"
                extra = []
                if a.exclusive:
                    extra.append("独占 " + "/".join(sorted(a.exclusive)))
                if a.kind == "tactic_step":
                    extra.append("战术步骤，无独立契约")
                if a.success:
                    extra.append("完成判据 " + _short(a.success, 80))
                out.append(
                    f"- `{a.id}`({params}) — {desc}" + (f"［{'；'.join(extra)}］" if extra else "")
                )
        trees = [t for t in tactics if t.section != "missions"]
        if trees:
            out.append('战术集子树 / 状态树（写 `tactic("id")`，只能覆盖公开参数）：')
            for t in sorted(trees, key=lambda x: x.id):
                desc = _short(t.raw.get("description"))
                params = ", ".join(f"{k}={v}" for k, v in t.parameters.items())
                running = (
                    "；无目标时保持运行"
                    if str(t.raw.get("no_target_result", "")).upper() == "RUNNING"
                    else ""
                )
                uses = (t.raw.get("uses") or {}).get("actions") or []
                out.append(
                    f"- `{t.id}`"
                    + (f"（参数 {params}）" if params else "")
                    + f" — {desc}{running}"
                    + (f"［用到 {', '.join(uses)}］" if uses else "")
                )
        for m in (t for t in tactics if t.section == "missions"):
            trans = "; ".join(
                f"{x.get('from')}→{x.get('to')} 当 {x.get('when')}"
                for x in m.raw.get("transitions") or []
            )
            params = ", ".join(f"{k}={v}" for k, v in m.parameters.items())
            out.append(
                f"战术集任务 `{m.id}`"
                + (f"（参数 {params}）" if params else "")
                + f"：初始 {m.raw.get('initial_state')}；{trans}"
            )
        fields = sorted(
            p
            for p, f in contracts.blackboard.items()
            if f.slice == sid and ".request." not in p and not p.startswith("execution.")
        )
        if fields:
            shown = []
            for p in fields:
                f = contracts.blackboard[p]
                kind = f.type + (f" {f.dimension}" if f.dimension else "")
                if f.enum:
                    kind += " " + "/".join(f.enum)
                shown.append(f"{p} ({kind})")
            out.append("黑板字段（条件里直接写字段路径）：" + "; ".join(shown))
        out.append("")
    return "\n".join(out)
