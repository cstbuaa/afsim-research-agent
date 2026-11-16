"""The behaviour model and the scenario as two layers: interface, binding, and the checks
around them (ir.v2.scenario_interface, falcon.v3)."""

from __future__ import annotations

import pytest

from afsim_research_agent.falcon.sheet.blocks import Block
from afsim_research_agent.falcon.v3 import autofix, semantics
from afsim_research_agent.falcon.v3.compiler import compile_sheet
from afsim_research_agent.ir.v2 import Contracts, normalize
from afsim_research_agent.ir.v2.afsim.scenario import generate
from afsim_research_agent.ir.v2.scenario_interface import (
    ROUTING,
    ScenarioInterface,
    bind,
    binding_for,
    check_binding,
    derive,
    interface_of,
    interface_param_issues,
    interface_parameters,
)
from afsim_research_agent.ir.v2.sim import simulate

R1 = (
    "我方一辆装甲车在道路西端出发，上级通报东方约1.5公里可能有敌方车辆。"
    "任务：前出到合适位置建立观察哨。约束：未经命令不得开火。"
)
R2 = (
    "我方一辆装甲车在路口担任观察哨，负责监视东侧方向，东侧数百米外可能出现敌方车辆。"
    "目标一时丢失时不要贸然离开阵位。约束：未经命令不得开火。"
)


@pytest.fixture(scope="module")
def contracts() -> Contracts:
    return Contracts.load()


def test_the_interface_follows_the_requirement() -> None:
    i1 = derive(R1)
    assert set(i1.points) == {"post", "ahead"} and set(i1.routes) == {"approach"}
    assert i1.points["post"].anchor == "own_start" and i1.points["ahead"].anchor == "ahead"
    assert i1.events["fire_order"].kind == "command" and not i1.events["fire_order"].required
    assert "1.5公里" in i1.facts["enemy"]
    i2 = derive(R2)
    assert set(i2.points) == {"post"} and not i2.routes
    assert "fire_order" in i2.events and "cancelled" in i2.events
    assert any("已在阵位" in s for s in i2.sources)


def test_the_binding_gives_every_name_a_reasonable_value() -> None:
    iface = derive(R1)
    b = bind(iface)  # no kit: every value is a default
    assert b.points_plane["post"] == (0.0, 0.0)
    east, north = b.points_plane["ahead"]
    # the requirement's enemy (about 1.5 km east) sets the distance; the point ahead stays
    # within 350 m of the start
    assert b.enemy_range_m == 1500.0
    assert east == pytest.approx(350.0) and abs(north) < 1e-6
    assert b.parameters["rt_approach"] == "ir_route_approach"
    lat, lon = b.parameters["pt_ahead_lat"], b.parameters["pt_ahead_lon"]
    assert lat == pytest.approx(45.0) and lon > -121.0
    assert b.routes["approach"].plane == [(0.0, 0.0), b.points_plane["ahead"]]
    assert b.routes["approach"].arrival_tolerance_m == 20.0
    assert b.events == {"cancelled": None, "fire_order": None}  # optional: not sent
    assert b.defaults
    kit = {
        "meta": {
            "origin": [45.0, -121.0],
            "heading_deg": 90.0,
            "ahead_m": 350.0,
            "end_time_s": 120,
        },
        "enemy": {"range_m": 1800.0, "count": 2, "spacing_m": 150.0},
        "events": {"cancelled": -20},
    }
    b = bind(iface, kit)
    assert b.points_plane["ahead"][0] == pytest.approx(350.0)
    assert b.events["cancelled"] == 100.0 and b.events["fire_order"] is None
    assert b.enemies_plane[0][0] == pytest.approx(1500.0)  # the requirement's, not the kit's


def test_a_model_carries_its_interface_in_its_parameters() -> None:
    iface = derive(R1)
    params = interface_parameters(iface)
    assert params["pt_ahead_lat"]["scenario"] == "point:ahead:lat:ahead"
    assert params["rt_approach"]["scenario"] == "route:approach:post:ahead"
    back = interface_of({"parameters": params})
    assert back.points["ahead"].anchor == "ahead" and back.routes["approach"].end == "ahead"


SHEET = """# 观察哨

```yaml model
model: {display_name: 观察哨, intent: 前出后停车搜索}
slices: [movement, perception]
roles:
  car: {capability_class: ground_vehicle, description: 装甲车}
events:
  go_done: {source: tree, result: success}
  cancelled: {source: external}
```

```mermaid car
stateDiagram-v2
  [*] --> GO
  GO --> WATCH : go_done
  * --> DONE : cancelled
  DONE --> [*]
```

```python car
def GO():
    movement.follow_route(ROUTE)
    movement.stop()

def WATCH():
    with parallel("all"):
        movement.hold_position()
        tactic("perception.search_for_target")
```
"""


def _sheet(route: str) -> str:
    return SHEET.replace("movement.follow_route(ROUTE)", f"movement.follow_route({route})")


def test_routes_and_points_are_written_by_name(contracts: Contracts) -> None:
    iface = derive(R1).model_dump()
    out = compile_sheet(
        _sheet("route=approach"), contracts, model_id="op", scenario_interface=iface
    )
    assert not out.errors, [p.render() for p in out.errors]
    leaf = out.spec["subtrees"][0]["root"]["children"][0]
    assert leaf["params"] == {"route_name": "$rt_approach"}
    assert out.spec["parameters"]["rt_approach"]["scenario"] == "route:approach:post:ahead"
    # the optional fire order may stay unused; an unknown route name is reported
    bad = compile_sheet(_sheet("route=road"), contracts, model_id="op", scenario_interface=iface)
    assert any("SCN_UNKNOWN" in p.message for p in bad.errors)
    model = normalize(out.spec, contracts)
    assert model.ok, [d.render() for d in model.diagnostics]
    assert semantics.scenario_literals(out.spec) == []
    assert check_binding(model.model, binding_for(model.model)) == []


def test_a_written_route_name_is_reported_and_repaired(contracts: Contracts) -> None:
    iface = derive(R1)
    out = compile_sheet(
        _sheet('route_name="east_road"'),
        contracts,
        model_id="op",
        scenario_interface=iface.model_dump(),
    )
    assert any(code == "SCN_LITERAL" for _p, _m, code in semantics.scenario_literals(out.spec))
    model = normalize(out.spec, contracts).model
    issues = interface_param_issues(model)
    assert [(i.kind, i.literal, ROUTING[i.route]) for i in issues] == [
        ("not_interface", True, "model")
    ]
    assert check_binding(model, binding_for(model)) == []  # the model's, not the scenario's
    blocks = [
        Block("pytree", "car", 'def GO():\n    movement.follow_route(route_name="east_road")\n', 1)
    ]
    fixed, notes = autofix.scenario_refs(blocks, iface)
    assert "movement.follow_route(route=approach)" in fixed[0].text and notes
    stay = [
        Block(
            "pytree",
            "car",
            "def GO():\n    movement.move_to_point(latitude_deg=lat, longitude_deg=lon)\n",
            1,
        )
    ]
    fixed, _ = autofix.scenario_refs(stay, derive(R2))
    assert "movement.stop()" in fixed[0].text  # already at the post


def test_both_runs_are_laid_out_from_one_binding(contracts: Contracts) -> None:
    iface = derive(R1).model_dump()
    spec = compile_sheet(
        _sheet("route=approach"), contracts, model_id="op", scenario_interface=iface
    ).spec
    model = normalize(spec, contracts).model
    kit = {
        "meta": {"origin": [45.0, -121.0], "heading_deg": 90.0, "ahead_m": 300.0, "end_time_s": 80},
        "enemy": {
            "range_m": 650.0,
            "platform": "platform {name} T side red position {lat} {lon} end_platform",
        },
        "events": {"cancelled": -20},
        "provides": {
            "route_endpoint": "double end_lat = {lat_deg} double end_lon = {lon_deg} double tol = {tolerance_m}"
        },
    }
    run = generate(model, kit, contracts)
    assert "route ir_route_approach" in run.preamble
    assert run.parameters["rt_approach"] == "ir_route_approach"
    assert (
        "double tol = 20" in run.platforms[0].body
        and "end_lat = 45.0000000" in run.platforms[0].body
    )
    # the 2D simulation drives the same route to the same point
    b = binding_for(model, kit)
    raw = {
        "id": "main",
        "environment": {"duration_s": 80},
        "expect": [{"id": "watch", "role": "car", "reaches": "WATCH"}],
    }
    res = simulate(model, contracts, raw, b)
    assert res.error is None and res.passed, res.render()
    # vehicles written as a list naming each role (drafts do, r128) are grouped by role
    listed = raw | {"vehicles": [{"role": "car", "position_m": [0, 0]}]}
    res = simulate(model, contracts, listed, b)
    assert res.error is None and res.passed, res.render()
    # a shape the binding cannot lay out is a scenario error, not a crash
    res = simulate(model, contracts, raw | {"vehicles": [["car", 0, 0]]}, b)
    assert res.error and res.error.startswith("scenario structure")


def test_the_interface_is_shown_to_the_drafter() -> None:
    text = derive(R2).prompt_text()
    assert "post" in text and "可选" in text and "point=" in text
    assert ScenarioInterface.model_validate(derive(R1).model_dump()).check() == []


def test_the_2d_simulation_records_positions_for_effect_checks(contracts: Contracts) -> None:
    from afsim_research_agent.ir.v2.effects import judge
    from afsim_research_agent.ir.v2.obligations import Obligation

    iface = derive(R1).model_dump()
    spec = compile_sheet(
        _sheet("route=approach"), contracts, model_id="op", scenario_interface=iface
    ).spec
    model = normalize(spec, contracts).model
    raw = {"id": "main", "environment": {"duration_s": 80}}
    res = simulate(model, contracts, raw, binding_for(model))
    ob = Obligation.model_validate({"id": "adv", "kind": "advances", "min_m": 150})
    (verdict,) = judge([ob], res.tracks, {"car_0": "car"}, res.activations, res.events)
    assert verdict.judged and verdict.passed, verdict.detail


def test_moving_to_the_start_is_staying() -> None:
    blocks = [Block("pytree", "car", "def GO():\n    movement.move_to_point(point=post)\n", 1)]
    fixed, notes = autofix.scenario_refs(blocks, derive(R2))
    assert "movement.stop()" in fixed[0].text and "出发位置" in notes[0]


def test_a_move_to_the_start_compiles_to_a_stop(contracts: Contracts) -> None:
    sheet = _sheet("route=approach").replace(
        "movement.follow_route(route=approach)", "movement.move_to_point(point=post)"
    )
    out = compile_sheet(sheet, contracts, model_id="op", scenario_interface=derive(R2).model_dump())
    leaf = out.spec["subtrees"][0]["root"]["children"][0]
    assert leaf["action"] == "movement.stop" and not leaf.get("params")


def test_an_approaching_enemy_moves_in_the_2d_scenario() -> None:
    from afsim_research_agent.ir.v2.scenario_interface import bind, bind_scenario, derive

    iface = derive("单车前出约 1 km 建立观察哨")
    kit = {"enemy": {"range_m": 7000.0, "approach_speed_mps": 10.0, "count": 1}}
    b = bind(iface, kit)
    assert b.enemy_velocity == (-10.0, -0.0) and b.enemy_stop_m == 500.0
    model = {"roles": {"car": {"role": {"count": 1}}}}
    sc = bind_scenario({"id": "s"}, model, b)
    assert sc["hostiles"][0]["velocity_mps"] == [-10.0, -0.0]
    assert "velocity_mps" not in bind_scenario({"id": "s"}, model, bind(iface, {}))["hostiles"][0]
    # written as an empty list: a scenario without enemies (r139 no_target_hold, 11-11)
    assert bind_scenario({"id": "s", "hostiles": []}, model, b)["hostiles"] == []


def test_a_delayed_enemy_approach_waits_then_stops_short() -> None:
    """approach_start_s: the enemy stands still until then, then drives in and stops at
    stop_range_m (a tracking test: our formation holds, then the enemy advances)."""
    from afsim_research_agent.ir.v2.scenario_interface import bind, bind_scenario, derive
    from afsim_research_agent.ir.v2.sim.world import Hostile, World

    iface = derive("单车前出约 1 km 建立观察哨")
    kit = {
        "enemy": {
            "range_m": 9000.0,
            "approach_speed_mps": 5.0,
            "stop_range_m": 1000.0,
            "approach_start_s": 800.0,
            "count": 1,
        }
    }
    b = bind(iface, kit)
    assert (b.enemy_move_start_s, b.enemy_move_stop_s) == (800.0, 2400.0)
    h = bind_scenario({"id": "s"}, {"roles": {"car": {"role": {"count": 1}}}}, b)["hostiles"][0]
    assert (h["move_start_s"], h["move_stop_s"]) == (800.0, 2400.0)
    w = World()
    w.hostiles.append(Hostile("e1", 9000.0, 0.0, vx=-5.0, move_start_s=800.0, move_stop_s=2400.0))
    for _ in range(3000):
        w.step(1.0)
    assert abs(w.hostiles[0].x - 1000.0) < 1e-6 and w.hostiles[0].speed_at(w.now) == 0.0


def test_a_route_action_without_a_route_is_routed_by_who_wrote_it(contracts: Contracts) -> None:
    """Not written at all: a model error when the drafter wrote the action (repair), the
    kit's default when it sits in a tactic-set subtree (AFSIM decides) -- never a binding
    problem that stops the model from running (r3, 11-04: members never moved)."""
    iface = derive(R1).model_dump()
    out = compile_sheet(_sheet(""), contracts, model_id="op", scenario_interface=iface)
    res = normalize(out.spec, contracts)
    assert res.model is not None, [d.render() for d in res.diagnostics]
    issues = interface_param_issues(res.model)
    assert [(i.kind, i.role, i.state, i.origin) for i in issues] == [
        ("missing", "car", "GO", "model")
    ]
    assert ROUTING[issues[0].route] == "model" and "route=<接口名>" in issues[0].message()
    assert check_binding(res.model, binding_for(res.model)) == []
    model = res.model
    leaf = model["roles"]["car"]["states"]["GO"]["tree"]
    # the same action as part of a tactic-set subtree
    stack = [leaf]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("action") == "movement.follow_route":
                node["origin"] = "formation_standoff.formation_search_advance"
            stack.extend(v for v in node.values() if isinstance(v, dict | list))
        elif isinstance(node, list):
            stack.extend(node)
    sub = interface_param_issues(model)
    assert ROUTING[sub[0].route] == "kit_default" and "AFSIM" in sub[0].message()


def test_the_binding_reports_only_what_the_scenario_lacks(contracts: Contracts) -> None:
    iface = derive(R1).model_dump()
    spec = compile_sheet(
        _sheet("route=approach"), contracts, model_id="op", scenario_interface=iface
    ).spec
    model = normalize(spec, contracts).model
    assert interface_param_issues(model) == []
    bound = binding_for(model)
    bound.parameters.pop("rt_approach")
    problems = check_binding(model, bound)
    assert len(problems) == 1 and "rt_approach" in problems[0]


def test_the_kits_routes_follow_the_scenarios_ahead_point(contracts: Contracts) -> None:
    """A scenario that moves "ahead" moves the kit's own routes and endpoints with it (the
    formation search route stayed at the kit's 350 m in a 9 km scenario, r3 11-04)."""
    iface = derive(R1).model_dump()
    spec = compile_sheet(
        _sheet("route=approach"), contracts, model_id="op", scenario_interface=iface
    ).spec
    model = normalize(spec, contracts).model
    kit = {
        "meta": {"origin": [45.0, -121.0], "heading_deg": 90.0, "ahead_m": 350.0, "end_time_s": 80},
        "enemy": {
            "range_m": 9000.0,
            "platform": "platform {name} T side red position {lat} {lon} end_platform",
        },
        "binding": {"points": {"ahead": [12000.0, 0.0]}},
        "slices": {
            "movement": {
                "preamble": "route kit_route position {ahead_lat} {ahead_lon} end_route",
                "aux": "double far = {ahead_lon_deg}",
            }
        },
    }
    run = generate(model, kit, contracts)
    far = float(run.platforms[0].body.split("double far = ")[1].split()[0])
    assert far == pytest.approx(-121.0 + 12000.0 / (111_320.0 * 0.70710678), abs=1e-3)


def test_the_2d_sensor_range_follows_the_kit_and_defaults_to_the_lab_radar() -> None:
    """The 2D search sensor reaches as far as the lab radar in AFSIM (about 6 km) unless the
    kit says otherwise, so scenario timings agree between the two."""
    from afsim_research_agent.ir.v2.scenario_interface import bind, bind_scenario, derive

    iface = derive("单车前出约 1 km 建立观察哨")
    model = {"roles": {"car": {"role": {"count": 1}}}}
    sc = bind_scenario({"id": "s"}, model, bind(iface, {}))
    assert sc["vehicles"]["car"][0]["sensor"]["range_m"] == 6000.0
    kit = {"platform": {"sensor_range_m": 4500}}
    sc = bind_scenario({"id": "s"}, model, bind(iface, kit))
    assert sc["vehicles"]["car"][0]["sensor"]["range_m"] == 4500.0


def test_the_enemy_distance_follows_the_requirement_and_the_model() -> None:
    from afsim_research_agent.ir.v2.scenario_interface import (
        bind,
        derive,
        enemy_distance,
        model_thresholds,
        profile,
    )

    assert enemy_distance("东方约10至20公里处") == 10_000.0
    assert enemy_distance("北侧约 800 米") == 800.0
    search = {"roles": {"leader": {"states": {"S": {"action": "perception.start_search"}}}}}
    strike = {"roles": {"leader": {"states": {"S": {"action": "engagement.fire_selected_weapon"}}}}}
    advance = derive("纵队沿路线搜索前进，发现敌方目标后展开横队")
    # a searching advance without a stated distance: the enemy beyond the radar
    p = profile(advance, search, sensor_m=6000.0)
    assert p.enemy_range_m == 8000.0 and p.notes
    b = bind(advance, {}, search)
    assert b.enemy_range_m == 8000.0
    assert b.ahead_m == 5000.0  # the route ends inside radar range of the enemy
    assert b.end_s >= (8000.0 - 6000.0) / 8.0 + 180.0  # long enough to reach the contact
    # an engaging model keeps the enemy within weapon range
    assert profile(advance, strike).enemy_range_m is None
    assert bind(advance, {}, strike).enemy_range_m == 650.0
    # the requirement's distance wins; the model's decision distances must fit in the run
    stated = derive("向东搜索前进，东方约10至20公里处可能有一辆敌方装甲车")
    assert bind(stated, {}, search).enemy_range_m == 10_000.0
    standoff = {
        "parameters": {"withdraw_dist": {"value": 12_000, "unit": "m"}, "spacing_m": 60},
        **search,
    }
    assert model_thresholds(standoff) == {"withdraw_dist": 12_000.0}
    assert bind(stated, {}, standoff).enemy_range_m == 13_000.0
