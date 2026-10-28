"""Expansion of catalog coordination patterns into explicit IR elements (design §11, §23).

Every generated element gets an ID ``<coordination_id>__<suffix>``; user IDs may
not contain ``__`` so generated IDs never collide with authored ones.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from afsim_research_agent.ir.catalog import Catalog
from afsim_research_agent.ir.diagnostics import Collector
from afsim_research_agent.ir.models import (
    ActionCall,
    Behavior,
    BehaviorSpec,
    Condition,
    Coordination,
    DistanceRef,
    Message,
    MessageField,
    Outcome,
    Role,
    Timeout,
    Transition,
    Tree,
    TreeNode,
)

_INSTANCE = re.compile(r"^([a-z][a-z0-9_]*)(?:\[(\d+)\])?$")
SEP = "__"
DEFAULT_SYNC_PRIORITY = 90


def parse_instance(text: str) -> tuple[str, int | None] | None:
    match = _INSTANCE.match(text.strip())
    if not match:
        return None
    index = match.group(2)
    return match.group(1), (int(index) if index is not None else None)


def gid(coordination_id: str, suffix: str) -> str:
    return f"{coordination_id}{SEP}{suffix}"


@dataclass
class Expansion:
    messages: list[Message] = field(default_factory=list)
    conditions: list[Condition] = field(default_factory=list)
    behaviors: list[Behavior] = field(default_factory=list)
    trees: list[Tree] = field(default_factory=list)
    transitions: dict[tuple[str, str], list[Transition]] = field(default_factory=dict)
    generated_from: dict[str, str] = field(default_factory=dict)
    members: dict[str, dict[str, list[tuple[str, int | None]]]] = field(default_factory=dict)

    def _mark(self, coordination_id: str, *ids: str) -> None:
        for item in ids:
            self.generated_from[item] = coordination_id


def distance_operand(ref: DistanceRef) -> dict[str, Any]:
    if ref.parameter_ref is not None:
        return {"parameter_ref": ref.parameter_ref}
    if ref.literal_distance is not None:
        return {"value": ref.literal_distance.value, "unit": ref.literal_distance.unit}
    return {}


def _fn(name: str, **arguments: Any) -> dict[str, Any]:
    return {"function_call": {"name": name, "arguments": arguments}}


def _roles(spec: BehaviorSpec) -> dict[str, Role]:
    return {role.id: role for role in spec.roles}


def expand(spec: BehaviorSpec, catalog: Catalog, diag: Collector) -> Expansion:
    out = Expansion()
    roles = _roles(spec)
    for index, coord in enumerate(spec.coordination):
        path = f"coordination[{index}]"
        pattern = catalog.patterns.get(coord.pattern)
        if pattern is None:
            diag.error(
                "COORD001",
                f"{path}.pattern",
                f"coordination pattern '{coord.pattern}' is not in the catalog",
                suggestion=f"use one of {sorted(catalog.patterns)}",
            )
            continue
        missing = [name for name in pattern.required if getattr(coord, name) is None]
        if missing:
            diag.error("COORD001", path, f"pattern '{coord.pattern}' requires fields {missing}")
            continue
        allowed = (
            set(pattern.required)
            | set(pattern.optional)
            | {"id", "pattern", "description", "basis", "priority"}
        )
        for name in Coordination.model_fields:
            if name not in allowed and getattr(coord, name) is not None:
                diag.warning(
                    "COORD001", f"{path}.{name}", f"field not used by pattern '{coord.pattern}'"
                )
        handler = _HANDLERS[coord.pattern]
        handler(spec, coord, roles, out, diag, path)
    return out


def _check_role(roles: dict[str, Role], name: str | None, diag: Collector, path: str) -> bool:
    if name is None or name not in roles:
        diag.error("COORD001", path, f"role '{name}' is not defined")
        return False
    return True


def _leader_follower(
    spec: BehaviorSpec,
    coord: Coordination,
    roles: dict[str, Role],
    out: Expansion,
    diag: Collector,
    path: str,
) -> None:
    ok = _check_role(roles, coord.leader, diag, f"{path}.leader")
    ok = _check_role(roles, coord.followers, diag, f"{path}.followers") and ok
    if not ok:
        return
    assert coord.leader is not None and coord.followers is not None
    if coord.leader == coord.followers:
        diag.error("COORD001", path, "leader and followers must be different roles")
        return
    formation_ref, default = _leader_formation_ref(coord, roles[coord.leader], diag, path)
    if formation_ref is None:
        return
    c = coord.id
    msg_formation = gid(c, "formation")
    msg_transition = gid(c, "transition")
    out.messages += [
        Message(
            id=msg_formation,
            description=f"{c}: leader announces the active formation",
            sender=coord.leader,
            to=coord.followers,
            fields=[MessageField(name="formation", type="string", default=default)],
        ),
        Message(
            id=msg_transition,
            description=f"{c}: leader announces a synchronized state transition",
            sender=coord.leader,
            to=coord.followers,
            fields=[MessageField(name="to_state", type="string", default="")],
        ),
    ]
    cond = gid(c, "formation_known")
    out.conditions.append(
        Condition(
            id=cond,
            description=f"{c}: a formation announcement arrived this tick",
            expression=_fn("message_received", message=msg_formation),
        )
    )
    announce = gid(c, "announce_formation")
    keep = gid(c, "keep_station")
    out.behaviors += [
        Behavior(
            id=announce,
            description=f"{c}: send the leader's current formation to followers",
            claims=["comm_control"],
            actions=[
                ActionCall(
                    command="send_message",
                    arguments={"message": msg_formation, "fields": {"formation": formation_ref}},
                    issue_policy="once_per_activation",
                )
            ],
            success=Outcome(expression={"literal": True}),
            basis=coord.basis,
        ),
        Behavior(
            id=keep,
            description=f"{c}: keep station on the leader in the announced formation",
            claims=["movement_control"],
            actions=[
                ActionCall(
                    command="keep_station",
                    arguments={
                        "leader": coord.leader,
                        "formation": {
                            "message_value": {"message": msg_formation, "field": "formation"}
                        },
                    },
                    issue_policy="every_tick",
                )
            ],
            continuous=True,
            basis=coord.basis,
        ),
    ]
    out._mark(c, msg_formation, msg_transition, cond, announce, keep)
    out.members[c] = {
        "leader": [(coord.leader, None)],
        "followers": [(coord.followers, None)],
    }
    _sync_transitions(
        spec, coord, coord.leader, [coord.followers], msg_transition, roles, out, diag
    )


def _leader_formation_ref(
    coord: Coordination, leader: Role, diag: Collector, path: str
) -> tuple[dict[str, Any] | None, str]:
    ref = coord.formation_ref or {}
    if "literal" in ref and isinstance(ref["literal"], str):
        return {"literal": ref["literal"]}, ref["literal"]
    if "blackboard_value" in ref and isinstance(ref["blackboard_value"], str):
        parts = ref["blackboard_value"].split(".")
        if len(parts) == 3:
            if parts[0] != leader.id:
                diag.error(
                    "COORD001",
                    f"{path}.formation_ref",
                    "formation_ref must read the leader's own blackboard",
                )
                return None, ""
            parts = parts[1:]
        if len(parts) != 2:
            diag.error("COORD001", f"{path}.formation_ref", "expected '<blackboard>.<variable>'")
            return None, ""
        default = ""
        for board in leader.blackboards:
            if board.id == parts[0]:
                for var in board.variables:
                    if var.name == parts[1] and isinstance(var.default, str):
                        default = var.default
        return {"blackboard_value": ".".join(parts)}, default
    diag.error(
        "COORD001",
        f"{path}.formation_ref",
        "formation_ref must be {literal: <formation>} or {blackboard_value: <bb>.<var>}",
    )
    return None, ""


def _sync_transitions(
    spec: BehaviorSpec,
    coord: Coordination,
    initiator: str,
    participants: list[str],
    message_id: str,
    roles: dict[str, Role],
    out: Expansion,
    diag: Collector,
) -> None:
    targets: list[str] = []
    for r_index, role in enumerate(spec.roles):
        machine = role.state_machine
        if machine is None:
            continue
        for s_index, state in enumerate(machine.states):
            for t_index, transition in enumerate(state.transitions):
                if transition.synchronize_with != coord.id:
                    continue
                if role.id != initiator:
                    diag.error(
                        "COORD001",
                        f"roles[{r_index}].state_machine.states[{s_index}]"
                        f".transitions[{t_index}].synchronize_with",
                        f"only '{initiator}' may initiate transitions synchronized by '{coord.id}'",
                    )
                    continue
                if transition.to not in targets:
                    targets.append(transition.to)
    priority = coord.priority if coord.priority is not None else DEFAULT_SYNC_PRIORITY
    for participant in participants:
        role = roles.get(participant)
        if role is None or role.state_machine is None:
            if targets:
                diag.error(
                    "COORD001",
                    f"coordination.{coord.id}",
                    f"participant '{participant}' has no state machine to synchronize",
                )
            continue
        state_ids = [state.id for state in role.state_machine.states]
        for target in targets:
            if target not in state_ids:
                diag.error(
                    "COORD001",
                    f"coordination.{coord.id}",
                    f"participant '{participant}' has no state '{target}' required by "
                    "a synchronized transition",
                    suggestion=f"add state '{target}' to {participant}'s state machine",
                )
        for state in role.state_machine.states:
            if state.terminal:
                continue
            generated: list[Transition] = []
            for target in targets:
                if target == state.id or target not in state_ids:
                    continue
                generated.append(
                    Transition(
                        transition_id=f"{coord.id}{SEP}{state.id}{SEP}to{SEP}{target}",
                        to=target,
                        priority=priority,
                        when={
                            "all": [
                                _fn("message_received", message=message_id),
                                {
                                    "compare": {
                                        "left": {
                                            "message_value": {
                                                "message": message_id,
                                                "field": "to_state",
                                            }
                                        },
                                        "operator": "eq",
                                        "right": {"literal": target},
                                    }
                                },
                            ]
                        },
                        basis=coord.basis,
                    )
                )
            if generated:
                out.transitions.setdefault((participant, state.id), []).extend(generated)
                out._mark(coord.id, *(t.transition_id for t in generated))


def _bounding_overwatch(
    spec: BehaviorSpec,
    coord: Coordination,
    roles: dict[str, Role],
    out: Expansion,
    diag: Collector,
    path: str,
) -> None:
    groups = coord.groups or {}
    if sorted(groups) != ["bounding", "overwatch"]:
        diag.error(
            "COORD001", f"{path}.groups", "groups must be exactly 'bounding' and 'overwatch'"
        )
        return
    members: dict[str, list[tuple[str, int | None]]] = {}
    seen: set[tuple[str, int | None]] = set()
    participants: list[str] = []
    ok = True
    for group, entries in groups.items():
        parsed: list[tuple[str, int | None]] = []
        if not entries:
            diag.error("COORD001", f"{path}.groups.{group}", "group must not be empty")
            ok = False
        for e_index, entry in enumerate(entries):
            item = parse_instance(entry)
            where = f"{path}.groups.{group}[{e_index}]"
            if item is None or item[0] not in roles:
                diag.error("ROLE001", where, f"'{entry}' is not a role or role instance")
                ok = False
                continue
            role = roles[item[0]]
            if item[1] is not None and item[1] >= role.count:
                diag.error("ROLE001", where, f"instance index out of range for '{role.id}'")
                ok = False
                continue
            overlap = {m for m in seen if m[0] == item[0] and (m[1] is None or item[1] is None)}
            if item in seen or overlap:
                diag.error("COORD001", where, f"'{entry}' appears in more than one group")
                ok = False
                continue
            seen.add(item)
            parsed.append(item)
            if item[0] not in participants:
                participants.append(item[0])
        members[group] = parsed
    if not ok or coord.bound_distance is None or coord.active_when is None:
        return
    c = coord.id
    msg = gid(c, "bound_complete")
    out.messages.append(
        Message(
            id=msg,
            description=f"{c}: a member finished its bound",
            sender=list(participants),
            to=list(participants),
            fields=[],
        )
    )
    active, my_turn = gid(c, "active"), gid(c, "my_turn")
    standoff = gid(c, "standoff_reached")
    turn_expr: dict[str, Any] = _fn("bounding_turn", coordination=c)
    out.conditions += [
        Condition(id=active, description=f"{c}: pattern active", expression=coord.active_when),
        Condition(
            id=standoff,
            description=f"{c}: nearest hostile within the standoff distance (stop advancing)",
            expression=_fn("standoff_reached", coordination=c),
        ),
    ]
    if coord.standoff_distance is not None:
        turn_expr = {"all": [turn_expr, {"not": {"condition_ref": standoff}}]}
    out.conditions.append(
        Condition(
            id=my_turn,
            description=f"{c}: this instance should bound now",
            expression=turn_expr,
        )
    )
    bound_args: dict[str, Any] = {"distance": distance_operand(coord.bound_distance)}
    if coord.objective is not None:
        bound_args["toward"] = coord.objective
    elif coord.standoff_distance is not None:
        bound_args["toward"] = "nearest_hostile"
        bound_args["stop_short"] = distance_operand(coord.standoff_distance)
    bound, announce, overwatch = gid(c, "bound"), gid(c, "announce_bound"), gid(c, "overwatch")
    out.behaviors += [
        Behavior(
            id=bound,
            description=f"{c}: bound forward while the other group overwatches",
            precondition={"condition_ref": my_turn},
            claims=["movement_control"],
            actions=[
                ActionCall(
                    command="bound_to_position",
                    arguments=bound_args,
                    issue_policy="once_per_activation",
                )
            ],
            success=Outcome(expression={"mover_value": "at_destination"}),
            failure=(
                Outcome(expression={"condition_ref": standoff})
                if coord.standoff_distance is not None
                else None
            ),
            basis=coord.basis,
        ),
        Behavior(
            id=announce,
            description=f"{c}: tell the team this bound is complete",
            claims=["comm_control"],
            actions=[
                ActionCall(
                    command="send_message",
                    arguments={"message": msg},
                    issue_policy="once_per_activation",
                )
            ],
            success=Outcome(expression={"literal": True}),
        ),
        Behavior(
            id=overwatch,
            description=f"{c}: hold an overwatch position",
            claims=["movement_control"],
            actions=[
                ActionCall(
                    command="hold_overwatch_position",
                    arguments={},
                    issue_policy="once_per_activation",
                )
            ],
            continuous=True,
            basis=coord.basis,
        ),
    ]
    tree = gid(c, "tree")
    out.trees.append(
        Tree(
            id=tree,
            description=f"{c}: bounding overwatch",
            root=TreeNode(
                node_id=gid(c, "root"),
                type="selector",
                children=[
                    TreeNode(
                        node_id=gid(c, "bound_seq"),
                        type="sequence",
                        children=[
                            TreeNode(
                                node_id=gid(c, "turn_check"), type="condition", condition=my_turn
                            ),
                            TreeNode(node_id=gid(c, "bound_node"), type="behavior", behavior=bound),
                            TreeNode(
                                node_id=gid(c, "announce_node"), type="behavior", behavior=announce
                            ),
                        ],
                    ),
                    TreeNode(node_id=gid(c, "overwatch_node"), type="behavior", behavior=overwatch),
                ],
            ),
        )
    )
    out._mark(c, msg, active, my_turn, standoff, bound, announce, overwatch, tree)
    out.members[c] = members


def _report_and_wait(
    spec: BehaviorSpec,
    coord: Coordination,
    roles: dict[str, Role],
    out: Expansion,
    diag: Collector,
    path: str,
) -> None:
    ok = _check_role(roles, coord.reporter, diag, f"{path}.reporter")
    ok = _check_role(roles, coord.receiver, diag, f"{path}.receiver") and ok
    if not ok or coord.ack_timeout is None:
        return
    assert coord.reporter is not None and coord.receiver is not None
    c = coord.id
    report, ack = gid(c, "report"), gid(c, "ack")
    out.messages += [
        Message(
            id=report,
            description=f"{c}: contact report",
            sender=coord.reporter,
            to=coord.receiver,
            fields=[
                MessageField(name="contact_range", type="double", default=0.0),
                MessageField(name="contact_count", type="int", default=0),
            ],
        ),
        Message(
            id=ack, description=f"{c}: acknowledgement", sender=coord.receiver, to=coord.reporter
        ),
    ]
    got_report, got_ack = gid(c, "report_received"), gid(c, "ack_received")
    out.conditions += [
        Condition(id=got_report, expression=_fn("message_received", message=report)),
        Condition(id=got_ack, expression=_fn("message_received", message=ack)),
    ]
    send, wait, acknowledge = gid(c, "send_report"), gid(c, "await_ack"), gid(c, "acknowledge")
    out.behaviors += [
        Behavior(
            id=send,
            description=f"{c}: send the contact report",
            claims=["comm_control"],
            actions=[
                ActionCall(
                    command="report_contact",
                    arguments={"message": report},
                    issue_policy="once_per_activation",
                )
            ],
            success=Outcome(expression={"literal": True}),
            basis=coord.basis,
        ),
        Behavior(
            id=wait,
            description=f"{c}: wait for acknowledgement",
            success=Outcome(expression={"condition_ref": got_ack}),
            timeout=Timeout(
                value=coord.ack_timeout.value, unit=coord.ack_timeout.unit, result="failure"
            ),
            basis=coord.basis,
        ),
        Behavior(
            id=acknowledge,
            description=f"{c}: acknowledge a received report",
            precondition={"condition_ref": got_report},
            claims=["comm_control"],
            actions=[
                ActionCall(
                    command="send_message",
                    arguments={"message": ack},
                    issue_policy="once_per_activation",
                )
            ],
            success=Outcome(expression={"literal": True}),
        ),
    ]
    out._mark(c, report, ack, got_report, got_ack, send, wait, acknowledge)
    out.members[c] = {"reporter": [(coord.reporter, None)], "receiver": [(coord.receiver, None)]}


def _synchronized_transition(
    spec: BehaviorSpec,
    coord: Coordination,
    roles: dict[str, Role],
    out: Expansion,
    diag: Collector,
    path: str,
) -> None:
    ok = _check_role(roles, coord.initiator, diag, f"{path}.initiator")
    participants = coord.participants or []
    if not participants:
        diag.error("COORD001", f"{path}.participants", "participants must not be empty")
        ok = False
    for p_index, name in enumerate(participants):
        ok = _check_role(roles, name, diag, f"{path}.participants[{p_index}]") and ok
        if name == coord.initiator:
            diag.error(
                "COORD001", f"{path}.participants[{p_index}]", "initiator cannot participate"
            )
            ok = False
    if not ok:
        return
    assert coord.initiator is not None
    msg = gid(coord.id, "transition")
    out.messages.append(
        Message(
            id=msg,
            description=f"{coord.id}: initiator announces a synchronized transition",
            sender=coord.initiator,
            to=list(participants) if len(participants) > 1 else participants[0],
            fields=[MessageField(name="to_state", type="string", default="")],
        )
    )
    out._mark(coord.id, msg)
    out.members[coord.id] = {
        "initiator": [(coord.initiator, None)],
        "participants": [(p, None) for p in participants],
    }
    _sync_transitions(spec, coord, coord.initiator, list(participants), msg, roles, out, diag)


_HANDLERS = {
    "leader_follower": _leader_follower,
    "bounding_overwatch": _bounding_overwatch,
    "report_and_wait": _report_and_wait,
    "synchronized_transition": _synchronized_transition,
}


def sync_message_for(coord: Coordination) -> str:
    return gid(coord.id, "transition")


def as_list(value: str | list[str]) -> list[str]:
    return [value] if isinstance(value, str) else list(value)
