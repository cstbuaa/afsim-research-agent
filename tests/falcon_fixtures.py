"""Armor-platoon Crow result and scripted model answers for Falcon tests.

The excerpts are short, invented paraphrases written for tests (not quotes
from any publication); numbers are illustrative.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from afsim_research_agent.crow.models import (
    CrowResearchResult,
    QuestionCoverage,
    ReadinessAssessment,
    ReadinessCheck,
    TacticalResearchPlan,
)
from afsim_research_agent.falcon.evidence import collect_excerpts, evidence_id
from afsim_research_agent.literature import PaperQAEvidence, PaperQAReport, SourceCollection
from afsim_research_agent.models import SourceQuality, SourceReference

REQUIREMENT = (
    "研究装甲排在开阔地和城区的编队方式：开阔地用楔形，接近城区时转纵队，纵队车距 50 米；"
    "接敌后报告并交替跃进，与敌保持安全距离。"
)
QUESTIONS = [
    "开阔地与城区分别采用什么队形，车间距多少？",
    "什么条件触发队形转换？",
    "接敌后如何报告与交替跃进，跃进距离和安全距离多少？",
]

EXCERPTS: dict[str, dict[str, Any]] = {
    "wedge": {
        "source": "doc_a",
        "q": 0,
        "location": "ch.3 p.12",
        "text": "In open terrain the platoon normally moves in a wedge formation, "
        "with 50 to 100 meters between vehicles.",
    },
    "column": {
        "source": "doc_b",
        "q": 1,
        "location": "p.4",
        "text": "When approaching built-up areas the platoon changes to a column, "
        "keeping intervals of about 50 meters; the change starts roughly 200 m before the urban edge.",
    },
    "wide": {
        "source": "paper_c",
        "q": 0,
        "location": "sec.2",
        "text": "Simulation results suggest wedge intervals of 150 to 300 m reduce exposure to indirect fire.",
    },
    "contact": {
        "source": "doc_a",
        "q": 2,
        "location": "ch.4 p.20",
        "text": "Upon contact the wingmen report to the platoon leader and the platoon "
        "moves by bounding overwatch; each bound is typically 300 meters.",
    },
    "standoff": {
        "source": "local_notes",
        "q": 2,
        "location": "notes p.1",
        "text": "Bounding elements should stop at least 800 m from the enemy position.",
    },
}

# (excerpt key) -> claims the scripted model returns
CLAIMS: dict[str, list[dict[str, Any]]] = {
    "wedge": [
        {
            "statement": "开阔地形坦克排通常采用楔形队形",
            "topic": "formation",
            "subject": "坦克排",
            "condition": "开阔地形",
            "quote": "In open terrain the platoon normally moves in a wedge formation",
        },
        {
            "statement": "楔形队形车间距 50–100 m",
            "topic": "spacing",
            "subject": "坦克排",
            "condition": "楔形",
            "quantity": {"name": "wedge interval", "min": 50, "max": 100, "unit": "meters"},
            "quote": "with 50 to 100 meters between vehicles",
        },
    ],
    "column": [
        {
            "statement": "接近城区时转为纵队",
            "topic": "transition",
            "subject": "坦克排",
            "condition": "接近城区",
            "quote": "When approaching built-up areas the platoon changes to a column",
        },
        {
            "statement": "纵队车间距约 50 m",
            "topic": "spacing",
            "subject": "坦克排",
            "condition": "纵队",
            "quantity": {"name": "column_interval", "min": 50, "max": 50, "unit": "meters"},
            "quote": "keeping intervals of about 50 meters",
        },
        {
            "statement": "距城区约 200 m 开始转换队形",
            "topic": "transition",
            "subject": "坦克排",
            "condition": "接近城区",
            "quantity": {"name": "urban_transition_distance", "min": 200, "max": 200, "unit": "m"},
            "quote": "the change starts roughly 200 m before the urban edge",
        },
    ],
    "wide": [
        {
            "statement": "楔形队形车间距 150–300 m",
            "topic": "spacing",
            "subject": "坦克排",
            "condition": "楔形",
            "quantity": {"name": "wedge_interval", "min": 150, "max": 300, "unit": "m"},
            "quote": "wedge intervals of 150 to 300 m",
        },
        {
            # invented quote: must be dropped by verification
            "statement": "楔形横向间距 120 m",
            "topic": "spacing",
            "quantity": {"name": "wedge_width", "min": 120, "max": 120, "unit": "m"},
            "quote": "lateral spacing of 120 m",
        },
    ],
    "contact": [
        {
            "statement": "接敌后僚车向排长报告",
            "topic": "contact",
            "subject": "僚车",
            "condition": "接敌",
            "quote": "Upon contact the wingmen report to the platoon leader",
        },
        {
            "statement": "接敌后以交替跃进机动，每次跃进约 300 m",
            "topic": "bounding",
            "subject": "坦克排",
            "condition": "接敌",
            "quantity": {"name": "bound_distance", "min": 300, "max": 300, "unit": "meters"},
            "quote": "moves by bounding overwatch; each bound is typically 300 meters",
        },
    ],
    "standoff": [
        {
            "statement": "跃进分队应在距敌至少 800 m 处停止",
            "topic": "bounding",
            "subject": "跃进分队",
            "condition": "接敌",
            "quantity": {"name": "standoff_distance", "min": 800, "max": 800, "unit": "m"},
            "quote": "Bounding elements should stop at least 800 m from the enemy position",
        },
    ],
}


def _source(source_id: str, tier: str, kind: str = "official_documentation") -> SourceReference:
    from datetime import UTC, datetime

    return SourceReference(
        source_id=source_id,
        kind=kind,
        title=f"Title {source_id}",
        uri=f"https://example.org/{source_id}"
        if kind != "local_document"
        else f"/home/u/{source_id}.pdf",
        retrieved_at=datetime(2026, 10, 12, tzinfo=UTC),
        content_hash=f"hash-{source_id}",
        quality=SourceQuality(
            score={"A": 0.85, "B": 0.6}[tier], tier=tier, reasons=["t"], rules_version="t"
        ),
    )


def crow_result(*, ready: bool = True) -> CrowResearchResult:
    reports = []
    for q_index, question in enumerate(QUESTIONS):
        evidence = [
            PaperQAEvidence(
                citation=f"Citation {spec['source']}",
                excerpt=spec["text"],
                summary="summary",
                location=spec["location"],
                score=8,
                source_id=spec["source"],
                source_quality_tier="A",
            )
            for spec in EXCERPTS.values()
            if spec["q"] == q_index
        ]
        reports.append(PaperQAReport(question=question, answer="answer", evidence=evidence))
    sources = [
        _source("doc_a", "A"),
        _source("doc_b", "B"),
        _source("paper_c", "B", kind="paper"),
        _source("local_notes", "B", kind="local_document"),
    ]
    checks = [
        ReadinessCheck(name="parsed_sources", passed=True, value=4, threshold=3, detail=""),
        ReadinessCheck(
            name="authoritative_sources",
            passed=ready,
            value=2 if ready else 1,
            threshold=2,
            detail="",
        ),
    ]
    return CrowResearchResult(
        plan=TacticalResearchPlan(
            requirement_summary="装甲排地形自适应编队",
            research_questions=QUESTIONS,
            search_queries=["tank platoon formations"],
        ),
        collection=SourceCollection(
            requirement=REQUIREMENT, queries=["tank platoon formations"], sources=sources
        ),
        reports=reports,
        readiness=ReadinessAssessment(
            ready=ready,
            checks=checks,
            coverage=[
                QuestionCoverage(
                    question=q,
                    strong_evidence=2 if ready else 0,
                    authoritative_evidence=1,
                    distinct_sources=2,
                    insufficient_evidence=not ready,
                )
                for q in QUESTIONS
            ],
            gate_version="falcon-gate-2026.10.1",
        ),
    )


def excerpt_ids(crow: CrowResearchResult) -> dict[str, str]:
    """excerpt key -> excerpt_id"""
    out = {}
    for ex in collect_excerpts(crow, 0):
        for key, spec in EXCERPTS.items():
            if spec["text"] == ex.excerpt:
                out[key] = ex.excerpt_id
    return out


def ev(key: str, index: int) -> str:
    """Evidence id of CLAIMS[key][index] as Falcon computes it."""
    spec = EXCERPTS[key]
    claim = CLAIMS[key][index]
    return evidence_id(spec["source"], spec["location"], claim["quote"], claim["statement"])


def extraction_handler(prompt: str, schema, index: int) -> dict[str, Any]:
    ids = re.findall(r"^\[(ex_[0-9a-f]{10})\]", prompt, flags=re.M)
    by_text = {spec["text"]: key for key, spec in EXCERPTS.items()}
    items = []
    for eid in ids:
        block = prompt.split(f"[{eid}]", 1)[1].split("\n", 2)[1]
        key = by_text.get(block.strip())
        items.append({"excerpt_id": eid, "claims": CLAIMS.get(key, []) if key else []})
    return {"items": items}


def grouping_handler(prompt: str, schema, index: int) -> dict[str, Any]:
    """Groups the two wedge-interval claims together and calls them consistent
    (the numeric rule must still flag the conflict); other claims stay single."""
    import json

    lines = [json.loads(line) for line in prompt.splitlines() if line.startswith("{")]
    ids = [item["evidence_id"] for item in lines]
    wedge = {ev("wedge", 1), ev("wide", 0)}
    groups = []
    together = [i for i in ids if i in wedge]
    if together:
        groups.append({"statement": "楔形队形车间距", "members": together})
    groups += [{"statement": "", "members": [i]} for i in ids if i not in wedge]
    return {"groups": groups, "conflicts": []}


def write_request(tmp: Path, crow: CrowResearchResult, **overrides: Any) -> Path:
    (tmp / "crow").mkdir(parents=True, exist_ok=True)
    (tmp / "crow" / "research_result.json").write_text(crow.model_dump_json(), encoding="utf-8")
    request = {
        "requirement": REQUIREMENT,
        "model_id": "armor_platoon_v2",
        "crow_result": "crow/research_result.json",
        "platforms": [
            {"role_hint": "platoon_leader", "capability_class": "ground_vehicle", "count": 1},
            {"role_hint": "wingman", "capability_class": "ground_vehicle", "count": 3},
        ],
        "terrain_classes": ["open", "urban"],
        "user_parameters": {},
    }
    request.update(overrides)
    path = tmp / "falcon_request.yaml"
    path.write_text(yaml.safe_dump(request, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- gold script

EXAMPLE = (
    Path(__file__).resolve().parents[1]
    / "examples"
    / "armor_platoon_terrain_formation"
    / "behavior_spec.yaml"
)

CLAUSES = [
    {"text": "开阔地用楔形", "kind": "formation", "roles": ["platoon_leader", "wingman"]},
    {"text": "接近城区时转纵队", "kind": "condition"},
    {
        "text": "纵队车距 50 米",
        "kind": "parameter",
        "user_values": [{"name": "column_interval", "value": 50, "unit": "m"}],
    },
    {"text": "接敌后报告", "kind": "coordination"},
    {"text": "交替跃进", "kind": "coordination"},
    {"text": "与敌保持安全距离", "kind": "constraint"},
]


def example() -> dict[str, Any]:
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def skeleton_answer() -> dict[str, Any]:
    ex = example()
    roles = []
    implements = {
        "platoon_leader": ["rq_01", "rq_02"],
        "wingman": ["rq_01", "rq_04"],
    }
    state_impl = {
        "travel_open": ["rq_01"],
        "travel_urban": ["rq_02"],
        "contact": ["rq_04", "rq_05"],
    }
    for role in ex["roles"]:
        deps = role["dependencies"]
        roles.append(
            {
                "id": role["id"],
                "description": role["description"],
                "count": role["count"],
                "capability_class": role["capability_class"],
                "superior": role["command"]["superior"],
                "mover_capability": deps["mover"]["capability"],
                "sensors": [
                    {"id": s["id"], "capability": s["capability"]} for s in deps["sensors"]
                ],
                "comms": [{"id": s["id"], "capability": s["capability"]} for s in deps["comms"]],
                "blackboard_variables": role["blackboards"][0]["variables"],
                "initial_state": role["state_machine"]["initial_state"],
                "states": [
                    {"id": s["id"], "tree": s["tree"], "implements": state_impl[s["id"]]}
                    for s in role["state_machine"]["states"]
                ],
                "implements": implements[role["id"]],
            }
        )
    return {
        "display_name": "装甲排地形自适应编队（Falcon 草稿）",
        "description": ex["model"]["description"],
        "intent": "formation_transition",
        "domain_concepts": ex["model"]["domain_concepts"] + ["not.a.concept"],
        "roles": roles,
        "formations": [
            {"id": "wedge", "implements": ["rq_01"]},
            {"id": "column", "implements": ["rq_02", "rq_03"]},
        ],
        "coordination": [
            {"id": "coord_leader_follower", "pattern": "leader_follower", "implements": ["rq_02"]},
            {
                "id": "coord_bounding",
                "pattern": "bounding_overwatch",
                "implements": ["rq_05", "rq_06"],
            },
            {"id": "coord_contact_report", "pattern": "report_and_wait", "implements": ["rq_04"]},
        ],
    }


PARAM_BASIS: dict[str, dict[str, Any]] = {
    "wedge_depth": {"status": "explicit", "evidence": [("wedge", 1)]},
    # explicit but the value (100 m) is outside the cited 150–300 m -> inferred
    "wedge_half_width": {"status": "explicit", "evidence": [("wide", 0)]},
    "column_interval": {"status": "user_specified"},
    # dangling evidence id -> removed -> explicit without evidence -> unknown
    "station_tolerance": {"status": "explicit", "evidence": ["ev_doesnotexist"]},
    "urban_transition_distance": {"status": "explicit", "evidence": [("column", 2)]},
    "contact_range": {"status": "unknown", "rationale": "资料未给出接敌判定距离"},
    "contact_hold_time": {"status": "unknown"},
    "travel_speed_open": {"status": "explicit"},  # no evidence -> unknown
    "travel_speed_urban": {"status": "user_specified"},  # not stated by the user -> unknown
    "bound_distance": {"status": "explicit", "evidence": [("contact", 1)]},
    "standoff_distance": {"status": "explicit", "evidence": [("standoff", 0)]},
}


def _basis(spec: dict[str, Any]) -> dict[str, Any]:
    out = dict(spec)
    out["evidence"] = [ev(*e) if isinstance(e, tuple) else e for e in spec.get("evidence", [])]
    return out


def parameters_answer() -> dict[str, Any]:
    ex = example()
    params = []
    for name, p in ex["parameters"].items():
        item = {k: v for k, v in p.items() if k != "basis"}
        item["name"] = name
        item["basis"] = _basis(PARAM_BASIS[name])
        params.append(item)
    assumptions = [
        {k: v for k, v in a.items() if k not in ("confirmation", "confirmed_by")}
        for a in ex["assumptions"]
    ]
    return {"parameters": params, "assumptions": assumptions}


def structure_answer() -> dict[str, Any]:
    ex = example()
    bases = {
        "wedge": {"status": "user_specified"},
        "column": {"status": "user_specified"},
        "coord_leader_follower": {
            "status": "inferred",
            "evidence": [("column", 0)],
            "rationale": "队形转换需要长车通报，僚车跟随",
        },
        "coord_bounding": {"status": "explicit", "evidence": [("contact", 1)]},
        "coord_contact_report": {"status": "explicit", "evidence": [("contact", 0)]},
    }
    formations = [{**f, "basis": _basis(bases[f["id"]])} for f in ex["formations"]]
    coordination = [{**c, "basis": _basis(bases[c["id"]])} for c in ex["coordination"]]
    return {"formations": formations, "coordination": coordination, "messages": ex["messages"]}


def conditions_answer() -> dict[str, Any]:
    return {"conditions": example()["conditions"]}


def behaviors_answer() -> dict[str, Any]:
    return {"behaviors": example()["behaviors"]}


def _flatten(node: dict[str, Any], out: list[dict[str, Any]]) -> None:
    flat = {k: v for k, v in node.items() if k != "children"}
    flat["children"] = [c["node_id"] for c in node.get("children", [])]
    out.append(flat)
    for child in node.get("children", []):
        _flatten(child, out)


TRANSITION_BASIS = {
    "open_to_contact": {"status": "explicit", "evidence": [("contact", 0)]},
    "open_to_urban": {"status": "explicit", "evidence": [("column", 0)]},
    "urban_to_contact": {"status": "explicit", "evidence": [("contact", 0)]},
}
TRANSITION_IMPL = {
    "urban_to_open": ["rq_02"],
    "contact_to_open": ["rq_04"],
    "wing_open_contact": ["rq_04"],
    "wing_urban_contact": ["rq_04"],
}


def control_answer(drop_tree: str | None = None) -> dict[str, Any]:
    ex = example()
    trees = []
    for tree in ex["trees"]:
        if tree["id"] == drop_tree:
            continue
        nodes: list[dict[str, Any]] = []
        _flatten(tree["root"], nodes)
        trees.append({"id": tree["id"], "root": tree["root"]["node_id"], "nodes": nodes})
    transitions = []
    for role in ex["roles"]:
        for state in role["state_machine"]["states"]:
            for tr in state["transitions"]:
                item = {
                    "role": role["id"],
                    "state": state["id"],
                    **{k: v for k, v in tr.items() if k != "basis"},
                }
                tid = tr["transition_id"]
                if tr["when"] != {"literal": True}:
                    item["basis"] = _basis(TRANSITION_BASIS.get(tid, {"status": "user_specified"}))
                    item["implements"] = TRANSITION_IMPL.get(tid, ["rq_02"])
                transitions.append(item)
    return {"trees": trees, "transitions": transitions}


def verification_answer() -> dict[str, Any]:
    ex = example()
    return {"tests": ex["tests"], "scenarios": ex["scenarios"]}


def gold_script(*, broken_control_first: bool = False) -> dict[str, Any]:
    control: Any = [control_answer()]
    if broken_control_first:
        control = [control_answer(drop_tree="leader_contact_tree"), control_answer()]
    return {
        "evidence.extract": extraction_handler,
        "evidence.group": grouping_handler,
        "requirements": [{"clauses": CLAUSES}],
        "skeleton": [skeleton_answer()],
        "section:parameters": [parameters_answer()],
        "section:structure": [structure_answer()],
        "section:conditions": [conditions_answer()],
        "section:behaviors": [behaviors_answer()],
        "section:control": control,
        "section:verification": [verification_answer()],
    }
