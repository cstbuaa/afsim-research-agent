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


def test_a_field_two_used_slices_set_is_written_once() -> None:
    from afsim_research_agent.ir.v2.afsim.scenario import _dedupe_aux

    out = _dedupe_aux(
        [
            'string role = "LEADER" double end_lat = 45.1',
            "double end_lat = 45.1 double end_lon = -121.0",
            "edit something end_edit",
        ]
    )
    assert out == [
        'string role = "LEADER" double end_lat = 45.1',
        "double end_lon = -121.0",
        "edit something end_edit",
    ]


def test_no_shot_is_expected_when_only_an_unsent_order_leads_to_firing() -> None:
    model = _model()
    model["events"]["fire_order"] = {"source": "external"}
    model["roles"]["member"]["mission"] = {
        "initial_state": "WAIT",
        "transitions": [{"from": "WAIT", "to": "FIRE", "on": "fire_order"}],
        "global": [],
    }
    model["roles"]["member"]["states"]["WAIT"] = {"tree": {"type": "action", "action": "org.load"}}
    assert generate(model, KIT, _contracts()).expect.events == []
    kit = {**KIT, "events": {**KIT["events"], "fire_order": 30}}
    run = generate(model, kit, _contracts())
    assert [(x.kind, x.by) for x in run.expect.events] == [("WEAPON_FIRED", "member")]


def test_an_approaching_enemy_gets_a_route_towards_the_own_start() -> None:
    kit = {**KIT, "enemy": {**KIT["enemy"], "range_m": 7000.0, "approach_speed_mps": 10.0}}
    run = generate(_model(), kit, _contracts())
    red = [line for line in run.extra.splitlines() if line.startswith("platform red_1")][0]
    assert "heading 270 deg route position" in red and "speed 10 m/s" in red
    assert red.count("position ") == 2 and red.endswith("end_platform") and "end_route" in red
    still = generate(_model(), KIT, _contracts())
    assert "route" not in [x for x in still.extra.splitlines() if "red_1" in x][0]


def test_a_delayed_enemy_approach_pauses_at_its_start() -> None:
    enemy = {**KIT["enemy"], "range_m": 7000.0, "approach_speed_mps": 5.0}
    run = generate(_model(), {**KIT, "enemy": {**enemy, "approach_start_s": 800.0}}, _contracts())
    red = [line for line in run.extra.splitlines() if line.startswith("platform red_1")][0]
    assert "speed 5 m/s pause_time 800 sec position" in red
    now = generate(_model(), {**KIT, "enemy": enemy}, _contracts())
    assert "pause_time" not in now.extra


def test_a_gone_window_deletes_the_enemy_and_brings_a_copy_back() -> None:
    import copy

    kit = copy.deepcopy(KIT)
    kit["enemy"] |= {"count": 1, "gone": [[100, 130], [200, None]]}
    run = generate(_model(), kit, _contracts())
    assert (
        'execute at_time 100 sec absolute\n   WsfPlatform p = WsfSimulation.FindPlatform("red_1");'
        in run.extra
    )
    assert "platform red_1_r1 TARGET side red" in run.extra
    assert "creation_time 130 sec\nend_platform" in run.extra
    assert 'FindPlatform("red_1_r1")' in run.extra and "at_time 200 sec" in run.extra
    assert "red_1_r2" not in run.extra  # gone for good


def test_gone_windows_split_the_2d_hostile_where_it_would_have_been() -> None:
    from afsim_research_agent.ir.v2.scenario_interface import Binding, enemy_segments

    b = Binding(origin=(45.0, -121.0), heading_deg=90.0, enemy_range_m=1000.0, end_s=300.0)
    b.enemy_velocity = (-5.0, 0.0)
    b.enemy_move_start_s, b.enemy_move_stop_s = 10.0, 110.0
    b.enemy_gone = [(50.0, 70.0)]
    segs = enemy_segments(b, 0, (1000.0, 0.0))
    assert segs[0] == {
        "id": "e1", "position_m": [1000.0, 0.0], "remove_s": 50.0,
        "velocity_mps": [-5.0, 0.0], "move_start_s": 10.0, "move_stop_s": 110.0,
    }  # fmt: skip
    assert segs[1]["id"] == "e1_r1" and segs[1]["appear_s"] == 70.0
    assert segs[1]["position_m"] == [700.0, 0.0] and segs[1]["move_start_s"] == 70.0
