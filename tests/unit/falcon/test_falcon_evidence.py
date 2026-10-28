"""F0 gate and F1 evidence normalization."""

from __future__ import annotations

import asyncio
import random

import pytest
from falcon_fixtures import (
    crow_result,
    ev,
    excerpt_ids,
    extraction_handler,
    grouping_handler,
    write_request,
)

from afsim_research_agent.evidence.merge import canonical_unit, interval, numbers_in
from afsim_research_agent.falcon.errors import FalconError
from afsim_research_agent.falcon.evidence import normalize_evidence
from afsim_research_agent.falcon.gate import run_gate
from afsim_research_agent.falcon.llm import ModelRunner, ScriptedModel
from afsim_research_agent.falcon.models import ClaimDraft, Excerpt


def _model() -> ScriptedModel:
    return ScriptedModel(
        {"evidence.extract": extraction_handler, "evidence.group": grouping_handler}
    )


def _package(crow=None):
    crow = crow or crow_result()
    return asyncio.run(normalize_evidence(crow, "需求", ModelRunner(_model()), batch_size=2))


# --------------------------------------------------------------------------- gate


def test_gate_refuses_failed_readiness_without_writing(tmp_path):
    request = write_request(tmp_path, crow_result(ready=False))
    with pytest.raises(FalconError) as err:
        run_gate(request)
    assert err.value.code == "FAL001" and err.value.exit_code == 2
    text = err.value.render()
    assert "authoritative_sources" in text and "证据不足的研究问题" in text
    assert not (tmp_path / "models").exists()


def test_gate_refuses_missing_readiness_and_broken_file(tmp_path):
    crow = crow_result()
    crow.readiness = None
    request = write_request(tmp_path, crow)
    with pytest.raises(FalconError) as err:
        run_gate(request)
    assert err.value.code == "FAL001"
    (tmp_path / "crow" / "research_result.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(FalconError) as err:
        run_gate(request)
    assert err.value.code == "FAL001"


def test_gate_rejects_bad_request_and_existing_output(tmp_path):
    request = write_request(tmp_path, crow_result(), terrain_classes=["swamp"])
    with pytest.raises(FalconError) as err:
        run_gate(request)
    assert err.value.code == "FAL003" and err.value.exit_code == 3
    request = write_request(
        tmp_path, crow_result(), user_parameters={"gap": {"value": 5, "unit": "parsec"}}
    )
    with pytest.raises(FalconError) as err:
        run_gate(request)
    assert err.value.code == "FAL003"
    request = write_request(tmp_path, crow_result())
    (tmp_path / "models" / "armor_platoon_v2").mkdir(parents=True)
    with pytest.raises(FalconError) as err:
        run_gate(request)
    assert err.value.code == "FAL002"
    gate, target = run_gate(request, out_dir=tmp_path / "elsewhere")
    assert target.name == "elsewhere" and gate.crow.readiness.ready


# --------------------------------------------------------------------------- helpers


def test_units_and_numbers():
    assert canonical_unit("meters") == ("m", "distance")
    assert canonical_unit("公里") == ("km", "distance")
    assert canonical_unit("km/h") == ("km/h", "speed")
    assert canonical_unit("parsec") is None
    assert numbers_in("50–100 meters, 1,000 m and 2.5 km") == [50, 100, 1000, 2.5]
    assert not interval(50, 100, "m", "distance").overlaps(interval(150, 300, "m", "distance"))
    assert interval(0.1, 0.2, "km", "distance").overlaps(interval(150, 300, "m", "distance"))


def test_quote_verification_rules():
    from afsim_research_agent.falcon.evidence import verify_claim

    ex = Excerpt(
        excerpt_id="ex_1", source_id="s", location="p1", excerpt="Keep  50–100 m\nbetween vehicles."
    )
    ok = ClaimDraft(
        statement="间距",
        quote="keep 50-100 m between",
        quantity={"name": "gap", "min": 100, "max": 50, "unit": "m"},
    )
    quantity, reason = verify_claim(ok, ex)
    assert reason is None and quantity.min == 50 and quantity.max == 100
    bad_number = ClaimDraft(
        statement="间距",
        quote="Keep 50–100 m",
        quantity={"name": "gap", "min": 60, "max": 60, "unit": "m"},
    )
    assert verify_claim(bad_number, ex)[1] == "number 60 not in quote"
    bad_unit = ClaimDraft(
        statement="间距",
        quote="Keep 50–100 m",
        quantity={"name": "gap", "min": 50, "max": 50, "unit": "furlong"},
    )
    assert "not recognized" in verify_claim(bad_unit, ex)[1]
    invented = ClaimDraft(statement="间距", quote="Keep 70 m apart")
    assert verify_claim(invented, ex)[1] == "quote not found in excerpt"


# --------------------------------------------------------------------------- package


def test_package_ids_stable_and_order_independent():
    first = _package()
    crow = crow_result()
    for report in crow.reports:
        random.Random(3).shuffle(report.evidence)
    random.Random(5).shuffle(crow.reports)
    second = _package(crow)
    assert [e.evidence_id for e in first.evidence] == [e.evidence_id for e in second.evidence]
    assert first.content_hash == second.content_hash
    assert first.content_hash.startswith("sha256:")


def test_package_content_and_conflicts():
    pkg = _package()
    ids = {e.evidence_id for e in pkg.evidence}
    assert ev("wedge", 1) in ids and ev("standoff", 0) in ids
    assert len(pkg.evidence) == 9  # 10 claims, one invented quote dropped
    assert any(d.reason == "quote not found in excerpt" for d in pkg.dropped)
    item = pkg.item(ev("wedge", 1))
    assert item.quantity.unit == "m" and item.quantity.name == "wedge_interval"
    # the model grouped both wedge-interval claims as consistent; numbers disagree
    pair = frozenset((ev("wedge", 1), ev("wide", 0)))
    assert pair in pkg.conflict_pairs()
    claim = next(c for c in pkg.claims if ev("wide", 0) in c.members)
    assert claim.conflicting == sorted(pair) and claim.conflicts[0].origin == "numeric"
    # ids are sequential and topics ordered
    assert [c.claim_id for c in pkg.claims] == [
        f"cl_{i:03d}" for i in range(1, len(pkg.claims) + 1)
    ]


def test_private_sources_have_no_local_path():
    pkg = _package()
    private = next(s for s in pkg.sources if s.source_id == "local_notes")
    assert private.classification == "user_private" and private.locator == ""
    public = next(s for s in pkg.sources if s.source_id == "doc_a")
    assert public.classification == "public" and public.quality_tier == "A"


def test_package_is_readable_by_ir_validator(tmp_path):
    from afsim_research_agent.falcon.evidence import write_package
    from afsim_research_agent.ir.validate import load_evidence_index

    pkg = _package()
    write_package(pkg, tmp_path / "evidence_package.json")
    index = load_evidence_index(tmp_path / "evidence_package.json")
    assert index.available and index.package_id == pkg.package_id
    assert ev("contact", 1) in index.evidence_ids and "doc_a" in index.source_ids


def test_excerpts_deduplicated_across_questions():
    crow = crow_result()
    crow.reports[1].evidence.append(crow.reports[0].evidence[0])
    ids = excerpt_ids(crow)
    pkg = _package(crow)
    wedge_items = [e for e in pkg.evidence if e.excerpt_id == ids["wedge"]]
    assert {tuple(e.questions) for e in wedge_items} == {("q1", "q2")}
