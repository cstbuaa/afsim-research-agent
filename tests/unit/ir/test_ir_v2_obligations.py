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


def test_roles_judge_only_their_own_actions() -> None:
    """With the actions each platform's model runs, an obligation is judged where its action
    belongs; an ordering across vehicles is not judged on action starts."""
    adopt = Obligation(id="adopt_after_send", kind="precedes", first="lead.send", then="mem.adopt")
    stop = Obligation(
        id="stop_on_end", kind="response", on_event="end", action="mem.stop", within_s=5
    )
    acts = [(2, "lead", "lead.send"), (3, "m1", "mem.adopt"), (21, "m1", "mem.stop")]
    capable = {"lead": {"lead.send"}, "m1": {"mem.adopt", "mem.stop"}}
    (v1, v2) = check([adopt, stop], acts, [(20.0, "end")], end_s=40, capable=capable)
    assert v1.passed and not v1.judged
    assert v2.passed and v2.judged  # the leader, which has no stop action, is not judged
    late = [(2, "lead", "lead.send"), (21, "m1", "mem.adopt")]
    (_, v2) = check([adopt, stop], late, [(20.0, "end")], end_s=40, capable=capable)
    assert not v2.passed  # the member never stopped
    # without the role map every platform is judged on its own starts
    (v1, v2) = check([adopt, stop], acts, [(20.0, "end")], end_s=40)
    assert not v1.passed and not v2.passed


def test_role_actions_follow_subtrees_and_tactics() -> None:
    from afsim_research_agent.ir.v2.obligations import role_actions

    spec = {
        "subtrees": [{"id": "s1", "root": {"type": "tactic", "tactic": "lib.t"}}],
        "state_trees": {"r": {"A": "s1", "B": "lib.u"}},
    }
    tactics = {
        "lib.t": {"root": {"type": "action", "action": "x.a"}},
        "lib.u": {"root": {"type": "sequence", "children": [{"type": "action", "action": "x.b"}]}},
    }
    assert role_actions(spec, tactics) == {"r": {"x.a", "x.b"}}


def test_responses_are_judged_where_the_event_leads() -> None:
    """A role that has the responding action only in a state the event does not lead to is
    not judged on that response (e.g. a member that expires its task while holding)."""
    from afsim_research_agent.ir.v2.obligations import event_actions

    missions = {
        "m": {
            "states": ["WORK", "HOLD", "SHUTDOWN", "DONE"],
            "transitions": [
                {"from": "WORK", "to": "HOLD", "on": "lost"},
                {"from": "SHUTDOWN", "to": "DONE", "on": "stopped"},
            ],
            "global": [{"to": "SHUTDOWN", "on": "end"}],
        }
    }
    per_state = {"m": {"WORK": {"fire"}, "HOLD": {"expire"}, "SHUTDOWN": {"cease"}}}
    after = event_actions(missions, per_state)
    assert after["m"]["end"] == {"cease"}
    assert after["m"]["lost"] == {"expire", "cease"}  # HOLD, then SHUTDOWN on "end"
    expire = Obligation(
        id="expire_on_end", kind="response", on_event="end", action="expire", within_s=5
    )
    capable = {"m1": {"fire", "expire", "cease"}}
    (v,) = check([expire], [(21, "m1", "cease")], [(20.0, "end")], end_s=40, capable=capable)
    assert not v.passed
    (v,) = check(
        [expire],
        [(21, "m1", "cease")],
        [(20.0, "end")],
        end_s=40,
        capable=capable,
        after_event={"m1": after["m"]},
    )
    assert v.passed and not v.judged


def test_periodic_not_before_uses_consecutive_starts() -> None:
    every = Obligation(id="every_5s", kind="not_before", action="a.x", after="a.x", min_s=5)
    ok = [(0, "p", "a.x"), (6, "p", "a.x"), (12, "p", "a.x")]
    (v,) = check([every], ok, [], end_s=20)
    assert v.passed and v.judged
    (v,) = check([every], [(0, "p", "a.x"), (1, "p", "a.x")], [], end_s=20)
    assert not v.passed and "1 s" in v.detail
    (v,) = check([every], [(0, "p", "a.x")], [], end_s=20)
    assert not v.judged


def test_fields_of_other_kinds_are_ignored() -> None:
    ob = Obligation.model_validate(
        {"id": "a_then_b", "kind": "precedes", "first": "a", "then": "b", "within_s": 0.0}
    )
    assert ob.within_s is None and ob.first == "a"


def test_not_before_times_the_first_start_after_the_anchor() -> None:
    again = Obligation(id="reassess", kind="not_before", action="a.assess", after="a.send", min_s=5)
    acts = [(0, "p", "a.assess"), (6, "p", "a.send"), (12, "p", "a.assess")]
    (v,) = check([again], acts, [], end_s=20)
    assert v.passed and "6 s" in v.detail
    (v,) = check([again], acts[:2] + [(7, "p", "a.assess")], [], end_s=20)
    assert not v.passed


def test_periodic_starts_in_one_update_are_one_round() -> None:
    every = Obligation(id="every_5s", kind="not_before", action="a.x", after="a.x", min_s=5)
    (v,) = check([every], [(0, "p", "a.x"), (0, "p", "a.x"), (6, "p", "a.x")], [], end_s=20)
    assert v.passed
