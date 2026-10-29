"""IR 2.0: schema, normalization, error codes, and conversion of tactic-set missions.

A synthetic capability set is used (no lab content); the last test runs the conversion
over the local capability set when it is present.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from afsim_research_agent.capability import CapabilityCatalog
from afsim_research_agent.ir.__main__ import main as cli
from afsim_research_agent.ir.__main__ import schema_text_v2
from afsim_research_agent.ir.v2 import Contracts, mission_to_ir, normalize
from afsim_research_agent.ir.v2.from_tactic import event_id

ROOT = Path(__file__).resolve().parents[3]


def _w(root: Path, rel: str, data) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


@pytest.fixture
def set_root(tmp_path: Path) -> Path:
    root = tmp_path / "set"
    _w(
        root,
        "index.yaml",
        {
            "format": "CapabilitySet",
            "format_version": "1.0.0",
            "catalog": {"id": "demo_set", "platform_classes": ["ground_vehicle"]},
            "slices": [
                {
                    "id": "nav",
                    "action_file": "action_set/nav.yaml",
                    "tactic_file": "tactic_set/nav.yaml",
                },
                {
                    "id": "sense",
                    "action_file": "action_set/sense.yaml",
                    "tactic_file": "tactic_set/sense.yaml",
                },
            ],
        },
    )
    _w(
        root,
        "action_set/nav.yaml",
        {
            "resources": {"drive": {"mode": "exclusive"}},
            "blackboard": {
                "fields": {
                    "self.speed_mps": {"type": "number", "units": "m/s"},
                    "nav.arrived": {"type": "boolean"},
                }
            },
            "actions": [
                {
                    "id": "nav.goto",
                    "status": "implemented",
                    "resources": [{"resource": "drive", "mode": "exclusive"}],
                    "parameters": {
                        "lat_deg": {
                            "type": "number",
                            "units": "deg",
                            "required": True,
                            "minimum": -90,
                            "maximum": 90,
                        },
                        "speed_mps": {"type": "number", "units": "m/s", "minimum": 0, "default": 5},
                    },
                },
                {
                    "id": "nav.hold",
                    "status": "implemented",
                    "resources": [{"resource": "drive", "mode": "exclusive"}],
                },
            ],
            "tactic_steps": [{"id": "nav.slot_step", "kind": "tactic_step", "used_by": ["nav"]}],
        },
    )
    _w(
        root,
        "action_set/sense.yaml",
        {
            "resources": {"selection": {"mode": "exclusive"}},
            "blackboard": {
                "fields": {
                    "sense.target.valid": {"type": "boolean"},
                    "sense.target.range_m": {"type": "number", "units": "m"},
                }
            },
            "actions": [
                {
                    "id": "sense.select",
                    "status": "implemented",
                    "resources": [
                        {"resource": "selection", "mode": "exclusive", "release_on_terminal": False}
                    ],
                },
                {"id": "sense.scan", "status": "implemented"},
            ],
        },
    )
    _w(
        root,
        "tactic_set/nav.yaml",
        {
            "subtrees": [
                {
                    "id": "nav.go_and_hold",
                    "root": {
                        "type": "sequence",
                        "children": [
                            {"type": "action", "action": "nav.goto"},
                            {"type": "action", "action": "nav.hold"},
                        ],
                    },
                }
            ],
            "state_trees": [
                {
                    "id": "nav.leader_moving",
                    "role": "LEADER",
                    "root": {"type": "subtree", "subtree": "nav.go_and_hold"},
                },
                {
                    "id": "nav.member_moving",
                    "role": "MEMBER",
                    "root": {"type": "action", "action": "nav.goto"},
                },
                {"id": "nav.shutdown", "root": {"type": "action", "action": "nav.hold"}},
            ],
            "missions": [
                {
                    "id": "nav.patrol",
                    "initial_state": "INIT",
                    "parameters": {"desired_range_m": 500},
                    "states": ["INIT", "LEADER_MOVING", "MEMBER_MOVING", "SHUTDOWN", "COMPLETE"],
                    "terminal_states": ["COMPLETE"],
                    "transitions": [
                        {"from": "INIT", "to": "LEADER_MOVING", "when": "role == LEADER"},
                        {"from": "INIT", "to": "MEMBER_MOVING", "when": "role == MEMBER"},
                        {"from": "LEADER_MOVING", "to": "SHUTDOWN", "when": "CANCELLED"},
                        {"from": "MEMBER_MOVING", "to": "SHUTDOWN", "when": "count >= 2"},
                        {"from": "SHUTDOWN", "to": "COMPLETE", "when": "CLEANUP_COMPLETE"},
                    ],
                }
            ],
        },
    )
    _w(
        root,
        "tactic_set/sense.yaml",
        {
            "subtrees": [
                {
                    "id": "sense.search",
                    "root": {
                        "type": "sequence",
                        "children": [
                            {"type": "action", "action": "sense.scan"},
                            {
                                "type": "timeout",
                                "duration_s": 2.0,
                                "child": {"type": "condition", "condition": "target.valid"},
                            },
                        ],
                    },
                }
            ],
            "state_trees": [],
            "missions": [],
        },
    )
    (root / "ir_alignment.toml").write_text(
        '[blackboard]\n"track_value.current_target_valid" = "sense.target.valid"\n',
        encoding="utf-8",
    )
    return root


@pytest.fixture
def contracts(set_root: Path) -> Contracts:
    return Contracts.load(set_root)


def _spec() -> dict:
    return {
        "schema_version": "2.0",
        "model": {"id": "demo", "display_name": "演示", "version": "0.1.0", "intent": "搜索后前进"},
        "roles": [{"id": "lead", "capability_class": "ground_vehicle"}],
        "parameters": {"rally_lat": {"type": "angle", "value": 30.5, "unit": "deg"}},
        "blackboard": {"mission.contact_reported": {"type": "bool", "default": False}},
        "conditions": [
            {"id": "target_seen", "expression": {"bb": "sense.target.valid"}},
            {
                "id": "target_close",
                "expression": {
                    "all": [
                        {"condition_ref": "target_seen"},
                        {
                            "compare": {
                                "left": {"bb": "sense.target.range_m"},
                                "operator": "lte",
                                "right": {"value": 1, "unit": "km"},
                            }
                        },
                    ]
                },
            },
        ],
        "events": [{"id": "cancelled", "source": "external"}],
        "subtrees": [
            {
                "id": "advance",
                "root": {
                    "type": "sequence_with_memory",
                    "children": [
                        {"type": "tactic", "tactic": "sense.search"},
                        {"type": "action", "action": "sense.select"},
                        {
                            "type": "action",
                            "action": "nav.goto",
                            "params": {
                                "lat_deg": "$rally_lat",
                                "speed_mps": {"value": 18, "unit": "km/h"},
                            },
                        },
                    ],
                },
            },
            {"id": "stop", "root": {"type": "action", "action": "nav.hold"}},
        ],
        "state_trees": {"lead": {"ADVANCE": "advance", "SHUTDOWN": "stop"}},
        "missions": {
            "lead": {
                "initial_state": "ADVANCE",
                "states": ["ADVANCE", "SHUTDOWN", "COMPLETE"],
                "terminal": ["COMPLETE"],
                "transitions": [
                    {"from": "ADVANCE", "to": "SHUTDOWN", "on": "target_close", "priority": 5},
                    {"from": "SHUTDOWN", "to": "COMPLETE", "on": "target_seen"},
                ],
                "global": [{"to": "SHUTDOWN", "on": "cancelled", "priority": 100}],
            }
        },
    }


def _codes(result) -> set[str]:
    return {d.code for d in result.diagnostics if d.severity.value == "error"}


def test_schema_file_is_current() -> None:
    path = ROOT / "src" / "afsim_research_agent" / "ir" / "schema" / "behavior_spec_v2.schema.json"
    assert path.read_text(encoding="utf-8") == schema_text_v2()


def test_a_valid_spec_normalizes_with_layers_filled_in(contracts: Contracts) -> None:
    result = normalize(_spec(), contracts)
    assert result.ok, [d.render() for d in result.diagnostics]
    model = result.model
    assert model["capability"]["set"] == "demo_set" and model["capability"]["slices"] == [
        "nav",
        "sense",
    ]
    assert {"sense.target.valid", "self.speed_mps", "mission.contact_reported"} <= set(
        model["blackboard"]
    )
    advance = model["roles"]["lead"]["states"]["ADVANCE"]
    tree = advance["tree"]
    assert tree["type"] == "sequence_with_memory"
    tactic = tree["children"][0]
    assert tactic["type"] == "tactic" and tactic["children"][0]["origin"] == "sense.search"
    assert tactic["children"][0]["children"][1]["type"] == "timeout"  # tactic content as written
    goto = tree["children"][2]
    assert goto["params"]["speed_mps"] == pytest.approx(5.0)  # 18 km/h in the contract unit
    assert goto["params"]["lat_deg"] == {"parameter_ref": "rally_lat"}
    leases = {lease["action"]: lease for lease in advance["leases"]}
    assert leases["nav.goto"]["request"] == "lead.ADVANCE.2"
    assert leases["nav.goto"]["release_on_exit"] == ["drive"]
    assert leases["sense.select"]["kept_on_exit"] == ["selection"]  # contract keeps ownership
    mission = model["roles"]["lead"]["mission"]
    assert mission["safe"] == ["SHUTDOWN"] and mission["global"][0]["to"] == "SHUTDOWN"


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda s: s["subtrees"][1]["root"].update(action="nav.fly"), "ACT001"),
        (lambda s: s["subtrees"][0]["root"]["children"][2]["params"].pop("lat_deg"), "ACT002"),
        (lambda s: s["subtrees"][0]["root"]["children"][2]["params"].update(lat_deg=120), "ACT002"),
        (lambda s: s["subtrees"][0]["root"]["children"][2]["params"].update(heading=1), "ACT002"),
        (
            lambda s: s["subtrees"][0]["root"]["children"][2]["params"].update(
                speed_mps={"value": 3, "unit": "m"}
            ),
            "UNIT001",
        ),
        (
            lambda s: s["subtrees"][0]["root"]["children"][0].update(tactic="sense.nothing"),
            "TAC001",
        ),
        (lambda s: s["subtrees"][0]["root"]["children"][0].update(params={"depth": 2}), "TAC001"),
        (lambda s: s["conditions"][0].update(expression={"bb": "sense.target.colour"}), "BB001"),
        (lambda s: s["blackboard"].update({"contact": {"type": "bool"}}), "BB001"),
        (lambda s: s["conditions"][0].update(expression={"self_value": "fuel_remaining"}), "BB001"),
        (
            lambda s: (
                s["missions"]["lead"]["transitions"].pop(0)
                and s["missions"]["lead"]["global"].clear()
            ),
            "FSM001",
        ),
        (
            lambda s: s["missions"]["lead"].update(
                states=["ADVANCE", "HALT", "SHUTDOWN", "COMPLETE"]
            ),
            "FSM002",
        ),
        (lambda s: s["state_trees"]["lead"].update(PARKED="stop"), "FSM002"),
        (lambda s: s["missions"]["lead"]["transitions"][0].update(on="never"), "REF001"),
        (
            lambda s: (
                s["subtrees"].append({"id": "loop", "root": {"type": "subtree", "subtree": "loop"}})
                or s["state_trees"]["lead"].update(ADVANCE="loop")
            ),
            "TREE001",
        ),
        (
            lambda s: s["subtrees"][1].update(
                root={
                    "type": "repeat",
                    "repeat": {"count": 2, "until_done": True},
                    "children": [{"type": "action", "action": "nav.hold"}],
                }
            ),
            "TREE001",
        ),
        (lambda s: s["subtrees"][1].update(root={"type": "inverter", "children": []}), "TREE001"),
    ],
)
def test_error_codes(contracts: Contracts, mutate, code: str) -> None:
    spec = _spec()
    mutate(spec)
    result = normalize(spec, contracts)
    assert code in _codes(result), [d.render() for d in result.diagnostics]


def test_parallel_branches_must_not_share_an_exclusive_resource(contracts: Contracts) -> None:
    spec = _spec()
    spec["subtrees"][1]["root"] = {
        "type": "parallel",
        "policy": {"type": "all"},
        "children": [
            {"type": "action", "action": "nav.hold"},
            {"type": "tactic", "tactic": "nav.go_and_hold"},
        ],
    }
    assert "ARB001" in _codes(normalize(spec, contracts))
    spec["subtrees"][1]["root"]["children"][1] = {"type": "action", "action": "sense.scan"}
    result = normalize(spec, contracts)
    assert result.ok and result.model["roles"]["lead"]["states"]["SHUTDOWN"]["tree"]["policy"] == {
        "type": "all"
    }


def test_needs_turn_an_unknown_action_into_a_gap(contracts: Contracts) -> None:
    spec = _spec()
    spec["subtrees"][1]["root"] = {"type": "action", "action": "nav.smoke_screen"}
    spec["needs"] = [{"action": "nav.smoke_screen", "description": "施放烟幕"}]
    result = normalize(spec, contracts)
    assert result.ok
    assert any(d.code == "ACT001" and d.severity.value == "warning" for d in result.diagnostics)


def test_v1_operands_read_the_aligned_blackboard_field(contracts: Contracts) -> None:
    spec = _spec()
    spec["conditions"][0]["expression"] = {"track_value": {"property": "current_target_valid"}}
    result = normalize(spec, contracts)
    assert result.ok and result.model["conditions"]["target_seen"] == {"bb": "sense.target.valid"}


def test_structure_errors_are_reported_without_a_model(contracts: Contracts) -> None:
    spec = _spec()
    spec["subtrees"][0]["root"]["type"] = "timeout"  # tactic-set only, not an IR node
    result = normalize(spec, contracts)
    assert result.model is None and _codes(result) == {"IR002"}
    assert _codes(normalize({**_spec(), "schema_version": "1.0"}, contracts)) == {"IR001"}


def test_tactic_set_mission_converts_per_role(contracts: Contracts) -> None:
    raw = mission_to_ir(contracts, "nav.patrol")
    assert set(raw["missions"]) == {"leader", "member"}
    leader = raw["missions"]["leader"]
    assert leader["states"] == ["INIT", "LEADER_MOVING", "SHUTDOWN", "COMPLETE"]
    assert raw["state_trees"]["leader"] == {
        "LEADER_MOVING": "nav.leader_moving",
        "SHUTDOWN": "nav.shutdown",
    }
    assert {e["id"] for e in raw["events"]} >= {"role_eq_leader", "count_gte_2", "cancelled"}
    assert raw["parameters"]["desired_range_m"]["type"] == "distance"
    result = normalize(raw, contracts)
    assert not result.ok and "FSM002" in _codes(result)  # INIT has no state tree in this set
    whole = copy.deepcopy(_spec())
    whole["roles"].append({"id": "wing", "capability_class": "ground_vehicle"})
    whole["missions"]["wing"] = {
        "tactic": "nav.patrol",
        "role": "MEMBER",
        "params": {"desired_range_m": 400},
    }
    whole["state_trees"]["wing"] = {"INIT": "stop"}
    result = normalize(whole, contracts)
    assert result.ok, [d.render() for d in result.diagnostics]
    assert (
        result.model["roles"]["wing"]["states"]["MEMBER_MOVING"]["tree_ref"] == "nav.member_moving"
    )
    whole["missions"]["wing"]["params"] = {"depth": 1}
    assert "TAC001" in _codes(normalize(whole, contracts))


def test_event_ids() -> None:
    assert event_id("fresh_message_count >= 2") == "fresh_message_count_gte_2"
    assert event_id("CANCELLED") == "cancelled" and event_id("3 > x") == "e_3_gt_x"


def test_cli_converts_into_a_new_directory(set_root: Path, tmp_path: Path, capsys) -> None:
    out = tmp_path / "converted"
    assert (
        cli(["v2-from-tactic", "nav.patrol", "--capability-set", str(set_root), "--out", str(out)])
        == 1
    )
    assert (out / "nav_patrol.behavior_spec.yaml").exists()
    assert "0/1 missions pass" in capsys.readouterr().out
    assert (
        cli(["v2-from-tactic", "nav.patrol", "--capability-set", str(set_root), "--out", str(out)])
        == 1
    )
    assert "not empty" in capsys.readouterr().out


def test_every_tactic_set_mission_converts_and_passes(capability_set: CapabilityCatalog) -> None:
    """Acceptance of IR 2.0 step 2 on the local capability set."""
    contracts = Contracts(capability_set)
    missions = [e.id for e in contracts.tactics.values() if e.section == "missions"]
    assert missions
    for mission_id in missions:
        result = normalize(mission_to_ir(contracts, mission_id), contracts)
        assert result.ok, (mission_id, [d.render() for d in result.diagnostics])
