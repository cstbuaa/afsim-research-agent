"""Structural comparison of generated state trees with the tactic set's state trees.

When the model writes its own state trees (reference policy ``subtrees``), the tactic set's
mission with the same state names is the yardstick: per state, the actions the generated
tree runs (tactic-set subtrees expanded) against those of the tactic set's state tree.
"""

from __future__ import annotations

from typing import Any

from afsim_research_agent.ir.v2.contracts import Contracts


def _tactic_actions(contracts: Contracts, ref: str, seen: tuple[str, ...] = ()) -> set[str]:
    entry = contracts.tactics.get(ref)
    if entry is None or ref in seen:
        return set()
    return _raw_actions(contracts, entry.raw.get("root") or {}, (*seen, ref))


def _raw_actions(contracts: Contracts, raw: Any, seen: tuple[str, ...]) -> set[str]:
    out: set[str] = set()
    if isinstance(raw, dict):
        if raw.get("type") == "action" and raw.get("operation") != "cancel":
            out.add(str(raw.get("action")))
        if raw.get("type") == "subtree":
            out |= _tactic_actions(contracts, str(raw.get("subtree")), seen)
        for key in ("children", "child"):
            value = raw.get(key)
            for kid in value if isinstance(value, list) else [value] if value else []:
                out |= _raw_actions(contracts, kid, seen)
    return out


def _model_actions(node: dict[str, Any] | None) -> set[str]:
    if not node:
        return set()
    out: set[str] = set()
    if node.get("type") == "action" and node.get("operation") != "cancel":
        out.add(str(node.get("action")))
    for kid in node.get("children") or []:
        out |= _model_actions(kid)
    return out


def _referenced(node: dict[str, Any] | None) -> list[str]:
    if not node:
        return []
    out = [str(node["tactic"])] if node.get("type") == "tactic" else []
    for kid in node.get("children") or []:
        if node.get("type") != "tactic":
            out += _referenced(kid)
    return out


def compare(model: dict[str, Any], contracts: Contracts) -> list[dict[str, Any]]:
    """One row per generated state that the tactic set's missions also have."""
    rows: list[dict[str, Any]] = []
    missions = [t for t in contracts.tactics.values() if t.section == "missions"]
    for rid, role in model["roles"].items():
        states = role["states"]
        best = max(
            missions,
            key=lambda m: len(set(m.raw.get("state_trees") or {}) & set(states)),
            default=None,
        )
        if best is None:
            continue
        trees = best.raw.get("state_trees") or {}
        for state, body in states.items():
            ref = trees.get(state)
            if ref is None or body.get("tree") is None:
                continue
            want = _tactic_actions(contracts, str(ref))
            got = _model_actions(body.get("tree"))
            rows.append(
                {
                    "role": rid,
                    "state": state,
                    "tactic": best.id,
                    "tactic_tree": ref,
                    "same": want == got,
                    "missing": sorted(want - got),
                    "extra": sorted(got - want),
                    "references": sorted(
                        set(_referenced(body.get("tree")))
                        | (
                            {body["tree_ref"]}
                            if body.get("tree_ref") in contracts.tactics
                            else set()
                        )
                    ),
                }
            )
    return rows


def render(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return []
    same = sum(r["same"] for r in rows)
    lines = [
        "## 与战术集状态树的结构对比",
        "",
        f"按状态比较动作集合（战术集子树展开后）：{same}/{len(rows)} 个状态一致。",
        "",
        "| 状态 | 范例状态树 | 结果 | 引用的子树 |",
        "| --- | --- | --- | --- |",
    ]
    for r in rows:
        if r["same"]:
            result = "一致"
        else:
            parts = []
            if r["missing"]:
                parts.append("少 " + ", ".join(r["missing"]))
            if r["extra"]:
                parts.append("多 " + ", ".join(r["extra"]))
            result = "；".join(parts)
        lines.append(
            f"| {r['role']}.{r['state']} | `{r['tactic_tree']}` | {result} | "
            f"{', '.join(r['references']) or '无（自己组合）'} |"
        )
    return lines
