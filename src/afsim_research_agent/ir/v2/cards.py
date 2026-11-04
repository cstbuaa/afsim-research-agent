"""Capability cards: how an action or a tactic-set tree may be used, as checkable facts.

A capability's name and description say what it does; they do not say whether one vehicle
can run it or whether it ever finishes. Drafts chose by name ("formation search advance" for
a single vehicle's "advance") and waited for a tree that never ended. A card states:

* ``min_vehicles`` — 1, or 2 when the capability commands or waits for other vehicles
  (formation slots, standoff commands, target sharing, threat allocation, hierarchical
  formations);
* ``termination`` — ``ends`` (finishes when its own actions finish: following a route,
  stopping), ``conditional`` (finishes only when something outside it holds: a target is
  selected, the followers are in their slots, a condition becomes true) or ``never`` (holds,
  tracks, keeps a formation: its tree event never fires);
* ``reason`` — why, in words a drafter can act on;
* ``commander`` — the capability commands other vehicles (an action whose resource claim
  covers the followers, or a tree containing one): the formation leader runs it and the
  members are moved by it; run on a member it has no one to command.

Cards are derived from the capability set (tree structure, ``persistent``, resource scopes,
``no_target_result``) and can be corrected by ``capability_cards.yaml`` next to this module
(e.g. from lab probe runs); a corrected card says so in ``source``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ENDS, CONDITIONAL, NEVER = "ends", "conditional", "never"
_ORDER = {ENDS: 0, CONDITIONAL: 1, NEVER: 2}
TERMINATION_TEXT = {ENDS: "自行结束", CONDITIONAL: "满足条件才结束", NEVER: "不会结束"}

# slices whose actions command or wait for other vehicles
MULTI_SLICES = frozenset(
    {
        "formation_standoff",
        "hierarchical_formation",
        "threat_allocation",
        "communication_target_sharing",
    }
)
OVERRIDES = Path(__file__).with_name("capability_cards.yaml")


@dataclass(frozen=True)
class Card:
    id: str
    min_vehicles: int
    termination: str
    reason: str
    source: str = "derived"  # derived / override
    commander: bool = False

    def tags(self, why: str = "") -> str:
        """Short labels for catalog text: ``[需≥2车][满足条件才结束]``; ``why`` is added to
        the termination label."""
        who = "单车可用" if self.min_vehicles <= 1 else f"需≥{self.min_vehicles}车"
        term = TERMINATION_TEXT[self.termination] + (f"：{why}" if why else "")
        out = f"[{who}][{term}]"
        if self.commander:
            out += "[长车执行，指挥僚车；僚车不要用]"
        return out


COMMANDING_SCOPES = ("followers", "members", "others")


def _commanding(contracts: Any) -> set[str]:
    return {
        aid
        for aid, a in contracts.actions.items()
        if any(r.get("scope") in COMMANDING_SCOPES for r in (getattr(a, "resources", None) or ()))
    }


def _uses(contracts: Any, node: Any, out: set[str], seen: set[str]) -> None:
    """Action ids a tactic-set tree runs (subtrees and tactic references expanded)."""
    if isinstance(node, dict):
        if node.get("type") == "action" and node.get("action"):
            out.add(str(node["action"]))
        for key in ("subtree", "tactic"):
            ref = node.get(key)
            if isinstance(ref, str) and ref not in seen and ref in contracts.tactics:
                seen.add(ref)
                _uses(contracts, (contracts.tactics[ref].raw or {}).get("root"), out, seen)
        for value in node.values():
            if isinstance(value, dict | list):
                _uses(contracts, value, out, seen)
    elif isinstance(node, list):
        for value in node:
            _uses(contracts, value, out, seen)


def _worst(values: list[str]) -> str:
    return max(values, key=_ORDER.__getitem__) if values else ENDS


def _best(values: list[str]) -> str:
    return min(values, key=_ORDER.__getitem__) if values else ENDS


class _Deriver:
    def __init__(self, contracts: Any, subtrees: dict[str, Any] | None = None) -> None:
        self.c = contracts
        self.subtrees = subtrees or {}
        self.memo: dict[str, tuple[str, int, list[str]]] = {}

    # -- leaves --------------------------------------------------------------------------
    def action(self, aid: str) -> tuple[str, int, list[str]]:
        a = self.c.actions.get(aid)
        if a is None:
            return CONDITIONAL, 1, [f"{aid} 不在能力集中"]
        others = any(
            r.get("scope") in ("followers", "members", "others", "leader")
            for r in (getattr(a, "resources", None) or ())
        )
        n = 2 if others or getattr(a, "slice", "") in MULTI_SLICES else 1
        if getattr(a, "persistent", None) or (getattr(a, "success", None) or {}).get("persistent"):
            return NEVER, n, [f"{aid} 一直运行"]
        if others:
            return CONDITIONAL, n, [f"{aid} 指挥其他车辆，要等它们到位"]
        return ENDS, n, []

    def entry(self, tid: str, stack: tuple[str, ...] = ()) -> tuple[str, int, list[str]]:
        if tid in self.memo:
            return self.memo[tid]
        t = self.c.tactics.get(tid)
        if t is None or tid in stack:
            return CONDITIONAL, 1, [f"{tid} 无法展开"]
        raw = t.raw or {}
        term, n, why = self.node(raw.get("root"), (*stack, tid))
        if raw.get("persistent"):
            term, why = NEVER, [*why, f"{tid} 标为一直运行"]
        elif str(raw.get("no_target_result", "")).upper() == "RUNNING" and term == ENDS:
            term, why = CONDITIONAL, [*why, f"{tid} 没有目标时保持运行"]
        if getattr(t, "slice", "") in MULTI_SLICES:
            n = max(n, 2)
        out = (term, n, why)
        self.memo[tid] = out
        return out

    # -- trees ---------------------------------------------------------------------------
    def node(self, node: Any, stack: tuple[str, ...] = ()) -> tuple[str, int, list[str]]:
        if not isinstance(node, dict):
            return ENDS, 1, []
        kind = node.get("type")
        kids = list(node.get("children") or [])
        if node.get("child") is not None:
            kids.append(node["child"])
        if kind == "action":
            return self.action(str(node.get("action")))
        if kind == "condition":
            return CONDITIONAL, 1, ["条件节点要等条件成立"]
        if kind in ("tactic", "subtree"):
            ref = str(node.get(kind) or node.get("tactic") or node.get("subtree"))
            if ref in self.subtrees and ref not in stack:
                return self.node(self.subtrees[ref], (*stack, ref))
            return self.entry(ref, stack)
        parts = [self.node(k, stack) for k in kids]
        terms = [p[0] for p in parts]
        n = max([p[1] for p in parts] or [1])

        def pick(term: str) -> tuple[str, int, list[str]]:
            # the reasons of the children that decide the result
            why = [w for p in parts if p[0] == term for w in p[2]]
            return term, n, why

        if kind in ("sequence", "sequence_with_memory"):
            return pick(_worst(terms))
        if kind == "parallel":
            policy = node.get("policy") or {}
            ptype = policy.get("type") if isinstance(policy, dict) else None
            ptype = ptype or node.get("success_policy") or "one"
            return pick(_worst(terms) if ptype == "all" else _best(terms))
        if kind in ("selector", "selector_with_memory", "priority_selector", "weighted_random"):
            return pick(_best(terms))
        if kind == "repeat":
            rep = node.get("repeat") or {}
            if node.get("until_done") or rep.get("until_done"):
                return pick(_worst(terms))
            if node.get("count") or rep.get("count") or rep.get("duration_s"):
                return ENDS, n, []
            return NEVER, n, ["repeat 没有结束条件"]
        if kind == "failure_as_running" or node.get("failure_as_running"):
            term, _, why = pick(_worst([*terms, CONDITIONAL]))
            return term, n, [*why, "失败时保持运行"]
        if kind == "timeout":
            return ENDS, n, []  # retried until it succeeds; fails when the time is up
        # inverter / succeeder / negator: the child's
        return pick(_worst(terms))


def _overrides() -> dict[str, dict[str, Any]]:
    try:
        data = yaml.safe_load(OVERRIDES.read_text(encoding="utf-8")) or {}
    except OSError:
        return {}
    return {str(k): dict(v or {}) for k, v in (data.get("overrides") or {}).items()}


def cards(contracts: Any) -> dict[str, Card]:
    """Cards of every action and tactic-set entry of the capability set (cached)."""
    cached = getattr(contracts, "_capability_cards", None)
    if cached is not None:
        return cached
    d = _Deriver(contracts)
    commanding = _commanding(contracts)
    out: dict[str, Card] = {}
    for aid in contracts.actions:
        term, n, why = d.action(aid)
        out[aid] = Card(aid, n, term, "；".join(dict.fromkeys(why)), commander=aid in commanding)
    for tid in contracts.tactics:
        term, n, why = d.entry(tid)
        used: set[str] = set()
        _uses(contracts, (contracts.tactics[tid].raw or {}).get("root"), used, {tid})
        out[tid] = Card(
            tid, n, term, "；".join(dict.fromkeys(why)), commander=bool(used & commanding)
        )
    for cid, o in _overrides().items():
        base = out.get(cid) or Card(cid, 1, CONDITIONAL, "")
        term = str(o.get("termination", base.termination))
        out[cid] = Card(
            cid,
            int(o.get("min_vehicles", base.min_vehicles)),
            term if term in _ORDER else base.termination,
            str(o.get("note") or base.reason),
            "override",
            bool(o.get("commander", base.commander)),
        )
    try:
        contracts._capability_cards = out
    except AttributeError:  # pragma: no cover - frozen test doubles
        pass
    return out


def tree_card(contracts: Any, root: Any, subtrees: dict[str, Any] | None = None) -> Card:
    """Card of a model's own tree (subtrees by id from the model; tactic-set entries from
    the capability set, overrides included)."""
    known = cards(contracts)
    d = _Deriver(contracts, subtrees)
    for cid, card in known.items():
        if card.source == "override":
            d.memo[cid] = (card.termination, card.min_vehicles, [card.reason])
    term, n, why = d.node(root)
    return Card("", n, term, "；".join(dict.fromkeys(why)))
