"""Scenario variants that trigger a model's branches (orchestrator.scenario_plan)."""

from __future__ import annotations

from afsim_research_agent.orchestrator import scenario_plan as SP

VALID = {"bb": "perception.selected_target.valid"}
RANGE = {"bb": "perception.selected_target.range_m"}


def _model(order: bool = True) -> dict:
    t = lambda a, b, on: {"from": a, "to": b, "on": on, "priority": 0}  # noqa: E731
    leader = {
        "initial_state": "SEARCH_ADVANCE",
        "transitions": [
            t("SEARCH_ADVANCE", "CONTACT_DEPLOY", "target_detected"),
            t("CONTACT_DEPLOY", "STANDOFF_TRACK", "line_ready"),
            t("STANDOFF_TRACK", "SHUTDOWN", "too_close"),
            t("STANDOFF_TRACK", "TARGET_LOST_HOLD", "target_lost"),
            t("TARGET_LOST_HOLD", "STANDOFF_TRACK", "target_detected"),
            t("TARGET_LOST_HOLD", "RESUME_SEARCH", "reacquire_time_up"),
            t("SHUTDOWN", "COMPLETE", "shutdown_done"),
        ],
        "global": [{"to": "SHUTDOWN", "on": "cancelled", "priority": 100}]
        + ([{"to": "SHUTDOWN", "on": "withdraw_order", "priority": 100}] if order else []),
    }
    return {
        "parameters": {"withdraw_threshold_m": {"value": {"value": 2000, "unit": "m"}}},
        "conditions": {
            "target_detected": VALID,
            "line_ready": {"compare": {"left": {"bb": "x"}, "operator": "lte", "right": 20}},
            "target_lost": {"all": [{"not": VALID}, {"compare": {"left": {"bb": "t"}, "operator": "gte", "right": 3}}]},
            "too_close": {"all": [VALID, {"compare": {"left": RANGE, "operator": "lt", "right": {"parameter_ref": "withdraw_threshold_m"}}}]},
        },
        "events": {
            "cancelled": {"source": "external"},
            "withdraw_order": {"source": "external"},
            "reacquire_time_up": {"source": "timer", "after_s": 30.0},
            "shutdown_done": {"source": "tree", "result": "success"},
        },
        "roles": {
            "leader": {"role": {"count": 1}, "mission": leader},
            "wingman": {"role": {"count": 2}, "mission": {"transitions": [], "global": []}},
        },
    }  # fmt: skip


BASE = {"enemy_range_m": 8000.0, "sensor_range_m": 6000.0, "end_s": 430.0}


def test_the_branches_of_the_leader_are_found():
    found = {b.id: b for b in SP.branches(_model())}
    assert set(found) == {
        "leader_lost_reacquire", "leader_lost_timeout", "leader_close", "leader_order_withdraw_order"
    }  # fmt: skip
    assert found["leader_lost_reacquire"].path == [
        "STANDOFF_TRACK", "TARGET_LOST_HOLD", "STANDOFF_TRACK"
    ]  # fmt: skip
    assert found["leader_lost_timeout"].path[-1] == "RESUME_SEARCH"
    assert found["leader_lost_timeout"].timeout_s == 30.0
    assert found["leader_close"].threshold_m == 2000.0
    assert found["leader_order_withdraw_order"].path == ["SHUTDOWN"]


def test_questions_offer_recommended_answers_and_variants_follow_them():
    found = SP.branches(_model())
    qs = SP.questions(found, BASE)
    ids = [q["id"] for q in qs]
    assert ids[:2] == ["branches", "enemy_range_m"] and "lost_for_s" in ids
    assert "close_speed_mps" in ids and "order_at_withdraw_order" in ids
    assert all(any("推荐" in o["label"] for o in q["options"]) for q in qs)
    answers = SP.recommended(qs)
    vs = {v["branch"]: v for v in SP.variants(found, answers, BASE, {"STANDOFF_TRACK": 298.0})}
    assert vs["leader_lost_reacquire"]["overrides"]["enemy"]["gone"] == [[358, 373]]
    assert vs["leader_lost_timeout"]["overrides"]["enemy"]["gone"] == [[358, 408]]
    close = vs["leader_close"]["overrides"]["enemy"]
    assert close == {"approach_speed_mps": 15.0, "stop_range_m": 1200.0, "approach_start_s": 328}
    assert vs["leader_order_withdraw_order"]["overrides"]["events"]["withdraw_order"] == 328
    assert "基准 AFSIM 运行" in vs["leader_close"]["note"]
    # nobody gave a baseline run: the time is estimated from the scenario
    est = SP.variants(found, answers | {"branches": ["leader_close"]}, BASE)
    assert len(est) == 1 and "按想定估计" in est[0]["note"]


def test_a_short_lost_answer_runs_only_the_reacquire_case_and_paths_are_judged_in_order():
    found = SP.branches(_model(order=False))
    vs = SP.variants(found, {"lost_for_s": "short"}, BASE, {"STANDOFF_TRACK": 100.0})
    assert {v["branch"] for v in vs} == {"leader_lost_reacquire", "leader_close"}
    states = SP.parse_states("A@0 → STANDOFF_TRACK@100 → TARGET_LOST_HOLD@163 → STANDOFF_TRACK@178")
    assert SP.path_met(states, ["STANDOFF_TRACK", "TARGET_LOST_HOLD", "STANDOFF_TRACK"])
    assert not SP.path_met(states, ["TARGET_LOST_HOLD", "RESUME_SEARCH"])
