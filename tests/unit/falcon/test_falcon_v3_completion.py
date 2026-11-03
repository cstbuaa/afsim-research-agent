"""Which transitions autofix repairs, and FIRE_BY_TIME (falcon.v3.semantics)."""

from __future__ import annotations

from types import SimpleNamespace

from afsim_research_agent.falcon.v3 import semantics as S


def _contracts():
    def act(writes=(), persistent=False):
        return SimpleNamespace(
            writes=[{"field": f} for f in writes],
            written={*writes},
            persistent=persistent,
            success={},
        )

    actions = {
        "movement.follow_route": act(["movement.derived.range_to_target_m"]),
        "movement.hold_position": act(["movement.derived.range_to_target_m"], persistent=True),
        "engagement.fire_selected_weapon": act(["engagement.shots"]),
    }
    board = {
        "movement.target.arrival_tolerance_m": SimpleNamespace(
            unwritten=True, updated="never", written_by=()
        ),
        "movement.derived.range_to_target_m": SimpleNamespace(
            unwritten=False, updated="tick", written_by=(), initial=0.0, type="double"
        ),
        "damage.assessment.normalized_result": SimpleNamespace(
            unwritten=False, updated="always", written_by=(), initial="", type="string"
        ),
    }
    return SimpleNamespace(actions=actions, blackboard=board, tactics={})


def _spec():
    return {
        "subtrees": [],
        "state_trees": {"car": {"GO": "go", "HOLD": "hold", "FIRE": "fire"}},
        "conditions": [
            {
                "id": "arrived",
                "expression": {
                    "compare": {
                        "left": {"bb": "movement.derived.range_to_target_m"},
                        "operator": "lte",
                        "right": {"bb": "movement.target.arrival_tolerance_m"},
                    }
                },
            },
            {
                "id": "later",
                "expression": {
                    "any": [
                        {
                            "compare": {
                                "left": {"bb": "damage.assessment.normalized_result"},
                                "operator": "ne",
                                "right": {"literal": "NONE"},
                            }
                        },
                        {
                            "function_call": {
                                "name": "time_in_state_gte",
                                "arguments": {"duration": {"value": 30, "unit": "s"}},
                            }
                        },
                    ]
                },
            },
        ],
        "missions": {
            "car": {
                "initial_state": "GO",
                "states": ["GO", "HOLD", "FIRE"],
                "transitions": [
                    {"from": "GO", "to": "HOLD", "on": "arrived"},
                    {"from": "HOLD", "to": "FIRE", "on": "later"},
                ],
            }
        },
    }


def _with_trees(spec):
    spec["subtrees"] = [
        {"id": "go", "root": {"type": "action", "action": "movement.follow_route"}},
        {"id": "hold", "root": {"type": "action", "action": "movement.hold_position"}},
        {"id": "fire", "root": {"type": "action", "action": "engagement.fire_selected_weapon"}},
    ]
    return spec


def test_a_route_that_ends_turns_a_broken_arrival_into_a_tree_event() -> None:
    spec, c = _with_trees(_spec()), _contracts()
    items = S.completion_items(spec, c, {})
    assert ("car", "GO", "HOLD", "arrived", True) in items
    assert S.finite_state(spec, "car", "GO", c, {}) and not S.finite_state(
        spec, "car", "HOLD", c, {}
    )


def test_firing_after_a_timer_alone_is_reported() -> None:
    spec, c = _with_trees(_spec()), _contracts()
    codes = [code for _p, _m, code in S.role_problems(spec, c, {})]
    assert "FIRE_BY_TIME" in codes
    assert S.fire_by_time(spec, c, {}) == [("car", "HOLD", "FIRE", "later")]


def test_arrival_from_speed_and_time_on_a_route_is_repaired_but_no_target_is_not() -> None:
    spec, c = _with_trees(_spec()), _contracts()
    c.blackboard["self.speed_mps"] = SimpleNamespace(
        unwritten=False, updated="tick", written_by=(), initial=0.0, type="double"
    )
    c.blackboard["perception.selected_target.valid"] = SimpleNamespace(
        unwritten=False, updated="tick", written_by=(), initial=False, type="bool"
    )
    c.actions["movement.follow_route"].written.add("self.speed_mps")
    c.actions["perception.search"] = SimpleNamespace(
        writes=[], written={"perception.selected_target.valid"}, persistent=False, success={}
    )
    spec["subtrees"].append(
        {"id": "look", "root": {"type": "action", "action": "perception.search"}}
    )
    spec["state_trees"]["car"]["LOOK"] = "look"
    spec["missions"]["car"]["states"].append("LOOK")
    spec["conditions"] += [
        {
            "id": "stopped",
            "expression": {
                "all": [
                    {
                        "compare": {
                            "left": {"bb": "self.speed_mps"},
                            "operator": "lte",
                            "right": {"literal": 0.1},
                        }
                    },
                    {
                        "function_call": {
                            "name": "time_in_state_gte",
                            "arguments": {"duration": {"value": 2, "unit": "s"}},
                        }
                    },
                ]
            },
        },
        {"id": "none_yet", "expression": {"not": {"bb": "perception.selected_target.valid"}}},
    ]
    spec["missions"]["car"]["transitions"] = [
        {"from": "GO", "to": "LOOK", "on": "stopped"},
        {"from": "LOOK", "to": "HOLD", "on": "none_yet"},
    ]
    items = S.completion_items(spec, c, {})
    assert ("car", "GO", "LOOK", "stopped", True) in items
    assert ("car", "LOOK", "HOLD", "none_yet", False) in items


def test_a_delay_the_requirement_does_not_give_becomes_an_order() -> None:
    from afsim_research_agent.falcon.v3.draft import Drafter

    d = Drafter.__new__(Drafter)
    d.r = SimpleNamespace(requirement="前进约 10 秒后开始搜索")
    d.clauses, d.notes = [{"id": "c1", "text": "开始搜索后保持位置"}], []
    item = {"id": "o1", "kind": "not_before", "action": "b", "after": "a", "min_s": 5.0}
    assert d._grounded(dict(item)) == {"id": "o1", "kind": "precedes", "first": "a", "then": "b"}
    assert d.notes
    assert d._grounded({**item, "min_s": 10})["kind"] == "not_before"
