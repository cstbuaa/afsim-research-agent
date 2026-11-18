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
    assert "ALLOCATING --> CEASE : engage_time_up" in text
    assert "* --> CEASE : cease_fire_order" in text
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
