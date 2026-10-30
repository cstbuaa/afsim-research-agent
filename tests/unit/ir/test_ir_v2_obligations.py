"""Requirement obligations judged on action starts (2D simulation and AFSIM consoles)."""

from __future__ import annotations

from afsim_research_agent.ir.v2.afsim.evaluate import judge
from afsim_research_agent.ir.v2.obligations import Obligation, check

DELAY = Obligation(
    id="scan_after_move", kind="not_before", action="sense.scan", after="nav.goto", min_s=10
)
ORDER = Obligation(id="stop_then_hold", kind="precedes", first="nav.stop", then="nav.hold")
RESPONSE = Obligation(
    id="off_on_cancel", kind="response", on_event="cancelled", action="sense.off", within_s=5
)


def test_not_before_precedes_and_response() -> None:
    late = [(0, "car_0", "nav.goto"), (11, "car_0", "sense.scan")]
    early = [(0, "car_0", "nav.goto"), (1, "car_0", "sense.scan")]
    (v,) = check([DELAY], late, [])
    assert v.passed and v.judged
    (v,) = check([DELAY], early, [])
    assert not v.passed and "不早于 10 s" in v.detail
    (v,) = check([DELAY], [(0, "car_0", "nav.goto")], [])
    assert v.passed and not v.judged  # the search never started: not judged
    (v,) = check([ORDER], [(5, "car_0", "nav.hold")], [])
    assert not v.passed
    (v,) = check([ORDER], [(4, "car_0", "nav.stop"), (5, "car_0", "nav.hold")], [])
    assert v.passed
    events = [(10.0, "cancelled")]
    (v,) = check([RESPONSE], [(12, "car_0", "sense.off")], events, end_s=60)
    assert v.passed
    (v,) = check([RESPONSE], [(30, "car_0", "sense.off")], events, end_s=60)
    assert not v.passed
    (v,) = check([RESPONSE], [], events, end_s=12)
    assert not v.judged  # the run ended too soon after the event


def test_problems_name_unknown_actions_and_missing_keys() -> None:
    bad = Obligation(id="x", kind="not_before", action="nav.fly", after="nav.goto")
    msgs = bad.problems({"nav.goto"}, set())
    assert any("min_s" in m for m in msgs) and any("nav.fly" in m for m in msgs)


def test_afsim_judge_reads_action_starts() -> None:
    console = (
        "IR_STATE|0|car|MOVE\nIR_ACT|0|car|nav.goto\nIR_ACT|1|car|sense.scan\nSimulation complete\n"
    )
    run = {"id": "r", "platforms": [{"name": "car"}], "expect": {}, "end_time_s": 30}
    j = judge(run, console, None, [DELAY.model_dump(exclude_none=True)])
    assert not j.passed and any("scan_after_move" in p for p in j.problems)
    assert "需求约束 scan_after_move" in j.render()
    ok = console.replace("IR_ACT|1|car|sense.scan", "IR_ACT|12|car|sense.scan")
    assert judge(run, ok, None, [DELAY.model_dump(exclude_none=True)]).passed
