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
    j = RunJudgement(run="strike", completed=True)
    j.states = {
        "blue_m1": [(0.0, "WAITING_TASK"), (1.0, "VALIDATING"), (1.0, "HOLDING")],
        "red_1": [(0.0, "X")],
    }
    j.errors = ["***** ERROR: Could not open D:/lab/model/native_file.txt"]
    j.completed = False
    j.problems = [
        "console has no line matching 'IR_ACT\\\\|[^|]+\\\\|blue_m1\\\\|engagement\\\\.fire_selected_weapon'",
        "console has no line matching 'WEAPON_HIT\\\\|red_1'",
        "console has no line matching 'NATIVE_SECRET\\\\|1'",
        "blue_m2: transitions kept firing from HOLDING within one update (a cycle of "
        "conditions that are all true); the update was cut after 8 state changes",
        "requirement obligation stop_on_end: blue_m1: 事件 cancelled 在 60 s，之后 "
        "engagement.cease_fire 没有执行，要求 10 s 内",
    ]
    text = feedback([j], [_run()], None)
    assert "member_0 从未开始动作 engagement.fire_selected_weapon" in text
    assert "成员车命中第 1 个敌方目标" in text
    assert "member_1 在 HOLDING" in text and "stop_on_end" in text
    assert "member_0 状态轨迹：WAITING_TASK@0 -> VALIDATING@1 -> HOLDING@1" in text
    # nothing local leaks: platform names, native markers, paths, positions, raw errors
    for leak in ("blue_", "red_1", "NATIVE_SECRET", "D:/", "native_file", "45n", "X@"):
        assert leak not in text, leak
    assert "AFSIM 没有完成仿真（输入错误" in text


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
            j.problems = ["console has no line matching 'WEAPON_HIT\\\\|red_1'"]
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
