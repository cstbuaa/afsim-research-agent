"""Mechanical repair of ENTRY_TRUE transitions: the trigger waits until the state's
actions have written their fields (falcon.v3.autofix)."""

from __future__ import annotations

from afsim_research_agent.falcon.sheet.blocks import Block
from afsim_research_agent.falcon.v3.autofix import GUARD, entry_guards


def _blocks():
    return [
        Block("model", None, "model: {display_name: x}\n", 1),
        Block(
            "mermaid",
            "car",
            "stateDiagram-v2\n  [*] --> GO\n  GO --> HOLD : arrived\n"
            "  HOLD --> WATCH : near or seen\n  WATCH --> DONE : cancelled\n",
            5,
        ),
    ]


def test_entry_true_triggers_wait_for_the_state_actions() -> None:
    out, notes = entry_guards(
        _blocks(), [("car", "GO", "HOLD", "arrived"), ("car", "HOLD", "WATCH", "_c1")]
    )
    assert len(notes) == 2
    assert f"GO --> HOLD : arrived and {GUARD}" in out[1].text
    assert f"HOLD --> WATCH : (near or seen) and {GUARD}" in out[1].text
    assert "WATCH --> DONE : cancelled\n" in out[1].text


def test_nothing_matching_leaves_the_sheet_as_it_was() -> None:
    blocks = _blocks()
    out, notes = entry_guards(blocks, [("car", "GO", "OTHER", "arrived")])
    assert notes == [] and [b.text for b in out] == [b.text for b in blocks]


def _model_blocks(events: str = ""):
    return [
        Block(
            "model",
            None,
            "model: {display_name: x}\n"
            + events
            + "parameters:\n  lat: {value: 30}\n  lon: {value: 120}\n",
            1,
        ),
        Block(
            "mermaid",
            "car",
            "stateDiagram-v2\n  [*] --> GO\n  GO --> LOOK : arrived\n  LOOK --> FIRE : later\n",
            8,
        ),
        Block(
            "pytree",
            "car",
            "def GO():\n    movement.follow_route(route_name=r)\n\n"
            "def LOOK():\n    movement.hold_position(latitude_deg=lat, longitude_deg=lon, arrival_tolerance_m=5.0)\n",
            14,
        ),
    ]


def test_done_conditions_become_tree_events_declared_in_the_model() -> None:
    from afsim_research_agent.falcon.v3.autofix import completion_events

    out, notes = completion_events(
        _model_blocks("events:\n  cancelled: {source: external}\n"),
        [("car", "GO", "LOOK", "arrived")],
    )
    assert len(notes) == 1
    assert "GO --> LOOK : go_done" in out[1].text
    assert "events:\n  go_done: {source: tree, result: success" in out[0].text
    assert "cancelled: {source: external}" in out[0].text
    out, _ = completion_events(_model_blocks(), [("car", "GO", "LOOK", "arrived")])
    assert (
        out[0]
        .text.rstrip()
        .endswith("go_done: {source: tree, result: success, description: GO 的树完成}")
    )


def test_firing_by_time_waits_for_the_fire_order() -> None:
    from afsim_research_agent.falcon.v3.autofix import fire_orders

    out, notes = fire_orders(
        _model_blocks(), [("car", "LOOK", "FIRE", "later")], "fire_order", "开火命令"
    )
    assert notes and "LOOK --> FIRE : fire_order" in out[1].text
    assert "fire_order: {source: external, description: 开火命令}" in out[0].text


def test_invented_hold_coordinates_are_dropped_with_their_parameters() -> None:
    from afsim_research_agent.falcon.v3.autofix import held_here

    out, notes = held_here(_model_blocks())
    assert "movement.hold_position(arrival_tolerance_m=5.0)" in out[2].text
    assert "lat:" not in out[0].text and "lon:" not in out[0].text
    assert len(notes) == 3


def test_requirement_orders_become_external_events() -> None:
    from afsim_research_agent.falcon.v3.draft import command_events

    assert command_events("约束：未经命令不得开火。") == {"fire_order": "上级下达开火命令"}
    assert "deploy_order" in command_events("接到展开命令后正面向东展开")
    assert command_events("前出建立观察哨") == {}
