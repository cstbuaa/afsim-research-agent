"""Fixes found in the first online P4 run (armor-eval-03, tactic tc_01)."""

from __future__ import annotations

from afsim_research_agent.falcon.autofix import autofix
from afsim_research_agent.falcon.enforce import RATIONALE_MAX, enforce
from afsim_research_agent.falcon.models import (
    CrowRef,
    EvidenceItem,
    EvidencePackage,
    FalconTrace,
    RequirementClause,
)
from afsim_research_agent.falcon.repair import CheckResult, Problem
from afsim_research_agent.falcon.review import shallow_clauses
from afsim_research_agent.ir.catalog import load_catalog


def _package(*ids: str) -> EvidencePackage:
    items = [
        EvidenceItem(
            evidence_id=i,
            claim="c",
            topic="other",
            quote="q",
            excerpt_id="x",
            excerpt="q",
            source_id="s",
            location="p.1",
            relevance_score=1,
            quality_score=1,
        )
        for i in ids
    ]
    return EvidencePackage(package_id="p", crow=CrowRef(gate_version="g"), evidence=items)


def _roles() -> list[dict]:
    def role(rid: str, states: list[str]) -> dict:
        return {
            "id": rid,
            "count": 1,
            "capability_class": "ground_vehicle",
            "dependencies": {"mover": {"required": True, "capability": "route_following_ground"}},
            "state_machine": {
                "initial_state": states[0],
                "states": [
                    {"id": s, "tree": "t", "transitions": [{"transition_id": f"{rid}_{s}"}]}
                    for s in states
                ],
            },
        }

    return [role("platoon_leader", ["travel", "contact"]), role("wingman", ["travel"])]


def test_clause_ids_cited_as_evidence_become_the_clause_evidence() -> None:
    clauses = [
        RequirementClause(id="rq_01", text="x", kind="behavior"),
        RequirementClause(
            id="tc_01_s1", text="y", kind="behavior", origin="tactic", evidence=["ev_a"]
        ),
    ]
    long = "理由 `superior_unit` " + "很长" * 400
    data = {
        "parameters": {},
        "coordination": [
            {
                "id": "c1",
                "basis": {"status": "inferred", "evidence": ["tc_01_s1"], "rationale": "r"},
            },
            {"id": "c2", "basis": {"status": "inferred", "evidence": ["rq_01"], "rationale": long}},
        ],
    }
    notes = enforce(data, _package("ev_a"), [], clauses, {})
    first, second = (c["basis"] for c in data["coordination"])
    assert first["evidence"] == ["ev_a"] and first["status"] == "inferred"
    assert second["status"] == "unknown"  # the requirement clause has no evidence
    assert "`" not in second["rationale"] and len(second["rationale"]) == RATIONALE_MAX + 1
    rules = {n.rule for n in notes}
    assert {"clause_as_evidence", "rationale_cleaned", "rationale_trimmed"} <= rules


def test_undeclared_components_get_defaults_and_generated_copies_go() -> None:
    data = {
        "roles": _roles(),
        "coordination": [
            {
                "id": "lf",
                "pattern": "leader_follower",
                "leader": "platoon_leader",
                "followers": "wingman",
            }
        ],
        "conditions": [
            {"id": "lf__formation_known", "expression": {"literal": True}},
            {"id": "seen", "expression": {"sensor_value": {"property": "on"}}},
        ],
        "behaviors": [
            {
                "id": "shoot",
                "description": "用 `main_gun` 射击 {{x}}",
                "actions": [{"command": "fire_weapon", "arguments": {"weapon": "gun_1"}}],
            }
        ],
        "formations": [{"id": "groups", "slots": []}, {"id": "wedge", "slots": [{"x": 1}]}],
        "scenarios": [
            {"id": "s", "traces_to": ["wedge", "coord_follow", "lf", "platoon_leader_travel"]}
        ],
    }
    notes = autofix(data, load_catalog())
    leader, wing = (r["dependencies"] for r in data["roles"])
    assert leader["comms"][0]["capability"] == "voice_data" and wing["comms"]
    assert leader["weapons"][0]["id"] == "gun_1" and leader["sensors"]
    assert [c["id"] for c in data["conditions"]] == ["seen"]
    assert data["conditions"][0]["expression"]["sensor_value"]["sensor"] == "vehicle_sight"
    assert data["behaviors"][0]["description"] == "用 main_gun 射击 x"
    assert [f["id"] for f in data["formations"]] == ["wedge"]
    assert data["scenarios"][0]["traces_to"] == ["wedge", "lf", "platoon_leader_travel"]
    assert any("简化处理" in n.detail for n in notes)


def test_unloadable_draft_never_counts_as_the_best() -> None:
    def check(n: int, loaded: bool) -> CheckResult:
        problems = [Problem(None, "X", "$", "m") for _ in range(n)]
        return CheckResult([], None, None, problems, None, loaded)

    # 2 errors only because loading stopped validation, vs 24 real errors
    assert check(24, True).score < check(2, False).score
    assert check(3, True).score < check(24, True).score


def test_clauses_covered_only_by_roles_are_flagged() -> None:
    trace = FalconTrace(
        model_id="m",
        falcon_version="f",
        prompt_version="p",
        catalog_versions={},
        clauses=[
            RequirementClause(id="tc_01_s1", text="a", kind="behavior", origin="tactic"),
            RequirementClause(id="tc_01_s2", text="b", kind="behavior", origin="tactic"),
        ],
        implements={
            "platoon_leader": ["tc_01_s1", "tc_01_s2"],
            "platoon_leader.travel": ["tc_01_s1"],
            "lf_transition": ["tc_01_s2"],
        },
    )
    assert shallow_clauses(trace, {"roles": _roles()}) == ["tc_01_s1"]
