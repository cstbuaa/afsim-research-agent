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


def expr_text(expr: Any) -> str:
    """An IR expression in the sheet's Python condition syntax."""
    if not isinstance(expr, dict):
        return str(expr)
    if set(expr) == {"value", "unit"}:
        return f"{expr['value']} * {expr['unit']}"
    if len(expr) != 1:
        return str(expr)
    key, value = next(iter(expr.items()))
    if key in ("all", "any"):
        joiner = " and " if key == "all" else " or "
        return "(" + joiner.join(expr_text(v) for v in value) + ")"
    if key == "not":
        return f"not {expr_text(value)}"
    if key in ("bb", "parameter_ref", "condition_ref"):
        return str(value)
    if key == "literal":
        return repr(value)
    if key == "compare":
        ops = {"eq": "==", "ne": "!=", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}
        return (
            f"{expr_text(value['left'])} {ops.get(value['operator'], value['operator'])} "
            f"{expr_text(value['right'])}"
        )
    if key == "function_call":
        args = value.get("arguments") or {}
        if value.get("name") == "time_in_state_gte":
            return f"time_in_state() >= {expr_text(args.get('duration'))}"
        if value.get("name") == "elapsed_since_gte":
            return f"elapsed_since({args.get('field')}) >= {expr_text(args.get('duration'))}"
    return str(expr)


def trigger_text(sem: dict[str, Any]) -> str:
    if sem.get("external"):
        return "外部事件"
    if "after_s" in sem:
        return f"进入状态 {sem['after_s']:g} s 后（timer 事件）"
    if "tree" in sem:
        return "本状态的树" + ("成功" if sem["tree"] == "success" else "失败") + "时"
    if "expression" in sem:
        return expr_text(sem["expression"])
    if "event" in sem:
        return f"事件 {sem['event']}"
    return "?"


def internal_field(path: str) -> bool:
    """Interface plumbing of the action contracts (request ids, execution status)."""
    return ".request." in path or path.startswith("execution.")


def closure(contracts: Contracts, wanted: list[str]) -> list[str]:
    """The slices named plus those their tactic-set entries use (e.g. a formation state
    tree that runs a perception subtree)."""
    out = [s for s in wanted]
    todo = list(out)
    while todo:
        sid = todo.pop()
        for entry in contracts.tactics.values():
            if entry.slice != sid:
                continue
            for ref in _refs(entry.raw):
                target = contracts.actions.get(ref) or contracts.tactics.get(ref)
                if target is not None and target.slice not in out:
                    out.append(target.slice)
                    todo.append(target.slice)
    return out


def _refs(raw: Any) -> list[str]:
    found: list[str] = []
    if isinstance(raw, dict):
        for key in ("action", "subtree"):
            if isinstance(raw.get(key), str):
                found.append(raw[key])
        if isinstance(raw.get("state_trees"), dict):
            found += [str(v) for v in raw["state_trees"].values()]
        for value in raw.values():
            if isinstance(value, dict | list):
                found += _refs(value)
    elif isinstance(raw, list):
        for item in raw:
            found += _refs(item)
    return found


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
            meanings = contracts.semantics.events.get(m.id) or {}
            if meanings:
                out.append(
                    "  触发条件的含义（自己写状态图和条件时照此写）："
                    + "; ".join(f"{k} = {trigger_text(v)}" for k, v in meanings.items())
                )
        fields = sorted(
            p for p, f in contracts.blackboard.items() if f.slice == sid and not internal_field(p)
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
