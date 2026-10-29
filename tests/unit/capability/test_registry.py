"""K5: gap registry, development queue, catalog write-back, re-check and faithful L3."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest
import yaml

from afsim_research_agent.capability import CapabilityCatalog, CatalogError, gap_id
from afsim_research_agent.capability.__main__ import main as cli
from afsim_research_agent.capability.catalog import OVERRIDES_FILE, default_catalog_root
from afsim_research_agent.capability.granularity import CapabilityPolicy
from afsim_research_agent.capability.ir_check import check_ir
from afsim_research_agent.capability.registry import GapRegistry, RegistryError, render_queue
from afsim_research_agent.crow.annotate import load_result
from afsim_research_agent.crow.capability_annotation import CapabilityAnnotator
from afsim_research_agent.crow.mission import MissionDraft, normalize_profile, write_profile
from afsim_research_agent.crow.proposal_models import NeedDraft, ProposalResult, Step, TacticCard
from afsim_research_agent.crow.proposal_report import write_proposal
from afsim_research_agent.falcon.llm import ModelRunner, ScriptedModel
from afsim_research_agent.ir.binding import bind
from afsim_research_agent.ir.sim import run_scenarios

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = ROOT / "examples" / "armor_platoon_terrain_formation"

GO = "movement.move_to_point"
STANDOFF = "formation.command_standoff"  # tactic step: implemented inside one tactic only
HOLD = "movement.hold_position"
URBAN = "ir:terrain_value.class"
DISTANCE = "ir:terrain_value.distance_to_class"
BOUNDING = "ir:bounding_turn"


@pytest.fixture
def catalog_root(tmp_path: Path, capability_set) -> Path:
    """A private copy: write-back must not touch the local capability set."""
    root = tmp_path / "catalog"
    shutil.copytree(
        default_catalog_root(),
        root,
        ignore=shutil.ignore_patterns("_local", "*.local.*", OVERRIDES_FILE),
    )
    return root


def _profile():
    draft = MissionDraft.model_validate(
        {
            "domain": "ground",
            "echelon": "platoon",
            "mission": {"type": "formation_transition", "objective": "x"},
            "own_forces": [{"role_hint": "排长车", "count": 1, "platform_class": "ground_vehicle"}],
            "user_specified": [],
        }
    )
    return normalize_profile(draft, "装甲排")


def _card(cid: str) -> TacticCard:
    return TacticCard(
        id=cid,
        name=cid,
        narrative="跃进后判断是否进入城区。" * 4,
        summary=cid,
        steps=[Step(phase="跃进", do="跃进到位置", until="进入城区")],
    )


def _need(text: str, maps_to=(), missing: str = "", group: str = "") -> dict:
    return NeedDraft(
        need=text, source="steps[0].do", maps_to=list(maps_to), missing=missing, group=group
    ).model_dump()


def _tactics(tmp_path: Path, catalog: CapabilityCatalog) -> Path:
    """tc_01 needs a bound (implemented), the standoff step (partial) and a hide-position
    judgment nothing in the set provides (terrain is a core group); tc_02 needs the same
    under other wording."""
    result = ProposalResult(
        requirement="装甲排",
        max_candidates=5,
        max_innovative=2,
        candidates=[_card("tc_01"), _card("tc_02")],
    )
    model = ScriptedModel(
        {
            "capability:tc_01": [
                {
                    "needs": [
                        _need("跃进到位置", [GO], group="maneuver"),
                        _need("与目标保持距离", [STANDOFF], group="formation"),
                        _need("判断是否在隐蔽阵地", missing="隐蔽阵地判断", group="terrain"),
                    ]
                }
            ],
            "capability:tc_02": [
                {"needs": [_need("识别可隐蔽的位置", missing="隐蔽位置识别", group="terrain")]}
            ],
            "capability_review": [{"items": []}],
        }
    )
    profile = _profile()
    asyncio.run(CapabilityAnnotator(ModelRunner(model), catalog).annotate(result, profile))
    out = tmp_path / "tactics"
    out.mkdir()
    from afsim_research_agent.crow.annotate import load_package

    write_proposal(result, load_package(None), out, None)
    write_profile(profile, out / "mission_profile.yaml")
    return out


def _run(*args: str) -> int:
    return cli([str(a) for a in args])


# ---------------------------------------------------------------------- catalog write-back
def test_overrides_are_applied_when_the_catalog_loads(catalog_root: Path) -> None:
    before = CapabilityCatalog.load(catalog_root)
    assert before.get(STANDOFF).status == "partial"
    (catalog_root / OVERRIDES_FILE).write_text(
        yaml.safe_dump({"overrides": [{"ref": STANDOFF, "basis": "user_attested", "gap": "g"}]}),
        encoding="utf-8",
    )
    after = CapabilityCatalog.load(catalog_root)
    cap = after.get(STANDOFF)
    assert cap.status == "available" and cap.basis == "user_attested"
    assert after.content_hash != before.content_hash and len(after.overrides) == 1
    (catalog_root / OVERRIDES_FILE).write_text(
        yaml.safe_dump({"overrides": [{"ref": "terrain.nope"}]}), encoding="utf-8"
    )
    with pytest.raises(CatalogError):
        CapabilityCatalog.load(catalog_root)


# ---------------------------------------------------------------------- Crow → queue → implement
def test_implementing_a_gap_changes_feasibility_after_recheck(tmp_path, catalog_root) -> None:
    catalog = CapabilityCatalog.load(catalog_root)
    tactics = _tactics(tmp_path, catalog)
    reg = tmp_path / "registry.yaml"
    gaps_file = tactics / "capability_gaps.yaml"
    common = ["--registry", reg, "--capability-catalog", catalog_root]

    assert _run(*common, "ingest", gaps_file) == 0
    assert GapRegistry.load(reg).queue() == []  # nothing selected yet
    assert _run(*common, "select", gaps_file, "--candidate", "tc_01", "--by", "u") == 0
    queue = GapRegistry.load(reg).queue()
    # the partial standoff step is not a gap; the missing judgment is
    assert [g.contract for g in queue] == ["隐蔽阵地判断"]
    assert yaml.safe_load(gaps_file.read_text(encoding="utf-8"))["gaps"][0]["selected"] is True
    first = load_result(tactics).candidates[0]
    assert first.feasibility.level == "needs_development"

    # a plugin implements it as an existing entry of the set
    hide = gap_id("隐蔽阵地判断")
    assert _run(*common, "start", hide, "--by", "u") == 0
    assert _run(*common, "implement", hide, "--by", "u", "--plugin", "hide_site", "--as", HOLD) == 0
    overrides = yaml.safe_load((catalog_root / OVERRIDES_FILE).read_text(encoding="utf-8"))
    assert overrides["overrides"][0]["ref"] == HOLD
    assert _run(*common, "recheck", tactics) == 0
    first = load_result(tactics).candidates[0]
    assert first.feasibility.level == "ready"
    need = next(n for n in first.capability_needs if n.need == "判断是否在隐蔽阵地")
    assert HOLD in need.maps_to and not need.gaps
    registry = GapRegistry.load(reg)
    assert registry.queue() == [] and HOLD in render_queue(registry)

    # reopening removes the write-back again; the need keeps the mapping the user chose
    assert _run(*common, "reopen", hide, "--by", "u") == 0
    assert HOLD not in (catalog_root / OVERRIDES_FILE).read_text(encoding="utf-8")
    assert GapRegistry.load(reg).require(hide).status == "open"


def test_transitions_are_checked(tmp_path, catalog_root) -> None:
    catalog = CapabilityCatalog.load(catalog_root)
    tactics = _tactics(tmp_path, catalog)
    registry = GapRegistry()
    registry.ingest(tactics / "capability_gaps.yaml")
    hide, other = gap_id("隐蔽阵地判断"), gap_id("隐蔽位置识别")
    with pytest.raises(RegistryError, match="no catalog entry"):
        registry.transition(hide, "implement", by="u", catalog_root=catalog_root)
    with pytest.raises(RegistryError, match="--by"):
        registry.transition(other, "confirm", by="", catalog_root=catalog_root)
    with pytest.raises(RegistryError, match="needs one of"):
        registry.transition(other, "reopen", by="u", catalog_root=catalog_root)
    registry.transition(hide, "reject", by="u", note="不做", catalog_root=catalog_root)
    assert registry.require(hide).status == "rejected"
    assert not (catalog_root / OVERRIDES_FILE).exists()


def test_merge_collects_differently_worded_gaps(tmp_path, catalog_root) -> None:
    catalog = CapabilityCatalog.load(catalog_root)
    tactics = _tactics(tmp_path, catalog)
    registry = GapRegistry()
    gaps_file = tactics / "capability_gaps.yaml"
    registry.ingest(gaps_file)
    registry.select(gaps_file, "tc_01")
    registry.select(gaps_file, "tc_02")
    a, b = gap_id("隐蔽阵地判断"), gap_id("隐蔽位置识别")
    target = registry.merge([b], a, by="u")
    assert target.aliases == [b] and registry.get(b).id == a
    assert len(target.tactics) == 2
    assert [g.id for g in registry.queue()][0] == a  # two tactics need it
    assert b not in {g.id for g in registry.queue()}
    # ingesting again lands on the merged record
    registry.ingest(gaps_file)
    assert len(registry.active()) == len({g.id for g in registry.active()})
    assert b not in {g.id for g in registry.active()}


# ---------------------------------------------------------------------- IR gaps
def test_ir_gaps_enter_the_queue_with_acceptance(tmp_path, catalog_root) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    shutil.copy(EXAMPLE / "normalized_ir.json", package / "normalized_ir.json")
    ir = json.loads((package / "normalized_ir.json").read_text(encoding="utf-8"))
    check = check_ir(ir, CapabilityCatalog.load(catalog_root), policy=CapabilityPolicy.make())
    (package / "capability_gaps.yaml").write_text(check.to_yaml(), encoding="utf-8")
    reg = tmp_path / "registry.yaml"
    common = ["--registry", reg, "--capability-catalog", catalog_root]
    assert _run(*common, "ingest", package / "capability_gaps.yaml") == 0
    queue = GapRegistry.load(reg).queue()
    assert {g.contract for g in queue} == {URBAN, DISTANCE, BOUNDING}
    assert all(g.acceptance for g in queue)
    # nothing in the set provides terrain: it has to be added in the lab and exported,
    # it cannot be confirmed here
    assert _run(*common, "confirm", gap_id(URBAN), "--by", "u") != 0
    assert _run(*common, "recheck", package) == 0
    gaps = yaml.safe_load((package / "capability_gaps.yaml").read_text(encoding="utf-8"))
    assert {g["contract"] for g in gaps["gaps"]} == {URBAN, DISTANCE, BOUNDING}


# ---------------------------------------------------------------------- faithful L3
def test_faithful_simulation_withholds_open_gaps(capability_set) -> None:
    ir = json.loads((EXAMPLE / "normalized_ir.json").read_text(encoding="utf-8"))
    check = check_ir(ir, capability_set, policy=CapabilityPolicy.make())
    withheld = check.withheld()
    # functions (bounding_turn) are not withheld yet, only actions and operand reads
    assert withheld == ["terrain_value.class", "terrain_value.distance_to_class"]
    context, _ = bind(ir, "python_sim", None)
    normal = {r.id: r for r in run_scenarios(ir, context, package_dir=EXAMPLE)}
    faithful = {r.id: r for r in run_scenarios(ir, context, package_dir=EXAMPLE, withheld=withheld)}
    assert normal["open_to_urban_transition"].passed
    # without terrain judgment the platoon never recognizes the urban area
    assert not faithful["open_to_urban_transition"].passed
    assert faithful["open_to_urban_transition"].withheld["terrain_value.distance_to_class"] > 0
    assert normal["open_to_urban_transition"].withheld is None
