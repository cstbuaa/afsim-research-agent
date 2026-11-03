# ruff: noqa: F811 - pytest fixtures imported from test_falcon_v3
"""Triage of failed runs and the repair loop's rules around it (diagnose → edit, classify)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from test_falcon_v3 import SHEET, _fix_drafter, contracts, set_root  # noqa: F401 - fixtures

from afsim_research_agent.falcon.v3 import triage
from afsim_research_agent.ir.v2.contracts import Contracts

MODEL = {
    "parameters": {
        "pt_post_lat": {"type": "angle", "value": 0.0, "scenario": "point:post:lat:own_start"},
        "pt_post_lon": {"type": "angle", "value": 0.0, "scenario": "point:post:lon:own_start"},
        "rt_approach": {"type": "string", "value": "", "scenario": "route:approach:a:b"},
    },
    "conditions": {"arrived": {"bb": "movement.derived.range_to_target_m"}},
    "events": {
        "fire_order": {"source": "external"},
        "move_done": {"source": "tree", "result": "success"},
    },
    "roles": {
        "car": {
            "mission": {
                "initial_state": "DEPLOY",
                "terminal": ["DONE"],
                "safe": ["SHUTDOWN"],
                "transitions": [
                    {"from": "DEPLOY", "to": "WAIT", "on": "arrived"},
                    {"from": "WAIT", "to": "FIRE", "on": "fire_order"},
                    {"from": "MOVE", "to": "WAIT", "on": "arrived"},
                ],
            },
            "states": {
                "DEPLOY": {
                    "tree": {
                        "type": "action",
                        "action": "movement.move_to_point",
                        "params": {
                            "latitude_deg": {"parameter_ref": "pt_post_lat"},
                            "longitude_deg": {"parameter_ref": "pt_post_lon"},
                        },
                    }
                },
                "MOVE": {
                    "tree": {
                        "type": "parallel",
                        "children": [
                            {
                                "type": "action",
                                "action": "movement.follow_route",
                                "params": {"route_name": {"parameter_ref": "rt_approach"}},
                            },
                            {"type": "action", "action": "movement.hold_position"},
                        ],
                    }
                },
                "FIRE": {"tree": {"type": "action", "action": "engagement.fire_weapon"}},
            },
        }
    },
}


def _issue(code, platform="car_1", source="model", **data):
    return SimpleNamespace(code=code, platform=platform, source=source, data=data)


def _run(**kw):
    base = {
        "platforms": [SimpleNamespace(name="car_1", role="car", instance=0)],
        "events": [],
        "end_time_s": 120.0,
        "parameters": {"rt_approach": "ir_route_approach"},
    }
    return SimpleNamespace(**(base | kw))


def _res(states, issues=(), motion=None):
    return SimpleNamespace(passed=False, states=states, issues=list(issues), motion=motion or {})


def test_scenario_triage_names_cause_category_and_change() -> None:
    entry = {
        "id": "sc_main",
        "passed": False,
        "duration_s": 60.0,
        "transitions": {
            "car_0": [
                {"t": 0.0, "from": "DEPLOY", "to": "MOVE", "on": "x"},
                {"t": 0.0, "from": "MOVE", "to": "WAIT", "on": "arrived"},
            ]
        },
        "states": {"car_0": [[0.0, "DEPLOY"], [0.0, "MOVE"], [0.0, "WAIT"]]},
    }
    found = triage.from_scenario(entry, MODEL, {"scenarios": [{"id": "sc_main", "events": []}]})
    kinds = {f.symptom: f for f in found}
    leave = kinds["ENTER_LEAVE"]
    assert leave.category == triage.MODEL and leave.state == "MOVE"
    assert "arrived_tree: {source: tree, result: success}" in leave.render()
    assert "movement.derived.range_to_target_m" in leave.render()
    # waits for an order the scenario never gives: the verification block, not the model
    stuck = kinds["STUCK_EVENT_TEST"]
    assert stuck.category == triage.TEST and "fire_order" in stuck.render()
    assert stuck.blocks == ["verification"]
    cover = triage.from_scenario(
        {"id": "obligation_coverage", "passed": False,
         "render": "obligation_coverage: FAIL\n  [FAIL] 需求约束 ob_1（x）没有被任何想定覆盖"},
        MODEL, {},
    )  # fmt: skip
    assert cover[0].symptom == "UNCOVERED" and cover[0].values["obligation"] == "ob_1"


def test_afsim_triage_separates_sheet_from_scenario_and_judge() -> None:
    # moving to the own start point: the judge's false alarm
    res = _res(
        {"car_1": [(0.0, "DEPLOY"), (1.0, "WAIT")]},
        [_issue("NO_MOTION", t=0.0, action="movement.move_to_point")],
    )
    found = triage.from_afsim([res], [_run()], MODEL)
    assert [f.symptom for f in found] == ["NO_MOTION_AT_START"]
    assert triage.only_outside_sheet(found) and "classify" in found[0].render()
    # a route held back by a parallel hold: the sheet's
    res = _res(
        {"car_1": [(0.0, "MOVE"), (90.0, "WAIT")]},
        [_issue("NO_MOTION", t=0.0, action="movement.follow_route")],
    )
    found = triage.from_afsim([res], [_run()], MODEL)
    assert found[0].symptom == "NO_MOTION_HELD" and found[0].sheet
    assert "movement.hold_position" in found[0].render()
    # the route parameter the scenario did not give
    found = triage.from_afsim([res], [_run(parameters={})], MODEL)
    assert found[0].symptom == "NO_MOTION_UNBOUND" and not found[0].sheet
    # waiting for an optional order the generated scenario does not send
    res = _res({"car_1": [(0.0, "DEPLOY"), (5.0, "WAIT")]})
    found = triage.from_afsim([res], [_run()], MODEL, optional_events={"fire_order"})
    assert found[0].symptom == "STUCK_EVENT_UNSENT" and found[0].category == triage.SCENARIO
    # stuck on a target field with the enemy beyond radar range
    model = {
        **MODEL,
        "conditions": {**MODEL["conditions"], "seen": {"bb": "perception.selected_target.valid"}},
    }
    model["roles"] = {"car": {**MODEL["roles"]["car"], "mission": {
        **MODEL["roles"]["car"]["mission"],
        "transitions": [{"from": "WAIT", "to": "FIRE", "on": "seen"}]}}}  # fmt: skip
    motion = {
        "car_1": {"start": [0, 0], "end": [300, 0]},
        "red_1": {"start": [7000, 0], "end": [6500, 0]},
    }
    res = _res({"car_1": [(0.0, "WAIT")]}, motion=motion)
    found = triage.from_afsim([res], [_run()], model)
    assert found[0].symptom == "TARGET_OUT_OF_RANGE" and "6.2 km" in found[0].render()
    # never reached the fire state: the sheet's
    res = _res({"car_1": [(0.0, "DEPLOY")]}, [_issue("EVENTS", kind="WEAPON_FIRED")])
    found = triage.from_afsim([res], [_run(end_time_s=1.0)], MODEL)
    assert any(f.symptom == "NO_FIRE_STATE" and f.sheet for f in found)


def _with_afsim(contracts: Contracts, findings):
    from afsim_research_agent.falcon.v3.afsim_loop import AfsimNote, sheet_hash

    drafter = _fix_drafter(contracts, SHEET)
    drafter.afsim_note = AfsimNote(
        sheet_hash(drafter.sheet), "afsim: FAIL\n  问题：x", False, findings
    )
    drafter.afsim_verdicts[drafter.afsim_note.sheet_hash] = drafter.afsim_note
    return drafter


def test_loop_spends_no_step_on_failures_outside_the_sheet(contracts: Contracts) -> None:
    from afsim_research_agent.falcon.llm import ModelRunner, ScriptedModel
    from afsim_research_agent.falcon.v3.fixer import SheetFixer, fix_sheet

    judge = triage.Finding("NO_MOTION_AT_START", "afsim", "scout", "MOVE",
                           values={"action": "nav.goto", "point": "post"})  # fmt: skip
    drafter = _with_afsim(contracts, [judge])
    fixer = SheetFixer(drafter)
    if any(not s["passed"] for s in fixer.last.l3 if not s.get("afsim")):
        pytest.skip("the fixture sheet fails a 2D scenario")
    assert "判定误报" in fixer.summary() and fixer.outside_left()
    outcome = asyncio.run(fix_sheet(drafter, ModelRunner(ScriptedModel({})), max_steps=5))
    assert outcome.steps == 0 and not outcome.edits
    assert outcome.findings[0]["category"] == triage.JUDGE


def test_classify_is_refused_for_the_sheets_own_failures(contracts: Contracts) -> None:
    from afsim_research_agent.falcon.v3.fixer import SheetFixer

    stuck = triage.Finding("STUCK", "afsim", "scout", "MOVE", "0 s 起一直停在 MOVE",
                           ["mermaid:scout", "conditions"], {"exits": "MOVE → OBSERVE 当 target_seen"})  # fmt: skip
    fixer = SheetFixer(_with_afsim(contracts, [stuck]))
    assert not fixer.outside_left()
    text = fixer.classify({"check": "afsim", "category": "scenario", "reason": "敌方太远"})
    assert text.startswith("不接受") and "STUCK" not in text and "停在非终止状态" in text
    assert "category 只能是" in fixer.classify(
        {"check": "afsim", "category": "model", "reason": "x"}
    )
    # no rule finding on the check: the loop's own classification is recorded
    fixer = SheetFixer(_with_afsim(contracts, []))
    assert "已记录" in fixer.classify({"check": "afsim", "category": "judge", "reason": "误报"})
    assert fixer.outside_left()


def test_diagnosis_must_be_followed_by_an_edit(contracts: Contracts) -> None:
    from afsim_research_agent.falcon.llm import ModelRunner, ScriptedModel
    from afsim_research_agent.falcon.v3.fixer import LOOKS_AFTER_DIAGNOSIS, SheetFixer, fix_sheet

    stuck = triage.Finding("STUCK", "afsim", "scout", "MOVE", blocks=["mermaid:scout"])
    fixer = SheetFixer(_with_afsim(contracts, [stuck]))
    fixer.diagnose({"issue": "停在 MOVE", "where": ["mermaid:scout"], "cause": "c", "plan": "p"})
    fixer.looks = LOOKS_AFTER_DIAGNOSIS
    assert "诊断之后已经查看" in fixer.read_refused("show")
    # without a diagnosis, reads stop after LOOKS in a row
    fixer = SheetFixer(_with_afsim(contracts, [stuck]))
    fixer.looks = 5
    assert "定位与改法" in fixer.read_refused("lookup")
    # finish right after a diagnosis is refused (twice), then the loop ends
    plan = {"issue": "停在 MOVE", "where": ["mermaid:scout"], "cause": "c", "plan": "p"}
    steps = [
        {"action": "diagnose", "args": plan},
        {"action": "show", "args": {"block": "mermaid:scout"}},
        {"action": "show", "args": {"block": "conditions"}},
        {"action": "show", "args": {"block": "model"}},
        {"action": "finish", "args": {"summary": "x"}},
    ]
    drafter = _with_afsim(contracts, [stuck])
    outcome = asyncio.run(
        fix_sheet(drafter, ModelRunner(ScriptedModel({"fix:v3:step": steps})), max_steps=5)
    )
    results = [r["result"] for r in outcome.log]
    assert "被拒绝：诊断之后已经查看 2 次" in results[3]
    assert results[4].startswith("不接受 finish：写了诊断")
    assert outcome.diagnosed_unfixed
