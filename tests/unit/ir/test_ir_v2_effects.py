"""Effects of a run on sampled positions (ir.v2.effects) and orderings across vehicles."""

from __future__ import annotations

from afsim_research_agent.ir.v2 import effects as E
from afsim_research_agent.ir.v2.afsim.evaluate import judge
from afsim_research_agent.ir.v2.obligations import Obligation, check

OWN = {"b1": "bounding", "o1": "overwatch", "o2": "overwatch"}


def _console(rows: list[tuple[float, str, str, float, float, float]]) -> str:
    """rows: (t, platform, side, north m, east m, speed)"""
    lines = ["IR_POS_ORIGIN|0|0"]
    lines += [f"IR_POS|{t}|{p}|{side}|{n}|{e}|{v}|90" for t, p, side, n, e, v in rows]
    return "\n".join(lines)


def _bounding_overwatch() -> str:
    """b1 bounds 0–10 s, o1 bounds 10–20 s, b1 again 20–30 s; o2 never moves; the enemy
    sits 1500 m east."""
    rows = []
    pos = {"b1": 0.0, "o1": 0.0, "o2": 0.0}
    for t in range(0, 41):
        mover = "b1" if t < 10 or 20 <= t < 30 else ("o1" if 10 <= t < 20 else None)
        for p in pos:
            moving = p == mover
            rows.append(
                (t, p, "blue", {"b1": 0, "o1": 100, "o2": -100}[p], pos[p], 8.0 if moving else 0.0)
            )
            if moving:
                pos[p] += 8.0
        rows.append((t, "red_1", "red", 0, 1500, 0.0))
    return _console(rows)


def test_parse_and_effect_obligations() -> None:
    tracks = E.parse(_bounding_overwatch())
    assert set(tracks.by_platform) == {"b1", "o1", "o2", "red_1"}
    obs = [
        Obligation(id="dist", kind="keeps_distance", min_m=1200),
        Obligation(id="dist_bad", kind="keeps_distance", min_m=1450),
        Obligation(id="never_all", kind="not_all_moving"),
        Obligation(id="turns", kind="moves_in_turn", min_bounds=3),
        Obligation(id="adv", kind="advances", min_m=50, roles=["bounding"]),
        Obligation(id="still", kind="stays_still", action="movement.stop", roles=["overwatch"]),
    ]
    acts = [(0.0, "o2", "movement.stop")]
    got = {v.id: v for v in E.judge(obs, tracks, OWN, acts, [], 40)}
    assert got["dist"].passed and not got["dist_bad"].passed
    assert got["never_all"].passed
    assert got["turns"].passed, got["turns"].detail
    assert got["adv"].passed
    assert got["still"].passed and got["still"].judged
    # one vehicle moving twice in a row is not bounding overwatch
    two = Obligation(id="t2", kind="moves_in_turn", min_bounds=2, roles=["bounding"])
    assert not E.judge([two], tracks, OWN, acts, [], 40)[0].passed
    # two vehicles moving at the same time are not moving in turns
    rows = [
        (t, p, "blue", n, 8.0 * t, 8.0)
        for t in range(0, 10)
        for p, n in (("o1", 100), ("o2", -100))
    ]
    rows += [
        (t, "b1", "blue", 0, 80 + 8.0 * (t - 10), 8.0 if t < 20 else 0.0) for t in range(10, 21)
    ]
    together = E.parse(_console(rows))
    v = E.judge([Obligation(id="t", kind="moves_in_turn")], together, OWN, [], [], 20)[0]
    assert not v.passed and "同时机动" in v.detail
    # without positions an effect obligation is not judged (the 2D simulation)
    assert not E.judge(obs[:1], E.Tracks(), OWN, acts, [], 40)[0].judged


def test_a_vehicle_that_keeps_moving_after_its_stop_is_reported() -> None:
    rows = [(t, "b1", "blue", 0, 5.0 * t, 5.0) for t in range(0, 30)]
    tracks = E.parse(_console(rows))
    issues = E.motion_issues(tracks, {"b1": "bounding"}, [(10.0, "b1", "movement.stop")])
    assert [i["kind"] for i in issues] == ["STOP_DRIFT"] and issues[0]["drift_m"] > 50
    # the standoff step drives a stopped leader on purpose (approach / back off): no drift
    acts = [(10.0, "b1", "movement.stop"), (12.0, "b1", "formation.command_standoff")]
    assert not E.motion_issues(tracks, {"b1": "leader"}, acts)
    # a route that does not move the vehicle
    still = E.parse(_console([(t, "b1", "blue", 0, 0, 0.0) for t in range(0, 30)]))
    issues = E.motion_issues(still, {"b1": "bounding"}, [(2.0, "b1", "movement.follow_route")])
    assert [i["kind"] for i in issues] == ["NO_MOTION"]


def test_orderings_across_vehicles_are_judged_on_completions() -> None:
    ob = Obligation(id="set_then_move", kind="precedes", first="comms.report_set", then="move.go")
    acts = [(0.0, "o1", "move.go"), (1.0, "b1", "comms.report_set")]
    # without completions: not judged (starts carry no causality across vehicles)
    assert not check([ob], acts, [])[0].judged
    # o1 completes its move only after b1 reported: satisfied
    done = [(5.0, "b1", "comms.report_set"), (6.0, "o1", "move.go")]
    v = check([ob], acts, [], completions=done)[0]
    assert v.judged and v.passed
    # o1 completed before anyone reported: violated
    done = [(2.0, "o1", "move.go"), (8.0, "b1", "comms.report_set")]
    v = check([ob], acts, [], completions=done)[0]
    assert v.judged and not v.passed


def test_the_afsim_judge_reports_motion_and_effects() -> None:
    console = _bounding_overwatch() + "\nIR_ACT|30|b1|movement.stop\nSimulation complete\n"
    for p in OWN:
        console += f"\nIR_STATE|0|{p}|START"
    run = {
        "id": "r",
        "platforms": [{"name": p, "role": r} for p, r in OWN.items()],
        "events": [],
        "end_time_s": 40,
    }
    obs = [{"id": "turns", "kind": "moves_in_turn", "min_bounds": 3}]
    j = judge(run, console, None, obligations=obs)
    assert j.motion["b1"]["path_m"] > 100 and j.motion["o2"]["path_m"] == 0
    assert any(v["id"] == "turns" and v["passed"] for v in j.obligations)
    assert "moved" in j.render()
