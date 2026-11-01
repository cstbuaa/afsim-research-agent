"""A minimal AFSIM run laid out from the model (ir.v2.afsim.scenario)."""

from __future__ import annotations

from types import SimpleNamespace

from afsim_research_agent.ir.v2.afsim.scenario import generate, model_refs, role_kinds

KIT = {
    "meta": {
        "start_time": "18:00:00",
        "end_time_s": 80,
        "origin": [45.0, -121.0],
        "heading_deg": 90.0,
        "spacing_m": 50.0,
        "includes": ["obs/base.txt"],
        "body": "edit thing on end_thing",
    },
    "enemy": {
        "count": 2,
        "range_m": 600.0,
        "platform": "platform {name} TARGET side red position {lat} {lon} end_platform",
    },
    "events": {"cancelled": -20},
    "expect": {"fire_event": "WEAPON_FIRED", "fire_actions": ["fires.shoot"]},
    "slices": {
        "tasking": {
            "includes": ["obs/tasking.txt"],
            "leader_aux": 'string task_role = "LEADER"',
            "member_aux": 'string task_role = "MEMBER" string task_boss = "{leader}"',
        },
        "org": {
            "aux": "int org_index = {index}",
            "extra": 'execute at_time 0 sec absolute load_org("{leader}"); end_execute',
            "per_platform": "# {name} is {role}",
        },
        "unused": {"includes": ["obs/unused.txt"], "aux": "string never = 1"},
    },
}


def _contracts():
    actions = {
        "tasking.send": SimpleNamespace(slice="tasking"),
        "fires.shoot": SimpleNamespace(slice="fires"),
        "org.load": SimpleNamespace(slice="org"),
    }
    return SimpleNamespace(actions=actions, tactics={})


def _model():
    tree = lambda *acts: {  # noqa: E731
        "type": "sequence",
        "children": [{"type": "action", "action": a} for a in acts],
    }
    return {
        "model": {"id": "crow_x"},
        "events": {"cancelled": {}},
        "subtrees": {"common": tree("org.load")},
        "roles": {
            "platoon_leader": {
                "role": {"count": 1},
                "states": {"ASSIGN": {"tree": tree("tasking.send")}},
            },
            "member": {
                "role": {"count": 2},
                "states": {"FIRE": {"tree": tree("fires.shoot")}},
            },
        },
    }


def test_roles_and_references() -> None:
    model = _model()
    assert role_kinds(model) == {"platoon_leader": "leader", "member": "member"}
    refs = model_refs(model)
    assert refs["member"] == {"fires.shoot", "org.load"}
    unnamed = {"roles": {"a": {"role": {"count": 1}}, "b": {"role": {"count": 3}}}}
    assert role_kinds(unnamed) == {"a": "leader", "b": "member"}


def test_generated_run_lays_out_roles_slices_events_and_checks() -> None:
    run = generate(_model(), KIT, _contracts())
    names = [p.name for p in run.platforms]
    assert names == ["blue_platoon_leader_1", "blue_member_1", "blue_member_2"]
    assert [p.instance for p in run.platforms] == [0, 0, 1]
    leader, m1, _ = run.platforms
    assert 'task_role = "LEADER"' in leader.body and "edit thing on" in leader.body
    assert 'task_boss = "blue_platoon_leader_1"' in m1.body and "org_index = 1" in m1.body
    assert "never" not in m1.body
    assert run.includes == ["obs/base.txt", "obs/tasking.txt"]
    assert "obs/unused.txt" not in run.includes
    assert "red_1" in run.extra and "red_2" in run.extra
    assert 'load_org("blue_platoon_leader_1")' in run.extra
    assert "# blue_member_2 is member" in run.extra
    assert [(e.at_s, e.event) for e in run.events] == [(60.0, "cancelled")]
    assert [(x.kind, x.by) for x in run.expect.events] == [("WEAPON_FIRED", "member")]
    assert run.end_time_s == 80 and run.start_time == "18:00:00"
