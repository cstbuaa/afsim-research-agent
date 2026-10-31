"""AFSIM feedback in the Falcon v3 drafting loop: what the repair loop is told, and the
rounds (compile → run → repair → run again)."""

from __future__ import annotations

from pathlib import Path

from test_falcon_v3 import SHEET, contracts, set_root  # noqa: F401 - fixtures

from afsim_research_agent.falcon.v3.afsim_loop import AfsimNote, feedback, sheet_hash
from afsim_research_agent.ir.v2.afsim.binding import RunConfig
from afsim_research_agent.ir.v2.afsim.evaluate import RunJudgement

SLOWER = SHEET.replace('approach_speed: {value: "18 km/h"', 'approach_speed: {value: "15 km/h"')


def _run() -> RunConfig:
    return RunConfig.model_validate(
        {
            "id": "strike",
            "end_time_s": 60,
            "platforms": [
                {"name": "blue_lead", "role": "leader", "body": "position 45n 121w"},
                {"name": "blue_m1", "role": "member", "instance": 0},
                {"name": "blue_m2", "role": "member", "instance": 1},
            ],
            "expect": {
                "markers": ["WEAPON_HIT\\|red_1", "NATIVE_SECRET\\|1"],
                "marker_notes": {"WEAPON_HIT\\|red_1": "成员车命中第 1 个敌方目标"},
            },
        }
    )


def test_feedback_names_roles_actions_and_notes_only() -> None:
    from afsim_research_agent.ir.v2.afsim.evaluate import Issue, judge

    run = _run().model_dump()
    run["expect"]["markers"].append(r"IR_ACT\|[^|]+\|blue_m1\|engagement\.fire_selected_weapon")
    run["expect"]["events"] = [
        {"kind": "WEAPON_HIT", "by": "member", "target": "red_2", "note": "命中第 2 个敌方目标"}
    ]
    console = (
        "IR_STATE|0|blue_m1|WAITING_TASK\nIR_STATE|1|blue_m1|VALIDATING\n"
        "IR_STATE|1|blue_m1|HOLDING\nIR_STATE|0|blue_m2|WAITING_TASK\n"
        "IR_STATE|0|blue_lead|ASSESSING\nIR_LOOP|3|blue_m2|HOLDING\n"
        "IR_STATE|0|red_1|X\n"
        "***** ERROR: Could not find processor NATIVE_FIXTURE\n"
        "         'D:/lab/model/native_file.txt', line 26, near column 65\n"
    )
    evt = (
        "1.0 WEAPON_FIRED blue_m1 red_1 blue_m1_gun_1 Engagement: 1\n"
        "1.5 WEAPON_HIT blue_m1 red_1 blue_m1_gun_1 Engagement: 1 Result: DAMAGED\n"
    )
    j = judge(run, console, None, events_text=evt)
    j.add(
        Issue(
            "OBLIGATION",
            "model",
            None,
            {
                "id": "stop_on_end",
                "detail": "blue_m1: 事件 cancelled 在 60 s，之后 cease_fire 没有执行",
            },
        )
    )
    assert j.infrastructure
    text = feedback([j], [RunConfig.model_validate(run)], None)
    assert "member_0 从未开始动作 engagement.fire_selected_weapon" in text
    assert "成员车命中第 1 个敌方目标" in text  # a marker with its note
    assert "命中第 2 个敌方目标——出现 0 次" in text  # an event expectation with its note
    assert "member_0 交战：开火 1 次，命中 1 次（目标 1 1 次）" in text
    assert "member_1 在 HOLDING" in text and "stop_on_end" in text and "member_0: 事件" in text
    assert "member_0 状态轨迹：WAITING_TASK@0 -> VALIDATING@1 -> HOLDING@1" in text
    assert "出在绑定或编译器" in text
    # nothing local leaks: platform names, native names, paths, positions, raw errors
    for leak in ("blue_", "red_", "NATIVE", "D:/", "native_file", "45n", "X@", "line 26"):
        assert leak not in text, leak


def test_compile_error_is_not_echoed() -> None:
    text = feedback(None, [_run()], "binding has no entry for: action engagement.secret_leaf")
    assert "secret_leaf" not in text and "AFSIM 编译失败" in text


def test_a_failed_run_stays_failed_until_the_sheet_changes(contracts) -> None:  # noqa: ANN001, F811
    from afsim_research_agent.falcon.llm import ModelRunner, ScriptedModel
    from afsim_research_agent.falcon.sheet.blocks import split_sheet
    from afsim_research_agent.falcon.v3.draft import Drafter, V3Request

    drafter = Drafter(
        V3Request(requirement="r", model_id="scout_demo"), contracts, ModelRunner(ScriptedModel({}))
    )
    drafter.blocks = split_sheet(SHEET)[0]
    base = drafter.check(verify=True)
    assert base.verified
    drafter.afsim_note = AfsimNote(sheet_hash(drafter.sheet), "afsim: FAIL\n  问题：x")
    held = drafter.check(verify=True)
    assert not held.verified and held.l3[0]["id"] == "afsim"
    drafter.blocks = split_sheet(SLOWER)[0]
    assert drafter.check(verify=True).verified


def test_rounds_repair_and_rerun(contracts, tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001, F811
    import asyncio

    from afsim_research_agent.falcon.llm import ModelRunner, ScriptedModel
    from afsim_research_agent.falcon.sheet.blocks import split_sheet
    from afsim_research_agent.falcon.v3 import draft as D
    from afsim_research_agent.falcon.v3.afsim_loop import AfsimConfig

    drafter = D.Drafter(
        D.V3Request(requirement="r", model_id="scout_demo"),
        contracts,
        ModelRunner(ScriptedModel({})),
    )
    drafter.blocks = split_sheet(SHEET)[0]
    check = drafter.check(verify=True)
    calls: list[str] = []

    def fake_run(model, contracts, obligations, cfg, out):  # noqa: ANN001, ANN202
        calls.append(sheet_hash(drafter.sheet))
        j = RunJudgement(run="strike", completed=True)
        if len(calls) == 1:
            from afsim_research_agent.ir.v2.afsim.evaluate import Issue

            j.add(
                Issue(
                    "MARKER",
                    "run",
                    None,
                    {"pattern": "WEAPON_HIT\\|red_1", "note": "成员车命中第 1 个敌方目标"},
                )
            )
        return [j], [_run()], None

    class Fix:
        steps = 3

    async def fake_fix(d, run, steps):  # noqa: ANN001, ANN202
        # the repair loop sees the AFSIM check failing, then edits the sheet
        assert d.check(verify=True).l3[0]["id"] == "afsim"
        d.blocks = split_sheet(SLOWER)[0]
        return Fix()

    monkeypatch.setattr(D, "run_once", fake_run)
    monkeypatch.setattr(D, "fix_sheet", fake_fix)
    cfg = AfsimConfig(binding=tmp_path / "b.toml", runs=[], mission="mission", rounds=2)
    fixes: list = []
    check, history = asyncio.run(D._afsim_rounds(drafter, drafter.run, check, fixes, cfg, tmp_path))
    assert [r.passed for r in history.rounds] == [False, True] and history.passed
    assert len(fixes) == 1 and len(set(calls)) == 2 and check.verified
    assert "成员车命中第 1 个敌方目标" in history.rounds[0].feedback


def test_infrastructure_failures_get_no_repair_round(
    contracts,  # noqa: F811
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001, F811
    import asyncio

    from afsim_research_agent.falcon.llm import ModelRunner, ScriptedModel
    from afsim_research_agent.falcon.sheet.blocks import split_sheet
    from afsim_research_agent.falcon.v3 import draft as D
    from afsim_research_agent.falcon.v3.afsim_loop import AfsimConfig
    from afsim_research_agent.ir.v2.afsim.evaluate import Issue

    drafter = D.Drafter(
        D.V3Request(requirement="r", model_id="scout_demo"),
        contracts,
        ModelRunner(ScriptedModel({})),
    )
    drafter.blocks = split_sheet(SHEET)[0]
    check = drafter.check(verify=True)

    def fake_run(model, contracts, obligations, cfg, out):  # noqa: ANN001, ANN202
        j = RunJudgement(run="strike", completed=False)
        j.add(Issue("AFSIM_ERROR", "infrastructure", None, {"message": "x", "kind": "other"}))
        return [j], [_run()], None

    async def no_fix(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("no repair round for an infrastructure failure")

    monkeypatch.setattr(D, "run_once", fake_run)
    monkeypatch.setattr(D, "fix_sheet", no_fix)
    cfg = AfsimConfig(binding=tmp_path / "b.toml", runs=[], mission="mission", rounds=2)
    _, history = asyncio.run(D._afsim_rounds(drafter, drafter.run, check, [], cfg, tmp_path))
    assert len(history.rounds) == 1 and not history.passed
    assert any("基础设施" in n for n in drafter.notes)


def test_loop_feedback_names_the_transitions() -> None:
    from afsim_research_agent.ir.v2.afsim.evaluate import judge

    console = (
        "IR_STATE|0|blue_m1|WAITING_TASK\n"
        "IR_STATE|1|blue_m1|ENGAGING\nIR_STATE|1|blue_m1|REATTACKING\n"
        "IR_STATE|1|blue_m1|ENGAGING\nIR_LOOP|1|blue_m1|ENGAGING\n"
    )
    j = judge(_run().model_dump(), console, None)
    loop = next(i for i in j.issues if i.code == "IR_LOOP")
    assert loop.data["chain"] == ["ENGAGING", "REATTACKING", "ENGAGING"]
    mission = {
        "transitions": [
            {"from": "ENGAGING", "to": "REATTACKING", "on": "fired"},
            {"from": "REATTACKING", "to": "ENGAGING", "on": "reattack_allowed"},
        ],
        "global": [],
    }
    text = feedback([j], [_run()], None, {"member": mission})
    assert "ENGAGING -(fired)-> REATTACKING -(reattack_allowed)-> ENGAGING" in text
