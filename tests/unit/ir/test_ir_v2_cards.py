"""Capability cards: who can run a capability and whether it ends (ir.v2.cards)."""

from __future__ import annotations

from afsim_research_agent.ir.v2.cards import CONDITIONAL, ENDS, NEVER, cards, tree_card
from afsim_research_agent.ir.v2.contracts import Contracts


def test_cards_of_the_capability_set() -> None:
    c = Contracts.load()
    k = cards(c)
    assert (k["movement.follow_route"].min_vehicles, k["movement.follow_route"].termination) == (
        1,
        ENDS,
    )
    assert k["movement.hold_position"].termination == NEVER
    assert k["perception.search_for_target"].termination == CONDITIONAL
    assert k["perception.track_selected_target"].termination == NEVER
    advance = k["formation_standoff.formation_search_advance"]
    assert advance.min_vehicles == 2 and advance.termination == CONDITIONAL
    assert "column_slots" in advance.reason
    assert advance.tags().startswith("[需≥2车]")


def test_a_model_tree_takes_the_worst_of_what_must_all_finish() -> None:
    c = Contracts.load()
    route_then_stop = {
        "type": "sequence_with_memory",
        "children": [
            {"type": "action", "action": "movement.follow_route"},
            {"type": "action", "action": "movement.stop"},
        ],
    }
    assert tree_card(c, route_then_stop).termination == ENDS
    hold_and_track = {
        "type": "parallel",
        "policy": {"type": "all"},
        "children": [
            {"type": "action", "action": "movement.hold_position"},
            {"type": "tactic", "tactic": "perception.track_selected_target"},
        ],
    }
    assert tree_card(c, hold_and_track).termination == NEVER
    by_id = tree_card(c, {"type": "subtree", "subtree": "go"}, {"go": route_then_stop})
    assert by_id.termination == ENDS and by_id.min_vehicles == 1


def test_commanding_capabilities_say_the_leader_runs_them() -> None:
    """Formation steps that command the followers, and the trees that contain them, are the
    leader's (r3, 11-04: members ran the leader's search-advance tree and never moved)."""
    k = cards(Contracts.load())
    for cid in (
        "formation.column_slots",
        "formation.target_line_slots",
        "formation_standoff.formation_search_advance",
        "formation_standoff.formation_standoff_track",
    ):
        assert k[cid].commander, cid
    for cid in (
        "formation.command_standoff",
        "movement.follow_route",
        "perception.search_for_target",
    ):
        assert not k[cid].commander, cid
    tags = k["formation_standoff.formation_search_advance"].tags("要等僚车到位")
    assert tags == "[需≥2车][满足条件才结束：要等僚车到位][长车执行，指挥僚车；僚车不要用]"
