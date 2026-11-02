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
  that changes nothing).
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
