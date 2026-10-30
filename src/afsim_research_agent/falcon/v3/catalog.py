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


def tree_python(contracts: Contracts, raw: dict[str, Any], indent: str = "    ") -> list[str]:
    """A tactic-set tree in the sheet's Python form (an example to learn from). Tactic-set
    ``sequence`` / ``selector`` resume at the running child, as ``with sequence():`` does."""
    kind = str(raw.get("type"))
    kids = raw.get("children") or ([raw["child"]] if raw.get("child") else [])
    if kind == "action":
        if raw.get("operation") == "cancel":
            return [f"{indent}# 取消正在执行的 {raw.get('action')}"]
        params = ", ".join(f"{k}={v!r}" for k, v in (raw.get("params") or {}).items())
        return [f"{indent}{raw.get('action')}({params})"]
    if kind == "condition":
        sem = contracts.semantics.tactic_conditions.get(str(raw.get("condition"))) or {}
        if "expression" in sem:
            return [f"{indent}assert {expr_text(sem['expression'])}"]
        return [f"{indent}assert {raw.get('condition')}  # 事件 {sem.get('event', '?')} 已发生"]
    if kind == "subtree":
        ref = str(raw.get("subtree"))
        return [f'{indent}tactic("{ref}")']
    if kind == "timeout":
        head = (
            f"{indent}# 战术集的 timeout（IR 没有此节点）：反复执行下面的节点直到成功，"
            f"{raw.get('duration_s')} s 内不成功则失败；可用计时条件加 selector 表达"
        )
        return [head, *[x for k in kids for x in tree_python(contracts, k, indent)]]
    if kind == "parallel":
        policy = str(raw.get("success_policy") or "one")
        arg = f'"{policy}"' if policy != "threshold" else str(raw.get("threshold") or 1)
        head = f"{indent}with parallel({arg}):"
    elif kind == "repeat":
        if raw.get("until_done"):
            head = f"{indent}with repeat(until_done=True):"
        elif raw.get("count"):
            head = f"{indent}with repeat(count={raw['count']}):"
        else:
            head = f"{indent}with repeat(duration={raw.get('duration_s')} * s):"
    elif kind in ("sequence", "selector"):
        head = f"{indent}with {kind}():"
    else:
        head = f"{indent}with {kind}():"
    body = [x for k in kids for x in tree_python(contracts, k, indent + "    ")]
    return [head, *(body or [f"{indent}    pass"])]


def entry_python(contracts: Contracts, entry: Any, name: str) -> str:
    raw = entry.raw
    lines = tree_python(contracts, raw.get("root") or {}, "    ")
    if str(raw.get("no_target_result", "")).upper() == "RUNNING":
        lines = [
            "    with keep_running():  # 战术集标注：没有目标时保持运行",
            *["    " + x for x in lines],
        ]
    desc = _short(raw.get("description"), 120)
    return "\n".join([f"def {name}():" + (f"  # {desc}" if desc else ""), *lines])


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


def details(contracts: Contracts, wanted: list[str], policy: str = "all") -> str:
    """Actions (contract summary), tactic-set entries and blackboard fields of the slices.
    ``policy="subtrees"``: subtrees are listed with their trees (to reference or imitate);
    state trees and missions are shown as examples only."""
    if policy == "subtrees":
        return _details_subtrees(contracts, wanted)
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


def _actions_text(contracts: Contracts, sid: str) -> list[str]:
    out: list[str] = []
    actions = [a for a in contracts.actions.values() if a.slice == sid]
    if not actions:
        return out
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
        out.append(f"- `{a.id}`({params}) — {desc}" + (f"［{'；'.join(extra)}］" if extra else ""))
    return out


def _fields_text(contracts: Contracts, sid: str) -> list[str]:
    fields = sorted(
        p for p, f in contracts.blackboard.items() if f.slice == sid and not internal_field(p)
    )
    if not fields:
        return []
    shown = []
    for p in fields:
        f = contracts.blackboard[p]
        kind = f.type + (f" {f.dimension}" if f.dimension else "")
        if f.enum:
            kind += " " + "/".join(f.enum)
        shown.append(f"{p} ({kind})")
    return ["黑板字段（条件里直接写字段路径）：" + "; ".join(shown)]


def _details_subtrees(contracts: Contracts, wanted: list[str]) -> str:
    out: list[str] = []
    for sid in wanted:
        tactics = [t for t in contracts.tactics.values() if t.slice == sid]
        actions = _actions_text(contracts, sid)
        if not actions and not tactics:
            continue
        out.append(f"### 切片 `{sid}`")
        out += actions
        subs = sorted((t for t in tactics if t.section == "subtrees"), key=lambda x: x.id)
        if subs:
            out.append(
                '可引用的战术集子树（写 `tactic("id")`；能力不够时，仿照它们的写法用动作新建子树）：'
            )
            out.append("```python")
            for t in subs:
                params = ", ".join(f"{k}={v}" for k, v in t.parameters.items())
                out.append(f"# {t.id}" + (f"（公开参数 {params}）" if params else ""))
                out.append(entry_python(contracts, t, "_" + t.id.split(".")[-1]))
                out.append("")
            out.append("```")
        examples = sorted((t for t in tactics if t.section == "state_trees"), key=lambda x: x.id)
        if examples:
            out.append("范例：战术集的状态树（只能参照写法，不能引用；状态树要自己写）：")
            out.append("```python")
            for t in examples:
                out.append(f"# 范例 {t.id}")
                out.append(entry_python(contracts, t, "EXAMPLE"))
                out.append("")
            out.append("```")
        for m in (t for t in tactics if t.section == "missions"):
            trans = "; ".join(
                f"{x.get('from')}→{x.get('to')} 当 {x.get('when')}"
                for x in m.raw.get("transitions") or []
            )
            out.append(
                f"范例：战术集任务 `{m.id}`（不能引用，参照它写自己的状态图）：初始 "
                f"{m.raw.get('initial_state')}；{trans}"
            )
            trees = m.raw.get("state_trees") or {}
            if trees:
                out.append(
                    "  各状态用的状态树：" + "; ".join(f"{k} → {v}" for k, v in trees.items())
                )
            meanings = contracts.semantics.events.get(m.id) or {}
            if meanings:
                out.append(
                    "  触发条件的含义（自己写状态图和条件时照此写）："
                    + "; ".join(f"{k} = {trigger_text(v)}" for k, v in meanings.items())
                )
        out += _fields_text(contracts, sid)
        out.append("")
    return "\n".join(out)
