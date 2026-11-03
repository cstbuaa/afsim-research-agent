"""Checks that a draft does what its sheet claims, beyond a valid structure (L1).

A model can pass every structural check and still not implement the tactic: a state named
SWAP_ROLES whose tree swaps nothing, a condition replaced by a constant, parameters that no
action uses. These checks look at the compiled spec (no model call):

* ``IMPL_MISSING``  a tactic clause no element of the model claims (``implements``);
* ``IMPL_EMPTY``    the elements claimed for a tactic clause run no action at all — a state
  name is not an implementation;
* ``IMPL_MISMATCH`` the clause was mapped onto actions before drafting (``map_stage``), and
  none of them runs in the claimed elements: a look-alike action stands in for it;
* ``CONST_COND``    a named condition is a constant (``True`` / ``False``): the check it
  stands for is bypassed;
* ``UNUSED_PARAM``  a parameter no action, condition or event uses (a value in the sheet
  that changes nothing);
* ``ROLE_FIELD``    a role's transition condition reads a field that no action of that role
  writes (each vehicle has its own blackboard): the condition never changes, the role is
  stuck (e.g. a platoon leader waiting for ``formation.quality.in_position``, which only
  members moving to their slots write);
* ``ENTRY_TRUE``    a transition condition already holds when its state is entered, before
  the state's own actions have run (the fields they write still hold their initial values):
  the state is left at once and its work never happens (e.g. "arrived" written as
  ``range_to_target_m <= 5`` on a field that starts at 0).
"""

from __future__ import annotations

import json
import re
from typing import Any

Problem = tuple[str, str, str]  # (IR path for locating, message, code)


def _walk(
    node: Any, out: set[str], subtrees: dict[str, Any], tactics: dict[str, Any], seen: set[str]
) -> None:
    if isinstance(node, dict):
        kind = node.get("type")
        if kind == "action" and node.get("action"):
            out.add(str(node["action"]))
        for key in ("subtree", "tactic"):
            ref = node.get(key) if kind == key else None
            if ref and ref not in seen:
                seen.add(ref)
                if key == "tactic":
                    out.add(str(ref))  # a tactic-set entry runs its own actions
                if ref in subtrees:
                    _walk(subtrees[ref], out, subtrees, tactics, seen)
                elif ref in tactics:
                    _walk(getattr(tactics[ref], "raw", None), out, subtrees, tactics, seen)
        for value in node.values():
            if isinstance(value, (dict, list)):
                _walk(value, out, subtrees, tactics, seen)
    elif isinstance(node, list):
        for value in node:
            _walk(value, out, subtrees, tactics, seen)


def _bypassed(node: Any, literal: set[str]) -> list[str]:
    """Constant-true conditions that follow other children of a selector."""
    out: list[str] = []
    if isinstance(node, dict):
        kids = node.get("children") or []
        if node.get("type") == "selector":
            for i, kid in enumerate(kids):
                if i and isinstance(kid, dict) and kid.get("condition") in literal:
                    out.append(str(kid["condition"]))
        for kid in kids:
            out += _bypassed(kid, literal)
        if isinstance(node.get("child"), dict):
            out += _bypassed(node["child"], literal)
    return out


def element_actions(spec: dict[str, Any], ref: str, tactics: dict[str, Any]) -> set[str] | None:
    """Actions (and subtrees / tactic-set entries) an element of the model runs; None when
    the reference names nothing in the model."""
    subtrees = {s["id"]: s.get("root") for s in spec.get("subtrees") or [] if "id" in s}
    trees = spec.get("state_trees") or {}
    out: set[str] = set()
    role, _, state = ref.partition(".")
    if role in trees and state in (trees.get(role) or {}):
        target = trees[role][state]
        if target in subtrees:
            _walk(subtrees[target], out, subtrees, tactics, {target})
        elif target in tactics:
            out.add(target)
            _walk(getattr(tactics[target], "raw", None), out, subtrees, tactics, {target})
        return out
    if ref in subtrees:
        _walk(subtrees[ref], out, subtrees, tactics, {ref})
        return out
    if ref in tactics:
        out.add(ref)
        _walk(getattr(tactics[ref], "raw", None), out, subtrees, tactics, {ref})
        return out
    return None


def bound_by_name(spec: dict[str, Any], tactics: dict[str, Any]) -> set[str]:
    """Public parameters of the tactic-set entries the model uses directly (a state tree, a
    ``tactic(…)`` call, a mission reference): a model parameter of the same name gives
    their value."""
    used: set[str] = set()
    for mapping in (spec.get("state_trees") or {}).values():
        used |= {str(t) for t in (mapping or {}).values()}
    for mission in (spec.get("missions") or {}).values():
        if isinstance(mission, dict) and mission.get("tactic"):
            used.add(str(mission["tactic"]))

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "tactic" and node.get("tactic"):
                used.add(str(node["tactic"]))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(spec.get("subtrees"))
    names: set[str] = set()
    for ref in used:
        entry = tactics.get(ref)
        names |= set(getattr(entry, "parameters", None) or {})
    return names


def problems(
    spec: dict[str, Any],
    implements: dict[str, list[str]],
    tactic_clauses: list[dict[str, Any]],
    clause_map: dict[str, dict[str, Any]],
    tactics: dict[str, Any],
    scenario_parameters: set[str] | None = None,
) -> list[Problem]:
    out: list[Problem] = []
    for c in tactic_clauses:
        cid = c["id"]
        refs = implements.get(cid) or []
        if not refs:
            out.append(
                (
                    "implements",
                    f"IMPL_MISSING 战术条款 {cid}（{str(c.get('text', ''))[:40]}）没有写进 "
                    "implements：写出实现它的状态或子树",
                    "IMPL_MISSING",
                )
            )
            continue
        runs: set[str] = set()
        known = False
        for ref in refs:
            acts = element_actions(spec, ref, tactics)
            if acts is not None:
                known = True
                runs |= acts
        if not known:
            continue  # unknown references are reported by the sheet compiler
        if not runs:
            out.append(
                (
                    "implements",
                    f"IMPL_EMPTY 战术条款 {cid} 对应的 {'、'.join(refs)} 没有执行任何动作："
                    "只有状态名不算实现，要在树里用能实现它的动作",
                    "IMPL_EMPTY",
                )
            )
            continue
        mapped = set((clause_map.get(cid) or {}).get("actions") or [])
        if mapped and not mapped & runs:
            out.append(
                (
                    "implements",
                    f"IMPL_MISMATCH 战术条款 {cid} 起草前核对为由 {'、'.join(sorted(mapped))} 实现，"
                    f"但 {'、'.join(refs)} 中没有用到其中任何一个（只用了 "
                    f"{'、'.join(sorted(runs))[:120]}）：不要用做别的事的动作冒充",
                    "IMPL_MISMATCH",
                )
            )
    literal = {
        str(c.get("id"))
        for c in spec.get("conditions") or []
        if isinstance(c.get("expression"), dict) and set(c["expression"]) == {"literal"}
    }
    for sub in spec.get("subtrees") or []:
        for cid in _bypassed(sub.get("root"), literal):
            out.append(
                (
                    f"subtrees.{sub.get('id')}",
                    f"CONST_COND 子树 {sub.get('id')} 的选择节点里，条件之后跟着恒为真的 {cid}："
                    "前面的判断不起作用，节点总是立即成功（要等待某个条件，写 "
                    "with until(条件): pass）",
                    "CONST_COND",
                )
            )
    for cond in spec.get("conditions") or []:
        expr = cond.get("expression")
        # the sheet compiler names its own helper conditions <role>_c<n> (e.g. the true leaf
        # of an empty ``with after(...)`` body); only conditions the draft named are checked
        if re.search(r"_c\d+$", str(cond.get("id"))):
            continue
        if isinstance(expr, dict) and set(expr) == {"literal"}:
            out.append(
                (
                    f"conditions.{cond.get('id')}",
                    f"CONST_COND 条件 {cond.get('id')} 是常量 {expr['literal']}：它代表的判断被绕过了，"
                    "写出真实的判断，或删掉这个条件和用到它的地方",
                    "CONST_COND",
                )
            )
    params = dict(spec.get("parameters") or {})
    rest = json.dumps({k: v for k, v in spec.items() if k != "parameters"}, ensure_ascii=False)
    bound = bound_by_name(spec, tactics)
    for name in params:
        if name in (scenario_parameters or set()) or name in bound:
            continue
        if f'"{name}"' not in rest:
            out.append(
                (
                    f"parameters.{name}",
                    f"UNUSED_PARAM 参数 {name} 没有被任何动作、条件或事件使用：接到对应动作的参数上，"
                    "或删掉它",
                    "UNUSED_PARAM",
                )
            )
    return out


# ---------------------------------------------------------------------- role / entry checks
def _fields(expr: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(expr, dict):
        for key, value in expr.items():
            if key == "bb" and isinstance(value, str):
                out.add(value)
            else:
                out |= _fields(value)
    elif isinstance(expr, list):
        for value in expr:
            out |= _fields(value)
    return out


class _Unknown(Exception):
    pass


def _eval(expr: Any, values: dict[str, Any], conds: dict[str, Any], params: dict[str, Any]) -> Any:
    """Value of an IR expression on the given field values (time functions are 0 s into the
    state, i.e. false); _Unknown when it cannot be decided."""
    if not isinstance(expr, dict) or len(expr) != 1:
        if isinstance(expr, (int, float, str, bool)):
            return expr
        raise _Unknown
    key, value = next(iter(expr.items()))
    if key == "all":
        return all(bool(_eval(v, values, conds, params)) for v in value)
    if key == "any":
        return any(bool(_eval(v, values, conds, params)) for v in value)
    if key == "not":
        return not bool(_eval(value, values, conds, params))
    if key == "literal":
        return value
    if key == "bb":
        if value not in values:
            raise _Unknown
        return values[value]
    if key == "condition_ref":
        if value not in conds:
            raise _Unknown
        return _eval(conds[value], values, conds, params)
    if key == "parameter_ref":
        p = params.get(value) or {}
        if not isinstance(p.get("value"), (int, float, str, bool)) or p.get("unit"):
            raise _Unknown
        return p["value"]
    if key == "function_call":
        name = str((value or {}).get("name", ""))
        if name.startswith(("time_in_state", "elapsed_since")):
            return False
        raise _Unknown
    if key == "compare":
        left = _eval(value.get("left"), values, conds, params)
        right = _eval(value.get("right"), values, conds, params)
        op = value.get("operator")
        try:
            return {
                "eq": left == right,
                "ne": left != right,
                "lt": left < right,
                "lte": left <= right,
                "gt": left > right,
                "gte": left >= right,
            }[op]
        except (KeyError, TypeError) as exc:
            raise _Unknown from exc
    if set(expr) == {"value", "unit"}:
        raise _Unknown
    raise _Unknown


def _writers(contracts: Any) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for aid, a in contracts.actions.items():
        for f in getattr(a, "written", set()) or set():
            out.setdefault(f, set()).add(aid)
    for path, f in getattr(contracts, "blackboard", {}).items():
        for aid in getattr(f, "written_by", None) or ():
            out.setdefault(path, set()).add(aid)
    return out


def _initial(contracts: Any, path: str) -> Any:
    f = getattr(contracts, "blackboard", {}).get(path)
    for value in (getattr(f, "initial", None), getattr(f, "default", None)):
        if value is not None:
            return value
    kind = getattr(f, "type", "double")
    return {"bool": False, "string": "", "any": None}.get(kind, 0.0)


ENTRY_ITEMS: list[tuple[str, str, str, str]] = []


def entry_true(
    spec: dict[str, Any], contracts: Any, tactics: dict[str, Any]
) -> list[tuple[str, str, str, str]]:
    """ENTRY_TRUE transitions as (role, from, to, condition id)."""
    ENTRY_ITEMS.clear()
    role_problems(spec, contracts, tactics)
    return list(ENTRY_ITEMS)


def role_problems(spec: dict[str, Any], contracts: Any, tactics: dict[str, Any]) -> list[Problem]:
    """ROLE_FIELD and ENTRY_TRUE (see the module doc)."""
    ENTRY_ITEMS.clear()
    out: list[Problem] = []
    conds = {c["id"]: c.get("expression") for c in spec.get("conditions") or [] if "id" in c}
    params = dict(spec.get("parameters") or {})
    writers = _writers(contracts)
    static = getattr(contracts, "blackboard", {})
    reported: set[tuple[str, str]] = set()
    for role, mission in (spec.get("missions") or {}).items():
        if not isinstance(mission, dict) or not mission.get("states"):
            continue
        states = {
            s: element_actions(spec, f"{role}.{s}", tactics) or set() for s in mission["states"]
        }
        runs = set().union(*states.values()) if states else set()
        for t in mission.get("transitions") or []:
            cid = t.get("on")
            if cid not in conds:
                continue
            expr = conds[cid]
            fields = _fields(expr)
            for f in sorted(fields):
                w = writers.get(f)
                bf = static.get(f)
                if not w or getattr(bf, "updated", None) in ("constant", "state_entry", "always"):
                    continue
                if not (w & runs) and (role, f) not in reported:
                    reported.add((role, f))
                    out.append(
                        (
                            f"conditions.{cid}",
                            f"ROLE_FIELD 角色 {role} 的转换条件 {cid} 读取 {f}，但这个字段只由 "
                            f"{'、'.join(sorted(w)[:4])} 写入，角色 {role} 的树里没有这些动作；"
                            "每辆车有自己的黑板，这个条件对它永远不会变化，状态会卡住。"
                            "改用本角色动作会写入的字段、计时、树事件或外部事件（如排长不入槽，"
                            "不能等 formation.quality.in_position）",
                            "ROLE_FIELD",
                        )
                    )
            # ENTRY_TRUE: every field read is written by the state's own actions (still at
            # its initial value) or is the vehicle's own motion (at rest on entry)
            source = states.get(str(t.get("from")))
            if source is None:
                continue
            values: dict[str, Any] = {}
            fresh = False
            for f in fields:
                if writers.get(f, set()) & source:
                    values[f] = _initial(contracts, f)
                    fresh = True
                elif f in ("self.speed_mps",) and t.get("from") == mission.get("initial_state"):
                    values[f] = 0.0
                else:
                    break
            else:
                if not fresh:
                    continue
                try:
                    holds = bool(_eval(expr, values, conds, params))
                except _Unknown:
                    continue
                if holds:
                    ENTRY_ITEMS.append((role, str(t.get("from")), str(t.get("to")), cid))
                    shown = "，".join(f"{k}={v}" for k, v in sorted(values.items()))
                    out.append(
                        (
                            f"conditions.{cid}",
                            f"ENTRY_TRUE 状态 {t.get('from')} → {t.get('to')} 的条件 {cid} 在进入 "
                            f"{t.get('from')} 时就成立（本状态的动作还没执行，字段仍是初值：{shown}），"
                            "状态会立即转出，它的工作不会发生。"
                            '"走完/到达/完成"用树事件（model 的 events 中声明 '
                            "`名字: {source: tree, result: success}`，本状态的树成功时触发，如路线走完），"
                            "或读进入后才会变化的量",
                            "ENTRY_TRUE",
                        )
                    )
    return out
