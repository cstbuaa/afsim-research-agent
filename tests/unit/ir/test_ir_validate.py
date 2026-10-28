from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from ir_specs import codes, confirmed, example_data, minimal_data

from afsim_research_agent.ir.validate import (
    load_evidence_index,
    validate_data,
)


def test_example_and_minimal_specs_validate() -> None:
    for data in (example_data(), minimal_data()):
        result = validate_data(data)
        assert result.ok, [d.render() for d in result.diagnostics if d.severity.value == "error"]


def test_schema_version_is_checked() -> None:
    data = minimal_data()
    data["schema_version"] = "2.0"
    assert codes(validate_data(data).diagnostics) == {"IR001"}
    del data["schema_version"]
    assert codes(validate_data(data).diagnostics) == {"IR001"}


def test_unknown_field_is_reported_with_path() -> None:
    data = minimal_data()
    data["roles"][0]["afsim_platform_type"] = "X"
    result = validate_data(data)
    assert not result.ok
    assert any(
        d.code == "IR002" and d.path == "roles[0].afsim_platform_type" for d in result.diagnostics
    )


def _mutate(
    fn: Callable[[dict[str, Any]], None], base: Callable[[], dict[str, Any]] = minimal_data
) -> set[str]:
    data = base()
    fn(data)
    return codes(validate_data(data).diagnostics)


def _set_trees(data: dict[str, Any], root: dict[str, Any]) -> None:
    data["trees"][0]["root"] = root


CASES: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
    ("bad identifier", lambda d: d["conditions"][0].update(id="Hostile-Near"), "ID001"),
    ("reserved separator", lambda d: d["conditions"][0].update(id="hostile__near"), "ID001"),
    ("duplicate behavior", lambda d: d["behaviors"].append(dict(d["behaviors"][0])), "ID001"),
    (
        "unknown condition",
        lambda d: d["conditions"][1].update(expression={"condition_ref": "nope"}),
        "REF001",
    ),
    (
        "unknown parameter unit",
        lambda d: d["parameters"]["contact_range"].update(unit="furlong"),
        "UNIT001",
    ),
    (
        "unit on integer",
        lambda d: d["parameters"].update(
            n={"type": "integer", "value": 2, "unit": "m", "basis": confirmed()}
        ),
        "UNIT001",
    ),
    (
        "quantity vs literal number",
        lambda d: d["conditions"][0]["expression"]["compare"].update(right={"literal": 900}),
        "UNIT001",
    ),
    (
        "string compared with distance",
        lambda d: d["conditions"][0]["expression"]["compare"].update(right={"literal": "far"}),
        "UNIT001",
    ),
    (
        "non-boolean expression",
        lambda d: d["conditions"][1].update(expression={"track_value": "hostile_count"}),
        "TYPE001",
    ),
    (
        "blackboard default type",
        lambda d: d["roles"][0]["blackboards"][0]["variables"][0].update(default="no"),
        "TYPE001",
    ),
    (
        "behavior never ends",
        lambda d: d["behaviors"][2].pop("success") and d["behaviors"][2].pop("failure"),
        "BEH001",
    ),
    (
        "missing issue policy",
        lambda d: d["behaviors"][1]["actions"][0].pop("issue_policy"),
        "BEH001",
    ),
    (
        "unknown action",
        lambda d: d["behaviors"][1]["actions"][0].update(command="raw_script"),
        "ACT001",
    ),
    (
        "unknown argument",
        lambda d: d["behaviors"][1]["actions"][0]["arguments"].update(
            altitude={"value": 1, "unit": "m"}
        ),
        "ACT001",
    ),
    (
        "altitude on ground vehicle",
        lambda d: d["behaviors"][1]["actions"].append(
            {
                "command": "set_altitude",
                "arguments": {"altitude": {"parameter_ref": "contact_range"}},
                "issue_policy": "once_per_activation",
            }
        ),
        "ACT001",
    ),
    (
        "whitelisted function only",
        lambda d: d["conditions"][1].update(
            expression={"function_call": {"name": "eval", "arguments": {}}}
        ),
        "ACT001",
    ),
    (
        "decorator child count",
        lambda d: _set_trees(
            d, {"node_id": "dec", "type": "decorator", "decorator": "inverter", "children": []}
        ),
        "TREE001",
    ),
    (
        "recursive subtree",
        lambda d: _set_trees(d, {"node_id": "loop", "type": "subtree", "tree": "patrol_tree"}),
        "TREE001",
    ),
    (
        "repeat without count",
        lambda d: _set_trees(
            d,
            {
                "node_id": "rep",
                "type": "decorator",
                "decorator": "repeat_count",
                "children": [{"node_id": "p", "type": "behavior", "behavior": "patrol"}],
            },
        ),
        "TREE001",
    ),
    (
        "parallel resource conflict",
        lambda d: _set_trees(
            d,
            {
                "node_id": "par",
                "type": "parallel",
                "success_policy": {"type": "all"},
                "failure_policy": {"type": "fail_on_one"},
                "children": [
                    {"node_id": "a", "type": "behavior", "behavior": "patrol"},
                    {"node_id": "b", "type": "behavior", "behavior": "observe"},
                ],
            },
        ),
        "RES001",
    ),
    (
        "missing fallback",
        lambda d: d["roles"][0]["state_machine"]["states"][0]["transitions"].pop(),
        "FSM001",
    ),
    (
        "unknown initial state",
        lambda d: d["roles"][0]["state_machine"].update(initial_state="ghost"),
        "FSM001",
    ),
    (
        "unreachable state",
        lambda d: d["roles"][0]["state_machine"]["states"].append(
            {
                "id": "orphan",
                "tree": "patrol_tree",
                "transitions": [
                    {"transition_id": "o_self", "to": "orphan", "when": {"literal": True}}
                ],
            }
        ),
        "FSM001",
    ),
    ("parameter without basis", lambda d: d["parameters"]["contact_range"].pop("basis"), "EVD001"),
    (
        "explicit without evidence",
        lambda d: d["parameters"]["contact_range"].update(
            basis={"status": "explicit", "evidence": []}
        ),
        "EVD001",
    ),
    (
        "inferred without rationale",
        lambda d: d["parameters"]["contact_range"].update(
            basis={"status": "inferred", "evidence": ["ev_1"]}
        ),
        "EVD001",
    ),
    (
        "conflicting needs two",
        lambda d: d["parameters"]["contact_range"].update(
            basis={"status": "conflicting", "evidence": ["ev_1"], "rationale": "x"}
        ),
        "EVD001",
    ),
    (
        "null value not unknown",
        lambda d: d["parameters"]["contact_range"].update(value=None),
        "PAR001",
    ),
    (
        "value above maximum",
        lambda d: d["parameters"]["contact_range"].update(maximum=0.5),
        "PAR001",
    ),
    ("superior undefined", lambda d: d["roles"][0].update(command={"superior": "hq"}), "ROLE001"),
    (
        "blackboard of another role",
        lambda d: d["conditions"][1].update(
            expression={"blackboard_value": "other.mission.withdraw_ordered"}
        ),
        "REF001",
    ),
    (
        "sensor not declared",
        lambda d: d["conditions"][1].update(
            expression={"sensor_value": {"sensor": "radar", "property": "on"}}
        ),
        "REF001",
    ),
    (
        "test references unknown node",
        lambda d: d["tests"][0]["expect"].update(selected_path=["ghost"]),
        "SCN001",
    ),
]


@pytest.mark.parametrize(("name", "mutation", "code"), CASES, ids=[c[0] for c in CASES])
def test_semantic_errors_are_reported(
    name: str, mutation: Callable[[dict[str, Any]], None], code: str
) -> None:
    assert code in _mutate(mutation)


def test_bare_numeric_literal_in_condition_warns() -> None:
    data = minimal_data()
    data["conditions"][1]["expression"] = {
        "compare": {
            "left": {"track_value": "hostile_count"},
            "operator": "gte",
            "right": {"literal": 3},
        }
    }
    result = validate_data(data)
    assert result.ok
    assert "EVD001" in codes(result.diagnostics, "warning")


def test_command_cycle_and_instance_range(tmp_path: Path) -> None:
    def cycle(d: dict[str, Any]) -> None:
        d["roles"][0]["command"] = {"superior": "wingman", "subordinates": ["wingman"]}

    assert "ROLE001" in _mutate(cycle, example_data)

    def out_of_range(d: dict[str, Any]) -> None:
        d["formations"][0]["slots"][0]["role_instance"] = "wingman[7]"

    assert "ROLE001" in _mutate(out_of_range, example_data)


def test_formation_rules() -> None:
    def duplicate(d: dict[str, Any]) -> None:
        d["formations"][0]["slots"][1]["role_instance"] = "wingman[0]"

    def no_tolerance(d: dict[str, Any]) -> None:
        d["formations"][0].pop("tolerance")

    def both_refs(d: dict[str, Any]) -> None:
        d["formations"][0]["slots"][0]["forward"]["literal_distance"] = {"value": 1, "unit": "m"}

    for mutation in (duplicate, no_tolerance, both_refs):
        assert "FORM001" in _mutate(mutation, example_data)


def test_coordination_rules() -> None:
    def unknown_pattern(d: dict[str, Any]) -> None:
        d["coordination"][0]["pattern"] = "swarm_magic"

    def follower_initiates_sync(d: dict[str, Any]) -> None:
        d["roles"][1]["state_machine"]["states"][0]["transitions"][0]["synchronize_with"] = (
            "coord_leader_follower"
        )

    def missing_state_for_sync(d: dict[str, Any]) -> None:
        states = d["roles"][1]["state_machine"]["states"]
        d["roles"][1]["state_machine"]["states"] = [s for s in states if s["id"] != "travel_urban"]

    def groups_overlap(d: dict[str, Any]) -> None:
        d["coordination"][1]["groups"]["overwatch"].append("wingman[2]")

    for mutation in (
        unknown_pattern,
        follower_initiates_sync,
        missing_state_for_sync,
        groups_overlap,
    ):
        assert "COORD001" in _mutate(mutation, example_data), mutation.__name__


def test_generated_ids_are_resolvable_and_typed() -> None:
    result = validate_data(example_data())
    assert result.expansion is not None
    generated = result.expansion.generated_from
    assert generated["coord_leader_follower__keep_station"] == "coord_leader_follower"
    assert generated["coord_bounding__tree"] == "coord_bounding"
    assert "coord_leader_follower__travel_open__to__contact" in generated


def test_scenario_rules() -> None:
    def bad_observable(d: dict[str, Any]) -> None:
        d["scenarios"][0]["expect"][0]["observable"] = "morale"

    def no_comm_event(d: dict[str, Any]) -> None:
        d["scenarios"] = [
            s
            for s in d["scenarios"]
            if not any(
                e["type"] in ("comm_degradation", "message_loss") for e in s.get("events", [])
            )
        ]

    def untraced_formation(d: dict[str, Any]) -> None:
        for s in d["scenarios"]:
            s["traces_to"] = [t for t in s["traces_to"] if t != "column"]

    def escaping_terrain_map(d: dict[str, Any]) -> None:
        d["scenarios"][0]["environment"]["terrain_map"] = "../../etc/passwd"

    for mutation in (bad_observable, no_comm_event, untraced_formation, escaping_terrain_map):
        assert "SCN001" in _mutate(mutation, example_data), mutation.__name__


def test_evidence_package_is_cross_checked(tmp_path: Path) -> None:
    package = tmp_path / "evidence_package.json"
    package.write_text(
        json.dumps(
            {
                "package_id": "crow_1",
                "evidence": [{"evidence_id": "ev_1"}],
                "sources": [{"source_id": "pdf:1"}],
            }
        ),
        encoding="utf-8",
    )
    index = load_evidence_index(package)
    data = minimal_data()
    data["provenance"]["evidence_package"] = {"package_id": "crow_1"}
    data["provenance"]["references"] = [{"source_id": "pdf:2", "kind": "crow_source"}]
    data["parameters"]["contact_range"]["basis"] = {
        "status": "explicit",
        "evidence": ["ev_1", "ev_9"],
        "confirmation": "pending",
    }
    result = validate_data(data, evidence=index)
    messages = [d.message for d in result.diagnostics if d.code == "REF001"]
    assert any("ev_9" in m for m in messages)
    assert any("pdf:2" in m for m in messages)
    assert not any("ev_1'" in m for m in messages)
