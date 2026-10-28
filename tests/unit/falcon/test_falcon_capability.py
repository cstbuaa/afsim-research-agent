"""K4: Falcon generation constrained by the platform capability catalog (+ K3 on the draft)."""

from __future__ import annotations

import asyncio
import json

import pytest
import yaml
from falcon_fixtures import crow_result, gold_script, write_request

from afsim_research_agent.capability import CapabilityCatalog
from afsim_research_agent.capability.constraints import CONSTRAINT_NOTE, vocabulary_constraints
from afsim_research_agent.falcon.fill import catalog_text
from afsim_research_agent.falcon.llm import ScriptedModel
from afsim_research_agent.falcon.pipeline import run_draft
from afsim_research_agent.falcon.prompts import catalog_actions, catalog_operands
from afsim_research_agent.ir.catalog import load_catalog


@pytest.fixture(scope="module")
def catalogs():
    return load_catalog(), CapabilityCatalog.load()


def test_ground_vocabulary_tiers(catalogs) -> None:
    ir_catalog, catalog = catalogs
    ground = vocabulary_constraints(ir_catalog, catalog, ["ground_vehicle"])
    assert ground.names("unavailable") == ["set_altitude"]
    assert {"send_message", "report_contact", "terrain_value.class"} <= set(ground.names("confirm"))
    assert ground.tier("follow_route") == "available"
    assert ground.tier("keep_station") == "available"  # partial is listed normally
    # a mixed model keeps an element some classes have, marked with those classes
    mixed = vocabulary_constraints(ir_catalog, catalog, ["ground_vehicle", "air_vehicle"])
    assert mixed.tier("set_altitude") != "unavailable"
    assert "仅 air_vehicle" in mixed.mark("set_altitude")


def test_coarse_drafts_hide_details_and_keep_core_marks(catalogs) -> None:
    from afsim_research_agent.capability.granularity import CapabilityPolicy

    ir_catalog, catalog = catalogs
    coarse = vocabulary_constraints(
        ir_catalog, catalog, ["ground_vehicle"], policy=CapabilityPolicy.make()
    )
    hidden = set(coarse.names("hidden"))
    assert {
        "self_value.fuel_remaining",
        "weapon_value.quantity_remaining",
        "sensor_value.on",
    } <= hidden
    assert {"simulation_value.tick", "turn_controller_on"} <= hidden
    # core groups keep their marks; supporting groups are simplified and listed plainly
    assert set(coarse.names("confirm")) == {
        "mover_value.route_complete",
        "terrain_value.class",
        "terrain_value.distance_to_class",
        "terrain_value.slope",
        "terrain_value.line_of_sight_to_nearest_hostile",
    }
    assert coarse.mark("send_message") == ""
    actions = catalog_actions(ir_catalog, coarse)
    assert "turn_controller_on" not in actions and "- send_message(" in actions
    assert "fuel_remaining" not in catalog_operands(ir_catalog, coarse)
    refined = vocabulary_constraints(
        ir_catalog, catalog, ["ground_vehicle"], policy=CapabilityPolicy.make("coarse", ["comms"])
    )
    assert refined.mark("send_message") == "【需确认】"


def test_prompt_lists_follow_the_tiers(catalogs) -> None:
    ir_catalog, catalog = catalogs
    ground = vocabulary_constraints(ir_catalog, catalog, ["ground_vehicle"])
    actions = catalog_actions(ir_catalog, ground)
    assert "set_altitude" not in actions and "set_altitude" in catalog_actions(ir_catalog)
    line = next(x for x in actions.splitlines() if x.startswith("- send_message("))
    assert line.endswith("【需确认】")
    follow = next(x for x in actions.splitlines() if x.startswith("- follow_route("))
    assert "【" not in follow
    operands = catalog_operands(ir_catalog, ground)
    assert "class:" in operands and "【需确认】" in operands
    text = catalog_text("behaviors", ir_catalog, ground)
    assert text.startswith(CONSTRAINT_NOTE)
    assert CONSTRAINT_NOTE not in catalog_text("behaviors", ir_catalog)
    assert catalog_text("structure", ir_catalog, ground) == catalog_text("structure", ir_catalog)


def test_draft_uses_constraints_and_checks_the_result(tmp_path) -> None:
    request = write_request(tmp_path, crow_result())
    model = ScriptedModel(gold_script())
    result = asyncio.run(run_draft(request, model))
    assert result.status == "draft_valid", result.problems
    prompts = [p for stage, p in model.calls if stage == "section:behaviors"]
    assert prompts and CONSTRAINT_NOTE in prompts[0] and "- set_altitude(" not in prompts[0]
    pkg = result.package_dir
    trace = json.loads((pkg / "falcon_trace.json").read_text(encoding="utf-8"))
    assert trace["capability_constraints"]["unavailable"] == ["set_altitude"]
    assert trace["capability_constraints"]["granularity"] == "coarse"
    assert "self_value.fuel_remaining" in trace["capability_constraints"]["hidden"]
    assert "fuel_remaining" not in prompts[0]
    decisions = yaml.safe_load((pkg / "capability_decisions.yaml").read_text(encoding="utf-8"))
    assert decisions == {"granularity": "coarse", "refine": [], "decisions": {}}
    gaps = yaml.safe_load((pkg / "capability_gaps.yaml").read_text(encoding="utf-8"))
    assert gaps["phase"] == "ir_check" and gaps["level"] == "needs_confirmation"
    review = (pkg / "falcon_review.md").read_text(encoding="utf-8")
    assert "## 8. 平台能力核对" in review and "按平台能力约束生成" in review
    report = json.loads((pkg / "validation_report.json").read_text(encoding="utf-8"))
    assert report["capability"]["afsim_ready"] is False


def test_draft_without_capability_catalog(tmp_path) -> None:
    request = write_request(tmp_path, crow_result())
    model = ScriptedModel(gold_script())
    result = asyncio.run(run_draft(request, model, capability_catalog=False))
    assert result.status == "draft_valid"
    prompts = [p for stage, p in model.calls if stage == "section:behaviors"]
    assert CONSTRAINT_NOTE not in prompts[0]
    assert not (result.package_dir / "capability_gaps.yaml").exists()
