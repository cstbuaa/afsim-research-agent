"""L2 single-step tests and L3 scenarios on the IR 2.0 internal model.

L3 runs every role instance in the flat 2D world: the world advances by one step, each
instance evaluates its state machine and ticks its state tree (native semantics,
:mod:`afsim_research_agent.ir.v2.runtime`), and action leaves are executed by the
generic action models the capability set names. Actions without a model, and triggers
without semantics, are reported: they are idealized (actions succeed at once) or never
fire, so the result says how much of the run was simulated faithfully.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from afsim_research_agent.ir.v2.contracts import Contracts
from afsim_research_agent.ir.v2.from_tactic import event_id
from afsim_research_agent.ir.v2.runtime import FAILURE, RUNNING, SUCCESS, Instance
from afsim_research_agent.ir.v2.sim.actions import (
    MODELS,
    ActionContext,
    ActionModel,
    sync_vehicle,
)
from afsim_research_agent.ir.v2.sim.world import Geo, Hostile, Sensor, Vehicle, World


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------------ scenario
class SensorSpec(_Strict):
    name: str = "search_sensor"
    range_m: float = 5000.0
    exists: bool = True
    operational: bool = True


class VehicleSpec(_Strict):
    position_m: tuple[float, float] = (0.0, 0.0)
    heading_deg: float = 0.0
    speed_mps: float = 0.0
    max_speed_mps: float = 20.0
    mobility_available: bool = True
    sensor: SensorSpec | None = Field(default_factory=SensorSpec)


class HostileSpec(_Strict):
    id: str
    position_m: tuple[float, float]
    velocity_mps: tuple[float, float] = (0.0, 0.0)
    affiliation: Literal["ENEMY", "FRIENDLY", "UNKNOWN"] = "ENEMY"
    appear_s: float = 0.0
    remove_s: float | None = None


class Environment(_Strict):
    origin_deg: tuple[float, float] = (0.0, 0.0)
    duration_s: float = Field(gt=0)
    step_s: float = Field(default=1.0, gt=0)
    routes: dict[str, list[tuple[float, float]]] = Field(default_factory=dict)
    seed: int = 0


class RaiseEvent(_Strict):
    at_s: float
    event: str  # event id, or the tactic-set trigger text
    role: str | None = None
    instance: int | None = None


class Fault(_Strict):
    at_s: float
    role: str
    instance: int = 0
    sensor_operational: bool | None = None
    mobility_available: bool | None = None


class Expectation(_Strict):
    id: str
    role: str
    instance: int = 0
    state_sequence: list[str] | None = None  # visited states, consecutive duplicates removed
    visits_in_order: list[str] | None = None  # these states visited in this order (others may
    # come between)
    final_state: str | None = None
    reaches: str | None = None
    within_s: float | None = None  # counted from the start, or from after_event
    after_event: str | None = None  # the scenario event the limit counts from
    never: str | None = None


class Scenario(_Strict):
    id: str
    description: str = ""
    environment: Environment
    vehicles: dict[str, list[VehicleSpec]] = Field(default_factory=dict)
    hostiles: list[HostileSpec] = Field(default_factory=list)
    events: list[RaiseEvent] = Field(default_factory=list)
    faults: list[Fault] = Field(default_factory=list)
    expect: list[Expectation] = Field(default_factory=list)


@dataclass
class Check:
    id: str
    passed: bool
    detail: str


@dataclass
class SimResult:
    scenario: str
    checks: list[Check] = field(default_factory=list)
    states: dict[str, list[tuple[float, str]]] = field(default_factory=dict)
    # instance → transitions taken: {t, from, to, on}
    transitions: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    idealized: set[str] = field(default_factory=set)
    placeholders: set[str] = field(default_factory=set)
    unresolved: set[str] = field(default_factory=set)
    dropped: set[str] = field(default_factory=set)  # model writes outside the contract
    # (time, instance, action) whenever an action leaf starts; (time, event) of raised events
    activations: list[tuple[float, str, str]] = field(default_factory=list)
    events: list[tuple[float, str]] = field(default_factory=list)
    step_s: float = 1.0
    duration_s: float = 0.0
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and all(c.passed for c in self.checks)

    def render(self) -> str:
        lines = [f"scenario {self.scenario}: {'PASS' if self.passed else 'FAIL'}"]
        if self.error:
            lines.append(f"  error: {self.error}")
        for c in self.checks:
            lines.append(f"  [{'ok' if c.passed else 'FAIL'}] {c.id}: {c.detail}")
        for name, seq in self.states.items():
            path = " -> ".join(f"{s}@{t:g}" for t, s in seq)
            lines.append(f"  {name}: {path}")
        if self.idealized:
            lines.append(f"  idealized actions (no model): {sorted(self.idealized)}")
        if self.placeholders:
            lines.append(f"  idealized gaps (stub): {sorted(self.placeholders)}")
        if self.unresolved:
            lines.append(f"  triggers without semantics: {sorted(self.unresolved)}")
        if self.dropped:
            lines.append(f"  model writes outside the contracts (dropped): {sorted(self.dropped)}")
        return "\n".join(lines)


# ------------------------------------------------------------------------ host
class SimHost:
    """Executes action leaves with the generic models named by the capability set."""

    def __init__(self, world: World, contracts: Contracts) -> None:
        self.world = world
        self.c = contracts
        self.vehicles: dict[str, Vehicle] = {}
        self.shared: dict[str, dict[str, Any]] = {}
        self.models: dict[tuple[str, str], tuple[ActionModel, ActionContext]] = {}
        self.idealized: set[str] = set()
        self.placeholders: set[str] = set()  # gaps run by their stub
        self.dropped: set[str] = set()  # fields a model wrote but the contract does not
        self.activations: list[tuple[float, str, str]] = []

    def _context(self, inst: Instance, leaf: dict[str, Any], options: dict[str, Any]):
        contract = self.c.actions.get(leaf.get("action") or "")
        return ActionContext(
            world=self.world,
            vehicle=self.vehicles[inst.name],
            blackboard=inst.blackboard,
            fields=self.c.semantics.fields,
            params=inst.resolve_params(leaf),
            success=dict(contract.success) if contract else {},
            options=options,
            shared=self.shared.setdefault(inst.name, {}),
            allowed=contract.written if contract and contract.writes is not None else None,
            dropped=self.dropped,
        )

    def tick(self, inst: Instance, leaf: dict[str, Any], new: bool) -> tuple[str, str | None]:
        action = str(leaf.get("action"))
        if "gap" in leaf:
            self.placeholders.add(leaf["gap"]["id"])
            return inst.gap_stub(leaf)
        key = (inst.name, leaf["node_id"])
        if new or key not in self.models:
            self.activations.append((self.world.now, inst.name, action))
        spec = self.c.semantics.actions.get(action)
        if spec is None or spec.get("model") not in MODELS:
            self.idealized.add(action)
            self.models.setdefault(key, None)  # type: ignore[arg-type]
            return SUCCESS, None
        if new or self.models.get(key) is None:
            options = {k: v for k, v in spec.items() if k != "model"}
            model = MODELS[spec["model"]]()
            ctx = self._context(inst, leaf, options)
            self.models[key] = (model, ctx)
            contract = self.c.actions.get(action)
            for w in (contract.writes or ()) if contract else ():
                if "reset_on_start" in w:  # the step first marks its fields "not there yet"
                    inst.blackboard[str(w["field"])] = w["reset_on_start"]
            status, code = model.start(ctx)
        else:
            model, ctx = self.models[key]
            status, code = model.update(ctx)
        self._execution(ctx, action, status, code, new)
        return status, code

    def cancel(self, inst: Instance, leaf: dict[str, Any]) -> None:
        key = (inst.name, leaf["node_id"])
        if self.models.get(key) is not None:
            model, ctx = self.models.pop(key)
            model.cancel(ctx)
            self._execution(ctx, str(leaf.get("action")), "cancelled", "CANCELLED", False)

    def _execution(self, ctx: ActionContext, action: str, status: str, code, new: bool):
        ctx.write("exec.action", action)
        ctx.write("exec.status", status.upper())
        ctx.write("exec.failure", code)
        ctx.write("exec.updated", ctx.now)
        if new:
            ctx.write("exec.started", ctx.now)
        if status not in (RUNNING,):
            ctx.write("exec.completed", ctx.now)


# ------------------------------------------------------------------------ L3
def simulate(model: dict[str, Any], contracts: Contracts, raw: dict[str, Any]) -> SimResult:
    try:
        sc = Scenario.model_validate(raw)
    except ValidationError as exc:
        return SimResult(str(raw.get("id", "?")), error=f"scenario structure: {exc.errors()[0]}")
    result = SimResult(sc.id)
    env = sc.environment
    world = World(geo=Geo(*env.origin_deg), routes={k: list(v) for k, v in env.routes.items()})
    for h in sc.hostiles:
        world.hostiles.append(
            Hostile(
                h.id,
                *h.position_m,
                affiliation=h.affiliation,
                vx=h.velocity_mps[0],
                vy=h.velocity_mps[1],
                appear_s=h.appear_s,
                remove_s=h.remove_s,
            )
        )
    host = SimHost(world, contracts)
    instances: list[Instance] = []
    for rid, role in model["roles"].items():
        specs = sc.vehicles.get(rid) or []
        for i in range(int(role["role"].get("count", 1))):
            vs = specs[i] if i < len(specs) else VehicleSpec()
            name = f"{rid}_{i}"
            sensor = Sensor(**vs.sensor.model_dump()) if vs.sensor else None
            world.vehicles[name] = Vehicle(
                name,
                *vs.position_m,
                heading=vs.heading_deg,
                speed=vs.speed_mps,
                max_speed=vs.max_speed_mps,
                mobility_available=vs.mobility_available,
                sensor=sensor,
            )
            host.vehicles[name] = world.vehicles[name]
            inst = Instance(name, rid, i, model, contracts, host, seed=env.seed + i)
            inst.enter(inst.state, 0.0)
            instances.append(inst)
            result.states[name] = [(0.0, inst.state)]
    for rid in sc.vehicles:
        if rid not in model["roles"]:
            result.error = f"scenario places vehicles for unknown role '{rid}'"
            return result
    pending = sorted(sc.events, key=lambda e: e.at_s)
    faults = sorted(sc.faults, key=lambda f: f.at_s)
    steps = int(round(env.duration_s / env.step_s))
    for _ in range(steps + 1):
        now = world.now
        while pending and pending[0].at_s <= now + 1e-9:
            ev = pending.pop(0)
            result.events.append((now, ev.event))
            for inst in instances:
                if (ev.role in (None, inst.role)) and (ev.instance in (None, inst.index)):
                    inst.raise_event(_event_name(inst, ev.event))
        while faults and faults[0].at_s <= now + 1e-9:
            f = faults.pop(0)
            v = world.vehicles.get(f"{f.role}_{f.instance}")
            if v is not None:
                if f.mobility_available is not None:
                    v.mobility_available = f.mobility_available
                if f.sensor_operational is not None and v.sensor is not None:
                    v.sensor.operational = f.sensor_operational
        for inst in instances:
            sync_vehicle(
                world, host.vehicles[inst.name], inst.blackboard, contracts.semantics.fields
            )
            hops = len(inst.trace)
            inst.step(now)
            # every state entered in this update, as AFSIM writes one line per state entry
            for t in inst.trace[hops:]:
                result.states[inst.name].append((now, t["to"]))
        world.step(env.step_s)
    for inst in instances:
        result.unresolved |= inst.unresolved
        result.transitions[inst.name] = list(inst.trace)
    result.idealized = set(host.idealized)
    result.placeholders = set(host.placeholders)
    result.activations = list(host.activations)
    result.step_s = env.step_s
    result.duration_s = env.duration_s
    result.dropped = set(host.dropped)
    result.checks = [_check(e, result) for e in sc.expect]
    return result


def _event_name(inst: Instance, name: str) -> str:
    """A scenario names an event by id or by its tactic-set text."""
    if name in inst.events:
        return name
    slug = event_id(name)
    return slug if slug in inst.events else name


def _in_order(visited: list[str], wanted: list[str]) -> bool:
    """``wanted`` is a subsequence of ``visited``."""
    it = iter(visited)
    return all(any(v == w for v in it) for w in wanted)


def _check(e: Expectation, result: SimResult) -> Check:
    name = f"{e.role}_{e.instance}"
    seq = result.states.get(name)
    if seq is None:
        return Check(e.id, False, f"no instance {name}")
    visited = [s for _, s in seq]
    problems = []
    if e.state_sequence is not None and visited != e.state_sequence:
        hint = (
            " (state_sequence is the whole visit sequence; to check only that these states "
            "come in this order write visits_in_order)"
            if _in_order(visited, e.state_sequence)
            else ""
        )
        problems.append(f"visited {visited}, expected {e.state_sequence}{hint}")
    if e.visits_in_order is not None and not _in_order(visited, e.visits_in_order):
        problems.append(f"visited {visited}, expected in this order {e.visits_in_order}")
    if e.final_state is not None and visited[-1] != e.final_state:
        problems.append(f"ended in {visited[-1]}, expected {e.final_state}")
    if e.reaches is not None:
        start, since = 0.0, ""
        if e.after_event is not None:
            raised = [
                t
                for t, ev in result.events
                if ev == e.after_event or event_id(ev) == event_id(e.after_event)
            ]
            if not raised:
                problems.append(f"after_event {e.after_event} is not raised in this scenario")
            start, since = (raised[0] if raised else 0.0), f" after {e.after_event}"
        times = [t for t, s in seq if s == e.reaches and t >= start]
        if not times:
            problems.append(f"never reached {e.reaches}{since}")
        elif e.within_s is not None and times[0] - start > e.within_s:
            if e.after_event is None:
                problems.append(
                    f"reached {e.reaches} at {times[0]:g}s, limit {e.within_s:g}s "
                    "(within_s counts from the start of the scenario; to count from an "
                    "event write after_event: <event id>)"
                )
            else:
                problems.append(
                    f"reached {e.reaches} {times[0] - start:g}s{since}, limit {e.within_s:g}s"
                )
    if e.never is not None and e.never in visited:
        problems.append(f"entered {e.never}")
    return Check(e.id, not problems, "; ".join(problems) or "met")


# ------------------------------------------------------------------------ L2
class Given(_Strict):
    role: str
    instance: int = 0
    state: str
    time_in_state_s: float = 0.0
    blackboard: dict[str, Any] = Field(default_factory=dict)
    events: list[str] = Field(default_factory=list)
    tree_status: Literal["success", "failure", "running"] | None = None
    actions: dict[str, Literal["success", "failure", "running"]] = Field(default_factory=dict)


class Expect(_Strict):
    next_state: str | None = None
    tree_status: Literal["success", "failure", "running"] | None = None
    issued_actions: list[str] | None = None


class TestCase(_Strict):
    __test__ = False

    id: str
    description: str = ""
    given: Given
    expect: Expect


class _StubHost:
    def __init__(self, outcomes: dict[str, str]) -> None:
        self.outcomes = outcomes

    def tick(self, inst: Instance, leaf: dict[str, Any], new: bool) -> tuple[str, str | None]:
        action = str(leaf.get("action"))
        if action not in self.outcomes and "gap" in leaf:
            return inst.gap_stub(leaf)
        status = self.outcomes.get(action, SUCCESS)
        return status, ("STUB_FAILURE" if status == FAILURE else None)

    def cancel(self, inst: Instance, leaf: dict[str, Any]) -> None:
        return None


def run_l2(model: dict[str, Any], contracts: Contracts, tests: list[dict[str, Any]]) -> list[Check]:
    checks = []
    for raw in tests:
        try:
            case = TestCase.model_validate(raw)
        except ValidationError as exc:
            checks.append(Check(str(raw.get("id", "?")), False, f"structure: {exc.errors()[0]}"))
            continue
        g = case.given
        if g.role not in model["roles"]:
            checks.append(Check(case.id, False, f"unknown role '{g.role}'"))
            continue
        inst = Instance(
            f"{g.role}_{g.instance}", g.role, g.instance, model, contracts, _StubHost(g.actions)
        )
        if g.state not in inst.mission["states"]:
            checks.append(Check(case.id, False, f"unknown state '{g.state}'"))
            continue
        inst.enter(g.state, -g.time_in_state_s)
        inst.tree_status = g.tree_status
        inst.blackboard.update(g.blackboard)
        for name in g.events:
            ev = _event_name(inst, name)
            spec = inst.events.get(ev) or {}
            if spec.get("source") == "tree":
                # "the tree ended that way": the state's tree is given that result
                inst.force_tree = str(spec.get("result") or "success")
            elif spec.get("source") == "timer":
                # "the timer ran out": the state has been entered at least that long ago
                inst.entered_at = min(inst.entered_at, -float(spec.get("after_s") or 0.0))
            else:
                inst.raise_event(ev)
        inst.step(0.0)
        problems = []
        # the state the given one leaves for; further transitions in the same tick follow
        # from fields the test did not give (they keep their defaults), so the first one
        # counts as "next" — the final one is accepted too
        hops = [t["to"] for t in inst.trace]
        first = hops[0] if hops else inst.state
        expected = case.expect.next_state
        if expected is not None and expected not in (first, inst.state):
            chain = " -> ".join([g.state, *hops]) if hops else g.state
            problems.append(f"state {first} (this tick: {chain}), expected {expected}")
        if case.expect.tree_status is not None and inst.tree_status != case.expect.tree_status:
            problems.append(f"tree {inst.tree_status}, expected {case.expect.tree_status}")
        if case.expect.issued_actions is not None and inst.issued != case.expect.issued_actions:
            problems.append(f"issued {inst.issued}, expected {case.expect.issued_actions}")
        checks.append(Check(case.id, not problems, "; ".join(problems) or "met"))
    return checks
