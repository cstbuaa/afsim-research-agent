"""A draft must do what it claims (falcon.v3.semantics) and core tactic elements the
capability set cannot carry block the draft (crow_bridge.blocked_elements)."""

from __future__ import annotations

from types import SimpleNamespace

from afsim_research_agent.falcon.v3 import crow_bridge as B
from afsim_research_agent.falcon.v3 import semantics as S
from afsim_research_agent.falcon.v3.draft import Check, coverage
from afsim_research_agent.ir.v2.obligations import Obligation


def _spec(swap_tree):
    return {
        "parameters": {"spacing": {"value": 250}, "speed": {"value": 10}},
        "conditions": [
            {"id": "far", "expression": {"bb": "x"}},
            {"id": "lead_c1", "expression": {"literal": True}},
            {"id": "always", "expression": {"literal": True}},
        ],
        "subtrees": [
            {"id": "lead_swap", "root": swap_tree},
            {
                "id": "move",
                "root": {
                    "type": "selector",
                    "children": [
                        {"type": "condition", "condition": "far"},
                        {"type": "condition", "condition": "lead_c1"},
                    ],
                },
            },
            {
                "id": "go",
                "root": {
                    "type": "action",
                    "action": "movement.follow_route",
                    "params": {"speed": {"parameter_ref": "speed"}},
                },
            },
        ],
        "state_trees": {"lead": {"SWAP": "lead_swap", "MOVE": "go"}},
    }


def test_claims_without_actions_or_with_look_alikes_are_problems() -> None:
    clauses = [
        {"id": "tc_01_s3", "text": "跃进"},
        {"id": "tc_01_s4", "text": "交换角色"},
        {"id": "tc_01_s5", "text": "报告"},
    ]
    implements = {"tc_01_s3": ["lead.MOVE"], "tc_01_s4": ["lead.SWAP"]}
    empty = {"type": "condition", "condition": "far"}
    found = {p[2] for p in S.problems(_spec(empty), implements, clauses, {}, {})}
    assert {"IMPL_EMPTY", "IMPL_MISSING", "CONST_COND", "UNUSED_PARAM"} <= found
    # a look-alike action stands in for the mapped one
    look = {"type": "action", "action": "formation.compute_slots"}
    cmap = {"tc_01_s4": {"actions": ["roles.swap"]}}
    codes = [p[2] for p in S.problems(_spec(look), implements, clauses[:2], cmap, {})]
    assert "IMPL_MISMATCH" in codes and "IMPL_EMPTY" not in codes
    msgs = [p[1] for p in S.problems(_spec(look), implements, [], {}, {})]
    # the constant fallback in a selector, and the named constant; not the helper itself
    assert sum("CONST_COND" in m for m in msgs) == 2
    assert any("spacing" in m for m in msgs) and not any("参数 speed" in m for m in msgs)


def test_core_elements_the_set_cannot_carry_are_blocked() -> None:
    clauses = [
        {"id": "tc_01_s3", "origin": "tactic"},
        {"id": "tc_01_s4", "origin": "tactic"},
        {"id": "tc_01_x1", "origin": "tactic"},
        {"id": "rq_01"},
    ]
    entries = [
        {"elements": ["tc_01_x1"], "missing": True, "need": "规避", "missing_text": "火力扇区"},
        {"elements": ["tc_01_s3"], "missing": False, "need": "推进"},
    ]
    cmap = {"tc_01_s4": {"actions": [], "missing": "按周期交换掩护/跃进角色"}}
    blocked = B.blocked_elements(clauses, entries, cmap)
    assert blocked == {"tc_01_s4": "按周期交换掩护/跃进角色", "tc_01_x1": "火力扇区"}
    assert B.is_core("tc_01_s4") and B.is_core("tc_01_g1") and not B.is_core("tc_01_x1")
    text = B.map_text({"tc_01_s3": {"actions": ["movement.follow_route"]}, **cmap}, blocked)
    assert "movement.follow_route" in text and "暂缺" in text


def test_a_clause_no_run_judged_is_not_verified() -> None:
    drafter = SimpleNamespace(
        clauses=[
            {"id": "tc_01_s3", "origin": "tactic", "text": "跃进"},
            {"id": "tc_01_s4", "origin": "tactic", "text": "交换"},
        ],
        obligations=[
            Obligation(id="turns", kind="moves_in_turn", clause="tc_01_s3, tc_01_s4"),
            Obligation(id="order", kind="precedes", first="a.x", then="a.y", clause="tc_01_s3"),
        ],
    )
    check = Check(None, None, [], [], l3=[{"obligations": [{"id": "order", "passed": True}]}])
    cover = coverage(drafter, check)
    assert cover["tc_01_s3"]["verified"] and cover["tc_01_s3"]["effect"] == []
    assert not cover["tc_01_s4"]["verified"]
    history = SimpleNamespace(rounds=[SimpleNamespace(judged={"turns": True})])
    cover = coverage(drafter, check, history)
    assert cover["tc_01_s4"]["verified"] and cover["tc_01_s4"]["effect"] == ["turns"]
