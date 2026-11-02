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
            "preamble": "processor ORG_LOADER WSF_SCRIPT_PROCESSOR end_processor",
            "extra": 'execute at_time 0 sec absolute load_org("{leader}"); end_execute',
            "per_platform": "# {name} is {role}",
        },
        "unused": {"includes": ["obs/unused.txt"], "aux": "string never = 1"},
        "route": {
            "leader_aux": "double end_lat = {ahead_lat_deg}",
            "member_aux": ['string wing = "LEFT"', 'string wing = "RIGHT"'],
            "preamble": "route r1 position {origin_lat} {origin_lon} position {ahead_lat} {ahead_lon} end_route",
            "parameters": {"search_route": "r1", "not_in_model": "x"},
        },
    },
}


def _contracts():
    actions = {
        "tasking.send": SimpleNamespace(slice="tasking"),
        "fires.shoot": SimpleNamespace(slice="fires"),
        "route.follow": SimpleNamespace(slice="route"),
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
        "parameters": {"search_route": {"value": "placeholder"}},
        "events": {"cancelled": {}},
        "subtrees": {"common": tree("org.load")},
        "roles": {
            "platoon_leader": {
                "role": {"count": 1},
                "states": {"ASSIGN": {"tree": tree("tasking.send", "route.follow")}},
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
    assert "ORG_LOADER" in run.preamble
    assert [(e.at_s, e.event) for e in run.events] == [(60.0, "cancelled")]
    assert [(x.kind, x.by) for x in run.expect.events] == [("WEAPON_FIRED", "member")]
    assert run.end_time_s == 80 and run.start_time == "18:00:00"


def test_route_points_wings_and_parameters() -> None:
    run = generate(_model(), KIT, _contracts())
    leader, m1, m2 = run.platforms
    assert "double end_lat = 45.00" in leader.body
    assert 'wing = "LEFT"' in m1.body and 'wing = "RIGHT"' in m2.body
    assert "route r1 position 45:00:00.00n 121:00:00.00w position 45:00:00.00n" in run.preamble
    assert run.parameters == {"search_route": "r1"}


def test_routes_the_model_names_are_laid_out() -> None:
    model = _model()
    model["roles"]["member"]["states"]["GO"] = {
        "tree": {"type": "action", "action": "route.follow", "params": {"route_name": "east"}}
    }
    run = generate(model, KIT, _contracts())
    assert "route east\n position 45:00:00.00n 121:00:00.00w speed 8 m/s" in run.preamble
    assert run.preamble.count("route r1") == 1  # the kit's own route is not laid out again
