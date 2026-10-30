"""IR 2.0 runtime (AFSIM native node semantics), 2D simulation, L2 and L3.

The node-semantics cases replay the AFSIM 2.9 syntax probes (design §7): the same trees,
the same leaf behaviours, and the outputs mission.exe printed. A synthetic capability set
is used; the last test runs the local scenarios when the local capability set has them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from afsim_research_agent.ir.__main__ import main as cli
from afsim_research_agent.ir.v2 import Contracts, mission_to_ir, normalize
from afsim_research_agent.ir.v2.runtime import FAILURE, RUNNING, SUCCESS, Instance
from afsim_research_agent.ir.v2.sim import run_l2, simulate

ALIGNMENT = """
[sim_fields]
"selected.valid" = "sense.target.valid"
"selected.id" = "sense.target.id"
"selected.lost_at" = "sense.target.lost_at_s"
"sensor.on" = "self.sensor_on"
"move.range_to_target" = "nav.range_m"

[sim."nav.goto"]
model = "move_to_point"
[sim."nav.stop"]
model = "stop"
[sim."sense.on"]
model = "sensor_on"
[sim."sense.off"]
model = "sensor_off"
[sim."sense.scan"]
model = "update_tracks"
[sim."sense.pick"]
model = "select_nearest"
[sim."sense.keep"]
model = "maintain_selected"
[sim."sense.clear"]
model = "clear_selected"

[tactic_condition."target.valid"]
expression = { bb = "sense.target.valid" }

[event."sense.watch"]
GO = { external = true }
FOUND = { expression = { bb = "sense.target.valid" } }
LOST = { expression = { all = [ { not = { bb = "sense.target.valid" } }, { function_call = { name = "elapsed_since_gte", arguments = { field = "sense.target.lost_at_s", duration = { value = 2, unit = "s" } } } } ] } }
DONE = { tree = "success" }
"""


def _w(root: Path, rel: str, data) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _action(aid: str, **kw) -> dict:
    return {"id": aid, "status": "implemented", **kw}


@pytest.fixture
def contracts(tmp_path: Path) -> Contracts:
    root = tmp_path / "set"
    slices = ["probe", "nav", "sense"]
    _w(
        root,
        "index.yaml",
        {
            "format": "CapabilitySet",
            "catalog": {"id": "demo_set", "platform_classes": ["ground_vehicle"]},
            "slices": [
                {
                    "id": s,
                    "action_file": f"action_set/{s}.yaml",
                    "tactic_file": f"tactic_set/{s}.yaml",
                }
                for s in slices
            ],
        },
    )
    _w(
        root,
        "action_set/probe.yaml",
        {"actions": [_action(f"p.{n}") for n in ("ok", "fail", "run3", "after")]},
    )
    drive = [{"resource": "drive", "mode": "exclusive"}]
    _w(
        root,
        "action_set/nav.yaml",
        {
            "resources": {"drive": {"mode": "exclusive"}},
            "blackboard": {
                "fields": {"nav.range_m": {"type": "number", "units": "m", "nullable": True}}
            },
            "actions": [
                _action(
                    "nav.goto",
                    resources=drive,
                    parameters={
                        "latitude_deg": {"type": "number", "units": "deg", "required": True},
                        "longitude_deg": {"type": "number", "units": "deg", "required": True},
                        "arrival_tolerance_m": {"type": "number", "units": "m", "default": 5.0},
                        "target_speed_mps": {"type": "number", "units": "m/s", "default": 8.0},
                    },
                    success={"consecutive_samples": 2, "maximum_speed_mps": 0.1},
                ),
                _action("nav.stop", resources=drive, success={"consecutive_samples": 2}),
            ],
        },
    )
    target = {"resource": "target", "mode": "exclusive"}
    _w(
        root,
        "action_set/sense.yaml",
        {
            "resources": {"target": {"mode": "exclusive", "conflict_code": "TARGET_BUSY"}},
            "blackboard": {
                "fields": {
                    "sense.target.valid": {"type": "boolean"},
                    "sense.target.id": {"type": "string", "nullable": True},
                    "sense.target.lost_at_s": {"type": "number", "units": "s", "nullable": True},
                    "self.sensor_on": {"type": "boolean"},
                }
            },
            "actions": [
                _action("sense.on"),
                _action("sense.off"),
                _action("sense.scan"),
                _action("sense.pick", resources=[{**target, "release_on_terminal": False}]),
                _action(
                    "sense.keep",
                    resources=[{**target, "acquire": False, "release_on_terminal": False}],
                ),
                _action("sense.clear", resources=[{**target, "acquire": False}]),
            ],
        },
    )
    _w(root, "tactic_set/probe.yaml", {})
    _w(root, "tactic_set/nav.yaml", {})
    seq = lambda *a: {"type": "sequence", "children": [{"type": "action", "action": x} for x in a]}  # noqa: E731
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
                            {"type": "action", "action": "sense.on"},
                            {"type": "action", "action": "sense.scan"},
                            {
                                "type": "selector",
                                "children": [
                                    {"type": "condition", "condition": "target.valid"},
                                    {"type": "action", "action": "sense.pick"},
                                ],
                            },
                        ],
                    },
                }
            ],
            "state_trees": [
                {"id": "sense.idle", "root": {"type": "action", "action": "sense.clear"}},
                {"id": "sense.searching", "root": {"type": "subtree", "subtree": "sense.search"}},
                {"id": "sense.tracking", "root": seq("sense.scan", "sense.keep")},
                {"id": "sense.shutdown", "root": seq("sense.clear", "sense.off")},
            ],
            "missions": [
                {
                    "id": "sense.watch",
                    "initial_state": "IDLE",
                    "states": ["IDLE", "SEARCHING", "TRACKING", "SHUTDOWN", "COMPLETE"],
                    "terminal_states": ["COMPLETE"],
                    "transitions": [
                        {"from": "IDLE", "to": "SEARCHING", "when": "GO"},
                        {"from": "SEARCHING", "to": "TRACKING", "when": "FOUND"},
                        {"from": "TRACKING", "to": "SHUTDOWN", "when": "LOST"},
                        {"from": "SHUTDOWN", "to": "COMPLETE", "when": "DONE"},
                    ],
                }
            ],
        },
    )
    (root / "ir_alignment.toml").write_text(ALIGNMENT, encoding="utf-8")
    return Contracts.load(root)


# ---------------------------------------------------------------- native semantics
class ProbeHost:
    """The probe leaves: ok succeeds, fail fails, run3 runs until its third execution."""

    def __init__(self) -> None:
        self.log: list[tuple[float, str]] = []
        self.n = 0

    def tick(self, inst: Instance, leaf: dict[str, Any], new: bool):
        name = leaf["action"].split(".")[1]
        if name == "run3":
            self.n += 1
            self.log.append((inst.now, f"run3|{self.n}"))
            return (RUNNING if self.n < 3 else SUCCESS), None
        self.log.append((inst.now, name))
        return (FAILURE if name == "fail" else SUCCESS), None

    def cancel(self, inst: Instance, leaf: dict[str, Any]) -> None:
        return None


def _leaf(name: str) -> dict:
    return {"type": "action", "action": f"p.{name}"}


def _probe(contracts: Contracts, root: dict) -> list[tuple[float, str]]:
    spec = {
        "schema_version": "2.0",
        "model": {"id": "probe", "display_name": "probe", "version": "0.1.0", "intent": "probe"},
        "roles": [{"id": "p", "capability_class": "ground_vehicle"}],
        "events": [{"id": "stop", "source": "external"}],
        "subtrees": [{"id": "tree", "root": root}],
        "state_trees": {"p": {"S1": "tree"}},
        "missions": {
            "p": {
                "initial_state": "S1",
                "states": ["S1", "S2"],
                "terminal": ["S2"],
                "transitions": [{"from": "S1", "to": "S2", "on": "stop"}],
            }
        },
    }
    result = normalize(spec, contracts)
    assert result.ok, [d.render() for d in result.diagnostics]
    host = ProbeHost()
    inst = Instance("p_0", "p", 0, result.model, contracts, host)
    for t in range(7):  # the probes ran updates at t = 0 … 6 s
        inst.step(float(t))
    return host.log


def _each(*names: str) -> list[tuple[float, str]]:
    return [(float(t), n) for t in range(7) for n in names]


PROBES = {
    "r01_repeater_repeat": (
        {
            "type": "sequence_with_memory",
            "children": [
                {"type": "repeat", "repeat": {"count": 3}, "children": [_leaf("ok")]},
                _leaf("after"),
            ],
        },
        [(0.0, "ok"), (1.0, "ok"), (2.0, "ok")] + [(float(t), "after") for t in range(3, 7)],
    ),
    "r02_repeater_until_done": (
        {
            "type": "sequence_with_memory",
            "children": [
                {"type": "repeat", "repeat": {"until_done": True}, "children": [_leaf("run3")]},
                _leaf("after"),
            ],
        },
        [(0.0, "run3|1"), (1.0, "run3|2"), (2.0, "run3|3")]
        + [(float(t), "after") for t in range(2, 7)],
    ),
    "r03_repeater_for": (
        {
            "type": "sequence_with_memory",
            "children": [
                {"type": "repeat", "repeat": {"duration_s": 3}, "children": [_leaf("ok")]},
                _leaf("after"),
            ],
        },
        [(0.0, "ok"), (1.0, "ok"), (2.0, "ok")] + [(float(t), "after") for t in range(3, 7)],
    ),
    "r04_repeater_repeat_fail": (
        {
            "type": "sequence_with_memory",
            "children": [
                {"type": "repeat", "repeat": {"count": 3}, "children": [_leaf("fail")]},
                _leaf("after"),
            ],
        },
        [(0.0, "fail"), (1.0, "fail"), (2.0, "fail")] + [(float(t), "after") for t in range(3, 7)],
    ),
    "s01_parallel_success_policy_all": (
        {
            "type": "sequence_with_memory",
            "children": [
                {
                    "type": "parallel",
                    "policy": {"type": "all"},
                    "children": [_leaf("ok"), _leaf("run3")],
                },
                _leaf("after"),
            ],
        },
        [(0.0, "ok"), (0.0, "run3|1"), (1.0, "ok"), (1.0, "run3|2")]
        + [
            e
            for t in range(2, 7)
            for e in ((float(t), "ok"), (float(t), f"run3|{t + 1}"), (float(t), "after"))
        ],
    ),
    "s03_parallel_all_with_fail": (
        {
            "type": "sequence_with_memory",
            "children": [
                {
                    "type": "parallel",
                    "policy": {"type": "all"},
                    "children": [_leaf("fail"), _leaf("run3")],
                },
                _leaf("after"),
            ],
        },
        [e for t in range(7) for e in ((float(t), "fail"), (float(t), f"run3|{t + 1}"))],
    ),
    "q01_decorator_inverter": (
        {
            "type": "sequence",
            "children": [{"type": "inverter", "children": [_leaf("fail")]}, _leaf("after")],
        },
        _each("fail", "after"),
    ),
    "q02_decorator_succeeder": (
        {
            "type": "sequence",
            "children": [{"type": "succeeder", "children": [_leaf("fail")]}, _leaf("after")],
        },
        _each("after"),
    ),
    "q03_decorator_negator": (
        {
            "type": "sequence",
            "children": [{"type": "negator", "children": [_leaf("fail")]}, _leaf("after")],
        },
        [],
    ),
}


@pytest.mark.parametrize("probe", sorted(PROBES))
def test_node_semantics_match_the_afsim_probes(contracts: Contracts, probe: str) -> None:
    root, expected = PROBES[probe]
    assert _probe(contracts, root) == expected


def test_memoryless_sequence_restarts_and_selector_skips_failures(contracts: Contracts) -> None:
    log = _probe(
        contracts, {"type": "sequence", "children": [_leaf("ok"), _leaf("run3"), _leaf("after")]}
    )
    assert log[:5] == [(0.0, "ok"), (0.0, "run3|1"), (1.0, "ok"), (1.0, "run3|2"), (2.0, "ok")]
    log = _probe(
        contracts, {"type": "selector", "children": [_leaf("fail"), _leaf("ok"), _leaf("after")]}
    )
    assert log == _each("fail", "ok")


def test_priority_selector_runs_the_heaviest_child(contracts: Contracts) -> None:
    root = {
        "type": "priority_selector",
        "children": [{**_leaf("fail"), "weight": 0.2}, {**_leaf("ok"), "weight": 0.9}],
    }
    assert _probe(contracts, root) == _each("ok")


# ---------------------------------------------------------------- simulation
def _spec(**over: Any) -> dict:
    spec = {
        "schema_version": "2.0",
        "model": {"id": "drive", "display_name": "机动", "version": "0.1.0", "intent": "到点停车"},
        "roles": [{"id": "car", "capability_class": "ground_vehicle"}],
        "events": [{"id": "go", "source": "external"}],
        "subtrees": [
            {"id": "wait", "root": {"type": "action", "action": "nav.stop"}},
            {
                "id": "drive",
                "root": {
                    "type": "action",
                    "action": "nav.goto",
                    "params": {
                        "latitude_deg": 30.009,
                        "longitude_deg": 120.0,
                        "target_speed_mps": 10,
                    },
                },
            },
        ],
        "conditions": [
            {
                "id": "arrived",
                "expression": {
                    "compare": {
                        "left": {"bb": "nav.range_m"},
                        "operator": "lte",
                        "right": {"value": 5, "unit": "m"},
                    }
                },
            }
        ],
        "state_trees": {"car": {"WAIT": "wait", "DRIVE": "drive", "SHUTDOWN": "wait"}},
        "missions": {
            "car": {
                "initial_state": "WAIT",
                "states": ["WAIT", "DRIVE", "SHUTDOWN"],
                "transitions": [
                    {"from": "WAIT", "to": "DRIVE", "on": "go"},
                    {"from": "DRIVE", "to": "SHUTDOWN", "on": "arrived"},
                ],
            }
        },
        "tests": [
            {
                "id": "go_starts_driving",
                "given": {"role": "car", "state": "WAIT", "events": ["go"]},
                "expect": {"next_state": "DRIVE", "issued_actions": ["nav.stop", "nav.goto"]},
            },
            {
                "id": "drive_runs",
                "given": {"role": "car", "state": "DRIVE", "actions": {"nav.goto": "running"}},
                "expect": {"next_state": "DRIVE", "tree_status": "running"},
            },
            {
                "id": "wrong",
                "given": {"role": "car", "state": "DRIVE", "blackboard": {"nav.range_m": 1.0}},
                "expect": {"next_state": "DRIVE"},
            },
        ],
        "scenarios": [
            {
                "id": "drive_1km",
                "environment": {"origin_deg": [30.0, 120.0], "duration_s": 200},
                "events": [{"at_s": 3, "event": "go"}],
                "expect": [
                    {
                        "id": "arrives",
                        "role": "car",
                        "state_sequence": ["WAIT", "DRIVE", "SHUTDOWN"],
                    },
                    {"id": "in_time", "role": "car", "reaches": "SHUTDOWN", "within_s": 150},
                ],
            }
        ],
    }
    spec.update(over)
    return spec


def test_vehicle_drives_to_the_point_and_stops(contracts: Contracts) -> None:
    spec = _spec()
    model = normalize(spec, contracts).model
    result = simulate(model, contracts, spec["scenarios"][0])
    assert result.passed, result.render()
    assert not result.idealized and not result.unresolved


def test_mobility_fault_fails_the_action(contracts: Contracts) -> None:
    spec = _spec()
    spec["scenarios"][0]["faults"] = [{"at_s": 10, "role": "car", "mobility_available": False}]
    result = simulate(normalize(spec, contracts).model, contracts, spec["scenarios"][0])
    assert not result.passed and result.states["car_0"][-1][1] == "DRIVE"


def test_l2_single_steps(contracts: Contracts) -> None:
    spec = _spec()
    checks = run_l2(normalize(spec, contracts).model, contracts, spec["tests"])
    assert [c.passed for c in checks] == [True, True, False]
    assert "SHUTDOWN" in checks[2].detail


def test_gap_placeholder_runs_its_stub(contracts: Contracts) -> None:
    spec = _spec()
    spec["blackboard"] = {"mission.cleared": {"type": "bool", "default": False}}
    spec["gaps"] = {
        "clear_route": {
            "kind": "subtree",
            "description": "排除路障",
            "resources": ["drive"],
            "blackboard": {"writes": ["mission.cleared"]},
            "stub": {"success_after_s": 5, "writes": {"mission.cleared": True}},
        }
    }
    spec["subtrees"][1]["root"] = {
        "type": "sequence_with_memory",
        "children": [
            {"type": "subtree", "subtree": "gap.clear_route"},
            spec["subtrees"][1]["root"],
        ],
    }
    model = normalize(spec, contracts).model
    result = simulate(model, contracts, spec["scenarios"][0])
    assert result.passed, result.render()
    assert result.placeholders == {"clear_route"} and "idealized gaps" in result.render()
    entered = dict((s, t) for t, s in result.states["car_0"])
    # the drive starts only after the 5 s stub; arrival is later than without the gap
    plain = simulate(normalize(_spec(), contracts).model, contracts, _spec()["scenarios"][0])
    assert entered["SHUTDOWN"] >= dict((s, t) for t, s in plain.states["car_0"])["SHUTDOWN"] + 4
    checks = run_l2(
        model,
        contracts,
        [
            {
                "id": "stub_runs",
                "given": {"role": "car", "state": "DRIVE"},
                "expect": {"tree_status": "running", "issued_actions": ["gap.clear_route"]},
            },
            {
                "id": "outcome_override",
                "given": {
                    "role": "car",
                    "state": "DRIVE",
                    "actions": {"gap.clear_route": "failure"},
                },
                "expect": {"tree_status": "failure"},
            },
        ],
    )
    assert all(c.passed for c in checks), [c.detail for c in checks]


def test_tactic_mission_in_the_simulation(contracts: Contracts) -> None:
    model = normalize(mission_to_ir(contracts, "sense.watch"), contracts).model
    scenario = {
        "id": "watch",
        "environment": {"duration_s": 40},
        "vehicles": {"vehicle": [{"sensor": {"range_m": 1000}}]},
        "hostiles": [
            {"id": "far", "position_m": [0, 900], "appear_s": 5, "remove_s": 20},
            {"id": "out", "position_m": [0, 5000]},
        ],
        "events": [{"at_s": 1, "event": "GO"}],
        "expect": [
            {
                "id": "seq",
                "role": "vehicle",
                "state_sequence": ["IDLE", "SEARCHING", "TRACKING", "SHUTDOWN", "COMPLETE"],
            }
        ],
    }
    result = simulate(model, contracts, scenario)
    assert result.passed, result.render()
    times = dict((s, t) for t, s in result.states["vehicle_0"])
    assert times["TRACKING"] == 5.0  # target appears at 5; picked and transitioned that update
    assert times["SHUTDOWN"] == 24.0  # gone at 20, track stale at 22, loss declared 2 s later


def test_unmodelled_actions_and_triggers_are_reported(contracts: Contracts) -> None:
    spec = _spec()
    spec["subtrees"][0]["root"] = {"type": "action", "action": "p.ok"}
    spec["events"].append({"id": "ping", "source": "message"})
    spec["missions"]["car"]["transitions"].append({"from": "WAIT", "to": "SHUTDOWN", "on": "ping"})
    result = simulate(normalize(spec, contracts).model, contracts, spec["scenarios"][0])
    assert result.idealized == {"p.ok"} and any("ping" in u for u in result.unresolved)


def test_a_failed_request_keeps_no_ownership(contracts: Contracts) -> None:
    model = normalize(mission_to_ir(contracts, "sense.watch"), contracts).model
    inst = Instance("v", "vehicle", 0, model, contracts, ProbeHost())
    tree = model["roles"]["vehicle"]["states"]["SEARCHING"]["tree"]
    pick = tree["children"][0]["children"][2]["children"][1]

    class Fails(ProbeHost):
        def tick(self, inst, leaf, new):
            return FAILURE, "NO_VALID_TRACK"

    inst.host = Fails()
    assert inst.tick(pick) == FAILURE and "target" not in inst.owners
    inst.host = ProbeHost()
    assert inst.tick(pick) == SUCCESS and inst.owners["target"] == pick["node_id"]


def test_cli_runs_l2_and_l3(contracts: Contracts, tmp_path: Path, capsys) -> None:
    root = str(tmp_path / "set")
    spec = tmp_path / "drive.yaml"
    spec.write_text(yaml.safe_dump(_spec(), allow_unicode=True), encoding="utf-8")
    assert cli(["v2-test", str(spec), "--capability-set", root]) == 1
    assert "2/3 L2 tests passed" in capsys.readouterr().out
    assert cli(["v2-simulate", str(spec), "--capability-set", root]) == 0
    assert "1/1 scenarios passed" in capsys.readouterr().out


def test_local_tactic_scenarios_pass(capability_set) -> None:
    """Acceptance of IR 2.0 step 3 on the local capability set: its L3 scenario files."""
    from afsim_research_agent.capability import default_catalog_root

    root = default_catalog_root()
    files = sorted((root / "scenarios").glob("*.yaml"))
    if not files:
        pytest.skip("本机能力集没有 L3 想定")
    contracts = Contracts.load(root)
    for path in files:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        model = normalize(mission_to_ir(contracts, doc["mission"]), contracts).model
        for scenario in doc["scenarios"]:
            result = simulate(model, contracts, scenario)
            assert result.passed, result.render()
            assert not result.idealized and not result.unresolved, result.render()


def test_formation_models_converge_and_initial_values_apply(contracts: Contracts) -> None:
    from afsim_research_agent.ir.v2.sim.actions import (
        ActionContext,
        FormationSlots,
        StandoffCommand,
    )
    from afsim_research_agent.ir.v2.sim.world import Geo, Vehicle, World

    world = World(geo=Geo(30.0, 120.0), routes={})
    bb: dict = {}
    fields = {
        "formation.left_error": "f.left",
        "formation.right_error": "f.right",
        "formation.mode": "f.mode",
        "formation.range_zone": "f.zone",
        "selected.valid": "t.valid",
        "selected.range": "t.range",
    }
    shared: dict = {}

    def ctx(options):
        return ActionContext(world, Vehicle("v", 0, 0), bb, fields, {}, {}, options, shared)

    line = FormationSlots()
    opts = {"mode": "LINE", "initial_error_m": 40.0, "converge_mps": 4.0, "write_mode": True}
    assert line.start(ctx(opts))[0] == RUNNING and bb["f.left"] == 40.0 and bb["f.mode"] == "LINE"
    for _ in range(5):
        world.step(1.0)
        line.update(ctx(opts))
    assert bb["f.left"] == 20.0 and bb["f.right"] == 20.0
    column = FormationSlots()
    column.start(ctx({"mode": "COLUMN", "away_m": 999.0}))
    assert bb["f.left"] == 999.0
    line.start(ctx(opts))  # back to the line: the errors start over
    assert bb["f.left"] == 40.0
    bb.update({"t.valid": True, "t.range": 620.0})
    StandoffCommand().update(ctx({"desired_m": 500, "tolerance_m": 50}))
    assert bb["f.zone"] == "TOO_FAR"
    # [sim_initial] values seed the blackboard of every instance
    contracts.semantics.initial["nav.range_m"] = 1234.0
    spec = _spec()
    inst = Instance("car_0", "car", 0, normalize(spec, contracts).model, contracts, host=None)  # type: ignore[arg-type]
    assert inst.blackboard["nav.range_m"] == 1234.0


def test_formation_models_follow_the_contract_results_and_writes() -> None:
    """Capability set 1.1: steps succeed as the platform's do, and a model never writes a
    field its contract does not list (that would let a guessed condition pass)."""
    from afsim_research_agent.ir.v2.sim.actions import (
        ActionContext,
        FormationSlots,
        StandoffCommand,
    )
    from afsim_research_agent.ir.v2.sim.world import Geo, Vehicle, World

    world = World(geo=Geo(30.0, 120.0), routes={})
    bb: dict = {"f.left": 999.0}
    fields = {
        "formation.left_error": "f.left",
        "formation.right_error": "f.right",
        "formation.range_zone": "f.zone",
        "formation.command_mode": "f.cmd",
        "selected.valid": "t.valid",
        "selected.range": "t.range",
        "exec.status": "x.status",
    }
    shared: dict = {}

    def ctx(options, allowed=None):
        c = ActionContext(world, Vehicle("v", 0, 0), bb, fields, {}, {}, options, shared)
        c.allowed = allowed
        return c

    column = ctx({"mode": "COLUMN", "initial_error_m": 30.0, "success_within_m": 20.0}, set())
    model = FormationSlots()
    assert model.start(column)[0] == RUNNING
    assert bb["f.left"] == 999.0 and column.dropped == {"f.left", "f.right"}
    for _ in range(3):
        world.step(1.0)
        status, _ = model.update(column)
    assert status == SUCCESS  # 30 m - 3 s * 4 m/s = 18 m <= 20 m
    column.write("exec.status", "SUCCESS")  # execution status is always written
    assert bb["x.status"] == "SUCCESS"
    standoff = ctx({"desired_m": 500, "tolerance_m": 50}, {"f.zone", "f.cmd"})
    bb.update({"t.valid": False})
    assert StandoffCommand().update(standoff) == (FAILURE, "NO_TARGET")
    bb.update({"t.valid": True, "t.range": 300.0})
    assert StandoffCommand().update(standoff) == (SUCCESS, None)
    assert bb["f.zone"] == "TOO_CLOSE" and bb["f.cmd"] == "WITHDRAW"
