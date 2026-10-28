from __future__ import annotations

import copy
import math
from typing import Any

import pytest
from ir_specs import codes, example_data, minimal_data

from afsim_research_agent.ir.l2 import run_test, run_tests
from afsim_research_agent.ir.normalize import normalize, verify_hash
from afsim_research_agent.ir.runtime import DictWorld, RoleController
from afsim_research_agent.ir.validate import validate_data


def _ir(data: dict[str, Any], *, draft: bool = False) -> dict[str, Any]:
    result = normalize(validate_data(data), draft=draft)
    assert result.ir is not None, [d.render() for d in result.diagnostics]
    return result.ir


def test_units_aliases_claims_and_ordering_are_normalized() -> None:
    ir = _ir(minimal_data())
    assert ir["parameters"]["contact_range"]["value"] == 1000.0
    assert ir["parameters"]["contact_range"]["unit"] == "m"
    assert ir["parameters"]["contact_range"]["original"] == {"value": 1, "unit": "km"}
    assert math.isclose(ir["parameters"]["patrol_speed"]["value"], 20 / 3.6)
    patrol = next(b for b in ir["behaviors"] if b["id"] == "patrol")
    assert patrol["actions"][0]["command"] == "move_to_position"
    assert patrol["timeout"] == {"seconds": 600.0, "result": "failure"}
    transitions = ir["roles"][0]["state_machine"]["states"][0]["transitions"]
    assert [t["priority"] for t in transitions] == sorted(
        (t["priority"] for t in transitions), reverse=True
    )
    assert transitions[-1]["fallback"] is True
    assert {r["id"] for r in ir["resources"]} >= {"movement_control", "comm_control"}


def test_claims_are_supplemented_from_catalog() -> None:
    data = minimal_data()
    data["behaviors"][2]["claims"] = []
    ir = _ir(data)
    withdraw = next(b for b in ir["behaviors"] if b["id"] == "withdraw")
    assert withdraw["claims"] == ["movement_control"]


def test_hash_is_stable_and_detects_tampering() -> None:
    first, second = _ir(minimal_data()), _ir(minimal_data())
    assert first["ir_hash"] == second["ir_hash"]
    assert verify_hash(first)
    tampered = copy.deepcopy(first)
    tampered["parameters"]["contact_range"]["value"] = 5.0
    assert not verify_hash(tampered)


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (
            lambda d: d["parameters"]["contact_range"].update(
                basis={"status": "explicit", "evidence": ["ev_1"], "confirmation": "rejected"}
            ),
            "PAR001",
        ),
        (
            lambda d: d["parameters"]["contact_range"].update(
                value=None, basis={"status": "unknown", "evidence": []}
            ),
            "PAR001",
        ),
        (
            lambda d: d["assumptions"].append(
                {"id": "a1", "statement": "s", "requires_confirmation": True}
            ),
            "ASM001",
        ),
        (
            lambda d: d["roles"][0]["state_machine"]["states"][0]["transitions"][0].update(
                basis={
                    "status": "inferred",
                    "evidence": ["ev_1"],
                    "rationale": "r",
                    "confirmation": "rejected",
                }
            ),
            "EVD002",
        ),
    ],
)
def test_confirmation_gate_blocks_normalization(mutation, code: str) -> None:
    data = minimal_data()
    mutation(data)
    validation = validate_data(data)
    assert validation.ok
    result = normalize(validation)
    assert result.ir is None
    assert code in codes(result.diagnostics)
    draft = normalize(validation, draft=True)
    assert draft.ir is not None and draft.ir["draft"] is True
    assert code in codes(draft.diagnostics, "warning")


def test_unconfirmed_parameters_pass_as_reference_values() -> None:
    from afsim_research_agent.ir.references import reference_values

    data = minimal_data()
    data["parameters"]["contact_range"].update(basis={"status": "unknown", "evidence": []})
    validation = validate_data(data)
    result = normalize(validation)
    assert result.ir is not None and result.ir["draft"] is False
    assert "PAR002" in codes(result.diagnostics, "info")
    refs = reference_values(result.ir)
    assert [(r.name, r.grounded) for r in refs] == [("contact_range", False)]
    # strict mode restores the old gate
    strict = normalize(validation, strict_parameters=True)
    assert strict.ir is None and "PAR001" in codes(strict.diagnostics)


def test_example_l2_tests_pass_with_full_transition_coverage() -> None:
    report = run_tests(_ir(example_data()))
    assert report.passed, [o.failures for o in report.outcomes if not o.passed]
    assert all(not cov["missing"] for cov in report.coverage.values())


def test_minimal_l2_tests_pass() -> None:
    assert run_tests(_ir(minimal_data())).passed


def test_failing_expectation_and_missing_coverage_are_reported() -> None:
    data = minimal_data()
    data["tests"][2]["expect"]["next_state"] = "patrol"
    data["tests"] = [t for t in data["tests"] if t["id"] != "withdraw_completes"]
    report = run_tests(_ir(data))
    assert not report.passed
    found = codes(report.diagnostics)
    assert {"TEST001", "SCN001"} <= found
    assert report.coverage["scout"]["missing"] == ["withdraw_done"]


def _controller(ir: dict[str, Any]) -> tuple[RoleController, DictWorld]:
    from afsim_research_agent.ir.catalog import load_catalog
    from afsim_research_agent.ir.l2 import defaults_from_catalog

    return RoleController(ir, "scout"), DictWorld({}, defaults_from_catalog(load_catalog()))


def test_once_per_activation_and_halting() -> None:
    ir = _ir(minimal_data())
    ctl, world = _controller(ir)
    first = ctl.tick(0.0, world)
    second = ctl.tick(1.0, world)
    assert [a.command for a in first.issued_actions] == ["move_to_position"]
    assert second.issued_actions == []  # same activation
    near = DictWorld({"track_value": {"nearest_hostile_range": 500.0}}, world.defaults)
    third = ctl.tick(2.0, near)  # selector switches branch -> patrol halted
    assert [a.command for a in third.issued_actions] == ["halt"]
    fourth = ctl.tick(3.0, world)  # patrol re-activated -> re-issued
    assert [a.command for a in fourth.issued_actions] == ["move_to_position"]


def test_wait_and_timeout_semantics() -> None:
    ir = _ir(minimal_data())
    ctl, world = _controller(ir)
    near = DictWorld({"track_value": {"nearest_hostile_range": 500.0}}, world.defaults)
    assert ctl.tick(0.0, near).tree_status == "running"
    assert ctl.tick(29.0, near).tree_status == "running"
    assert ctl.tick(30.0, near).tree_status == "success"  # wait(observe_time) elapsed
    ctl2, _ = _controller(ir)
    assert ctl2.tick(0.0, world).tree_status == "running"
    assert ctl2.tick(600.0, world).tree_status == "failure"  # patrol timeout


def _tree_ir(root: dict[str, Any]) -> dict[str, Any]:
    data = minimal_data()
    data["trees"][0]["root"] = root
    data["tests"] = [t for t in data["tests"] if t["id"] not in ("patrols", "observes_contact")]
    return _ir(data)


def _beh(node_id: str, behavior: str = "patrol") -> dict[str, Any]:
    return {"node_id": node_id, "type": "behavior", "behavior": behavior}


def test_sequence_with_memory_skips_completed_children() -> None:
    data = minimal_data()
    data["behaviors"].append(
        {
            "id": "count",
            "actions": [
                {
                    "command": "set_blackboard",
                    "arguments": {"key": "mission.observations", "value": {"literal": 1}},
                }
            ],
            "success": {"expression": {"literal": True}},
        }
    )
    data["trees"][0]["root"] = {
        "node_id": "mem",
        "type": "sequence_with_memory",
        "children": [_beh("c", "count"), _beh("p")],
    }
    data["tests"] = [t for t in data["tests"] if t["id"] not in ("patrols", "observes_contact")]
    ir = _ir(data)
    ctl, world = _controller(ir)
    assert ctl.tick(0.0, world).selected_path == ["mem", "p"]
    ctl.blackboard["mission"]["observations"] = 0
    second = ctl.tick(1.0, world)
    assert second.selected_path == ["mem", "p"]
    assert ctl.blackboard["mission"]["observations"] == 0  # count not re-run


def test_decorators_and_parallel() -> None:
    ir = _tree_ir(
        {
            "node_id": "inv",
            "type": "decorator",
            "decorator": "inverter",
            "children": [{"node_id": "near", "type": "condition", "condition": "hostile_near"}],
        }
    )
    ctl, world = _controller(ir)
    assert ctl.tick(0.0, world).tree_status == "success"

    ir = _tree_ir(
        {
            "node_id": "rep",
            "type": "decorator",
            "decorator": "repeat_count",
            "count": 2,
            "children": [{"node_id": "ok", "type": "condition", "condition": "hostile_near"}],
        }
    )
    ctl, _ = _controller(ir)
    near = DictWorld({"track_value": {"nearest_hostile_range": 1.0}}, world.defaults)
    assert ctl.tick(0.0, near).tree_status == "running"
    assert ctl.tick(1.0, near).tree_status == "success"

    ir = _tree_ir(
        {
            "node_id": "par",
            "type": "parallel",
            "success_policy": {"type": "threshold", "count": 1},
            "failure_policy": {"type": "fail_on_all"},
            "children": [
                {"node_id": "c1", "type": "condition", "condition": "hostile_near"},
                {"node_id": "c2", "type": "condition", "condition": "withdraw_ordered"},
            ],
        }
    )
    ctl, _ = _controller(ir)
    assert ctl.tick(0.0, world).tree_status == "failure"
    ctl, _ = _controller(ir)
    assert ctl.tick(0.0, near).tree_status == "success"


def test_message_received_is_an_event_and_value_persists() -> None:
    ir = _ir(example_data())
    test = next(t for t in ir["tests"] if t["id"] == "wingman_keeps_station")
    outcome = run_test(ir, test)
    assert outcome.passed
    from afsim_research_agent.ir.runtime import Delivery

    ctl = RoleController(ir, "wingman", 0)
    from afsim_research_agent.ir.catalog import load_catalog
    from afsim_research_agent.ir.l2 import defaults_from_catalog

    world = DictWorld({}, defaults_from_catalog(load_catalog()))
    msg = "coord_leader_follower__formation"
    ctl.tick(0.0, world, [Delivery(msg, {"formation": "column"})])
    assert msg in ctl.inbox_ids
    result = ctl.tick(1.0, world)
    assert msg not in ctl.inbox_ids
    assert ctl.last_values[msg]["formation"] == "column"
    assert result.issued_actions[-1].arguments["formation"] == "column"


def _example_ctl(
    ir: dict[str, Any], role: str, instance: int = 0
) -> tuple[RoleController, DictWorld]:
    from afsim_research_agent.ir.catalog import load_catalog
    from afsim_research_agent.ir.l2 import defaults_from_catalog

    return RoleController(ir, role, instance), DictWorld({}, defaults_from_catalog(load_catalog()))


def test_leader_rebroadcasts_synchronized_state_and_formation() -> None:
    ir = _ir(example_data())
    coord = next(c for c in ir["coordination"] if c["id"] == "coord_leader_follower")
    assert coord["resync_interval_s"] == 5.0
    ctl, world = _example_ctl(ir, "platoon_leader")
    sent: list[tuple[float, str, dict[str, Any]]] = []
    for t in range(0, 12):
        for msg in ctl.tick(float(t), world).sent_messages:
            sent.append((float(t), msg.message, msg.fields))
    transitions = [(t, f["to_state"]) for t, m, f in sent if m.endswith("__transition")]
    formations = [(t, f["formation"]) for t, m, f in sent if m.endswith("__formation")]
    assert transitions == [(5.0, "travel_open"), (10.0, "travel_open")]
    assert formations == [(0.0, "wedge"), (5.0, "wedge"), (10.0, "wedge")]


def test_follower_ignores_rebroadcast_of_its_current_state() -> None:
    from afsim_research_agent.ir.runtime import Delivery

    ir = _ir(example_data())
    ctl, world = _example_ctl(ir, "wingman", 1)
    msg = Delivery(
        "coord_leader_follower__transition", {"to_state": "travel_open"}, "platoon_leader", 0
    )
    result = ctl.tick(0.0, world, [msg])
    assert result.next_state == "travel_open" and result.fired_transition == "wing_open_self"
    msg = Delivery(
        "coord_leader_follower__transition", {"to_state": "travel_urban"}, "platoon_leader", 0
    )
    assert ctl.tick(1.0, world, [msg]).next_state == "travel_urban"


def test_resync_can_be_disabled() -> None:
    data = example_data()
    data["coordination"][0]["resync_interval"] = {"value": 0, "unit": "s"}
    ir = _ir(data)
    ctl, world = _example_ctl(ir, "platoon_leader")
    messages = [m.message for t in range(12) for m in ctl.tick(float(t), world).sent_messages]
    assert messages == ["coord_leader_follower__formation"]


def test_bounding_stops_at_standoff_and_aims_at_threat() -> None:
    ir = _ir(example_data())
    bound = next(b for b in ir["behaviors"] if b["id"] == "coord_bounding__bound")
    args = bound["actions"][0]["arguments"]
    assert args["toward"] == {"kind": "id", "value": "nearest_hostile"}
    assert args["stop_short"] == {"kind": "parameter", "name": "standoff_distance"}
    assert bound["failure"] == {"op": "condition_ref", "id": "coord_bounding__standoff_reached"}
    ctl, _ = _example_ctl(ir, "wingman", 2)
    ctl.state = "contact"
    ctl.blackboard["mission"]["contact_reported"] = True
    from afsim_research_agent.ir.catalog import load_catalog
    from afsim_research_agent.ir.l2 import defaults_from_catalog

    near = DictWorld(
        {"track_value": {"nearest_hostile_range": 805.0}}, defaults_from_catalog(load_catalog())
    )
    far = DictWorld(
        {"track_value": {"nearest_hostile_range": 1200.0}}, defaults_from_catalog(load_catalog())
    )
    assert [a.command for a in ctl.tick(0.0, far).issued_actions] == ["bound_to_position"]
    assert [a.command for a in ctl.tick(1.0, near).issued_actions] == ["hold_overwatch_position"]


def test_bounding_without_stop_condition_warns() -> None:
    data = example_data()
    data["coordination"][1].pop("standoff_distance")
    result = validate_data(data)
    assert result.ok
    assert any(d.code == "COORD001" and "stop condition" in d.message for d in result.diagnostics)
