"""Execution of the IR 2.0 internal model with AFSIM 2.9 native node semantics.

One :class:`Instance` runs one role instance. Every update follows AFSIM 2.9's
``STATE_MACHINE.Evaluate`` (probes t01 / t02): tick the current state's tree, check its
transitions in priority order, and on a transition run exit / entry and continue with the
new state (its tree, its transitions) in the same update until a state stays.
Node semantics follow AFSIM 2.9 as confirmed by probes (design §7):

- sequence / selector are memoryless; ``*_with_memory`` resume at the running child;
- parallel ticks every child each update; success when the policy is met, failure when
  every child has finished without meeting it;
- succeeder / negator return success / failure without ticking their child;
- repeat ticks its child once per update and then latches success (``count``: after n
  ticks; ``until_done``: once the child finishes; ``duration_s``: once the time is up).

Actions are executed by an :class:`ActionHost` (the 2D simulation, or a stub in L2).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Protocol

from afsim_research_agent.ir.units import infer_dimension, to_canonical
from afsim_research_agent.ir.v2.contracts import Contracts

RUNNING, SUCCESS, FAILURE = "running", "success", "failure"


class ActionHost(Protocol):
    def tick(self, inst: Instance, leaf: dict[str, Any], new: bool) -> tuple[str, str | None]: ...

    def cancel(self, inst: Instance, leaf: dict[str, Any]) -> None: ...


@dataclass
class Activation:
    running: bool = False
    leaf: dict[str, Any] | None = None


@dataclass
class Instance:
    name: str
    role: str
    index: int
    model: dict[str, Any]
    contracts: Contracts
    host: ActionHost
    blackboard: dict[str, Any] = field(default_factory=dict)
    seed: int = 0
    state: str = ""
    entered_at: float = 0.0
    now: float = 0.0
    latched: set[str] = field(default_factory=set)  # external events not yet consumed
    tree_status: str | None = None
    memory: dict[str, Any] = field(default_factory=dict)
    activations: dict[str, Activation] = field(default_factory=dict)
    owners: dict[str, str] = field(default_factory=dict)  # resource → request id
    issued: list[str] = field(default_factory=list)  # actions started in the last update
    trace: list[dict[str, Any]] = field(default_factory=list)
    unresolved: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.role_model = self.model["roles"][self.role]
        self.mission = self.role_model["mission"]
        self.params = {k: v.get("value") for k, v in self.model["parameters"].items()}
        self.events = self.model.get("events", {})
        self.rng = random.Random(self.seed)
        for path, spec in self.model["blackboard"].items():
            self.blackboard.setdefault(path, _default(spec, self.contracts, path))
        self.state = self.mission["initial_state"]

    # ------------------------------------------------------------------ public
    def raise_event(self, name: str) -> None:
        self.latched.add(name)

    def enter(self, state: str, now: float) -> None:
        self.state = state
        self.entered_at = now
        self.memory = {}
        self.tree_status = None

    def step(self, now: float) -> None:
        """One update, as WSF_STATE_MACHINE.Evaluate does it: tick the current state's tree,
        check its transitions, and on a transition exit, enter and continue with the new
        state in the same update, until a state stays (probes t01 / t02)."""
        self.now = now
        self.issued = []
        for _ in range(len(self.mission["states"]) + 1):
            tree = self.role_model["states"].get(self.state, {}).get("tree")
            if tree is not None and self.state not in self.mission["terminal"]:
                self.tree_status = self.tick(tree)
            fired = self._transition()
            if fired is None:
                return
            old = self.state
            self._exit(old)
            self.enter(fired["to"], now)
            self.trace.append({"t": now, "from": old, "to": fired["to"], "on": fired["on"]})
        self.unresolved.add(f"transition chain did not settle at t={now:g}")

    @property
    def time_in_state(self) -> float:
        return self.now - self.entered_at

    # ------------------------------------------------------------------ state machine
    def _transition(self) -> dict[str, Any] | None:
        if self.state in self.mission["terminal"]:
            return None
        local = [t for t in self.mission["transitions"] if t["from"] == self.state]
        globals_ = [g for g in self.mission["global"] if g["to"] != self.state]
        candidates = sorted(local + globals_, key=lambda t: -int(t.get("priority", 0)))
        for t in candidates:
            if self._trigger(t["on"]):
                self.latched.discard(t["on"])
                return t
        return None

    def _trigger(self, name: str) -> bool:
        if name in self.model["conditions"]:
            return bool(self.eval(self.model["conditions"][name]))
        event = self.events.get(name)
        if event is None:
            return False
        source = event.get("source")
        if source == "external":
            return name in self.latched
        if source == "timer":
            return self.time_in_state >= float(event.get("after_s") or 0.0)
        if source == "tactic":
            sem = self.contracts.semantics.event(event.get("tactic"), event.get("text"))
            if sem is None:
                self.unresolved.add(f"event {event.get('tactic')}: {event.get('text')}")
                return False
            return self._semantic(sem, name)
        self.unresolved.add(f"{source} event {name}")
        return False

    def _semantic(self, sem: dict[str, Any], name: str) -> bool:
        if sem.get("external"):
            return name in self.latched
        if "tree" in sem:
            return self.tree_status == sem["tree"]
        if "after_s" in sem:
            return self.time_in_state >= float(sem["after_s"])
        if "expression" in sem:
            return bool(self.eval(sem["expression"]))
        if "event" in sem:
            return sem["event"] in self.latched
        return False

    def _exit(self, state: str) -> None:
        for lease in self.role_model["states"].get(state, {}).get("leases", []):
            act = self.activations.get(lease["request"])
            if act is not None and act.running and act.leaf is not None:
                self.host.cancel(self, act.leaf)
                act.running = False
            for res in lease.get("release_on_exit", []):
                if self.owners.get(res) == lease["request"]:
                    del self.owners[res]

    # ------------------------------------------------------------------ tree
    def tick(self, node: dict[str, Any]) -> str:
        kind = node["type"]
        nid = node["node_id"]
        kids = node.get("children") or []
        if kind == "action":
            return self._action(node)
        if kind == "condition":
            if "tactic_condition" in node:
                return SUCCESS if self._tactic_condition(node["tactic_condition"]) else FAILURE
            cond = self.model["conditions"].get(node.get("condition"))
            return SUCCESS if cond is not None and self.eval(cond) else FAILURE
        if kind in ("tactic", "subtree"):
            status = self.tick(kids[0]) if kids else FAILURE
            if status == FAILURE and node.get("failure_as_running"):
                return RUNNING
            return status
        if kind == "sequence":
            for kid in kids:
                status = self.tick(kid)
                if status != SUCCESS:
                    return status
            return SUCCESS
        if kind == "selector":
            for kid in kids:
                status = self.tick(kid)
                if status != FAILURE:
                    return status
            return FAILURE
        if kind in ("sequence_with_memory", "selector_with_memory"):
            stop_on = FAILURE if kind.startswith("sequence") else SUCCESS
            start = self.memory.get(nid, 0)
            for index in range(start, len(kids)):
                status = self.tick(kids[index])
                if status == RUNNING:
                    self.memory[nid] = index
                    return RUNNING
                if status == stop_on:
                    self.memory[nid] = 0
                    return status
            self.memory[nid] = 0
            return SUCCESS if stop_on == FAILURE else FAILURE
        if kind == "priority_selector":
            weights = [self._weight(k) for k in kids]
            best = max(range(len(kids)), key=lambda i: (weights[i], -i))
            return self.tick(kids[best])
        if kind == "weighted_random":
            chosen = self.memory.get(nid)
            if chosen is None:
                weights = [max(0.0, self._weight(k)) for k in kids]
                total = sum(weights)
                if total <= 0:
                    return FAILURE
                pick, acc = self.rng.uniform(0, total), 0.0
                chosen = len(kids) - 1
                for i, w in enumerate(weights):
                    acc += w
                    if pick <= acc:
                        chosen = i
                        break
            status = self.tick(kids[chosen])
            self.memory[nid] = chosen if status == RUNNING else None
            return status
        if kind == "parallel":
            statuses = [self.tick(k) for k in kids]
            policy = node.get("policy") or {"type": "one"}
            need = {"one": 1, "all": len(kids)}.get(policy["type"], policy.get("count", 1))
            if statuses.count(SUCCESS) >= need:
                return SUCCESS
            if RUNNING not in statuses:
                return FAILURE
            return RUNNING
        if kind == "inverter":
            status = self.tick(kids[0])
            return {SUCCESS: FAILURE, FAILURE: SUCCESS}.get(status, status)
        if kind == "succeeder":
            return SUCCESS
        if kind == "negator":
            return FAILURE
        if kind == "repeat":
            return self._repeat(node, kids[0])
        if kind == "timeout":  # tactic-set content only
            return self._timeout(node, kids[0] if kids else None)
        raise ValueError(f"unknown node type {kind}")

    def _repeat(self, node: dict[str, Any], child: dict[str, Any]) -> str:
        nid = node["node_id"]
        mem = self.memory.setdefault(nid, {"n": 0, "start": self.now, "done": False})
        rep = node["repeat"]
        if mem["done"]:
            return SUCCESS
        if "count" in rep and mem["n"] >= rep["count"]:
            mem["done"] = True
            return SUCCESS
        if "duration_s" in rep and self.now - mem["start"] >= rep["duration_s"]:
            mem["done"] = True
            return SUCCESS
        status = self.tick(child)
        mem["n"] += 1
        if "until_done" in rep and status != RUNNING:
            mem["done"] = True
            return SUCCESS
        return RUNNING

    def _timeout(self, node: dict[str, Any], child: dict[str, Any] | None) -> str:
        """Lab tactic-set timeout: retry the child until it succeeds; fail when time is up."""
        nid = node["node_id"]
        start = self.memory.setdefault(nid, self.now)
        if child is not None and self.tick(child) == SUCCESS:
            self.memory.pop(nid, None)
            return SUCCESS
        if self.now - start >= float(node.get("duration_s") or 0.0):
            self.memory.pop(nid, None)
            return FAILURE
        return RUNNING

    def _weight(self, node: dict[str, Any]) -> float:
        w = node.get("weight", 0.0)
        if isinstance(w, int | float):
            return float(w)
        value = self._operand(w)
        return float(value) if isinstance(value, int | float) else 0.0

    def _tactic_condition(self, name: str) -> bool:
        sem = self.contracts.semantics.tactic_conditions.get(name)
        if sem is None:
            self.unresolved.add(f"tactic condition {name}")
            return False
        if "event" in sem:
            return str(sem["event"]) in self.latched
        return bool(self.eval(sem.get("expression")))

    # ------------------------------------------------------------------ actions
    def _action(self, leaf: dict[str, Any]) -> str:
        rid = leaf["node_id"]
        act = self.activations.setdefault(rid, Activation(leaf=leaf))
        contract = self.contracts.actions.get(leaf.get("action") or "")
        if leaf.get("operation") == "cancel":
            for other in self.activations.values():
                if other.running and other.leaf and other.leaf.get("action") == leaf["action"]:
                    self.host.cancel(self, other.leaf)
                    other.running = False
            return SUCCESS
        new = not act.running
        if new and contract is not None:
            for res in contract.resources:
                if res.get("mode") != "exclusive" or res.get("acquire") is False:
                    continue
                owner = self.owners.get(res["resource"])
                if owner is not None and owner != rid:
                    spec = self.contracts.resources.get(res["resource"], {})
                    self._execution(leaf, FAILURE, spec.get("conflict_code", "RESOURCE_BUSY"))
                    return FAILURE
                self.owners[res["resource"]] = rid
        if new:
            self.issued.append(str(leaf.get("action")))
        status, code = self.host.tick(self, leaf, new)
        act.running = status == RUNNING
        if status != RUNNING and contract is not None:
            for res in contract.resources:
                if res.get("mode") != "exclusive":
                    continue
                name = res["resource"]
                if status == FAILURE and self.owners.get(name) == rid:
                    self.owners.pop(name, None)  # a failed request keeps no ownership
                elif res.get("release_on_terminal") is False:
                    continue
                elif res.get("acquire") is False or self.owners.get(name) == rid:
                    self.owners.pop(name, None)
        self._execution(leaf, status, code)
        return status

    def _execution(self, leaf: dict[str, Any], status: str, code: str | None) -> None:
        self.blackboard["sim.exec.action"] = leaf.get("action")
        self.blackboard["sim.exec.status"] = status.upper()
        self.blackboard["sim.exec.failure"] = code

    def resolve_params(self, leaf: dict[str, Any]) -> dict[str, Any]:
        contract = self.contracts.actions.get(leaf.get("action") or "")
        out: dict[str, Any] = {}
        specs = contract.parameters if contract else {}
        for name, spec in specs.items():
            if isinstance(spec, dict) and "default" in spec:
                out[name] = spec["default"]
        for name, value in (leaf.get("params") or {}).items():
            out[name] = self._operand(value) if isinstance(value, dict) else value
        return out

    # ------------------------------------------------------------------ expressions
    def eval(self, expr: Any) -> Any:
        if not isinstance(expr, dict) or len(expr) != 1:
            return False
        key, value = next(iter(expr.items()))
        if key == "all":
            return all(self.eval(v) for v in value)
        if key == "any":
            return any(self.eval(v) for v in value)
        if key == "not":
            return not self.eval(value)
        if key == "condition_ref":
            cond = self.model["conditions"].get(value)
            return bool(cond is not None and self.eval(cond))
        if key == "compare":
            return _compare(
                self._operand(value["left"]), value["operator"], self._operand(value["right"])
            )
        return bool(self._operand(expr))

    def _operand(self, op: Any) -> Any:
        if not isinstance(op, dict):
            return op
        if set(op) == {"value", "unit"}:
            dim = infer_dimension(str(op["unit"]))
            return to_canonical(float(op["value"]), str(op["unit"]), dim) if dim else op["value"]
        key, value = next(iter(op.items()))
        if key == "literal":
            return value
        if key == "parameter_ref":
            return self.params.get(value)
        if key == "bb":
            return self.blackboard.get(value)
        if key == "function_call":
            args = value.get("arguments") or {}
            if value["name"] == "time_in_state_gte":
                return self.time_in_state >= float(self._operand(args["duration"]))
            if value["name"] == "elapsed_since_gte":
                since = self.blackboard.get(args["field"])
                if not isinstance(since, int | float):
                    return False
                return self.now - since >= float(self._operand(args["duration"]))
        return None


def _compare(left: Any, op: str, right: Any) -> bool:
    if op == "eq":
        return left == right
    if op == "ne":
        return left != right
    if left is None or right is None:
        return False
    try:
        return {
            "lt": left < right,
            "lte": left <= right,
            "gt": left > right,
            "gte": left >= right,
        }[op]
    except TypeError:
        return False


def _default(spec: dict[str, Any], contracts: Contracts, path: str) -> Any:
    if spec.get("default") is not None:
        return spec["default"]
    fld = contracts.blackboard.get(path)
    if fld is not None and fld.default is not None:
        return fld.default
    if fld is not None and fld.nullable:
        return None
    return {"bool": False, "int": 0, "double": 0.0, "string": ""}.get(spec.get("type"), None)
