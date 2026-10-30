"""Falcon v3: tactic sheet v3 → IR 2.0 (compiler, source map, drafting loop).

A synthetic capability set is used (no lab content).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
import types
from pathlib import Path

import pytest
import yaml

from afsim_research_agent.falcon.v3.compiler import compile_sheet
from afsim_research_agent.ir.v2 import Contracts, normalize


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
    drive = [{"resource": "drive", "mode": "exclusive"}]
    _w(
        root,
        "action_set/nav.yaml",
        {
            "resources": {"drive": {"mode": "exclusive"}},
            "blackboard": {
                "fields": {
                    "self.speed_mps": {"type": "number", "units": "m/s"},
                    "nav.range_m": {"type": "number", "units": "m"},
                }
            },
            "actions": [
                {
                    "id": "nav.goto",
                    "status": "implemented",
                    "description": "Drive to a point.",
                    "resources": drive,
                    "parameters": {
                        "lat_deg": {"type": "number", "units": "deg", "required": True},
                        "lon_deg": {"type": "number", "units": "deg", "required": True},
                        "speed_mps": {"type": "number", "units": "m/s", "minimum": 0, "default": 5},
                    },
                },
                {"id": "nav.hold", "status": "implemented", "resources": drive},
                {"id": "nav.stop", "status": "implemented", "resources": drive},
            ],
        },
    )
    _w(
        root,
        "action_set/sense.yaml",
        {
            "resources": {"sensor": {"mode": "exclusive"}},
            "blackboard": {
                "fields": {
                    "sense.target.valid": {"type": "boolean"},
                    "sense.mode": {"type": "string", "enum": ["SEARCH", "TRACK"]},
                    "sense.request.id": {"type": "string"},
                    "sense.label": {"type": "string"},
                }
            },
            "actions": [
                {
                    "id": "sense.scan",
                    "status": "implemented",
                    "resources": [{"resource": "sensor", "mode": "exclusive"}],
                },
                {"id": "sense.off", "status": "implemented"},
            ],
        },
    )
    _w(root, "tactic_set/nav.yaml", {"subtrees": [], "state_trees": [], "missions": []})
    _w(
        root,
        "tactic_set/sense.yaml",
        {
            "subtrees": [
                {
                    "id": "sense.search",
                    "description": "Scan until a target is valid.",
                    "root": {"type": "action", "action": "sense.scan"},
                },
                {"id": "sense.track", "root": {"type": "action", "action": "sense.scan"}},
            ],
            "state_trees": [
                {
                    "id": "sense.searching_state",
                    "root": {"type": "subtree", "subtree": "sense.search"},
                },
                {"id": "sense.shutdown_state", "root": {"type": "action", "action": "sense.off"}},
            ],
            "missions": [
                {
                    "id": "sense.watch",
                    "description": "Search until cancelled.",
                    "initial_state": "SEARCHING",
                    "states": ["SEARCHING", "SHUTDOWN", "COMPLETE"],
                    "terminal_states": ["COMPLETE"],
                    "transitions": [
                        {"from": "SEARCHING", "to": "SHUTDOWN", "when": "CANCELLED"},
                        {"from": "SHUTDOWN", "to": "COMPLETE", "when": "CLEANUP_COMPLETE"},
                    ],
                    "state_trees": {
                        "SEARCHING": "sense.searching_state",
                        "SHUTDOWN": "sense.shutdown_state",
                    },
                }
            ],
        },
    )
    return root


@pytest.fixture
def contracts(set_root: Path) -> Contracts:
    return Contracts.load(set_root)


SHEET = """# 侦察

```yaml model
model: {display_name: 单车侦察, intent: 前出搜索，发现后观察}
slices: [nav, sense]
roles:
  scout: {capability_class: ground_vehicle, description: 侦察车}
parameters:
  obs_lat: {value: "30.01 deg", basis: inferred, rationale: 观察点}
  obs_lon: {value: "120.0 deg", basis: inferred}
  approach_speed: {value: "18 km/h", basis: unknown}
events:
  cancelled: {source: external, description: 上级取消}
  lost_timeout: {after: "20 s"}
assumptions: [上级会在任务结束时发出取消]
implements: {rq_01: [scout.MOVE], rq_02: [scout.OBSERVE]}
```

```mermaid scout
stateDiagram-v2
  [*] --> MOVE
  MOVE --> OBSERVE : target_seen
  OBSERVE --> MOVE : lost_timeout
  SHUTDOWN --> DONE : stopped
  * --> SHUTDOWN : cancelled
  DONE --> [*]
```

```python conditions
target_seen = sense.target.valid
stopped = self.speed_mps <= 0.1 and time_in_state() >= 2 * s
```

```python scout
def MOVE():
    with parallel("all"):
        with repeat(until_done=True):
            nav.goto(lat_deg=obs_lat, lon_deg=obs_lon, speed_mps=approach_speed)
        tactic("sense.search")

def OBSERVE():
    with parallel("one"):
        nav.hold()
        guard()

def guard():
    if nav.range_m < "300 m":
        gap("smoke_screen", duration_s=30 * s)
    else:
        tactic("sense.track")

def SHUTDOWN():
    nav.stop()
    sense.off()
```

```yaml gaps
gaps:
  smoke_screen:
    kind: action
    description: 施放烟幕
    reason: 能力集没有遮蔽类动作
    clauses: [rq_02]
    capability_class: ground_vehicle
    params: {duration_s: {type: double, unit: s, required: true, range: [5, 120]}}
    stub: {success_after_s: 2}
```

```yaml verification
tests:
  - id: sees_target
    given: {role: scout, state: MOVE, blackboard: {sense.target.valid: true}}
    expect: {next_state: OBSERVE}
scenarios:
  - id: cancel
    environment: {origin_deg: [30.0, 120.0], duration_s: 20}
    events: [{at_s: 3, event: cancelled}]
    expect: [{id: shut, role: scout, reaches: SHUTDOWN, within_s: 5}]
```
"""


def _compile(contracts: Contracts, text: str = SHEET, **kw):
    return compile_sheet(text, contracts, model_id="scout_demo", **kw)


def test_sheet_compiles_to_ir_v2(contracts: Contracts) -> None:
    out = _compile(contracts)
    assert not out.errors, [p.render() for p in out.errors]
    spec = out.spec
    mission = spec["missions"]["scout"]
    assert mission["initial_state"] == "MOVE" and mission["terminal"] == ["DONE"]
    assert mission["global"] == [{"to": "SHUTDOWN", "on": "cancelled", "priority": 100}]
    assert {"from": "OBSERVE", "to": "MOVE", "on": "lost_timeout"} in mission["transitions"]
    assert spec["parameters"]["approach_speed"] == {
        "type": "speed",
        "value": 18,
        "unit": "km/h",
        "basis": {"status": "unknown", "evidence": [], "rationale": None},
    }
    assert spec["events"][1] == {"id": "lost_timeout", "source": "timer", "after_s": 20.0}
    trees = spec["state_trees"]["scout"]
    assert trees == {
        "MOVE": "scout_move",
        "OBSERVE": "scout_observe",
        "SHUTDOWN": "scout_shutdown",
    }
    subs = {s["id"]: s["root"] for s in spec["subtrees"]}
    move = subs["scout_move"]
    assert move["type"] == "parallel" and move["policy"] == {"type": "all"}
    assert move["children"][0]["repeat"] == {"until_done": True}
    assert move["children"][0]["children"][0]["params"]["lat_deg"] == "$obs_lat"
    guard = subs["guard"]
    assert guard["type"] == "selector"
    cond, smoke = guard["children"][0]["children"]
    assert cond == {"type": "condition", "condition": "guard_c1"} or cond["condition"].endswith(
        "_c1"
    )
    assert smoke == {
        "type": "action",
        "action": "gap.smoke_screen",
        "params": {"duration_s": {"value": 30, "unit": "s"}},
    }
    assert subs["scout_shutdown"]["type"] == "sequence_with_memory"
    result = normalize(spec, contracts)
    assert result.ok, [d.render() for d in result.diagnostics]
    assert result.model["gaps"]["smoke_screen"]["used_by"] == ["scout.OBSERVE"]
    assert result.model["capability"]["slices"] == ["nav", "sense"]


def test_problems_point_at_blocks_and_lines(contracts: Contracts) -> None:
    text = SHEET.replace("nav.hold()", "nav.hodl()").replace(
        "target_seen = sense.target.valid", "target_seen = sense.target.vaild"
    )
    out = _compile(contracts, text)
    lines = text.splitlines()
    by_block = {p.block: p for p in out.errors}
    tree = by_block["pytree:scout"]
    assert "nav.hodl" in tree.message and "nav.hold" in tree.candidates
    assert "nav.hodl()" in lines[tree.line - 1]
    cond = by_block["conditions"]
    assert "sense.target.vaild" in cond.message and "sense.target.valid" in cond.candidates
    assert lines[cond.line - 1].startswith("target_seen")
    # a normalization error (unknown parameter of an action) is mapped back to its line
    text = SHEET.replace("speed_mps=approach_speed", "speed_mps=approach_speed, heading=3")
    out = _compile(contracts, text)
    result = normalize(out.spec, contracts)
    errors = [out.problem(d) for d in result.diagnostics if d.severity.value == "error"]
    assert errors and errors[0].block == "pytree:scout"
    assert "nav.goto(" in text.splitlines()[errors[0].line - 1]


def test_mission_reference_and_state_override(contracts: Contracts) -> None:
    text = """```yaml model
model: {display_name: 值守, intent: 搜索直到取消}
roles:
  scout: {capability_class: ground_vehicle, mission: {tactic: sense.watch}}
```

```python scout
def SEARCHING():
    with parallel("all"):
        nav.hold()
        tactic("sense.search")
```
"""
    out = _compile(contracts, text)
    assert not out.errors, [p.render() for p in out.errors]
    assert out.spec["missions"]["scout"] == {"tactic": "sense.watch"}
    result = normalize(out.spec, contracts)
    assert result.ok, [d.render() for d in result.diagnostics]
    states = result.model["roles"]["scout"]["states"]
    assert states["SEARCHING"]["tree_ref"] == "scout_searching"
    # the referenced mission's triggers are part of the normalized model
    assert result.model["events"]["cancelled"]["source"] == "tactic"
    assert states["SHUTDOWN"]["tree_ref"] == "sense.shutdown_state"
    refused = _compile(contracts, text, allow_mission_ref=False)
    assert any("不允许整体引用" in p.message for p in refused.errors)


def test_tree_and_condition_errors(contracts: Contracts) -> None:
    text = SHEET.replace(
        "def SHUTDOWN():\n    nav.stop()",
        'def SHUTDOWN():\n    bb.x = 1\n    nav.stop(3)\n    tactic("sense.nope")\n    gap("nope")\n    helper()',
    )
    out = _compile(contracts, text)
    messages = " | ".join(p.message for p in out.errors)
    for part in ("不能给黑板赋值", "关键字参数", "sense.nope", "缺口 'nope'", "'helper'"):
        assert part in messages, part
    bad = SHEET.replace("def OBSERVE():", "def WATCH():")
    out = _compile(contracts, bad)
    assert any("不是角色 scout 的状态" in p.message for p in out.errors)
    result = normalize(out.spec, contracts)
    fsm = [out.problem(d) for d in result.diagnostics if d.code == "FSM002"]
    assert fsm and fsm[0].block == "mermaid:scout"


# ---------------------------------------------------------------------- drafting loop
def _stub_langchain() -> None:
    if "langchain_openai" not in sys.modules:
        try:
            import langchain_openai  # noqa: F401
        except ModuleNotFoundError:
            mod = types.ModuleType("langchain_openai")
            mod.ChatOpenAI = object  # type: ignore[attr-defined]
            sys.modules["langchain_openai"] = mod


def _blocks(text: str, *labels: str) -> str:
    from afsim_research_agent.falcon.sheet.blocks import split_sheet

    blocks, _ = split_sheet(text)
    return "\n\n".join(
        b.fence()
        for b in blocks
        if b.label.split()[0:2] and any(b.label.startswith(x) for x in labels)
    )


def test_draft_loop_repairs_and_writes_the_package(set_root: Path, tmp_path: Path) -> None:
    _stub_langchain()
    from afsim_research_agent.falcon.llm import ScriptedModel
    from afsim_research_agent.falcon.v3.draft import run_draft3

    request = tmp_path / "req.yaml"
    request.write_text(
        yaml.safe_dump(
            {
                "requirement": "侦察车前出到观察点搜索目标，发现后保持观察。",
                "model_id": "scout_demo",
                "roles": {"scout": {"capability_class": "ground_vehicle"}},
                "capability_set": str(set_root),
                "allow_mission_ref": False,
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    structure = _blocks(SHEET, "yaml model", "mermaid")
    control_bad = _blocks(SHEET, "python", "yaml gaps").replace("nav.hold()", "nav.hodl()")
    fixed_tree = _blocks(SHEET, "python scout")
    verification = _blocks(SHEET, "yaml verification")
    prompts: dict[str, list[str]] = {}

    def answer(text):
        def handler(prompt, schema, index):
            return {"text": text}

        return handler

    script = {
        "requirements": [
            {
                "clauses": [
                    {"text": "侦察车前出到观察点搜索目标", "kind": "behavior"},
                    {"text": "发现后保持观察", "kind": "behavior"},
                ]
            }
        ],
        "v3:obligations": [
            {
                "obligations": [
                    {
                        "id": "search_while_moving",
                        "kind": "not_before",
                        "action": "sense.scan",
                        "after": "nav.goto",
                        "min_s": 0,
                        "clause": "rq_01",
                        "description": "前出时搜索",
                    },
                    {
                        "id": "stop_on_cancel",
                        "kind": "response",
                        "on_event": "cancelled",
                        "action": "nav.stop",
                        "within_s": 5,
                        "description": "取消后停车",
                    },
                    {"id": "bogus", "kind": "precedes", "first": "nav.fly", "then": "nav.stop"},
                ]
            }
        ],
        "v3:structure": answer(structure),
        "v3:control": answer(control_bad),
        "v3:repair:pytree": answer(fixed_tree),
        "v3:verification": answer(verification),
    }
    model = ScriptedModel(script)
    result = asyncio.run(run_draft3(request, model, out_dir=tmp_path / "out"))
    for stage, prompt in model.calls:
        prompts.setdefault(stage, []).append(prompt)
    assert result.check.l1_ok, [p.render() for p in result.check.problems]
    assert result.status == "draft_valid", result.check.l2 + result.check.l3
    assert [r["stage"] for r in result.rounds] == ["control"]
    pkg = result.package_dir
    for name in (
        "behavior_spec.yaml",
        "gaps.yaml",
        "tactic_sheet.md",
        "falcon_review.md",
        "validation_report.json",
        "falcon_trace.json",
        "source_map.json",
    ):
        assert (pkg / name).exists(), name
    spec = yaml.safe_load((pkg / "behavior_spec.yaml").read_text(encoding="utf-8"))
    assert spec["schema_version"] == "2.0"
    gaps = yaml.safe_load((pkg / "gaps.yaml").read_text(encoding="utf-8"))["gaps"]
    assert gaps["smoke_screen"]["local_only"] is True
    report = json.loads((pkg / "validation_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "draft_valid" and report["gaps"] == ["smoke_screen"]
    review = (pkg / "falcon_review.md").read_text(encoding="utf-8")
    assert "缺口 `smoke_screen`" in review and "自己写" in review
    # requirement obligations: written to the package, judged on the scenario runs; one
    # naming an action the capability set does not have is dropped
    obligations = yaml.safe_load((pkg / "obligations.yaml").read_text(encoding="utf-8"))
    assert [o["id"] for o in obligations["obligations"]] == [
        "search_while_moving",
        "stop_on_cancel",
    ]
    assert "stop_on_cancel" in review and "cancel 通过" in review, review
    assert "需求约束 bogus 已忽略" in review
    # the structure prompt sees the slice index; the control prompt sees the slices in full
    assert (
        "切片 `nav`" in prompts["v3:structure"][0]
        and "Drive to a point." not in prompts["v3:structure"][0]
    )
    assert "Drive to a point." in prompts["v3:control"][0]
    assert "不能引用战术集任务和状态树" in prompts["v3:structure"][0]
    assert "nav.hodl" in prompts["v3:repair:pytree"][0]
    # an existing package is never overwritten
    with pytest.raises(FileExistsError):
        asyncio.run(run_draft3(request, ScriptedModel(script), out_dir=tmp_path / "out"))


def test_conditions_are_checked_against_the_chosen_slices(contracts: Contracts) -> None:
    text = SHEET.replace(
        "target_seen = sense.target.valid",
        'target_seen = sense.mode == "LOCKED"\nbusy = sense.request.id != ""\nloop = loop',
    ).replace(
        '  lost_timeout: {after: "20 s"}',
        '  lost_timeout: {after: "20 s"}\n  spare: {source: external}',
    )
    out = _compile(contracts, text)
    errors = " | ".join(p.message for p in out.errors)
    everything = " | ".join(p.message for p in out.problems)
    assert "SEARCH, TRACK" in errors  # target_seen is used by a transition: an error
    # busy and loop are used by nothing: reported, left out, not sent for repair
    assert "接口字段" in everything and "引用了它自己" in everything
    assert "接口字段" not in errors and "引用了它自己" not in errors
    assert "事件 spare 没有被任何转换使用" in everything
    # a field of a slice the sheet did not choose
    only_nav = SHEET.replace("slices: [nav, sense]", "slices: [nav]")
    out = _compile(contracts, only_nav)
    assert any("属于切片 sense" in p.message for p in out.errors)


def test_guessed_values_and_shadowed_mission_triggers(contracts: Contracts) -> None:
    text = SHEET.replace("target_seen = sense.target.valid", 'target_seen = sense.label == "ENEMY"')
    assert any("猜测" in p.message for p in _compile(contracts, text).errors)
    # comparing with the field's declared initial value ("not set") is not a guess
    unset = SHEET.replace("target_seen = sense.target.valid", 'target_seen = sense.label != ""')
    assert any("猜测" in p.message for p in _compile(contracts, unset).errors)
    label = contracts.blackboard["sense.label"]
    contracts.blackboard["sense.label"] = dataclasses.replace(label, initial="", has_initial=True)
    try:
        assert not any("猜测" in p.message for p in _compile(contracts, unset).errors)
    finally:
        contracts.blackboard["sense.label"] = label
    text = """```yaml model
model: {display_name: 值守, intent: 搜索直到取消}
roles:
  scout: {capability_class: ground_vehicle, mission: {tactic: sense.watch}}
```

```python conditions
cancelled = sense.target.valid
```
"""
    out = _compile(contracts, text)
    # unused by the model's own transitions and trees: dropped with a warning
    assert not out.errors
    assert any("同名" in p.message and p.severity == "warning" for p in out.problems)
    assert out.spec["conditions"] == []
    text = text.replace(
        "```python conditions",
        "```python scout\ndef SEARCHING():\n    assert cancelled\n    nav.hold()\n```\n\n```python conditions",
    )
    assert any("同名" in p.message for p in _compile(contracts, text).errors)


def test_a_state_never_left_in_the_simulation_goes_back_to_the_model(contracts: Contracts) -> None:
    from afsim_research_agent.falcon.llm import ModelRunner, ScriptedModel
    from afsim_research_agent.falcon.sheet.blocks import split_sheet
    from afsim_research_agent.falcon.v3.draft import Drafter, V3Request

    stuck = SHEET.replace("target_seen = sense.target.valid", "target_seen = nav.range_m < 0 * m")
    stuck = stuck.replace(
        "expect: [{id: shut, role: scout, reaches: SHUTDOWN, within_s: 5}]",
        "expect: [{id: seen, role: scout, reaches: OBSERVE}]",
    ).replace("events: [{at_s: 3, event: cancelled}]", "events: []")
    drafter = Drafter(
        V3Request(requirement="r", model_id="scout_demo"), contracts, ModelRunner(ScriptedModel({}))
    )
    drafter.blocks = split_sheet(stuck)[0]
    check = drafter.check(verify=True)
    problems = drafter._stuck(check)
    assert {k for k, _ in problems} == {"mermaid:scout", "conditions"}
    assert "一直停在 MOVE" in problems[0][1] and "target_seen" in problems[0][1]


def test_scenario_events_constants_and_nested_verification(contracts: Contracts) -> None:
    text = SHEET.replace("target_seen = sense.target.valid", "target_seen = True")
    assert any("是常量" in p.message for p in _compile(contracts, text).errors)
    out = _compile(contracts, SHEET, scenario_events={"stop_request": "上级结束任务"})
    messages = " | ".join(p.message for p in out.errors)
    assert "不会发出外部事件 cancelled" in messages and "stop_request" in messages
    nested = SHEET.replace(
        "```yaml verification\ntests:", "```yaml verification\nverification:\n tests:"
    )
    nested = nested.replace("\nscenarios:", "\n scenarios:")
    lines = nested.split("```yaml verification")[1]
    body = lines.split("```")[0]
    indented = "\n".join(
        (" " + x if x and not x.startswith(("verification", " tests", " scenarios")) else x)
        for x in body.split("\n")
    )
    nested = nested.replace(body, indented)
    out = _compile(contracts, nested)
    assert len(out.spec["tests"]) == 1 and len(out.spec["scenarios"]) == 1


def test_undefined_triggers_are_filed_for_the_conditions_block(contracts: Contracts) -> None:
    text = SHEET.replace("MOVE --> OBSERVE : target_seen", "MOVE --> OBSERVE : seen_timer")
    out = _compile(contracts, text)
    keys = {p.block for p in out.errors if "seen_timer" in p.message}
    assert keys == {"mermaid:scout", "conditions"}


def test_subtree_policy_learns_from_state_trees_without_referencing_them(
    contracts: Contracts,
) -> None:
    from afsim_research_agent.falcon.v3 import catalog as K
    from afsim_research_agent.falcon.v3.compare import compare
    from afsim_research_agent.ir.v2.runtime import RUNNING, Instance

    text = SHEET.replace(
        '        tactic("sense.search")', '        tactic("sense.searching_state")'
    )
    out = _compile(contracts, text, reference_policy="subtrees")
    assert any("只能参照" in p.message for p in out.errors)
    ref = """```yaml model
model: {display_name: 值守, intent: 搜索直到取消}
roles:
  scout: {capability_class: ground_vehicle, mission: {tactic: sense.watch}}
```
"""
    assert any(
        "不允许整体引用" in p.message
        for p in _compile(contracts, ref, reference_policy="subtrees").errors
    )
    # subtrees are listed with their trees; state trees and missions only as examples
    details = K.details(contracts, ["sense"], "subtrees")
    assert "可引用的战术集子树" in details and "def _search():" in details
    assert "范例 sense.searching_state" in details and "范例：战术集任务 `sense.watch`" in details
    # keep_running: a failing child is reported as running
    text = SHEET.replace(
        "def SHUTDOWN():\n    nav.stop()",
        "def SHUTDOWN():\n    with keep_running():\n        assert sense.target.valid\n    nav.stop()",
    )
    out = _compile(contracts, text, reference_policy="subtrees")
    assert not out.errors, [p.render() for p in out.errors]
    result = normalize(out.spec, contracts)
    assert result.ok, [d.render() for d in result.diagnostics]
    tree = result.model["roles"]["scout"]["states"]["SHUTDOWN"]["tree"]
    assert tree["children"][0]["type"] == "failure_as_running"
    inst = Instance("scout_0", "scout", 0, result.model, contracts, host=None)  # type: ignore[arg-type]
    assert inst.tick(tree["children"][0]) == RUNNING
    # structural comparison against the tactic set's mission with the same states
    rows = compare(result.model, contracts)
    assert [(r["state"], r["same"], r["extra"]) for r in rows] == [
        ("SHUTDOWN", False, ["nav.stop"])  # sense.watch shuts down with sense.off only
    ]


# ---------------------------------------------------------------------- repair loop
def _fix_drafter(contracts: Contracts, text: str):
    from afsim_research_agent.falcon.llm import ModelRunner, ScriptedModel
    from afsim_research_agent.falcon.sheet.blocks import split_sheet
    from afsim_research_agent.falcon.v3.draft import Drafter, V3Request

    drafter = Drafter(
        V3Request(requirement="侦察车前出搜索", model_id="scout_demo"),
        contracts,
        ModelRunner(ScriptedModel({})),
    )
    drafter.blocks = split_sheet(text)[0]
    return drafter


def test_fix_loop_edits_several_blocks_and_keeps_the_guards(contracts: Contracts) -> None:
    from afsim_research_agent.falcon.llm import ModelRunner, ScriptedModel
    from afsim_research_agent.falcon.v3.fixer import SheetFixer, fix_sheet, render_fix

    broken = SHEET.replace("MOVE --> OBSERVE : target_seen", "MOVE --> OBSERVE : seen").replace(
        "nav.hold()", "nav.hodl()"
    )
    drafter = _fix_drafter(contracts, broken)
    fixer = SheetFixer(drafter)
    assert not fixer.last.l1_ok
    assert "nav.hold" in fixer.lookup("nav.hodl")  # near matches
    assert "动作 nav.goto" in fixer.lookup("nav.goto") and "def _search():" in fixer.lookup(
        "sense.search"
    )
    assert "状态树（只能参照）" in fixer.lookup("sense.searching_state")
    steps = [
        {"thought": "看问题", "action": "explain", "args": {"problem": 1}},
        # deleting the verification block is refused
        {"action": "edit", "args": {"changes": [{"block": "verification", "text": "tests: []"}]}},
        # one step fixes the diagram and the tree
        {
            "thought": "触发名与条件对齐，动作名改正",
            "action": "edit",
            "args": {
                "changes": [
                    {
                        "block": "mermaid:scout",
                        "old": "OBSERVE : seen",
                        "new": "OBSERVE : target_seen",
                    },
                    {"block": "pytree:scout", "old": "nav.hodl()", "new": "nav.hold()"},
                ],
                "reason": "对齐触发名；改正动作名",
            },
        },
        {"action": "finish", "args": {"summary": "已修正"}},
    ]
    model = ScriptedModel({"fix:v3:step": steps})
    outcome = asyncio.run(fix_sheet(drafter, ModelRunner(model), max_steps=6))
    assert outcome.solved, [r["result"][:300] for r in outcome.log]
    assert [r["action"] for r in outcome.log] == ["explain", "edit", "edit"]  # solved: stops
    assert "修改被拒绝：不能减少" in outcome.log[1]["result"]
    assert {e.block for e in outcome.edits} == {"mermaid:scout", "pytree:scout"}
    review = "\n".join(render_fix(outcome))
    assert "自检纠错记录" in review and "+  MOVE --> OBSERVE : target_seen" in review
    # the prompt carries the current problems and the tools
    assert "explain" in model.calls[0][1] and "seen" in model.calls[0][1]


def test_fix_loop_refuses_an_edit_that_breaks_l1_and_explains_conflicts(
    contracts: Contracts,
) -> None:
    from afsim_research_agent.falcon.v3.fixer import SheetFixer

    fixer = SheetFixer(_fix_drafter(contracts, SHEET))
    assert fixer.last.l1_ok
    result = fixer.edit(
        [{"block": "pytree:scout", "old": "nav.stop()", "new": "nav.nope()"}], "试错"
    )
    assert result.startswith("修改被拒绝：会让 L1 不通过") and fixer.last.l1_ok
    # a parallel whose branches both drive: the explanation names the branches
    clash = SHEET.replace(
        '    with parallel("one"):\n        nav.hold()\n        guard()',
        '    with parallel("one"):\n        nav.hold()\n        nav.stop()',
    )
    fixer = SheetFixer(_fix_drafter(contracts, clash))
    idx = next(i for i, p in enumerate(fixer.last.problems, 1) if "ARB001" in p.message)
    text = fixer.explain(idx)
    assert "分支 0：drive: nav.hold" in text and "分支 1：drive: nav.stop" in text
    tick = SheetFixer(_fix_drafter(contracts, SHEET)).try_tick(
        "scout", "MOVE", {"blackboard": {"sense.target.valid": True}}
    )
    assert "MOVE → OBSERVE" in tick


def test_fix_tools_avoid_rereading_and_diagnose_skipped_states(contracts: Contracts) -> None:
    from afsim_research_agent.falcon.v3.fixer import SheetFixer, _skipped

    fixer = SheetFixer(_fix_drafter(contracts, SHEET.replace("nav.hold()", "nav.hodl()")))
    assert "def OBSERVE" in fixer.show("pytree:scout")
    assert "没有改动" in fixer.show("pytree:scout")
    summary = fixer.summary()
    assert ">| " in summary and "nav.hodl()" in summary  # the failing line is shown in place
    scenario = {
        "transitions": {
            "car_0": [
                {"t": 3.0, "from": "A", "to": "B", "on": "go"},
                {"t": 3.0, "from": "B", "to": "C", "on": "ready"},
            ]
        }
    }
    assert "进入 B 后同一拍就因 ready 离开" in _skipped(scenario)[0]


def test_fix_loop_stops_reading_and_cannot_write_the_verification_block(
    contracts: Contracts,
) -> None:
    from afsim_research_agent.falcon.llm import ModelRunner, ScriptedModel
    from afsim_research_agent.falcon.v3.fixer import STALL, SheetFixer, fix_sheet

    no_verification = SHEET.split("```yaml verification")[0]
    fixer = SheetFixer(_fix_drafter(contracts, no_verification))
    assert "验证阶段" in fixer.edit([{"block": "verification", "text": "tests: []"}], "x")
    # a model that only reads: show is refused after a few reads, the loop stops on stall
    reads = [
        {"action": "show", "args": {"block": "pytree:scout", "start": i}} for i in range(40, 60)
    ]
    drafter = _fix_drafter(contracts, SHEET.replace("nav.hold()", "nav.hodl()"))
    outcome = asyncio.run(
        fix_sheet(drafter, ModelRunner(ScriptedModel({"fix:v3:step": reads})), max_steps=20)
    )
    assert outcome.steps == STALL
    assert any("连续查看" in r["result"] for r in outcome.log)


def test_tree_events_end_a_state_when_its_tree_succeeds(contracts: Contracts) -> None:
    from afsim_research_agent.ir.v2.sim import simulate

    text = SHEET.replace("SHUTDOWN --> DONE : stopped", "SHUTDOWN --> DONE : cleaned").replace(
        '  lost_timeout: {after: "20 s"}',
        '  lost_timeout: {after: "20 s"}\n  cleaned: {source: tree, result: success}',
    )
    out = _compile(contracts, text)
    assert not out.errors, [p.render() for p in out.errors]
    assert {"id": "cleaned", "source": "tree", "result": "success"} in out.spec["events"]
    result = normalize(out.spec, contracts)
    assert result.ok, [d.render() for d in result.diagnostics]
    scenario = out.spec["scenarios"][0] | {
        "expect": [{"id": "done", "role": "scout", "reaches": "DONE", "within_s": 8}]
    }
    sim = simulate(result.model, contracts, scenario)
    assert sim.passed, sim.render()
    from afsim_research_agent.ir.v2.afsim.compiler import Compiler

    compiler = Compiler.__new__(Compiler)
    compiler.m = result.model
    assert compiler._semantics("cleaned") == {"tree": "success"}


def _annotate(set_root: Path) -> Contracts:
    """Capability set 1.1: contracts state their writes, fields their writers."""
    nav = yaml.safe_load((set_root / "action_set/nav.yaml").read_text(encoding="utf-8"))
    for a in nav["actions"]:
        a["writes"] = (
            [{"field": "nav.range_m", "updated": "while_running"}]
            if a["id"] in ("nav.goto", "nav.hold")
            else []
        )
    nav["actions"][0]["result"] = {"running": "未到达", "success": "到达"}
    nav["blackboard"]["fields"]["nav.range_m"].update(
        written_by=["nav.goto", "nav.hold"], updated="while_running", description="到目标点距离"
    )
    nav["blackboard"]["fields"]["self.speed_mps"].update(updated="always")
    _w(set_root, "action_set/nav.yaml", nav)
    sense = yaml.safe_load((set_root / "action_set/sense.yaml").read_text(encoding="utf-8"))
    sense["actions"][0]["writes"] = [{"field": "sense.target.valid", "updated": "on_success"}]
    sense["actions"][1]["writes"] = []
    fields = sense["blackboard"]["fields"]
    fields["sense.target.valid"].update(written_by=["sense.scan"], initial=False)
    fields["sense.mode"].update(written_by=[], description="设计中的模式字段")
    _w(set_root, "action_set/sense.yaml", sense)
    return Contracts.load(set_root)


def test_conditions_must_read_fields_the_state_keeps_up_to_date(set_root: Path) -> None:
    contracts = _annotate(set_root)
    out = _compile(contracts)
    assert not out.errors, [p.render() for p in out.errors]
    # a field nothing writes cannot drive a condition
    bad = SHEET.replace("target_seen = sense.target.valid", 'target_seen = sense.mode == "TRACK"')
    msgs = [p.message for p in _compile(contracts, bad).errors]
    assert any("sense.mode" in m and "没有任何动作写入" in m for m in msgs), msgs
    # a transition of a state whose tree does not update the field
    bad = SHEET.replace("SHUTDOWN --> DONE : stopped", "SHUTDOWN --> DONE : not sense.target.valid")
    errs = _compile(contracts, bad).errors
    assert any("状态 SHUTDOWN" in p.message and "sense.target.valid" in p.message for p in errs), [
        p.message for p in errs
    ]
    # fields the platform updates every tick are fine anywhere (stopped reads self.speed_mps)
    assert not any("self.speed_mps" in p.message for p in errs)
    # a decision state (tree `pass`) leaving on X or on not X decides on its entry values
    decide = SHEET.replace(
        "  OBSERVE --> MOVE : lost_timeout\n",
        "  OBSERVE --> DECIDE : lost_timeout\n"
        "  DECIDE --> OBSERVE : target_seen\n  DECIDE --> MOVE : not target_seen\n",
    ).replace("def SHUTDOWN():", "def DECIDE():\n    pass\n\ndef SHUTDOWN():")
    out = _compile(contracts, decide)
    assert not out.errors, [p.render() for p in out.errors]
    tree = out.spec["state_trees"]["scout"]["DECIDE"]
    assert tree is not None
    # one-sided, the same read is reported
    one = decide.replace("  DECIDE --> MOVE : not target_seen\n", "")
    assert any("状态 DECIDE" in p.message for p in _compile(contracts, one).errors)


def test_rings_of_field_transitions_are_refused(set_root: Path) -> None:
    contracts = _annotate(set_root)
    ring = SHEET.replace(
        "  OBSERVE --> MOVE : lost_timeout\n", "  OBSERVE --> MOVE : self.speed_mps >= 0\n"
    )
    msgs = [p.message for p in _compile(contracts, ring).errors]
    assert any("状态环" in m and "MOVE" in m and "OBSERVE" in m for m in msgs), msgs
    # a timer on one transition of the ring makes it wait
    timed = SHEET.replace(
        "  OBSERVE --> MOVE : lost_timeout\n",
        "  OBSERVE --> MOVE : self.speed_mps >= 0 and time_in_state() >= 5 * s\n",
    )
    msgs = [p.message for p in _compile(contracts, timed).errors]
    assert not any("状态环" in m for m in msgs), msgs
    # out on "seen", back on "not seen": both cannot hold, so the ring cannot turn
    back = SHEET.replace(
        "  OBSERVE --> MOVE : lost_timeout\n", "  OBSERVE --> MOVE : not sense.target.valid\n"
    )
    msgs = [p.message for p in _compile(contracts, back).errors]
    assert not any("状态环" in m for m in msgs), msgs


def test_ring_contradictions() -> None:
    from afsim_research_agent.falcon.v3.compiler import _atoms, _contradict

    def cmp(field: str, op: str, value: object) -> dict:
        right = {"value": value, "unit": "m"} if isinstance(value, int) else {"literal": value}
        return {"compare": {"left": {"bb": field}, "operator": op, "right": right}}

    exprs = {"lost": {"not": {"bb": "t.valid"}}, "seen": {"bb": "t.valid"}}
    atoms = _atoms({"condition_ref": "lost"}, exprs) + _atoms({"condition_ref": "seen"}, exprs)
    assert _contradict(atoms)
    near, far = cmp("r", "lt", 300), cmp("r", "gte", 500)
    assert _contradict(_atoms(near, {}) + _atoms(far, {}))
    assert not _contradict(_atoms(cmp("r", "lt", 500), {}) + _atoms(cmp("r", "gte", 300), {}))
    assert _contradict(_atoms(cmp("r", "lte", 300), {}) + _atoms(cmp("r", "gt", 300), {}))
    assert not _contradict(_atoms(cmp("r", "lte", 300), {}) + _atoms(cmp("r", "gte", 300), {}))
    # a disjunction requires nothing in particular
    assert not _contradict(_atoms({"any": [{"bb": "t.valid"}, {"bb": "x"}]}, {}) + atoms[:1])
    # string states
    a = _atoms(cmp("mode", "eq", "A"), {}) + _atoms(cmp("mode", "eq", "B"), {})
    assert _contradict(a)
    # x == False is "not x"
    assert _contradict(_atoms(cmp("t.valid", "eq", False), {}) + _atoms({"bb": "t.valid"}, {}))


def test_prompt_shows_field_writers_and_results(set_root: Path) -> None:
    from afsim_research_agent.falcon.v3 import catalog as K

    contracts = _annotate(set_root)
    text = K.details(contracts, ["nav", "sense"], "subtrees")
    assert "nav.range_m (double distance) — 到目标点距离；由 nav.goto, nav.hold 写入" in text
    assert "未实现的字段（没有任何动作写入，不能用作条件）：sense.mode（设计中的模式字段）" in text
    assert "结果：运行=未到达；成功=到达" in text
    assert "初值 False" in text


def test_step_results_in_conditions_point_to_tree_events(contracts: Contracts) -> None:
    for wrong in ("nav.goto.result == 1", "tactic('sense.search').result"):
        bad = SHEET.replace(
            "stopped = self.speed_mps <= 0.1 and time_in_state() >= 2 * s", f"stopped = {wrong}"
        )
        msgs = [p.message for p in _compile(contracts, bad).errors]
        assert any("source: tree, result: success" in m for m in msgs), msgs


def test_gates_compile_to_the_usual_tree_patterns(contracts: Contracts) -> None:
    sheet = SHEET.replace(
        """def OBSERVE():
    with parallel("one"):
        nav.hold()
        guard()""",
        """def OBSERVE():
    with parallel("one"):
        with after(5 * s):
            nav.hold()
        with until(sense.target.valid):
            tactic("sense.track")
        with when(nav.range_m < "300 m"):
            sense.off()""",
    ).replace("def guard():", "def unused():")
    out = _compile(contracts, sheet)
    assert not out.errors, [p.render() for p in out.errors]
    subs = {s["id"]: s["root"] for s in out.spec["subtrees"]}
    after, until, when = subs["scout_observe"]["children"]
    assert after["type"] == "sequence" and after["children"][0]["type"] == "failure_as_running"
    cond = {c["id"]: c["expression"] for c in out.spec["conditions"]}
    gate = cond[after["children"][0]["children"][0]["condition"]]
    assert gate["function_call"]["name"] == "time_in_state_gte"
    assert until["type"] == "selector" and until["children"][0]["type"] == "condition"
    assert when["children"][1]["action"] == "sense.off"
    # the hand-made form of "until" that runs the wrong way round is refused
    bad = SHEET.replace(
        """        nav.hold()
        guard()""",
        """        nav.hold()
        with selector():
            assert sense.target.valid
            guard()""",
    )
    msgs = [p.message for p in _compile(contracts, bad).errors]
    assert any("with until" in m for m in msgs), msgs


def test_tree_events_of_trees_that_never_finish_are_refused(set_root: Path) -> None:
    contracts = _annotate(set_root)
    import dataclasses

    hold = contracts.actions["nav.hold"]
    contracts.actions["nav.hold"] = dataclasses.replace(hold, persistent=True)
    sheet = (
        SHEET.replace(
            '  lost_timeout: {after: "20 s"}',
            '  lost_timeout: {after: "20 s"}\n  held: {source: tree, result: success}',
        )
        .replace("OBSERVE --> MOVE : lost_timeout", "OBSERVE --> MOVE : held")
        .replace(
            """    with parallel("one"):
        nav.hold()
        guard()""",
            "    nav.hold()",
        )
    )
    msgs = [p.message for p in _compile(contracts, sheet).errors]
    assert any("树事件 held" in m and "OBSERVE" in m for m in msgs), msgs
