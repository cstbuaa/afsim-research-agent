"""Normalization and L1 checks of Tactical Behavior IR 2.0.

``normalize(raw, contracts)`` checks a ``behavior_spec`` written in IR 2.0 against a
capability set and returns the internal model: every role's mission state machine, each
state's tree with tactic-set references expanded and node ids assigned, the blackboard
the model reads, and the resource leases derived from the action contracts.

Error codes (design §6): ACT001 / ACT002 actions, TAC001 tactic-set references, BB001
blackboard reads, ARB001 resource conflicts, FSM001 / FSM002 state machine and state
trees, GAP001 gap placeholders (falcon-v3 design §4); IR002, REF001, TYPE001, UNIT001,
TREE001, ROLE001 as in IR 1.0.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from afsim_research_agent.ir import irtypes as T
from afsim_research_agent.ir.diagnostics import Collector, Diagnostic, has_errors, sort_diagnostics
from afsim_research_agent.ir.units import UnitError, infer_dimension, to_canonical
from afsim_research_agent.ir.v2.contracts import (
    CONTRACT_UNITS,
    ActionContract,
    BBField,
    Contracts,
    TacticEntry,
)
from afsim_research_agent.ir.v2.models import (
    COMPOSITES,
    BehaviorSpecV2,
    Mission,
    MissionRef,
    Node,
)

IDENT = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
STATE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
FIELD_PATH = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")
COMPARE_OPS = ("eq", "ne", "lt", "lte", "gt", "gte")
FUNCTIONS: dict[str, dict[str, str]] = {
    "time_in_state_gte": {"duration": "time"},
    # seconds since the time stored in a blackboard field (false while the field is empty)
    "elapsed_since_gte": {"field": "field", "duration": "time"},
}
V1_OPERANDS = (
    "self_value",
    "platform_value",
    "mover_value",
    "sensor_value",
    "weapon_value",
    "comm_value",
    "track_value",
    "terrain_value",
    "formation_value",
    "team_value",
    "simulation_value",
)
DEFAULT_SAFE = ("SHUTDOWN",)
PARAM_TYPES = {"number": "double", "integer": "int", "boolean": "bool", "string": "string"}


@dataclass
class Result:
    model: dict[str, Any] | None
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.model is not None and not has_errors(self.diagnostics)


def normalize(raw: dict[str, Any], contracts: Contracts) -> Result:
    d = Collector()
    if not isinstance(raw, dict) or str(raw.get("schema_version")) != "2.0":
        d.error("IR001", "schema_version", "expected schema_version '2.0'")
        return Result(None, sort_diagnostics(d.items))
    try:
        spec = BehaviorSpecV2.model_validate(raw)
    except ValidationError as exc:
        for err in exc.errors():
            path = ".".join(str(p) for p in err["loc"])
            d.error("IR002", path or "<root>", err["msg"])
        return Result(None, sort_diagnostics(d.items))
    model = _Normalizer(spec, contracts, d).run()
    return Result(model, sort_diagnostics(d.items))


class _Normalizer:
    def __init__(self, spec: BehaviorSpecV2, contracts: Contracts, d: Collector) -> None:
        self.spec = spec
        self.c = contracts
        self.d = d
        self.roles = {r.id: r for r in spec.roles}
        self.params = spec.parameters
        self.subtrees = {s.id: s for s in spec.subtrees}
        self.conditions = {c.id: c for c in spec.conditions}
        self.events = {e.id: e for e in spec.events}
        self.gaps = spec.gaps
        self.gap_contracts = {f"{GAP}{gid}": _gap_contract(gid, g) for gid, g in spec.gaps.items()}
        self.gap_uses: dict[str, list[str]] = {gid: [] for gid in spec.gaps}
        self.used_slices: set[str] = set()
        self.declared: dict[str, BBField] = {}
        self.reads: set[str] = set()

    # ------------------------------------------------------------------ driver
    def run(self) -> dict[str, Any]:
        self._identifiers()
        self._declared_blackboard()
        missions = self._missions()
        role_states: dict[str, dict[str, Any]] = {}
        for rid, mission in missions.items():
            role_states[rid] = self._state_trees(rid, mission)
        self._unmapped_state_trees(missions)
        conditions = {
            cid: self._condition(c.id, c.expression) for cid, c in self.conditions.items()
        }
        self._events(missions)
        self._holds(missions)
        gaps = self._gaps()
        imported = self.c.slice_fields(self.used_slices)
        for path in sorted(self.reads):
            if path not in imported and path not in self.declared:
                # read through a condition of a slice the trees do not use: import it too
                fld = self.c.blackboard.get(path)
                if fld is not None and fld.slice:
                    self.used_slices.add(fld.slice)
        imported = self.c.slice_fields(self.used_slices)
        blackboard = {
            p: {"type": f.type, "dimension": f.dimension, "slice": f.slice, "default": f.default}
            for p, f in sorted({**imported, **self.declared}.items())
        }
        return {
            "schema_version": "2.0",
            "model": self.spec.model.model_dump(),
            "capability": {
                "set": self.c.catalog.catalog_id,
                "content_hash": self.c.catalog.content_hash,
                "slices": sorted(self.used_slices),
            },
            "parameters": self._parameters(),
            "blackboard": blackboard,
            "conditions": conditions,
            "events": {e.id: e.model_dump(exclude_none=True) for e in self.spec.events},
            "roles": {
                rid: {
                    "role": self.roles[rid].model_dump(),
                    "mission": missions[rid],
                    "states": role_states[rid],
                }
                for rid in missions
            },
            "gaps": gaps,
        }

    # ------------------------------------------------------------------ identifiers
    def _identifiers(self) -> None:
        seen: dict[str, str] = {}
        groups = (
            ("roles", [r.id for r in self.spec.roles]),
            ("subtrees", [s.id for s in self.spec.subtrees]),
            ("conditions", [c.id for c in self.spec.conditions]),
            ("events", [e.id for e in self.spec.events]),
        )
        for section, ids in groups:
            for index, ident in enumerate(ids):
                if not IDENT.match(ident):
                    self.d.error("ID001", f"{section}[{index}].id", f"'{ident}' is not a valid id")
                key = f"{section}:{ident}"
                if key in seen:
                    self.d.error("ID001", f"{section}[{index}].id", f"duplicate id '{ident}'")
                seen[key] = section
        for cid in self.conditions:
            if cid in self.events:
                self.d.error("ID001", f"events.{cid}", f"'{cid}' is both a condition and an event")
        for name in self.params:
            if not IDENT.match(name):
                self.d.error("ID001", f"parameters.{name}", f"'{name}' is not a valid id")
        for rid in self.missions_keys():
            if rid not in self.roles:
                self.d.error("ROLE001", f"missions.{rid}", f"role '{rid}' is not defined")

    def missions_keys(self) -> list[str]:
        return list(self.spec.missions)

    def _declared_blackboard(self) -> None:
        for path, decl in self.spec.blackboard.items():
            p = f"blackboard.{path}"
            if not path.startswith("mission.") or not FIELD_PATH.match(path):
                self.d.error("BB001", p, "fields the model declares must be named 'mission.<name>'")
                continue
            if path in self.c.blackboard:
                self.d.error("BB001", p, "field already exists in the capability set")
                continue
            dim = None
            if decl.unit:
                dim = infer_dimension(decl.unit)
                if dim is None:
                    self.d.error("UNIT001", f"{p}.unit", f"unknown unit '{decl.unit}'")
            self.declared[path] = BBField(path, decl.type, dim, None, default=decl.default)

    # ------------------------------------------------------------------ parameters
    def _parameters(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, param in self.params.items():
            value = param.value
            unit = param.unit
            if param.type in T_PHYSICAL and isinstance(value, int | float) and unit:
                try:
                    value = to_canonical(float(value), unit, param.type)
                except UnitError as exc:
                    self.d.error("UNIT001", f"parameters.{name}.unit", str(exc))
                    continue
            elif param.type in T_PHYSICAL and value is not None and not unit:
                self.d.error("UNIT001", f"parameters.{name}.unit", "a quantity needs a unit")
            out[name] = {"type": param.type, "value": value}
        return out

    # ------------------------------------------------------------------ layer 7
    def _missions(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for rid in self.roles:
            if rid not in self.spec.missions:
                self.d.error("FSM001", f"missions.{rid}", f"role '{rid}' has no mission")
        for rid, mission in self.spec.missions.items():
            if rid not in self.roles:
                continue
            path = f"missions.{rid}"
            if isinstance(mission, MissionRef):
                expanded = self._mission_ref(rid, mission, path)
                if expanded is None:
                    continue
                mission = expanded
            out[rid] = self._mission(rid, mission, path)
        return out

    def _mission_ref(self, rid: str, ref: MissionRef, path: str) -> Mission | None:
        from afsim_research_agent.ir.v2.from_tactic import TacticConversionError, role_mission

        entry = self.c.tactics.get(ref.tactic)
        if entry is None or entry.section != "missions":
            self.d.error("TAC001", f"{path}.tactic", f"'{ref.tactic}' is not a tactic-set mission")
            return None
        for key in sorted(set(ref.params) - set(entry.parameters)):
            self.d.error(
                "TAC001", f"{path}.params.{key}", f"'{ref.tactic}' has no parameter '{key}'"
            )
        try:
            converted = role_mission(self.c, entry, ref.role)
        except TacticConversionError as exc:
            self.d.error("TAC001", path, str(exc))
            return None
        self.used_slices.add(entry.slice)
        mission, state_trees, events = converted
        for event in events:
            if event["id"] not in self.events and event["id"] not in self.conditions:
                from afsim_research_agent.ir.v2.models import Event

                self.events[event["id"]] = Event.model_validate(event)
        trees = self.spec.state_trees.setdefault(rid, {})
        for state, tree in state_trees.items():
            trees.setdefault(state, tree)
        return Mission.model_validate(mission)

    def _mission(self, rid: str, m: Mission, path: str) -> dict[str, Any]:
        states = list(m.states)
        known = set(states)
        for index, state in enumerate(states):
            if not STATE.match(state):
                self.d.error(
                    "ID001", f"{path}.states[{index}]", f"'{state}' is not a valid state name"
                )
        if len(known) != len(states):
            self.d.error("FSM001", f"{path}.states", "duplicate state names")
        if m.initial_state not in known:
            self.d.error("FSM001", f"{path}.initial_state", f"'{m.initial_state}' is not a state")
        terminal = [s for s in m.terminal if s in known]
        for s in sorted(set(m.terminal) - known):
            self.d.error("FSM001", f"{path}.terminal", f"'{s}' is not a state")
        safe = list(m.safe) if m.safe is not None else [s for s in DEFAULT_SAFE if s in known]
        for s in sorted(set(safe) - known):
            self.d.error("FSM001", f"{path}.safe", f"'{s}' is not a state")
        edges: dict[str, set[str]] = {s: set() for s in states}
        transitions = []
        for index, t in enumerate(m.transitions):
            tp = f"{path}.transitions[{index}]"
            bad = False
            for end, name in (("from", t.from_state), ("to", t.to)):
                if name not in known:
                    self.d.error("FSM001", f"{tp}.{end}", f"'{name}' is not a state")
                    bad = True
            if t.from_state in terminal:
                self.d.error("FSM001", tp, f"terminal state '{t.from_state}' has a transition")
            self._trigger(t.on, f"{tp}.on")
            if not bad:
                edges[t.from_state].add(t.to)
            transitions.append(
                {
                    "from": t.from_state,
                    "to": t.to,
                    "on": t.on,
                    "priority": t.priority,
                    "index": index,
                }
            )
        globals_ = []
        for index, g in enumerate(m.global_transitions):
            gp = f"{path}.global[{index}]"
            if g.to not in known:
                self.d.error("FSM001", f"{gp}.to", f"'{g.to}' is not a state")
            self._trigger(g.on, f"{gp}.on")
            globals_.append({"to": g.to, "on": g.on, "priority": g.priority})
            for s in states:
                if s not in terminal and s != g.to and g.to in known:
                    edges[s].add(g.to)
        # priority order: higher first, then as written
        transitions.sort(key=lambda t: (t["from"], -t["priority"], t["index"]))
        for t in transitions:
            t.pop("index")
        goals = set(terminal) | set(safe)
        if not goals:
            self.d.error("FSM001", path, "mission has neither a terminal state nor a safe state")
        else:
            reach = _reaches(edges, goals)
            for s in states:
                if s not in goals and s not in reach:
                    self.d.error(
                        "FSM001",
                        f"{path}.states",
                        f"state '{s}' cannot reach a safe or terminal state ({sorted(goals)})",
                    )
        if m.initial_state in known:
            reachable = _reachable(edges, m.initial_state)
            for s in states:
                if s not in reachable:
                    self.d.warning("FSM001", f"{path}.states", f"state '{s}' is unreachable")
        return {
            "initial_state": m.initial_state,
            "states": states,
            "terminal": terminal,
            "safe": safe,
            "transitions": transitions,
            "global": globals_,
        }

    def _trigger(self, name: str, path: str) -> None:
        if name in self.conditions or name in self.events:
            return
        self.d.error("REF001", path, f"'{name}' is neither a condition nor an event")

    def _events(self, missions: dict[str, Any]) -> None:
        for e in self.spec.events:
            p = f"events.{e.id}"
            if e.source == "timer" and e.after_s is None:
                self.d.error("IR002", p, "a timer event needs after_s")
            if e.source == "tactic":
                entry = self.c.tactics.get(e.tactic or "")
                if entry is None or entry.section != "missions":
                    self.d.error(
                        "TAC001", f"{p}.tactic", "a tactic event names its tactic-set mission"
                    )
                else:
                    self.used_slices.add(entry.slice)
        used = {t["on"] for m in missions.values() for t in m["transitions"] + m["global"]}
        for e in self.spec.events:
            if e.id not in used:
                self.d.info("REF001", f"events.{e.id}", "event is not used by any transition")

    def _holds(self, missions: dict[str, Any]) -> None:
        all_states = {s for m in missions.values() for s in m["states"]}
        for index, h in enumerate(self.spec.arbitration.hold):
            p = f"arbitration.hold[{index}]"
            if h.resource not in self.c.resources:
                self.d.error("ARB001", f"{p}.resource", f"unknown resource '{h.resource}'")
            for end, s in (("from", h.from_state), ("to", h.to)):
                if s not in all_states:
                    self.d.error("FSM001", f"{p}.{end}", f"'{s}' is not a state")

    # ------------------------------------------------------------------ layer 6
    def _state_trees(self, rid: str, mission: dict[str, Any]) -> dict[str, Any]:
        mapping = self.spec.state_trees.get(rid, {})
        out: dict[str, Any] = {}
        held = {(h.from_state, h.resource) for h in self.spec.arbitration.hold}
        for state in mission["states"]:
            ref = mapping.get(state)
            path = f"state_trees.{rid}.{state}"
            if ref is None:
                if state not in mission["terminal"]:
                    self.d.error("FSM002", path, f"state '{state}' has no state tree")
                out[state] = {"tree_ref": None, "tree": None, "leases": []}
                continue
            if state in mission["terminal"]:
                self.d.warning("FSM002", path, "terminal states do not run a tree")
            tree = self._tree_ref(ref, path, rid, f"{rid}.{state}")
            leases = []
            if tree is not None:
                for leaf in _leaves(tree):
                    if leaf["type"] != "action":
                        continue
                    contract = self._contract(leaf["action"])
                    # resources this request owns: exclusive ones it acquires itself
                    exclusive = sorted(
                        r["resource"]
                        for r in (contract.resources if contract else ())
                        if r.get("mode") == "exclusive" and r.get("acquire") is not False
                    )
                    # ownership the contract keeps after the action ends (e.g. a selected
                    # target), or the model holds into the next state, is not released on exit
                    kept = sorted(
                        r["resource"]
                        for r in (contract.resources if contract else ())
                        if r.get("mode") == "exclusive"
                        and (
                            r.get("release_on_terminal") is False or (state, r["resource"]) in held
                        )
                    )
                    leases.append(
                        {
                            "request": leaf["node_id"],
                            "action": leaf["action"],
                            "resources": exclusive,
                            "release_on_exit": [r for r in exclusive if r not in kept],
                            "kept_on_exit": kept,
                        }
                    )
            out[state] = {"tree_ref": ref, "tree": tree, "leases": leases}
        return out

    def _unmapped_state_trees(self, missions: dict[str, Any]) -> None:
        for rid, mapping in self.spec.state_trees.items():
            if rid not in self.roles:
                self.d.error("ROLE001", f"state_trees.{rid}", f"role '{rid}' is not defined")
                continue
            states = set(missions.get(rid, {}).get("states", ()))
            for state in mapping:
                if state not in states:
                    self.d.error(
                        "FSM002", f"state_trees.{rid}.{state}", f"'{state}' is not a state"
                    )

    def _tree_ref(self, ref: str, path: str, rid: str, prefix: str) -> dict[str, Any] | None:
        if ref in self.subtrees:
            return self._node(self.subtrees[ref].root, path, rid, prefix, (ref,))
        entry = self.c.tactics.get(ref)
        if entry is None or "root" not in entry.raw:
            code = "TAC001" if "." in ref else "REF001"
            self.d.error(code, path, f"'{ref}' is neither a subtree nor a tactic-set tree")
            return None
        self.used_slices.add(entry.slice)
        return self._tactic_node(entry.raw["root"], entry, prefix, (ref,))

    # ------------------------------------------------------------------ layer 5
    def _node(
        self, node: Node, path: str, rid: str, node_id: str, stack: tuple[str, ...]
    ) -> dict[str, Any] | None:
        kind = node.type
        out: dict[str, Any] = {"type": kind, "node_id": node_id, "origin": "model"}
        if node.id:
            out["label"] = node.id
        leaf = kind in ("action", "condition", "tactic", "subtree")
        if leaf and node.children:
            self.d.error("TREE001", path, f"'{kind}' takes no children")
        if kind in COMPOSITES and not node.children:
            self.d.error("TREE001", path, f"'{kind}' needs at least one child")
        if kind in ("inverter", "succeeder", "negator", "repeat") and len(node.children) != 1:
            self.d.error("TREE001", path, f"decorator '{kind}' takes exactly one child")
        if node.policy is not None and kind != "parallel":
            self.d.error("IR002", f"{path}.policy", "policy only applies to parallel")
        if node.repeat is not None and kind != "repeat":
            self.d.error("IR002", f"{path}.repeat", "repeat settings only apply to repeat")
        gap_subtree = kind == "subtree" and (node.subtree or "").startswith(GAP)
        if node.params and kind not in ("action", "tactic") and not gap_subtree:
            self.d.error("IR002", f"{path}.params", f"'{kind}' takes no params")
        for name, attr in (
            ("action", "action"),
            ("condition", "condition"),
            ("tactic", "tactic"),
            ("subtree", "subtree"),
        ):
            if getattr(node, attr) is not None and kind != name:
                self.d.error("IR002", f"{path}.{attr}", f"'{attr}' only applies to {name} nodes")
        if kind == "parallel":
            policy = node.policy or _default_policy()
            if policy.type == "threshold" and (
                policy.count is None or policy.count > len(node.children)
            ):
                self.d.error(
                    "TREE001",
                    f"{path}.policy",
                    "threshold needs a count of at most the child count",
                )
            if policy.type != "threshold" and policy.count is not None:
                self.d.error("IR002", f"{path}.policy.count", "count only applies to threshold")
            out["policy"] = policy.model_dump(exclude_none=True)
        if kind == "repeat":
            rep = node.repeat
            chosen = (
                []
                if rep is None
                else [
                    k
                    for k, v in (
                        ("count", rep.count),
                        ("until_done", rep.until_done or None),
                        ("duration_s", rep.duration_s),
                    )
                    if v
                ]
            )
            if len(chosen) != 1:
                self.d.error(
                    "TREE001",
                    f"{path}.repeat",
                    "repeat needs exactly one of count, until_done, duration_s",
                )
            else:
                out["repeat"] = {chosen[0]: getattr(rep, chosen[0])}
        if kind == "action":
            self._action_leaf(node, path, rid, out)
        elif kind == "condition":
            if node.condition not in self.conditions:
                self.d.error(
                    "REF001", f"{path}.condition", f"condition '{node.condition}' is not defined"
                )
            out["condition"] = node.condition
        elif kind == "tactic":
            return self._tactic_ref(node, path, node_id, stack)
        elif kind == "subtree":
            sid = node.subtree or ""
            if gap_subtree:
                return self._gap_leaf(node, sid, "subtree", path, rid, out)
            if sid not in self.subtrees:
                self.d.error("REF001", f"{path}.subtree", f"subtree '{sid}' is not defined")
                return out
            if sid in stack:
                self.d.error(
                    "TREE001", f"{path}.subtree", f"recursive subtree: {' -> '.join([*stack, sid])}"
                )
                return out
            child = self._node(
                self.subtrees[sid].root,
                f"subtrees.{sid}.root",
                rid,
                f"{node_id}.{sid}",
                (*stack, sid),
            )
            out = {**out, "subtree": sid, "children": [child] if child else []}
            return out
        parent_weighted = kind in ("priority_selector", "weighted_random")
        children = []
        for index, child in enumerate(node.children):
            cp = f"{path}.children[{index}]"
            if child.weight is not None and not parent_weighted:
                self.d.error(
                    "IR002",
                    f"{cp}.weight",
                    "weight only applies under priority_selector or weighted_random",
                )
            if parent_weighted and child.weight is None:
                self.d.error("TREE001", f"{cp}.weight", f"children of '{kind}' need a weight")
            built = self._node(child, cp, rid, f"{node_id}.{index}", stack)
            if built is not None:
                if parent_weighted and child.weight is not None:
                    built["weight"] = self._weight(child.weight, f"{cp}.weight")
                children.append(built)
        if children or not leaf:
            out["children"] = children
        if kind == "parallel":
            self._parallel_resources(out, path)
        return out

    def _weight(self, weight: Any, path: str) -> Any:
        if isinstance(weight, int | float) and not isinstance(weight, bool):
            return weight
        kind = self._operand(weight, path)
        if kind is not None and not T.is_numeric(kind):
            self.d.error("TYPE001", path, "weight must be numeric")
        return weight

    def _tactic_ref(
        self, node: Node, path: str, node_id: str, stack: tuple[str, ...]
    ) -> dict[str, Any] | None:
        ref = node.tactic or ""
        entry = self.c.tactics.get(ref)
        if entry is None or entry.section == "missions" or "root" not in entry.raw:
            self.d.error(
                "TAC001", f"{path}.tactic", f"'{ref}' is not a tactic-set subtree or state tree"
            )
            return None
        for key in sorted(set(node.params) - set(entry.parameters)):
            self.d.error(
                "TAC001", f"{path}.params.{key}", f"'{ref}' has no public parameter '{key}'"
            )
        self.used_slices.add(entry.slice)
        built = self._tactic_node(entry.raw["root"], entry, node_id, (*stack, ref))
        out = {
            "type": "tactic",
            "node_id": node_id,
            "origin": "model",
            "tactic": ref,
            "params": dict(node.params),
            "children": [built],
        }
        if _failure_as_running(entry.raw):
            out["failure_as_running"] = True
        return out

    def _tactic_node(
        self, raw: dict[str, Any], entry: TacticEntry, node_id: str, stack: tuple[str, ...]
    ) -> dict[str, Any]:
        """Tactic-set content is implemented and tested in the lab; it is expanded as written,
        with two readings of the lab's tree vocabulary (design §5.4):

        - ``sequence`` / ``selector`` are the usual behavior-tree nodes that resume at a
          running child (AFSIM ``*_with_memory``): a lab action spans several updates, and
          restarting the sequence would issue it again every update;
        - a subtree marked ``no_target_result: RUNNING`` keeps running instead of failing.
        """
        kind = str(raw.get("type"))
        kind = TACTIC_NODES.get(kind, kind)
        out: dict[str, Any] = {"type": kind, "node_id": node_id, "origin": entry.id}
        if kind == "action":
            out["action"] = raw.get("action")
            if raw.get("operation"):
                out["operation"] = raw["operation"]
            contract = self.c.actions.get(str(raw.get("action")))
            if contract is not None:
                self.used_slices.add(contract.slice)
            return out
        if kind == "condition":
            out["tactic_condition"] = raw.get("condition")
            return out
        if kind == "subtree":
            ref = str(raw.get("subtree"))
            sub = self.c.tactics.get(ref)
            out["subtree"] = ref
            if sub is None or "root" not in sub.raw or ref in stack:
                self.d.error(
                    "TAC001", f"tactic:{entry.id}", f"tactic-set reference '{ref}' does not resolve"
                )
                out["children"] = []
                return out
            self.used_slices.add(sub.slice)
            out["children"] = [
                self._tactic_node(sub.raw["root"], sub, f"{node_id}.{ref}", (*stack, ref))
            ]
            if _failure_as_running(sub.raw):
                out["failure_as_running"] = True
            return out
        if kind == "timeout":
            out["duration_s"] = raw.get("duration_s")
        if kind == "parallel":
            policy = str(raw.get("success_policy") or "one")
            out["policy"] = {"type": policy}
            if policy == "threshold":
                out["policy"]["count"] = int(raw.get("threshold") or 1)
        if kind == "repeat":
            if raw.get("until_done"):
                out["repeat"] = {"until_done": True}
            elif raw.get("count"):
                out["repeat"] = {"count": int(raw["count"])}
            else:
                out["repeat"] = {"duration_s": float(raw.get("duration_s") or 0.0)}
        kids = raw.get("children") or ([raw["child"]] if raw.get("child") else [])
        out["children"] = [
            self._tactic_node(k, entry, f"{node_id}.{i}", stack) for i, k in enumerate(kids)
        ]
        return out

    # ------------------------------------------------------------------ layer 2
    def _action_leaf(self, node: Node, path: str, rid: str, out: dict[str, Any]) -> None:
        aid = node.action or ""
        if aid.startswith(GAP):
            self._gap_leaf(node, aid, "action", path, rid, out)
            return
        out["action"] = aid
        contract = self.c.actions.get(aid)
        if contract is None:
            self.d.error(
                "ACT001",
                f"{path}.action",
                f"'{aid}' is not in the capability set",
                suggestion="use an action from the action set, or record a gap and use gap.<id>",
            )
            out["params"] = dict(node.params)
            return
        self.used_slices.add(contract.slice)
        role = self.roles.get(rid)
        if (
            role
            and contract.platform_classes
            and role.capability_class not in contract.platform_classes
        ):
            self.d.error(
                "ACT001", f"{path}.action", f"'{aid}' does not apply to {role.capability_class}"
            )
        if contract.kind == "tactic_step":
            self.d.info(
                "ACT001",
                f"{path}.action",
                f"'{aid}' is a tactic step without its own contract (partial)",
            )
        out["contract"] = {
            "slice": contract.slice,
            "kind": contract.kind,
            "resources": [dict(r) for r in contract.resources],
            "failure_codes": list(contract.failure_codes),
        }
        out["params"] = self._action_params(contract, node.params, f"{path}.params")

    def _action_params(
        self, contract: ActionContract, given: dict[str, Any], path: str
    ) -> dict[str, Any]:
        specs = contract.parameters
        out: dict[str, Any] = {}
        for name in sorted(set(given) - set(specs)):
            self.d.error("ACT002", f"{path}.{name}", f"'{contract.id}' has no parameter '{name}'")
        for name, spec in specs.items():
            spec = spec if isinstance(spec, dict) else {}
            p = f"{path}.{name}"
            if name not in given:
                if spec.get("required") and "default" not in spec:
                    self.d.error("ACT002", p, f"required parameter of '{contract.id}' is missing")
                continue
            out[name] = self._param_value(given[name], spec, p)
        return out

    def _param_value(self, value: Any, spec: dict[str, Any], path: str) -> Any:
        ptype = PARAM_TYPES.get(str(spec.get("type")), "any")
        unit = spec.get("units")
        dim, cunit = CONTRACT_UNITS.get(str(unit), (None, None)) if unit else (None, None)
        if unit and dim is None:
            self.d.warning(
                "UNIT001", path, f"contract unit '{unit}' is not known; value not converted"
            )
        if isinstance(value, str) and value.startswith("$"):
            value = {"parameter_ref": value[1:]}
        number: float | None = None
        if isinstance(value, dict) and set(value) == {"parameter_ref"}:
            name = value["parameter_ref"]
            param = self.params.get(name)
            if param is None:
                self.d.error("REF001", path, f"parameter '{name}' is not defined")
                return value
            ptype_param = param.type
            if dim is not None:
                if ptype_param not in T_PHYSICAL or not T.comparable(ptype_param, dim)[0]:
                    self.d.error(
                        "ACT002",
                        path,
                        f"parameter '{name}' ({ptype_param}) does not fit a {dim} argument",
                    )
                    return value
                if isinstance(param.value, int | float) and param.unit:
                    try:
                        number = to_canonical(float(param.value), param.unit, dim)
                    except UnitError:
                        number = None
            elif ptype != "any" and T.parameter_value_type(ptype_param) not in (
                ptype,
                "double" if ptype == "int" else ptype,
            ):
                if not (ptype == "double" and ptype_param in ("integer",)):
                    self.d.error(
                        "ACT002",
                        path,
                        f"parameter '{name}' ({ptype_param}) does not fit a {ptype} argument",
                    )
            self._range(number, spec, path)
            return value
        if isinstance(value, dict) and set(value) == {"value", "unit"}:
            if dim is None:
                self.d.error("ACT002", path, "this argument takes no unit")
                return value
            try:
                number = to_canonical(float(value["value"]), str(value["unit"]), dim)
            except (UnitError, TypeError, ValueError) as exc:
                self.d.error("UNIT001", path, str(exc))
                return value
            self._range(number, spec, path)
            return number  # canonical unit equals the contract unit
        if isinstance(value, bool):
            if ptype not in ("bool", "any"):
                self.d.error("ACT002", path, f"expected {ptype}, got a boolean")
            return value
        if isinstance(value, int | float):
            if ptype in ("string", "bool"):
                self.d.error("ACT002", path, f"expected {ptype}, got a number")
                return value
            if ptype == "int" and not float(value).is_integer():
                self.d.error("ACT002", path, "expected an integer")
            self._range(float(value), spec, path)
            return value
        if isinstance(value, str):
            if ptype not in ("string", "any"):
                self.d.error("ACT002", path, f"expected {ptype}, got a string")
            if spec.get("minLength") and len(value) < int(spec["minLength"]):
                self.d.error("ACT002", path, "string is too short")
            if spec.get("enum") and value not in spec["enum"]:
                self.d.error("ACT002", path, f"expected one of {spec['enum']}")
            return value
        self.d.error("ACT002", path, "unsupported argument value")
        return value

    def _range(self, number: float | None, spec: dict[str, Any], path: str) -> None:
        if number is None:
            return
        checks = (
            ("minimum", lambda v, b: v >= b, ">="),
            ("maximum", lambda v, b: v <= b, "<="),
            ("exclusiveMinimum", lambda v, b: v > b, ">"),
            ("exclusiveMaximum", lambda v, b: v < b, "<"),
        )
        for key, ok, sym in checks:
            bound = spec.get(key)
            if isinstance(bound, int | float) and not ok(number, float(bound)):
                self.d.error("ACT002", path, f"value {number:g} must be {sym} {bound}")

    # ------------------------------------------------------------------ gaps
    def _contract(self, aid: str) -> ActionContract | None:
        return self.gap_contracts.get(aid) or self.c.actions.get(aid)

    def _gap_leaf(
        self, node: Node, ref: str, kind: str, path: str, rid: str, out: dict[str, Any]
    ) -> dict[str, Any]:
        """A ``gap.<id>`` placeholder: checked against the gap's contract like a real entry,
        executed by its stub in L2 and the 2D simulation, refused by the AFSIM compiler."""
        attr = "action" if kind == "action" else "subtree"
        gid = ref[len(GAP) :]
        gap = self.gaps.get(gid)
        out.update({"type": "action", "action": ref})
        if gap is None:
            self.d.error("REF001", f"{path}.{attr}", f"gap '{gid}' is not listed under gaps")
            out["params"] = dict(node.params)
            return out
        if gap.kind != kind:
            self.d.error(
                "GAP001",
                f"{path}.{attr}",
                f"gap '{gid}' is a{'n' if gap.kind == 'action' else ''} {gap.kind} gap; "
                f"reference it as a{'n' if gap.kind == 'action' else ''} {gap.kind} leaf",
            )
        role = self.roles.get(rid)
        if role and gap.capability_class and role.capability_class != gap.capability_class:
            self.d.error("GAP001", f"{path}.{attr}", f"gap '{gid}' is for {gap.capability_class}")
        self.gap_uses[gid].append(out["node_id"])
        self.d.warning(
            "GAP001",
            f"{path}.{attr}",
            f"placeholder for gap '{gid}' ({gap.kind}): runs its stub in L2 / L3, "
            "not compiled to AFSIM until the gap is filled",
        )
        contract = self.gap_contracts[ref]
        out["gap"] = {
            "id": gid,
            "kind": gap.kind,
            "stub": gap.stub.model_dump(exclude_none=True),
        }
        out["contract"] = {
            "slice": None,
            "kind": "gap",
            "resources": [dict(r) for r in contract.resources],
            "failure_codes": list(contract.failure_codes),
        }
        out["params"] = self._action_params(contract, node.params, f"{path}.params")
        return out

    def _gaps(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for gid, gap in self.gaps.items():
            p = f"gaps.{gid}"
            if not IDENT.match(gid):
                self.d.error("ID001", p, f"'{gid}' is not a valid gap id")
            for r in gap.roles:
                if r not in self.roles:
                    self.d.error("ROLE001", f"{p}.roles", f"role '{r}' is not defined")
            for name, prm in gap.params.items():
                if prm.unit and infer_dimension(prm.unit) is None:
                    self.d.error("UNIT001", f"{p}.params.{name}.unit", f"unknown unit '{prm.unit}'")
            for index, field_path in enumerate(gap.blackboard.reads):
                self._bb(field_path, f"{p}.blackboard.reads[{index}]")
            writes = set(gap.blackboard.writes)
            for index, field_path in enumerate(gap.blackboard.writes):
                if field_path not in self.declared and field_path not in self.c.blackboard:
                    self.d.error(
                        "BB001",
                        f"{p}.blackboard.writes[{index}]",
                        f"field '{field_path}' is neither imported nor declared "
                        "(declare new fields as mission.<name>)",
                    )
            for key in sorted(set(gap.stub.writes) - writes):
                self.d.error(
                    "GAP001", f"{p}.stub.writes.{key}", "the stub may only write the gap's writes"
                )
            for res in gap.resources:
                if res not in self.c.resources:
                    self.d.info("GAP001", f"{p}.resources", f"'{res}' is a new resource")
            if gap.stub.result == "failure" and not gap.stub.failure_code:
                self.d.warning("GAP001", f"{p}.stub", "a failing stub should name a failure code")
            for name in gap.uses:
                if name not in self.c.actions and name not in self.c.tactics:
                    self.d.warning("GAP001", f"{p}.uses", f"'{name}' is not in the capability set")
            uses = self.gap_uses.get(gid, [])
            if not uses:
                self.d.warning("GAP001", p, "gap is not used by any tree")
            data = gap.model_dump(exclude_none=True)
            data["used_by"] = gap.used_by or sorted({".".join(u.split(".")[:2]) for u in uses})
            if gap.kind == "action":
                data["local_only"] = True
            out[gid] = data
        return out

    # ------------------------------------------------------------------ layer 4
    def _parallel_resources(self, node: dict[str, Any], path: str) -> None:
        owners: dict[str, int] = {}
        for index, child in enumerate(node.get("children", [])):
            mine: set[str] = set()
            for leaf in _leaves(child):
                if leaf["type"] == "action":
                    contract = self._contract(leaf.get("action") or "")
                    if contract is not None and leaf.get("operation") != "cancel":
                        mine |= contract.exclusive
            for res in sorted(mine):
                if res in owners:
                    self.d.error(
                        "ARB001",
                        path,
                        f"branches {owners[res]} and {index} both need exclusive resource '{res}'",
                    )
                else:
                    owners[res] = index

    # ------------------------------------------------------------------ layer 3: expressions
    def _condition(self, cid: str, expr: Any) -> Any:
        expr = copy.deepcopy(expr)
        return self._expr(expr, f"conditions.{cid}.expression", (cid,))

    def _expr(self, expr: Any, path: str, stack: tuple[str, ...]) -> Any:
        if not isinstance(expr, dict) or len(expr) != 1:
            self.d.error("IR002", path, "expression must be a mapping with exactly one key")
            return expr
        key, value = next(iter(expr.items()))
        if key in ("all", "any"):
            if not isinstance(value, list) or not value:
                self.d.error("IR002", f"{path}.{key}", f"'{key}' needs a non-empty list")
                return expr
            return {key: [self._expr(v, f"{path}.{key}[{i}]", stack) for i, v in enumerate(value)]}
        if key == "not":
            return {"not": self._expr(value, f"{path}.not", stack)}
        if key == "condition_ref":
            if value not in self.conditions:
                self.d.error(
                    "REF001", f"{path}.condition_ref", f"condition '{value}' is not defined"
                )
            elif value in stack:
                self.d.error("REF001", f"{path}.condition_ref", f"recursive condition '{value}'")
            return expr
        if key == "compare":
            return {"compare": self._compare(value, f"{path}.compare")}
        expr = self._rewrite_v1(expr, path)
        kind = self._operand(expr, path)
        if kind is not None and kind not in (T.BOOL, T.ANY):
            self.d.error("TYPE001", path, f"expression must be boolean, got {kind}")
        return expr

    def _compare(self, value: Any, path: str) -> Any:
        if not isinstance(value, dict) or set(value) != {"left", "operator", "right"}:
            self.d.error("IR002", path, "compare needs exactly left, operator and right")
            return value
        if value["operator"] not in COMPARE_OPS:
            self.d.error(
                "IR002", f"{path}.operator", f"operator must be one of {list(COMPARE_OPS)}"
            )
        left_op = self._rewrite_v1(value["left"], f"{path}.left")
        right_op = self._rewrite_v1(value["right"], f"{path}.right")
        left = self._operand(left_op, f"{path}.left")
        right = self._operand(right_op, f"{path}.right")
        out = {"left": left_op, "operator": value["operator"], "right": right_op}
        if left is None or right is None:
            return out
        ok, reason = T.comparable(left, right)
        if not ok:
            code = "UNIT001" if T.is_quantity(left) or T.is_quantity(right) else "TYPE001"
            self.d.error(code, path, reason)
        elif value["operator"] not in ("eq", "ne") and (
            T.BOOL in (left, right) or T.STRING in (left, right)
        ):
            self.d.error("TYPE001", path, f"operator '{value['operator']}' needs numeric operands")
        return out

    def _rewrite_v1(self, op: Any, path: str) -> Any:
        """IR 1.0 operands become blackboard reads through the alignment's [blackboard] key."""
        if not isinstance(op, dict) or len(op) != 1:
            return op
        key, value = next(iter(op.items()))
        if key not in V1_OPERANDS:
            return op
        prop = value.get("property") if isinstance(value, dict) else value
        name = f"{key}.{prop}"
        target = self.c.blackboard_aliases.get(name)
        if target is None:
            self.d.error(
                "BB001",
                path,
                f"IR 1.0 operand '{name}' has no blackboard field in this capability set",
                suggestion="read a blackboard field with {bb: <field>}, or record a gap",
            )
            return op
        self.d.info("BB001", path, f"IR 1.0 operand '{name}' read as blackboard field '{target}'")
        return {"bb": target}

    def _operand(self, op: Any, path: str) -> str | None:
        if not isinstance(op, dict):
            self.d.error("IR002", path, "operand must be a mapping")
            return None
        if set(op) == {"value", "unit"}:
            dim = infer_dimension(str(op["unit"]))
            if dim is None:
                self.d.error("UNIT001", path, f"unknown unit '{op['unit']}'")
            return dim
        if len(op) != 1:
            self.d.error("IR002", path, "operand must have exactly one key")
            return None
        key, value = next(iter(op.items()))
        if key == "literal":
            kind = T.literal_type(value)
            if kind is None:
                self.d.error("TYPE001", path, "literal must be bool, number or string")
            return kind
        if key == "parameter_ref":
            param = self.params.get(value) if isinstance(value, str) else None
            if param is None:
                self.d.error(
                    "REF001", f"{path}.parameter_ref", f"parameter '{value}' is not defined"
                )
                return None
            return T.parameter_value_type(param.type)
        if key == "bb":
            return self._bb(value, f"{path}.bb")
        if key == "function_call":
            return self._function(value, path)
        if key in V1_OPERANDS:
            return None  # reported by _rewrite_v1
        self.d.error("IR002", path, f"unknown operand '{key}'")
        return None

    def _bb(self, field_path: Any, path: str) -> str | None:
        if not isinstance(field_path, str):
            self.d.error("IR002", path, "expected a blackboard field path")
            return None
        fld = self.declared.get(field_path) or self.c.blackboard.get(field_path)
        if fld is None:
            self.d.error(
                "BB001", path, f"blackboard field '{field_path}' is neither imported nor declared"
            )
            return None
        self.reads.add(field_path)
        return fld.dimension or fld.type

    def _function(self, value: Any, path: str) -> str | None:
        if not isinstance(value, dict) or "name" not in value:
            self.d.error("IR002", path, "function_call needs name and arguments")
            return None
        args_spec = FUNCTIONS.get(value["name"])
        if args_spec is None:
            self.d.error("REF001", f"{path}.name", f"function '{value['name']}' is not available")
            return None
        args = value.get("arguments") or {}
        for name, kind in args_spec.items():
            if name not in args:
                self.d.error("IR002", f"{path}.arguments.{name}", "missing argument")
                continue
            if kind == "field":
                got = self._bb(args[name], f"{path}.arguments.{name}")
                if got is not None and got != "time":
                    self.d.error("UNIT001", f"{path}.arguments.{name}", "expected a time field")
                continue
            got = self._operand(args[name], f"{path}.arguments.{name}")
            if got is not None and not T.comparable(kind, got)[0]:
                self.d.error("UNIT001", f"{path}.arguments.{name}", f"expected {kind}")
        return T.BOOL


GAP = "gap."
GAP_TYPES = {"double": "number", "int": "integer", "bool": "boolean", "string": "string"}


def _gap_contract(gid: str, gap: Any) -> ActionContract:
    params: dict[str, dict[str, Any]] = {}
    for name, prm in gap.params.items():
        spec: dict[str, Any] = {"type": GAP_TYPES[prm.type], "required": prm.required}
        lo_hi = list(prm.range) if prm.range is not None else None
        if prm.unit:
            dim = infer_dimension(prm.unit)
            canonical = {u: c for d, c in CONTRACT_UNITS.values() for u in (d,)}.get(dim or "")
            spec["units"] = canonical or prm.unit
            if canonical and lo_hi is not None and dim:
                try:
                    lo_hi = [to_canonical(float(v), prm.unit, dim) for v in lo_hi]
                except UnitError:
                    pass
        if lo_hi is not None:
            spec["minimum"], spec["maximum"] = lo_hi
        if prm.default is not None:
            spec["default"] = prm.default
        params[name] = spec
    return ActionContract(
        id=f"{GAP}{gid}",
        slice="gap",
        kind="gap",
        status="gap",
        parameters=params,
        resources=tuple({"resource": r, "mode": "exclusive"} for r in gap.resources),
        failure_codes=tuple(gap.result.failure.codes),
        platform_classes=(gap.capability_class,) if gap.capability_class else (),
    )


TACTIC_NODES = {"sequence": "sequence_with_memory", "selector": "selector_with_memory"}


def _failure_as_running(raw: dict[str, Any]) -> bool:
    return str(raw.get("no_target_result", "")).upper() == "RUNNING"


T_PHYSICAL = ("distance", "speed", "time", "angle", "altitude", "mass", "frequency", "ratio")


def _default_policy() -> Any:
    from afsim_research_agent.ir.v2.models import Policy

    return Policy()


def _leaves(node: dict[str, Any] | None) -> list[dict[str, Any]]:
    if node is None:
        return []
    kids = node.get("children") or []
    if not kids and node["type"] in ("action", "condition"):
        return [node]
    out: list[dict[str, Any]] = []
    for kid in kids:
        out.extend(_leaves(kid))
    return out


def _reachable(edges: dict[str, set[str]], start: str) -> set[str]:
    seen = {start}
    todo = [start]
    while todo:
        for nxt in edges.get(todo.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return seen


def _reaches(edges: dict[str, set[str]], goals: set[str]) -> set[str]:
    """States from which some goal is reachable."""
    back: dict[str, set[str]] = {}
    for src, dsts in edges.items():
        for dst in dsts:
            back.setdefault(dst, set()).add(src)
    seen = set(goals)
    todo = list(goals)
    while todo:
        for prev in back.get(todo.pop(), ()):
            if prev not in seen:
                seen.add(prev)
                todo.append(prev)
    return seen
