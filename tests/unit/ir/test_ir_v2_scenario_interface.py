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
    ScenarioInterface,
    bind,
    binding_for,
    check_binding,
    derive,
    interface_of,
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
    assert east == pytest.approx(325.0) and abs(north) < 1e-6  # half of 650 m, due east
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
    assert b.enemies_plane[0][0] == pytest.approx(1800.0)


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
    assert any("follow_route" in p for p in check_binding(model, binding_for(model)))
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
