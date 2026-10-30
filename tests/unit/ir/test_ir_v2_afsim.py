"""IR 2.0 → AFSIM 2.9 compiler and evaluator, on a synthetic capability set and binding.

mission.exe is not run here; the generated input is checked for the native structure the
probes confirmed, and the evaluator is checked on recorded consoles.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from afsim_research_agent.ir.__main__ import main as cli
from afsim_research_agent.ir.v2 import Contracts, mission_to_ir, normalize
from afsim_research_agent.ir.v2.afsim import Binding, CompileError, Compiler, RunConfig, judge
from afsim_research_agent.ir.v2.runtime import Instance

TACTICS = """
[tactic_condition."target.valid"]
expression = { bb = "sense.target.valid" }

[event."sense.watch"]
GO = { external = true }
FOUND = { expression = { bb = "sense.target.valid" } }
DONE = { tree = "success" }
"""

BINDING = """
[meta]
name = "demo"
model_root = "{root}"
includes = ["model/demo_scripts.txt"]
base_platform_type = "DEMO_VEHICLE"

[blackboard.files]
sense = "bindings/sense.yaml"

[instance]
request = "{model}_{role}"
on_initialize = 'Demo_Init(PLATFORM);'
state_entry = 'PLATFORM.SetAuxData("demo_state", "{state}");'

[action."sense.scan"]
kind = "script"
body = '''
if (Demo_Scan({platform}, {request})) { return "SUCCESS"; }
return "FAILURE:NO_SENSOR";
'''

[action."sense.pick"]
kind = "script"
body = '''
if (Demo_Pick({platform}, {request}, {owner})) { return "SUCCESS"; }
return "RUNNING";
'''
cancel = 'Demo_Cancel({platform}, {request});'

[action."nav.goto"]
kind = "native_leaf"
leaf = "demo_goto"
inputs = '''
{platform}.SetAuxData("demo_req", {request});
{platform}.SetAuxData("demo_lat", {p:latitude_deg});
'''
done = '{platform}.AuxDataString("demo_done") == {request}'
cancel = 'Demo_Stop({platform}, {request});'

[resource.target]
release = 'Demo_Release({platform}, "{resource}", {request});'
"""

RUN = """
id = "watch_run"
end_time_s = 20
includes = ["fixtures/demo_observer.txt"]
extra = '''
platform enemy DEMO_VEHICLE
   side red
end_platform
'''
[[platforms]]
name = "watcher"
role = "vehicle"
body = "side blue"
[[events]]
at_s = 1
event = "GO"
[expect]
sequence = ["IDLE", "SEARCHING", "DONE"]
[reference]
console = "output/reference.log"
state_pattern = 'DEMO\\|(?P<t>[^|]+)\\|(?P<state>[^|\\r\\n]+)'
"""


def _w(root: Path, rel: str, data) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


@pytest.fixture
def env(tmp_path: Path):
    root = tmp_path / "set"
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
                for s in ("sense", "nav")
            ],
        },
    )
    _w(
        root,
        "action_set/sense.yaml",
        {
            "resources": {"target": {"mode": "exclusive"}},
            "blackboard": {"fields": {"sense.target.valid": {"type": "boolean"}}},
            "actions": [
                {"id": "sense.scan", "status": "implemented"},
                {
                    "id": "sense.pick",
                    "status": "implemented",
                    "resources": [{"resource": "target", "mode": "exclusive"}],
                },
            ],
        },
    )
    _w(
        root,
        "action_set/nav.yaml",
        {
            "actions": [
                {
                    "id": "nav.goto",
                    "status": "implemented",
                    "parameters": {
                        "latitude_deg": {"type": "number", "units": "deg", "required": True}
                    },
                }
            ]
        },
    )
    _w(root, "tactic_set/nav.yaml", {})
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
                                "type": "selector",
                                "children": [
                                    {"type": "condition", "condition": "target.valid"},
                                    {
                                        "type": "timeout",
                                        "duration_s": 4,
                                        "child": {"type": "action", "action": "sense.pick"},
                                    },
                                ],
                            },
                        ],
                    },
                }
            ],
            "state_trees": [
                {"id": "sense.idle", "root": {"type": "condition", "condition": "target.valid"}},
                {"id": "sense.searching", "root": {"type": "subtree", "subtree": "sense.search"}},
            ],
            "missions": [
                {
                    "id": "sense.watch",
                    "initial_state": "IDLE",
                    "states": ["IDLE", "SEARCHING", "DONE"],
                    "terminal_states": ["DONE"],
                    "transitions": [
                        {"from": "IDLE", "to": "SEARCHING", "when": "GO"},
                        {"from": "SEARCHING", "to": "DONE", "when": "FOUND"},
                        {"from": "SEARCHING", "to": "DONE", "when": "DONE"},
                    ],
                }
            ],
        },
    )
    (root / "ir_alignment.toml").write_text(TACTICS, encoding="utf-8")
    model_root = tmp_path / "project"
    _w(
        model_root,
        "bindings/sense.yaml",
        {"blackboard": {"sense.target.valid": "demo_target_valid"}},
    )
    (tmp_path / "binding.toml").write_text(
        BINDING.replace("{root}", model_root.as_posix()), encoding="utf-8"
    )
    (tmp_path / "run.toml").write_text(RUN, encoding="utf-8")
    contracts = Contracts.load(root)
    return (
        contracts,
        Binding.load(tmp_path / "binding.toml"),
        RunConfig.load(tmp_path / "run.toml"),
        tmp_path,
    )


def _compile(env, spec=None):
    contracts, binding, run, _ = env
    raw = spec or mission_to_ir(contracts, "sense.watch")
    result = normalize(raw, contracts)
    assert result.ok, [d.render() for d in result.diagnostics]
    return Compiler(result.model, contracts, binding).package([run])


def test_state_machine_and_trees_use_native_structure(env) -> None:
    pkg = _compile(env)
    text = pkg.behavior
    assert "processor IR_SENSE_WATCH_VEHICLE_SM WSF_STATE_MACHINE" in text
    assert 'string currentState = "IR_BOOT";' in text and "next_state IDLE return true;" in text
    assert "platform_type IR_SENSE_WATCH_VEHICLE DEMO_VEHICLE" in text
    assert "include_once model/demo_scripts.txt" in text
    # one next_state per target: FOUND and DONE are merged into one block
    searching = text.split("   state SEARCHING")[1].split("   end_state")[0]
    assert searching.count("next_state DONE") == 1
    # a cycle of true transitions cannot hang the update: at most 8 state changes per update
    assert 'PLATFORM.SetAuxData("ir_hops", 0);' in text
    assert 'if (PLATFORM.AuxDataInt("ir_hops") >= 8)' in searching and "IR_LOOP|" in searching
    assert 'PLATFORM.AuxDataBool("demo_target_valid")' in searching
    assert 'PLATFORM.AuxDataString("ir_sense_watch_tree") == "success"' in searching
    # external event: set by the run file, consumed by the transition
    idle = text.split("   state IDLE")[1].split("   end_state")[0]
    assert 'PLATFORM.SetAuxData("ir_ev_go", false);' in idle
    assert 'writeln("IR_STATE|", TIME_NOW, "|", PLATFORM.Name(), "|IDLE");' in idle
    assert 'PLATFORM.SetAuxData("demo_state", "IDLE");' in idle
    # binding scripts become functions; leaves report RUNNING and are cancelled on exit
    assert "script string IR_SENSE_WATCH_VEHICLE_SEARCHING_" in text
    assert 'Demo_Pick(aPlatform, "sense_watch_vehicle", "sense_watch")' in text
    assert "Demo_Cancel(PLATFORM" in searching and 'Demo_Release(PLATFORM, "target"' in searching
    # each action leaf reports its first execution after the state was entered (obligations)
    assert 'writeln("IR_ACT|", TIME_NOW, "|", PLATFORM.Name(), "|' in text
    assert '_act", false);' in searching
    run_text = next(iter(pkg.runs.values()))
    assert "event_pipe\n   file output/$(CASE).aer\nend_event_pipe" in run_text
    assert list(pkg.manifest["recordings"].values())[0].endswith(".aer")
    # tactic-set timeout: the child, or a guard that fails when the time is up
    assert "_timeout" in searching and '{ return Failure("timeout"); }' in text
    assert "on_exit" in searching


def test_run_file_places_platforms_and_stimuli(env) -> None:
    pkg = _compile(env)
    run = pkg.runs["run_watch_run.txt"]
    assert "file_path ." in run and f"file_path {env[1].meta.model_root}" in run
    assert "include_once fixtures/demo_observer.txt" in run
    assert f"include_once {pkg.behavior_file}" in run
    assert "platform watcher IR_SENSE_WATCH_VEHICLE" in run and "platform enemy DEMO_VEHICLE" in run
    assert 'WsfSimulation.FindPlatform("watcher").SetAuxData("ir_ev_go", true);' in run
    assert "end_time 20.0 sec" in run


def test_composites_decorators_and_native_leaves(env) -> None:
    contracts, *_ = env
    spec = mission_to_ir(contracts, "sense.watch")
    spec["subtrees"] = [
        {
            "id": "mix",
            "root": {
                "type": "sequence_with_memory",
                "children": [
                    {
                        "type": "parallel",
                        "policy": {"type": "threshold", "count": 2},
                        "children": [
                            {"type": "action", "action": "sense.scan"},
                            {
                                "type": "action",
                                "action": "nav.goto",
                                "params": {"latitude_deg": 30.5},
                            },
                        ],
                    },
                    {
                        "type": "repeat",
                        "repeat": {"duration_s": 3},
                        "children": [{"type": "action", "action": "sense.scan"}],
                    },
                    {
                        "type": "repeat",
                        "repeat": {"count": 2},
                        "children": [{"type": "action", "action": "sense.scan"}],
                    },
                    {"type": "inverter", "children": [{"type": "action", "action": "sense.scan"}]},
                    {
                        "type": "weighted_random",
                        "children": [
                            {"type": "action", "action": "sense.scan", "weight": 0.3},
                            {"type": "action", "action": "sense.scan", "weight": 0.7},
                        ],
                    },
                ],
            },
        }
    ]
    spec["state_trees"]["vehicle"]["SEARCHING"] = "mix"
    text = _compile(env, spec).behavior
    for line in (
        "sequence_with_memory",
        "success_policy threshold 2",
        "decorator repeater for 3.0 sec",
        "decorator repeater repeat 2",
        "decorator inverter",
        "weighted_random",
        "precondition return 0.3; end_precondition",
        "behavior_node demo_goto",
        'PLATFORM.SetAuxData("demo_lat", 30.5);',
    ):
        assert line in text, line
    goto = text.split("_in\n")[1].split("end_advanced_behavior")[0]
    assert 'fresh = (PLATFORM.AuxDataString("demo_done") == req)' in goto


def test_missing_binding_entries_stop_the_compile(env) -> None:
    contracts, binding, run, _ = env
    del binding.action["sense.pick"]
    result = normalize(mission_to_ir(contracts, "sense.watch"), contracts)
    with pytest.raises(CompileError, match="action sense.pick"):
        Compiler(result.model, contracts, binding).package([run])


def test_gap_placeholders_stop_the_compile(env) -> None:
    contracts, binding, run, _ = env
    raw = mission_to_ir(contracts, "sense.watch")
    raw["gaps"] = {"jam": {"kind": "action", "description": "干扰", "stub": {}}}
    sid, tree = next(iter(raw["state_trees"].items()))
    state = next(iter(tree))
    raw["subtrees"] = [
        *raw.get("subtrees", []),
        {"id": "jam_tree", "root": {"type": "action", "action": "gap.jam"}},
    ]
    raw["state_trees"][sid][state] = "jam_tree"
    result = normalize(raw, contracts)
    assert result.ok, [d.render() for d in result.diagnostics]
    with pytest.raises(CompileError, match="GAP002.*jam"):
        Compiler(result.model, contracts, binding).package([run])


def test_package_goes_to_a_new_directory(env, tmp_path: Path) -> None:
    contracts, binding, run, _ = env
    result = normalize(mission_to_ir(contracts, "sense.watch"), contracts)
    compiler = Compiler(result.model, contracts, binding)
    out = tmp_path / "pkg"
    written = compiler.write(compiler.package([run]), out)
    assert {p.name for p in written} == {
        "ir_sense_watch_behavior.txt",
        "run_watch_run.txt",
        "manifest.json",
    }
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["model"] == "sense_watch" and "run_watch_run.txt" in manifest["runs"]
    with pytest.raises(CompileError, match="not empty"):
        compiler.write(compiler.package([run]), out)


CONSOLE = """Initializing simulation.
IR_STATE|0|watcher|IDLE
Starting simulation.
IR_STATE|1|watcher|SEARCHING
IR_STATE|5|watcher|DONE
Simulation complete
"""


def test_judge_compares_with_expectation_and_reference(env) -> None:
    run = env[2].model_dump()
    reference = "DEMO|0|IDLE\nDEMO|1|SEARCHING\nDEMO|4|DONE\n"
    j = judge(run, CONSOLE, reference)
    assert j.passed, j.render()
    assert [d["delta"] for d in j.time_deltas] == [0.0, 1.0]
    j = judge(run, CONSOLE.replace("DONE", "IDLE"), reference)
    assert not j.passed and any("differs from the handwritten" in p for p in j.problems)
    j = judge(
        run,
        CONSOLE.replace("Simulation complete", "***** FATAL: Could not process input files."),
        None,
    )
    assert not j.passed and j.errors and any("reference" in p for p in j.problems)
    run["expect"]["markers"] = [r"^CLEANUP\|1"]
    j = judge(run, CONSOLE, reference)
    assert not j.passed and any("CLEANUP" in p for p in j.problems)
    assert judge(run, CONSOLE + "CLEANUP|1\n", reference).passed
    looped = judge(run, CONSOLE + "CLEANUP|1\nIR_LOOP|3|vehicle_0|SEARCHING\n", reference)
    assert not looped.passed and any("SEARCHING within one update" in p for p in looped.problems)
    # time spent in each state against the reference (generated: SEARCHING for 4 s, the
    # reference 3 s)
    run["expect"]["max_interval_delta_s"] = 0.5
    j = judge(run, CONSOLE + "CLEANUP|1\n", reference)
    assert not j.passed and any("stayed 4s in SEARCHING" in p for p in j.problems), j.problems
    run["expect"]["max_interval_delta_s"] = 2.0
    assert judge(run, CONSOLE + "CLEANUP|1\n", reference).passed
    looped = judge(run, CONSOLE + "CLEANUP|1\nIR_LOOP|3|vehicle_0|SEARCHING\n", reference)
    assert not looped.passed and any("SEARCHING within one update" in p for p in looped.problems)


def test_cli_compile_and_judge(env, tmp_path: Path, capsys) -> None:
    contracts, binding, run, base = env
    out = tmp_path / "cli_pkg"
    args = [
        "v2-afsim",
        "compile",
        "--tactic",
        "sense.watch",
        "--binding",
        str(base / "binding.toml"),
        "--run",
        str(base / "run.toml"),
        "--out",
        str(out),
        "--capability-set",
        str(base / "set"),
    ]
    assert cli(args) == 0
    (out / "output" / "run_watch_run_console.log").write_text(CONSOLE, encoding="utf-8")
    ref = Path(binding.meta.model_root) / "output" / "reference.log"
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_text("DEMO|0|IDLE\nDEMO|1|SEARCHING\nDEMO|5|DONE\n", encoding="utf-8")
    assert cli(["v2-afsim", "judge", str(out)]) == 0
    assert "1/1 AFSIM runs passed" in capsys.readouterr().out
    assert json.loads((out / "output" / "report.json").read_text(encoding="utf-8"))[0]["passed"]


def test_runtime_chains_transitions_like_afsim(env) -> None:
    """Probes t01 / t02: tree, transition, and the new state's tree in one update."""
    contracts, *_ = env
    model = normalize(mission_to_ir(contracts, "sense.watch"), contracts).model

    class Host:
        def __init__(self) -> None:
            self.log: list[tuple[float, str]] = []

        def tick(self, inst, leaf, new):
            self.log.append((inst.now, leaf["action"]))
            return "success", None

        def cancel(self, inst, leaf):
            return None

    host = Host()
    inst = Instance("v", "vehicle", 0, model, contracts, host)
    inst.step(0.0)
    inst.raise_event("go")
    inst.blackboard["sense.target.valid"] = False
    inst.step(1.0)
    # IDLE's tree, the transition on GO, and SEARCHING's tree, all at t = 1
    assert inst.state == "DONE" and [t for t, _ in host.log] == [1.0, 1.0]
    assert [e["to"] for e in inst.trace] == ["SEARCHING", "DONE"]


def test_binding_hooks_fallback_parameters_and_external_flags(env, tmp_path: Path) -> None:
    contracts, binding, run, base = env
    text = (base / "binding.toml").read_text(encoding="utf-8")
    text = text.replace(
        'base_platform_type = "DEMO_VEHICLE"',
        'base_platform_type = "DEMO_VEHICLE"\nscripts = "script void Demo_Helper() end_script"\n'
        "platform_body = 'processor demo_rx DEMO_RX end_processor'",
    )
    text = text.replace("{p:latitude_deg}", "{p?:latitude_deg}")
    text = text.replace(
        "cancel = 'Demo_Stop({platform}, {request});'",
        "cancel = 'Demo_Stop({platform}, {request});'\nexit = 'Demo_Leave({platform});'\nenter = 'Demo_Enter(PLATFORM);'",
    )
    text += "\n[external]\ngo = 'PLATFORM.AuxDataBool(\"demo_go\")'\n"
    path = tmp_path / "hooks.toml"
    path.write_text(text, encoding="utf-8")
    hooked = Binding.load(path)
    spec = mission_to_ir(contracts, "sense.watch")
    spec["subtrees"] = [
        {
            "id": "go",
            "root": {"type": "action", "action": "nav.goto", "params": {"latitude_deg": 31.0}},
        }
    ]
    spec["state_trees"]["vehicle"]["SEARCHING"] = "go"
    model = normalize(spec, contracts).model
    pkg = Compiler(model, contracts, hooked).package([run])
    text = pkg.behavior
    assert "script void Demo_Helper() end_script" in text
    ptype = text.split("platform_type IR_")[1].split("end_platform_type")[0]
    assert "   processor demo_rx DEMO_RX end_processor" in ptype
    assert 'PLATFORM.SetAuxData("demo_lat", 31.0);' in text  # given: the literal
    searching = text.split("   state SEARCHING")[1].split("   end_state")[0]
    assert "Demo_Enter(PLATFORM);" in searching and "Demo_Leave(PLATFORM);" in searching
    idle = text.split("   state IDLE")[1].split("   end_state")[0]
    assert '(PLATFORM.AuxDataBool("ir_ev_go") || (PLATFORM.AuxDataBool("demo_go")))' in idle
    # a tactic-set leaf without the parameter reads the platform's ir_param_<name>
    spec["subtrees"][0]["root"] = {"type": "tactic", "tactic": "sense.search"}
    binding_fallback = Binding.load(path)
    binding_fallback.action["sense.scan"].body = "return {p?:latitude_deg};"
    contracts.actions["sense.scan"].parameters["latitude_deg"] = {"type": "number"}
    try:
        pkg = Compiler(normalize(spec, contracts).model, contracts, binding_fallback).package([run])
    finally:
        del contracts.actions["sense.scan"].parameters["latitude_deg"]
    assert 'return aPlatform.AuxDataDouble("ir_param_latitude_deg");' in pkg.behavior


def test_tactic_set_trees_resume_and_conditions_succeed(env) -> None:
    pkg = _compile(env)
    text = pkg.behavior
    # lab sequences/selectors resume at a running child; condition leaves have an execute
    assert "sequence_with_memory" in text and "selector_with_memory" in text
    cond = text.split("_cond\n")[1].split("end_advanced_behavior")[0]
    assert "execute return Success(); end_execute" in cond


def test_failure_as_running_and_trace(env, tmp_path: Path) -> None:
    contracts, binding, run, base = env
    sets = base / "set" / "tactic_set" / "sense.yaml"
    data = yaml.safe_load(sets.read_text(encoding="utf-8"))
    data["subtrees"][0]["no_target_result"] = "RUNNING"
    sets.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    fresh = Contracts.load(base / "set")
    model = normalize(mission_to_ir(fresh, "sense.watch"), fresh).model
    text = Compiler(model, fresh, binding, trace=True).package([run]).behavior
    assert "behavior_node ir_sense_watch_running" in text
    assert 'execute return Running("running"); end_execute' in text
    assert 'writeln("IR_TICK|", TIME_NOW, "|' in text
    tree = model["roles"]["vehicle"]["states"]["SEARCHING"]["tree"]
    assert tree["failure_as_running"] is True

    class Failing:
        def tick(self, inst, leaf, new):
            return "failure", "X"

        def cancel(self, inst, leaf):
            return None

    inst = Instance("v", "vehicle", 0, model, fresh, Failing())
    assert inst.tick(tree) == "running"


def test_tactic_set_parallel_and_repeat(env) -> None:
    contracts, binding, run, base = env
    sets = base / "set" / "tactic_set" / "sense.yaml"
    data = yaml.safe_load(sets.read_text(encoding="utf-8"))
    data["state_trees"][1]["root"] = {
        "type": "parallel",
        "success_policy": "one",
        "children": [
            {
                "type": "repeat",
                "until_done": True,
                "child": {"type": "action", "action": "sense.scan"},
            },
            {"type": "subtree", "subtree": "sense.search"},
        ],
    }
    sets.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    fresh = Contracts.load(base / "set")
    model = normalize(mission_to_ir(fresh, "sense.watch"), fresh).model
    tree = model["roles"]["vehicle"]["states"]["SEARCHING"]["tree"]
    assert tree["type"] == "parallel" and tree["policy"] == {"type": "one"}
    assert tree["children"][0]["repeat"] == {"until_done": True}
    text = Compiler(model, fresh, binding).package([run]).behavior
    assert "success_policy succeed_on_one" in text and "decorator repeater until_done" in text
