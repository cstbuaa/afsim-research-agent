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
