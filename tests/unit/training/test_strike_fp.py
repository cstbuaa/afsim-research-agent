"""Strike skeletons (training.strike_fp): every state has a way out, modules change the
machines, the sheet carries tests, scenarios and obligations."""

from __future__ import annotations

import re

import yaml

from afsim_research_agent.training.strike_fp import StrikeSpec, catalog, roles, sheet


def test_every_member_step_has_a_success_and_a_failure_exit():
    mac = roles(StrikeSpec())["member"][1]
    exits = {}
    for a, _b, c in mac.transitions:
        exits.setdefault(a, set()).add(c)
    for st in ("VALIDATING", "SELECTING", "FIRING", "OBSERVING"):
        assert len(exits[st]) == 2, st
    assert ("OBSERVING", "VALIDATING", "reattack") in mac.transitions
    once = roles(StrikeSpec(reattack=False))["member"][1]
    assert [t for t in once.transitions if t[0] == "OBSERVING"] == [
        ("OBSERVING", "WAITING_TASK", "engagement_done")
    ]


def test_the_leader_allocates_and_broadcasts_on_every_update():
    text, request = sheet(StrikeSpec(engage_limit_s=40, cease_order="cease_fire_order"))
    tree = re.search(r"def ALLOCATING\(\):\n(.*?)\n\n", text, re.S).group(1)
    assert tree.strip().splitlines()[-1].strip() == "threat_allocation.broadcast_assignments()"
    assert "ALLOCATING --> HOLD : engage_time_up" in text
    assert "* --> HOLD : cease_fire_order" in text and "* --> CEASE : cancelled" in text
    ver = yaml.safe_load(re.search(r"```yaml verification\n(.*?)```", text, re.S).group(1))
    ids = {sc["id"] for sc in ver["scenarios"]}
    assert {"engage_two_targets", "no_targets", "member_radio_lost", "engage_time_limit",
            "cease_order"} <= ids  # fmt: skip
    assert {o["id"] for o in request["obligations"]} == {
        "allocate_before_fire", "cease_on_cancel", "cease_on_order"
    }  # fmt: skip
    assert set(request["scenario_events"]) == {"cancelled", "cease_fire_order"}


def test_the_catalog_ids_are_distinct():
    ids = [s.model_id for s in catalog()]
    assert len(ids) == len(set(ids)) == 6


def test_failure_tests_name_the_tree_event_so_the_reattack_cycle_is_not_followed():
    text, _ = sheet(StrikeSpec())
    ver = yaml.safe_load(re.search(r"```yaml verification\n(.*?)```", text, re.S).group(1))
    t = next(t for t in ver["tests"] if t["id"] == "t_member_03_validating_waiting_task")
    assert t["given"]["events"] == ["task_rejected"] and "tree_status" not in t["given"]


def test_without_a_limit_or_order_there_is_no_hold_state():
    assert "HOLD" not in roles(StrikeSpec())["leader"][1].states
    assert "HOLD" in roles(StrikeSpec(engage_limit_s=40))["leader"][1].states
    assert "HOLD" not in roles(StrikeSpec(engage_limit_s=40))["member"][1].states
    assert "HOLD" in roles(StrikeSpec(cease_order="x"))["member"][1].states


def test_the_end_of_the_task_drops_the_assignment():
    for r in ("leader", "member"):
        tree = roles(StrikeSpec())[r][1].trees["CEASE"]
        assert "threat_allocation.expire_assignment()" in tree and "cease_fire" in tree
