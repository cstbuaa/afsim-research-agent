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

from afsim_research_agent.ir.v2.contracts import Contracts, param_ref, param_spec

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
        word = "成功" if sem["tree"] == "success" else "失败"
        return f"本状态的树{word}时（写成 tree 事件：{{source: tree, result: {sem['tree']}}}）"
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
        params = ", ".join(f"{k}={_arg(v)}" for k, v in (raw.get("params") or {}).items())
        return [f"{indent}{raw.get('action')}({params})"]
    if kind == "condition":
        sem = contracts.semantics.tactic_conditions.get(str(raw.get("condition"))) or {}
        if "expression" in sem:
            return [f"{indent}assert {expr_text(sem['expression'])}"]
        return [f"{indent}assert {raw.get('condition')}  # 事件 {sem.get('event', '?')} 已发生"]
    if kind == "subtree":
        ref = str(raw.get("subtree"))
        args = "".join(f", {k}={_arg(v)}" for k, v in (raw.get("params") or {}).items())
        return [f'{indent}tactic("{ref}"{args})']
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
    elif kind == "selector" and kids and kids[0].get("type") == "condition" and len(kids) > 1:
        # "the rest until the condition holds" — written with the sheet's until gate
        sem = contracts.semantics.tactic_conditions.get(str(kids[0].get("condition"))) or {}
        cond = expr_text(sem["expression"]) if "expression" in sem else kids[0].get("condition")
        body = [x for k in kids[1:] for x in tree_python(contracts, k, indent + "    ")]
        return [f"{indent}with until({cond}):", *body]
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


def action_list(
    contracts: Contracts, classes: set[str] | None = None, slices: list[str] | None = None
) -> str:
    """Every action with its description and the tactic-set subtrees that use it (for
    writing requirement obligations, which name actions)."""
    used: dict[str, list[str]] = {}
    for t in contracts.tactics.values():
        if t.section != "subtrees":
            continue
        for ref in _refs(t.raw):
            if ref in contracts.actions:
                used.setdefault(ref, []).append(t.id)
    lines = []
    for a in sorted(contracts.actions.values(), key=lambda x: x.id):
        if classes and a.platform_classes and not classes & set(a.platform_classes):
            continue
        if slices is not None and a.slice not in slices:
            continue
        cap = contracts.catalog.capabilities.get(a.id)
        desc = _short((cap.raw.get("description") if cap else "") or a.description, 100)
        subs = sorted(set(used.get(a.id, [])))
        lines.append(f"- {a.id} — {desc}" + (f"（用于子树 {', '.join(subs)}）" if subs else ""))
    return "\n".join(lines)


CATEGORY_TEXT = {"design": "设计", "platform": "平台", "scenario": "想定"}


def _param(spec: Any) -> str:
    spec = param_spec(spec)
    parts = []
    if spec.get("category") in CATEGORY_TEXT:
        parts.append(f"[{CATEGORY_TEXT[spec['category']]}]")
    parts.append(str(spec.get("type", "any")))
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
    effect = f"（{_short(spec['effect'], 60)}）" if spec.get("effect") else ""
    return " ".join(parts) + effect


def params_text(specs: dict[str, Any]) -> str:
    return "; ".join(f"{k}: {_param(v)}" for k, v in specs.items())


def _arg(value: Any) -> str:
    """An argument of a tactic-set tree in the sheet's form (a parameter by its name)."""
    name = param_ref(value)
    return name if name is not None else repr(value)


def details(contracts: Contracts, wanted: list[str], policy: str = "all") -> str:
    """Actions (contract summary), tactic-set entries and blackboard fields of the slices.
    ``policy="subtrees"``: subtrees are listed with their trees (to reference or imitate);
    state trees and missions are shown as examples only."""
    if policy == "subtrees":
        return _details_subtrees(contracts, wanted)
    out: list[str] = []
    for sid in wanted:
        tactics = [t for t in contracts.tactics.values() if t.slice == sid]
        actions = _actions_text(contracts, sid)
        if not actions and not tactics:
            continue
        out.append(f"### 切片 `{sid}`")
        out += actions
        trees = [t for t in tactics if t.section != "missions"]
        if trees:
            out.append(
                '战术集子树 / 状态树（写 `tactic("id")`；有公开参数时 `tactic("id", 参数=值)`）：'
            )
            for t in sorted(trees, key=lambda x: x.id):
                desc = _short(t.raw.get("description"))
                params = params_text(t.parameters)
                running = (
                    "；无目标时保持运行"
                    if str(t.raw.get("no_target_result", "")).upper() == "RUNNING"
                    else ""
                )
                uses = (t.raw.get("uses") or {}).get("actions") or []
                result = result_text(t.raw.get("result"), t.raw.get("persistent"))
                out.append(
                    f"- `{t.id}`"
                    + (f"（参数 {params}）" if params else "")
                    + f" — {desc}{running}"
                    + (f"；{result}" if result else "")
                    + (f"［用到 {', '.join(uses)}］" if uses else "")
                )
        for m in (t for t in tactics if t.section == "missions"):
            trans = "; ".join(
                f"{x.get('from')}→{x.get('to')} 当 {x.get('when')}"
                for x in m.raw.get("transitions") or []
            )
            params = params_text(m.parameters)
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
        out += _fields_text(contracts, sid)
        out.append("")
    return "\n".join(out)


UPDATED_TEXT = {
    "constant": "常量",
    "state_entry": "进入状态时由运行时写入",
    "always": "平台每拍更新",
    "while_running": "执行时每拍更新",
    "on_start": "启动时写入",
    "on_success": "成功时写入",
    "on_failure": "失败时写入",
}


def result_text(result: Any, persistent: Any = None) -> str:
    """The meaning of running / success / failure of an action or tree (capability set 1.1)."""
    parts = []
    if isinstance(result, dict):
        for key, word in (("running", "运行"), ("success", "成功"), ("failure", "失败")):
            if result.get(key):
                parts.append(f"{word}={_short(result[key], 90)}")
        codes = result.get("codes")
        if isinstance(codes, dict):
            # failure codes the capability set explains (a timing limit, a missing earlier step)
            parts += [f"{code}={_short(text, 120)}" for code, text in codes.items()]
    text = "结果：" + "；".join(parts) if parts else ""
    if persistent is True and "不会自己成功" not in text:
        text += ("；" if text else "") + "一直运行，不会自己成功"
    return text


def _writes_text(contracts: Contracts, a: Any, common: set[str] | None = None) -> str:
    shown = []
    for w in a.writes or ():
        path = str(w.get("field"))
        if common and path in common:
            continue
        f = contracts.blackboard.get(path)
        if internal_field(path) or (f is not None and f.internal):
            continue
        extra = []
        if "reset_on_start" in w:
            extra.append(f"启动时置 {w['reset_on_start']}")
        for key, word in (("on_success", "成功时"), ("on_failure", "失败时")):
            if key in w:
                extra.append(f"{word}={w[key]}")
        if "only" in w:
            extra.append(f"只会置为 {w['only']}")
        shown.append(path + (f"（{'，'.join(extra)}）" if extra else ""))
    return ("写 " + ", ".join(shown)) if shown else "不写黑板字段"


def _actions_text(contracts: Contracts, sid: str) -> list[str]:
    out: list[str] = []
    actions = [a for a in contracts.actions.values() if a.slice == sid]
    if not actions:
        return out
    out.append("动作（叶节点写 `切片.动作(参数=值)`）：")
    # fields every action of the slice writes are listed once (e.g. the vehicle's own state)
    annotated = [a for a in actions if a.writes is not None]
    common: set[str] = set()
    if len(annotated) > 1 and len(annotated) == len(actions):
        common = set.intersection(*(a.written for a in annotated))
        common = {
            f
            for f in common
            if not internal_field(f)
            and not (contracts.blackboard.get(f) and contracts.blackboard[f].internal)
        }
    for a in sorted(actions, key=lambda x: x.id):
        cap = contracts.catalog.capabilities.get(a.id)
        desc = _short(cap.raw.get("description") if cap else "")
        params = ", ".join(f"{k}: {_param(v)}" for k, v in a.parameters.items()) or "无参数"
        extra = []
        if a.exclusive:
            extra.append("独占 " + "/".join(sorted(a.exclusive)))
        if a.kind == "tactic_step":
            extra.append("战术步骤" if a.writes is not None else "战术步骤，无独立契约")
        result = result_text(a.result, a.persistent)
        if result:
            extra.append(result)
        elif a.success:
            extra.append("完成判据 " + _short(a.success, 80))
        if a.writes is not None:
            text = _writes_text(contracts, a, common)
            if not (common and text == "不写黑板字段"):
                extra.append(text)
        out.append(f"- `{a.id}`({params}) — {desc}" + (f"［{'；'.join(extra)}］" if extra else ""))
    if common:
        out.append(f"  （以上动作执行时都会更新：{', '.join(sorted(common))}）")
    return out


def field_text(contracts: Contracts, path: str) -> str:
    """One blackboard field with its meaning: who writes it, when, initial values."""
    f = contracts.blackboard[path]
    kind = f.type + (f" {f.dimension}" if f.dimension else "")
    if f.enum:
        kind += " " + "/".join(f.enum)
    parts = []
    if f.description:
        parts.append(f.description)
    if f.unwritten:
        parts.append("没有任何动作写入，不能用作条件")
    elif f.written_by:
        how = f.updated if isinstance(f.updated, str) else None
        parts.append(
            f"由 {', '.join(f.written_by)} 写入"
            + (f"，{UPDATED_TEXT.get(how, how)}" if how else "")
        )
    elif isinstance(f.updated, str) and f.updated in UPDATED_TEXT:
        if UPDATED_TEXT[f.updated] not in f.description:
            parts.append(UPDATED_TEXT[f.updated])
    if f.reset_on_start is not None:
        parts.append(f"写入者启动时先置为 {f.reset_on_start}")
    if f.has_initial:
        parts.append(f"初值 {f.initial!r}")
    if f.enum_meaning:
        parts.append("取值：" + "，".join(f"{k}={v}" for k, v in f.enum_meaning))
    if f.note and not f.unwritten:
        parts.append(f"注意：{f.note}")
    return f"- {path} ({kind})" + (" — " + "；".join(parts) if parts else "")


def _fields_text(contracts: Contracts, sid: str) -> list[str]:
    fields = sorted(
        p
        for p, f in contracts.blackboard.items()
        if f.slice == sid and not internal_field(p) and not f.internal
    )
    if not fields:
        return []
    unwritten = [p for p in fields if contracts.blackboard[p].unwritten]
    out = [
        "黑板字段（条件里直接写字段路径）。字段只在写入它的动作（或含有这些动作的子树）"
        "运行时变化：转换条件和树中的条件只能读本状态的树会更新的字段，否则条件在该状态中"
        "永远不变：",
        *(field_text(contracts, p) for p in fields if p not in unwritten),
    ]
    if unwritten:
        out.append(
            "- 未实现的字段（没有任何动作写入，不能用作条件）："
            + "；".join(
                p
                + (
                    f"（{contracts.blackboard[p].description}）"
                    if contracts.blackboard[p].description
                    else ""
                )
                for p in unwritten
            )
        )
    return out


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
                '可引用的战术集子树（写 `tactic("id")`，公开参数写 `tactic("id", 参数=值)`；能力不够时，仿照它们的写法用动作新建子树）：'
            )
            out.append("```python")
            for t in subs:
                params = params_text(t.parameters)
                out.append(f"# {t.id}" + (f"（公开参数 {params}）" if params else ""))
                result = result_text(t.raw.get("result"), t.raw.get("persistent"))
                if result:
                    out.append(f"# {result}")
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
