"""ROLE_FIELD / ENTRY_TRUE: conditions a role can never see change, and conditions that
already hold when their state is entered (falcon.v3.semantics.role_problems)."""

from __future__ import annotations

from types import SimpleNamespace

from afsim_research_agent.falcon.v3.semantics import role_problems


def _contracts():
    def act(*written):
        return SimpleNamespace(written=set(written))

    actions = {
        "movement.follow_route": act("movement.derived.range_to_target_m", "self.speed_mps"),
        "movement.stop": act("self.speed_mps"),
        "formation.move_to_slot": act("formation.quality.in_position"),
        "formation.plan": act("formation.platoon_slots.count"),
    }
    blackboard = {
        "movement.derived.range_to_target_m": SimpleNamespace(
            initial=None, default=None, type="double", written_by=None, updated="while_running"
        ),
        "formation.quality.in_position": SimpleNamespace(
            initial=False, default=None, type="bool", written_by=None, updated="while_running"
        ),
    }
    return SimpleNamespace(actions=actions, blackboard=blackboard, tactics={})


def _cmp(field, op, value):
    return {"compare": {"left": {"bb": field}, "operator": op, "right": {"literal": value}}}


def _spec(conditions, trees, missions):
    return {
        "conditions": [{"id": k, "expression": v} for k, v in conditions.items()],
        "subtrees": [
            {
                "id": k,
                "root": {
                    "type": "sequence",
                    "children": [{"type": "action", "action": a} for a in acts],
                },
            }
            for k, acts in trees.items()
        ],
        "state_trees": {
            r: {s: f"{r}_{s.lower()}" for s in m["states"]} for r, m in missions.items()
        },
        "missions": missions,
    }


def test_arrival_written_as_a_field_that_starts_at_zero_holds_on_entry() -> None:
    arrived = {
        "all": [
            _cmp("movement.derived.range_to_target_m", "lte", 5.0),
            _cmp("self.speed_mps", "lte", 0.1),
        ]
    }
    spec = _spec(
        {"arrived": arrived},
        {"car_go": ["movement.follow_route"], "car_hold": ["movement.stop"]},
        {
            "car": {
                "initial_state": "GO",
                "states": ["GO", "HOLD"],
                "transitions": [{"from": "GO", "to": "HOLD", "on": "arrived"}],
            }
        },
    )
    found = role_problems(spec, _contracts(), {})
    assert [p[2] for p in found] == ["ENTRY_TRUE"] and found[0][0] == "conditions.arrived"
    assert "树事件" in found[0][1]
    far = {"arrived": _cmp("movement.derived.range_to_target_m", "gt", 5.0)}
    spec["conditions"] = [{"id": "arrived", "expression": far["arrived"]}]
    assert role_problems(spec, _contracts(), {}) == []


def test_leader_waiting_for_a_field_only_members_write_is_stuck() -> None:
    ready = _cmp("formation.quality.in_position", "eq", True)
    spec = _spec(
        {"ready": ready},
        {
            "leader_form": ["formation.plan"],
            "leader_alert": ["movement.stop"],
            "member_form": ["formation.move_to_slot"],
            "member_alert": ["movement.stop"],
        },
        {
            "leader": {
                "initial_state": "FORM",
                "states": ["FORM", "ALERT"],
                "transitions": [{"from": "FORM", "to": "ALERT", "on": "ready"}],
            },
            "member": {
                "initial_state": "FORM",
                "states": ["FORM", "ALERT"],
                "transitions": [{"from": "FORM", "to": "ALERT", "on": "ready"}],
            },
        },
    )
    found = role_problems(spec, _contracts(), {})
    assert [(p[2], "leader" in p[1]) for p in found] == [("ROLE_FIELD", True)]


def test_a_member_running_the_leaders_commanding_step_is_reported() -> None:
    """A formation step that commands the followers (scope: followers) run by a member has
    nobody to command and succeeds at once; the member does not move (r3, 11-04)."""
    contracts = _contracts()
    contracts.actions["formation.column_slots"] = SimpleNamespace(
        written=set(),
        resources=({"resource": "locomotion", "mode": "exclusive", "scope": "followers"},),
    )
    missions = {
        r: {"initial_state": "ADVANCE", "states": ["ADVANCE"], "transitions": []}
        for r in ("leader", "member")
    }
    spec = _spec(
        {},
        {
            "leader_advance": ["movement.follow_route"],
            "member_advance": ["movement.follow_route", "formation.column_slots"],
        },
        missions,
    )
    spec["roles"] = [{"id": "leader", "count": 1}, {"id": "member", "count": 2}]
    found = [p for p in role_problems(spec, contracts, {}) if p[2] == "COMMAND_ROLE"]
    assert [p[0] for p in found] == ["state_trees.member.ADVANCE"]
    assert "formation.column_slots" in found[0][1] and "leader" in found[0][1]
    # the same step in the leader's tree is what the capability means
    spec["subtrees"][0]["root"]["children"].append(
        {"type": "action", "action": "formation.column_slots"}
    )
    spec["subtrees"][1]["root"]["children"].pop()
    assert not [p for p in role_problems(spec, contracts, {}) if p[2] == "COMMAND_ROLE"]
    # inside a tactic-set subtree the member runs, too
    tactics = {
        "fs.search_advance": SimpleNamespace(
            raw={
                "root": {
                    "type": "parallel",
                    "children": [
                        {"type": "action", "action": "movement.follow_route"},
                        {"type": "action", "action": "formation.column_slots"},
                    ],
                }
            }
        )
    }
    spec["state_trees"]["member"]["ADVANCE"] = "fs.search_advance"
    found = [p for p in role_problems(spec, contracts, tactics) if p[2] == "COMMAND_ROLE"]
    assert [p[0] for p in found] == ["state_trees.member.ADVANCE"]


def test_roles_are_told_apart_like_the_scenario_kit() -> None:
    from afsim_research_agent.falcon.v3.semantics import role_kinds

    assert role_kinds({"leader": 1, "member": 2}) == {"leader": "leader", "member": "member"}
    assert role_kinds({"car": 1, "wing": 2}) == {"car": "leader", "wing": "member"}
    assert role_kinds({"a": 1, "b": 1}) == {"a": "", "b": ""}
    assert role_kinds({"platoon_commander": 2, "tank": 2})["platoon_commander"] == "leader"
