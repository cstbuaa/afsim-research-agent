"""Reference interpreter for normalized IR: state machine + behavior trees.

Used by L2 tests, the built-in 2D simulation and the ``py_trees_sim`` backend,
so all three share one set of semantics:

* Each tick: deliver inbox -> evaluate transitions (priority order) -> tick the
  tree of the (possibly new) state.
* Changing state resets all tree memory and halts active behaviors.
* A behavior activation starts on the first tick it is reached while inactive;
  its precondition is checked only then. Actions are issued before success /
  failure are evaluated. A behavior that is not ticked in a tick is halted.
* ``message_received`` is true only on the tick a message is delivered;
  ``message_value`` keeps the last received value (or the field default).
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

SUCCESS, FAILURE, RUNNING = "success", "failure", "running"


class World(Protocol):
    """Read-only view of the platform, its components and its surroundings."""

    def property(self, source: str, prop: str, spec: Mapping[str, Any]) -> Any: ...


@dataclass
class Delivery:
    message: str
    fields: dict[str, Any]
    sender_role: str = ""
    sender_instance: int = 0


@dataclass
class IssuedAction:
    command: str
    arguments: dict[str, Any]
    behavior: str
    node_id: str
    external: bool


@dataclass
class OutgoingMessage:
    message: str
    fields: dict[str, Any]


@dataclass
class TickResult:
    previous_state: str | None
    next_state: str | None
    fired_transition: str | None
    tree_status: str | None
    selected_path: list[str]
    ticked_nodes: list[str]
    issued_actions: list[IssuedAction]
    sent_messages: list[OutgoingMessage]
    log: list[str] = field(default_factory=list)


@dataclass
class _Activation:
    active: bool = False
    start: float = 0.0
    issued: set[int] = field(default_factory=set)
    memory: dict[str, Any] = field(default_factory=dict)


class EvaluationError(RuntimeError):
    pass


_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
}


class RoleController:
    """One controlled platform instance of a role."""

    def __init__(
        self,
        ir: Mapping[str, Any],
        role_id: str,
        instance: int = 0,
        *,
        parameter_overrides: Mapping[str, Any] | None = None,
        seed: int | None = None,
    ) -> None:
        self.ir = ir
        self.role = next(r for r in ir["roles"] if r["id"] == role_id)
        self.role_id = role_id
        self.instance = instance
        self.params = {name: p["value"] for name, p in ir["parameters"].items()}
        self.params.update(parameter_overrides or {})
        self.conditions = {c["id"]: c for c in ir["conditions"]}
        self.behaviors = {b["id"]: b for b in ir["behaviors"]}
        self.trees = {t["id"]: t for t in ir["trees"]}
        self.messages = {m["id"]: m for m in ir["messages"]}
        self.coordination = {c["id"]: c for c in ir["coordination"]}
        self.blackboard: dict[str, dict[str, Any]] = {
            board["id"]: {v["name"]: v["default"] for v in board["variables"]}
            for board in self.role["blackboards"]
        }
        self._bb_defaults = {b: dict(v) for b, v in self.blackboard.items()}
        machine = self.role["state_machine"]
        self.states = {s["id"]: s for s in machine["states"]} if machine else {}
        self.state: str | None = machine["initial_state"] if machine else None
        self.state_entry = 0.0
        self.now = 0.0
        self.activations: dict[str, _Activation] = {}
        self.lifetime_issued: set[tuple[str, int]] = set()
        self.node_memory: dict[str, dict[str, Any]] = {}
        self.inbox_ids: set[str] = set()
        self.last_values: dict[str, dict[str, Any]] = {}
        self.bounding_completions: dict[str, dict[tuple[str, int], int]] = {}
        self.coordination_overrides: dict[str, dict[str, Any]] = {}
        seed_param = ir["execution"].get("random_seed_parameter")
        base = (
            seed if seed is not None else int(self.params.get(seed_param) or 0) if seed_param else 0
        )
        self.rng = random.Random(base * 1009 + instance)
        self._ticked: set[str] = set()
        self._world: World | None = None
        self.heartbeats = self._heartbeats()
        self._result: TickResult | None = None

    # ------------------------------------------------------------------ tick
    def tick(self, now: float, world: World, inbox: list[Delivery] | None = None) -> TickResult:
        self.now = now
        self._world = world
        self.inbox_ids = set()
        for item in inbox or []:
            self.inbox_ids.add(item.message)
            values = self.last_values.setdefault(item.message, {})
            values.update(item.fields)
            self._observe_coordination_message(item)
        previous = self.state
        result = TickResult(previous, previous, None, None, [], [], [], [])
        self._result = result
        if self.state is not None:
            state = self.states[self.state]
            if not state["terminal"]:
                for tr in state["transitions"]:
                    if self.eval(tr["when"]):
                        result.fired_transition = tr["transition_id"]
                        if tr["to"] != self.state:
                            self._enter(tr["to"])
                            if tr.get("synchronize_with"):
                                msg = f"{tr['synchronize_with']}__transition"
                                result.sent_messages.append(
                                    OutgoingMessage(msg, {"to_state": tr["to"]})
                                )
                        break
            result.next_state = self.state
            state = self.states[self.state]
            tree_id = None if state["terminal"] else state["tree"]
        else:
            tree_id = self.role["root_tree"]
        self._ticked = set()
        if tree_id:
            status, path = self._tick_node(self.trees[tree_id]["root"])
            result.tree_status = status
            result.selected_path = path
        for node_id, act in self.activations.items():
            if act.active and node_id not in self._ticked:
                act.active = False
        result.ticked_nodes = sorted(self._ticked)
        self._heartbeat(result)
        self._world = None
        return result

    # ------------------------------------------------------------------ heartbeats
    def _heartbeats(self) -> list[dict[str, Any]]:
        """Synchronizing patterns this role initiates (design §30.5: resync)."""
        out: list[dict[str, Any]] = []
        for coord in self.coordination.values():
            initiator = coord.get("leader") if coord["pattern"] == "leader_follower" else None
            if coord["pattern"] == "synchronized_transition":
                initiator = coord.get("initiator")
            if initiator != self.role_id or float(coord.get("resync_interval_s", 0.0)) <= 0:
                continue
            targets = {
                tr["to"]
                for state in self.states.values()
                for tr in state["transitions"]
                if tr.get("synchronize_with") == coord["id"]
            }
            formation_msg = (
                f"{coord['id']}__formation" if coord["pattern"] == "leader_follower" else None
            )
            out.append(
                {
                    "id": coord["id"],
                    "interval": float(coord["resync_interval_s"]),
                    "targets": targets,
                    "transition_msg": f"{coord['id']}__transition",
                    "formation_msg": formation_msg if formation_msg in self.messages else None,
                    "formation_ref": coord.get("formation_ref"),
                    "last_transition": None,
                    "last_formation": None,
                }
            )
        return out

    def _heartbeat(self, result: TickResult) -> None:
        """Re-broadcast the current synchronized state and formation so a lost
        message is repaired at the next interval instead of never."""
        sent = {m.message for m in result.sent_messages}
        for hb in self.heartbeats:
            due = hb["interval"] - 1e-9
            if hb["transition_msg"] in sent:
                hb["last_transition"] = self.now
            elif self.state in hb["targets"]:
                if hb["last_transition"] is None:
                    hb["last_transition"] = self.now
                elif self.now - hb["last_transition"] >= due:
                    result.sent_messages.append(
                        OutgoingMessage(hb["transition_msg"], {"to_state": self.state})
                    )
                    hb["last_transition"] = self.now
            fmsg = hb["formation_msg"]
            if fmsg is None:
                continue
            if fmsg in sent:
                hb["last_formation"] = self.now
            elif hb["last_formation"] is None:
                hb["last_formation"] = self.now
            elif self.now - hb["last_formation"] >= due and hb["formation_ref"] is not None:
                msg = self.messages[fmsg]
                fields = {f["name"]: f["default"] for f in msg["fields"]}
                fields["formation"] = self.value(hb["formation_ref"])
                result.sent_messages.append(OutgoingMessage(fmsg, fields))
                hb["last_formation"] = self.now

    def _enter(self, state: str) -> None:
        self.state = state
        self.state_entry = self.now
        self.node_memory.clear()
        for act in self.activations.values():
            act.active = False

    # ------------------------------------------------------------------ nodes
    def _tick_node(self, node: Mapping[str, Any]) -> tuple[str, list[str]]:
        node_id = node["node_id"]
        self._ticked.add(node_id)
        kind = node["type"]
        handler = getattr(self, f"_node_{kind}")
        status, sub = handler(node)
        return status, [node_id, *sub]

    def _node_condition(self, node: Mapping[str, Any]) -> tuple[str, list[str]]:
        return (SUCCESS if self.condition(node["condition"]) else FAILURE), []

    def _node_subtree(self, node: Mapping[str, Any]) -> tuple[str, list[str]]:
        return self._tick_node(self.trees[node["tree"]]["root"])

    def _node_sequence(self, node: Mapping[str, Any]) -> tuple[str, list[str]]:
        path: list[str] = []
        for child in node["children"]:
            status, path = self._tick_node(child)
            if status != SUCCESS:
                return status, path
        return SUCCESS, path

    def _node_sequence_with_memory(self, node: Mapping[str, Any]) -> tuple[str, list[str]]:
        mem = self.node_memory.setdefault(node["node_id"], {"index": 0})
        path: list[str] = []
        children = node["children"]
        while mem["index"] < len(children):
            status, path = self._tick_node(children[mem["index"]])
            if status == RUNNING:
                return RUNNING, path
            if status == FAILURE:
                mem["index"] = 0
                return FAILURE, path
            mem["index"] += 1
        mem["index"] = 0
        return SUCCESS, path

    def _node_selector(self, node: Mapping[str, Any]) -> tuple[str, list[str]]:
        return self._select(node, list(node["children"]))

    def _select(
        self, node: Mapping[str, Any], children: list[Mapping[str, Any]]
    ) -> tuple[str, list[str]]:
        path: list[str] = []
        for child in children:
            status, path = self._tick_node(child)
            if status != FAILURE:
                return status, path
        return FAILURE, path

    def _node_selector_with_memory(self, node: Mapping[str, Any]) -> tuple[str, list[str]]:
        mem = self.node_memory.setdefault(node["node_id"], {"index": 0})
        children = node["children"]
        path: list[str] = []
        for index in range(mem["index"], len(children)):
            status, path = self._tick_node(children[index])
            if status == RUNNING:
                mem["index"] = index
                return RUNNING, path
            if status == SUCCESS:
                mem["index"] = 0
                return SUCCESS, path
        mem["index"] = 0
        return FAILURE, path

    def _node_priority_selector(self, node: Mapping[str, Any]) -> tuple[str, list[str]]:
        scored = [
            (-float(self.value(child["fitness"]) or 0.0), index, child)
            for index, child in enumerate(node["children"])
        ]
        return self._select(node, [child for _, _, child in sorted(scored, key=lambda t: t[:2])])

    def _node_weighted_random(self, node: Mapping[str, Any]) -> tuple[str, list[str]]:
        mem = self.node_memory.setdefault(node["node_id"], {"running": None})
        children = node["children"]
        if mem["running"] is None:
            weights = [max(0.0, float(self.value(c["weight"]) or 0.0)) for c in children]
            if sum(weights) <= 0:
                return FAILURE, []
            mem["running"] = self.rng.choices(range(len(children)), weights=weights)[0]
        status, path = self._tick_node(children[mem["running"]])
        if status != RUNNING:
            mem["running"] = None
        return status, path

    def _node_parallel(self, node: Mapping[str, Any]) -> tuple[str, list[str]]:
        mem = self.node_memory.setdefault(node["node_id"], {"done": {}})
        children = node["children"]
        n = len(children)
        for index, child in enumerate(children):
            if mem["done"].get(index) == SUCCESS:
                continue
            status, _ = self._tick_node(child)
            if status != RUNNING:
                mem["done"][index] = status
        successes = sum(1 for s in mem["done"].values() if s == SUCCESS)
        failures = sum(1 for s in mem["done"].values() if s == FAILURE)
        fp, sp = node["failure_policy"], node["success_policy"]
        need_fail = {"fail_on_one": 1, "fail_on_all": n}.get(fp["type"], fp.get("count") or n)
        need_ok = {"all": n, "one": 1}.get(sp["type"], sp.get("count") or n)
        if failures >= need_fail:
            mem["done"] = {}
            return FAILURE, []
        if successes >= need_ok:
            mem["done"] = {}
            return SUCCESS, []
        if failures:  # failed children restart next tick
            mem["done"] = {k: v for k, v in mem["done"].items() if v == SUCCESS}
        return RUNNING, []

    def _node_decorator(self, node: Mapping[str, Any]) -> tuple[str, list[str]]:
        kind = node["decorator"]
        child = node["children"][0]
        mem = self.node_memory.setdefault(node["node_id"], {"count": 0, "start": None})
        if kind == "repeat_for_duration" and mem["start"] is None:
            mem["start"] = self.now
        status, path = self._tick_node(child)
        if kind in ("inverter", "negator"):
            return {SUCCESS: FAILURE, FAILURE: SUCCESS}.get(status, status), path
        if kind == "succeeder":
            return (SUCCESS if status == FAILURE else status), path
        if kind == "repeat_count":
            if status == FAILURE:
                mem["count"] = 0
                return FAILURE, path
            if status == SUCCESS:
                mem["count"] += 1
                if mem["count"] >= node["count"]:
                    mem["count"] = 0
                    return SUCCESS, path
            return RUNNING, path
        if kind == "repeat_until_done":
            done = status == SUCCESS
            if node.get("until") is not None:
                done = self.eval(node["until"])
            if done:
                mem["count"] = 0
                return SUCCESS, path
            if status != RUNNING:
                mem["count"] += 1
                if node.get("count") and mem["count"] >= node["count"]:
                    mem["count"] = 0
                    return FAILURE, path
            return RUNNING, path
        if kind == "repeat_for_duration":
            if status == FAILURE:
                mem["start"] = None
                return FAILURE, path
            if self.now - mem["start"] >= node["duration_s"]:
                mem["start"] = None
                return SUCCESS, path
            return RUNNING, path
        raise EvaluationError(f"unknown decorator {kind}")

    def _node_behavior(self, node: Mapping[str, Any]) -> tuple[str, list[str]]:
        behavior = self.behaviors[node["behavior"]]
        act = self.activations.setdefault(node["node_id"], _Activation())
        if not act.active:
            pre = behavior.get("precondition")
            if pre is not None and not self.eval(pre):
                return FAILURE, []
            act.active = True
            act.start = self.now
            act.issued = set()
            act.memory = {m["name"]: m["default"] for m in behavior["memory"]}
        self._current_memory = act.memory
        assert self._result is not None
        for index, action in enumerate(behavior["actions"]):
            policy = action["issue_policy"]
            key = (behavior["id"], index)
            if policy == "once_per_activation" and index in act.issued:
                continue
            if policy == "once_per_model_lifetime" and key in self.lifetime_issued:
                continue
            act.issued.add(index)
            self.lifetime_issued.add(key)
            self._issue(behavior, node["node_id"], action)
        elapsed = self.now - act.start
        status = RUNNING
        if behavior.get("failure") is not None and self.eval(behavior["failure"]):
            status = FAILURE
        elif behavior.get("success") is not None and self.eval(behavior["success"]):
            status = SUCCESS
        else:
            waits = [
                self.value(a["arguments"]["duration"])
                for a in behavior["actions"]
                if a["command"] == "wait"
            ]
            if behavior.get("success") is None and waits and elapsed >= max(waits):
                status = SUCCESS
            timeout = behavior.get("timeout")
            if status == RUNNING and timeout is not None and elapsed >= timeout["seconds"]:
                status = timeout["result"]
        if status != RUNNING:
            act.active = False
        self._current_memory = None
        return status, []

    _current_memory: dict[str, Any] | None = None

    def _issue(self, behavior: Mapping[str, Any], node_id: str, action: Mapping[str, Any]) -> None:
        assert self._result is not None
        command = action["command"]
        args = action["arguments"]
        if command == "set_blackboard":
            key = args["key"]
            self.blackboard[key["board"]][key["var"]] = self.value(args["value"])
            return
        if command == "delete_blackboard_value":
            key = args["key"]
            self.blackboard[key["board"]][key["var"]] = self._bb_defaults[key["board"]][key["var"]]
            return
        evaluated = {name: self.value(arg) for name, arg in args.items()}
        if command == "send_message":
            msg = self.messages[evaluated["message"]]
            fields = {f["name"]: f["default"] for f in msg["fields"]}
            fields.update(evaluated.get("fields") or {})
            self._result.sent_messages.append(OutgoingMessage(msg["id"], fields))
            self._record_own_send(msg["id"])
        elif command == "report_contact":
            msg = self.messages[evaluated["message"]]
            fields = {f["name"]: f["default"] for f in msg["fields"]}
            names = {f["name"] for f in msg["fields"]}
            if "contact_range" in names:
                rng = self._prop("track_value", "nearest_hostile_range", {})
                fields["contact_range"] = (
                    float(rng) if rng is not None and math.isfinite(rng) else 0.0
                )
            if "contact_count" in names:
                fields["contact_count"] = int(self._prop("track_value", "hostile_count", {}) or 0)
            self._result.sent_messages.append(OutgoingMessage(msg["id"], fields))
        if action["external"]:
            self._result.issued_actions.append(
                IssuedAction(command, evaluated, behavior["id"], node_id, True)
            )
            apply = getattr(self._world, "apply", None)
            if apply is not None:
                # The world sees the command immediately, so completion flags such
                # as mover.at_destination refer to the new command in this tick.
                apply(command, evaluated)

    # ------------------------------------------------------------------ eval
    def condition(self, cond_id: str) -> bool:
        return self.eval(self.conditions[cond_id]["expression"])

    def eval(self, expr: Mapping[str, Any]) -> bool:
        op = expr["op"]
        if op == "all":
            return all(self.eval(item) for item in expr["args"])
        if op == "any":
            return any(self.eval(item) for item in expr["args"])
        if op == "not":
            return not self.eval(expr["arg"])
        if op == "condition_ref":
            return self.condition(expr["id"])
        if op == "literal":
            return bool(expr["value"])
        if op == "compare":
            left, right = self.value(expr["left"]), self.value(expr["right"])
            if left is None or right is None:
                return False
            return _OPS[expr["operator"]](left, right)
        if op == "operand":
            return bool(self.value(expr["operand"]))
        raise EvaluationError(f"unknown expression op {op}")

    def value(self, operand: Mapping[str, Any]) -> Any:
        kind = operand["kind"]
        if kind in ("literal", "id"):
            return operand["value"]
        if kind == "quantity":
            return operand["value"]
        if kind == "parameter":
            return self.params.get(operand["name"])
        if kind == "blackboard":
            return self.blackboard[operand["board"]][operand["var"]]
        if kind == "memory":
            return (self._current_memory or {}).get(operand["name"])
        if kind == "message_value":
            values = self.last_values.get(operand["message"], {})
            if operand["field"] in values:
                return values[operand["field"]]
            msg = self.messages[operand["message"]]
            return next(f["default"] for f in msg["fields"] if f["name"] == operand["field"])
        if kind == "fields":
            return {name: self.value(v) for name, v in operand["values"].items()}
        if kind == "function":
            return self._function(operand["name"], operand["arguments"])
        if kind == "property":
            spec = {k: v for k, v in operand.items() if k not in ("kind", "source", "property")}
            return self._prop(operand["source"], operand["property"], spec)
        if kind == "blackboard_key":
            return f"{operand['board']}.{operand['var']}"
        raise EvaluationError(f"unknown operand kind {kind}")

    def _prop(self, source: str, prop: str, spec: Mapping[str, Any]) -> Any:
        if source == "self_value" and prop == "instance_index":
            return self.instance
        assert self._world is not None
        return self._world.property(source, prop, spec)

    def _function(self, name: str, args: Mapping[str, Any]) -> Any:
        if name == "message_received":
            return args["message"]["value"] in self.inbox_ids
        if name == "time_in_state_gte":
            return self.now - self.state_entry >= float(self.value(args["duration"]) or 0.0)
        if name == "range_to_current_target_lte":
            rng = self._prop("track_value", "current_target_range", {})
            limit = self.value(args["range"])
            return rng is not None and limit is not None and rng <= limit
        if name == "formation_is":
            return self._prop("formation_value", "current_id", {}) == args["formation"]["value"]
        if name == "bounding_turn":
            return self.bounding_turn(args["coordination"]["value"])
        if name == "standoff_reached":
            coord = self.coordination.get(args["coordination"]["value"], {})
            standoff = coord.get("standoff_distance")
            if not standoff:
                return False
            rng = self._prop("track_value", "nearest_hostile_range", {})
            margin = float(coord.get("standoff_margin_m", 0.0))
            return rng is not None and rng <= standoff["value_m"] + margin
        if name == "in_group":
            return self.group_of(args["coordination"]["value"]) == args["group"]["value"]
        raise EvaluationError(f"unknown function {name}")

    # ------------------------------------------------------------------ coordination
    def _members(self, coord_id: str, group: str) -> list[tuple[str, int]]:
        coord = self.coordination[coord_id]
        out: list[tuple[str, int]] = []
        roles = {r["id"]: r for r in self.ir["roles"]}
        for item in coord["members"].get(group, []):
            if item["instance"] is None:
                out += [(item["role"], i) for i in range(roles[item["role"]]["count"])]
            else:
                out.append((item["role"], item["instance"]))
        return out

    def group_of(self, coord_id: str) -> str | None:
        me = (self.role_id, self.instance)
        for group in self.coordination[coord_id]["members"]:
            if me in self._members(coord_id, group):
                return group
        return None

    def bounding_phase(self, coord_id: str) -> int:
        override = self.coordination_overrides.get(coord_id, {})
        if "phase" in override:
            return int(override["phase"])
        done = self.bounding_completions.get(coord_id, {})
        groups = [self._members(coord_id, "bounding"), self._members(coord_id, "overwatch")]
        phase = 0
        for _ in range(10_000):
            need = phase // 2 + 1
            if all(done.get(m, 0) >= need for m in groups[phase % 2]):
                phase += 1
            else:
                break
        return phase

    def bounding_turn(self, coord_id: str) -> bool:
        if coord_id not in self.coordination:
            return False
        phase = self.bounding_phase(coord_id)
        moving = ["bounding", "overwatch"][phase % 2]
        if self.group_of(coord_id) != moving:
            return False
        override = self.coordination_overrides.get(coord_id, {})
        if "completed" in override:
            return not bool(override["completed"])
        done = self.bounding_completions.get(coord_id, {}).get((self.role_id, self.instance), 0)
        return done < phase // 2 + 1

    def _bounding_coord_for(self, message_id: str) -> str | None:
        for coord in self.coordination.values():
            if (
                coord["pattern"] == "bounding_overwatch"
                and message_id == f"{coord['id']}__bound_complete"
            ):
                return coord["id"]
        return None

    def _record_own_send(self, message_id: str) -> None:
        coord = self._bounding_coord_for(message_id)
        if coord is not None:
            done = self.bounding_completions.setdefault(coord, {})
            me = (self.role_id, self.instance)
            done[me] = done.get(me, 0) + 1

    def _observe_coordination_message(self, item: Delivery) -> None:
        coord = self._bounding_coord_for(item.message)
        if coord is not None:
            done = self.bounding_completions.setdefault(coord, {})
            sender = (item.sender_role, item.sender_instance)
            done[sender] = done.get(sender, 0) + 1


class DictWorld:
    """World view backed by nested dictionaries with catalog defaults (used by L2)."""

    def __init__(
        self, data: Mapping[str, Mapping[str, Any]], defaults: Mapping[str, Mapping[str, Any]]
    ):
        self.data = data
        self.defaults = defaults

    def property(self, source: str, prop: str, spec: Mapping[str, Any]) -> Any:
        values = self.data.get(source, {})
        keys = []
        component = spec.get("component")
        argument = spec.get("argument")
        if component:
            keys.append(f"{component}.{prop}")
        if argument:
            keys.append(f"{prop}.{argument}")
        if spec.get("formation"):
            keys.append(f"{prop}.{spec['formation']}")
        keys.append(prop)
        for key in keys:
            if key in values:
                return values[key]
        return self.defaults.get(source, {}).get(prop)
