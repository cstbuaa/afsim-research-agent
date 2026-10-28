"""Tests for the capability catalog access layer and judgment engine (K1)."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
import yaml

from afsim_research_agent.capability import (
    Aliases,
    CapabilityCatalog,
    CatalogError,
    assess,
    assess_ir_element,
    assess_ref,
    gap_id,
    map_binding,
    map_ir_element,
    summarize,
)
from afsim_research_agent.capability.ir_elements import used_elements
from afsim_research_agent.ir.binding import SIM_CAPABILITIES

ROOT = Path(__file__).resolve().parents[3]
IR_CATALOG = ROOT / "src" / "afsim_research_agent" / "ir" / "catalog"
ARMOR = ROOT / "examples" / "armor_platoon_terrain_formation" / "normalized_ir.json"


@pytest.fixture(scope="module")
def catalog() -> CapabilityCatalog:
    return CapabilityCatalog.load()


def ir_vocabulary() -> list[str]:
    actions = tomllib.loads((IR_CATALOG / "actions.toml").read_text(encoding="utf-8"))["action"]
    operands = tomllib.loads((IR_CATALOG / "operands.toml").read_text(encoding="utf-8"))
    names = [a["name"] for a in actions]
    names += [f"{k}.{p}" for k, v in operands["operand"].items() for p in v.get("properties", {})]
    names += list(operands.get("function", {}))
    return names + ["blackboard"]


# ---------------------------------------------------------------------- synthetic catalog
def write_catalog(root: Path, *, classification: str = "public") -> Path:
    (root / "catalog").mkdir(parents=True)
    meta = {
        "id": "test_cat",
        "platform_family": "demo",
        "platform_version": "1",
        "classification": classification,
    }
    old = {
        "catalog": meta,
        "capability_domains": [{"id": "mobility"}, {"id": "resource"}],
        "capabilities": [
            {"id": "moved", "domain": "resource", "display_name": "旧位置", "status": "unknown"},
        ],
    }
    (root / "capability_catalog.yaml").write_text(yaml.safe_dump(old, allow_unicode=True))
    part = {
        "catalog": meta,
        "part": {"domain": "mobility", "display_name": "运动类"},
        "capabilities": [
            {
                "id": "go",
                "domain": "mobility",
                "display_name": "前往",
                "status": "available",
                "kind": "mover",
                "applicable_platform_classes": ["ground_vehicle"],
                "keywords": ["前往", "机动"],
                "tactical_uses": ["跃进到指定位置"],
                "accepts": [
                    {
                        "ir": "m.go",
                        "display_name": "前往",
                        "status": "supported",
                        "ir_v1": "move_to_position",
                        "basis": "user_attested",
                    }
                ],
                "provides": [
                    {"ir": "m.raw", "display_name": "原始量", "status": "supported", "ir_v1": None}
                ],
            },
            {
                "id": "talk",
                "domain": "mobility",
                "display_name": "通报",
                "status": "partial",
                "accepts": [
                    {
                        "ir": "c.send",
                        "display_name": "发送",
                        "status": "unknown",
                        "ir_v1": "send_message",
                    }
                ],
            },
            {
                "id": "combo",
                "domain": "mobility",
                "display_name": "组合",
                "status": "available",
                "kind": "composite",
                "composed_of": [{"capability": "go", "role": "x"}],
            },
            {"id": "gone", "domain": "mobility", "display_name": "没有", "status": "unavailable"},
            {"id": "moved", "domain": "mobility", "display_name": "新位置", "status": "partial"},
        ],
    }
    (root / "catalog" / "mobility.yaml").write_text(yaml.safe_dump(part, allow_unicode=True))
    (root / "catalog" / "index.yaml").write_text("domains: []\n")
    return root


def test_load_merges_parts_over_single_file(tmp_path: Path) -> None:
    cat = CapabilityCatalog.load(write_catalog(tmp_path))
    assert len(cat) == 5
    assert cat.get("moved").domain == "mobility"  # part file replaces the 0.2.0 entry
    assert cat.catalog_id == "test_cat" and cat.platform == "demo 1"
    assert cat.content_hash.startswith("sha256:")
    assert CapabilityCatalog.load(tmp_path).content_hash == cat.content_hash
    assert set(cat.domains) == {"mobility", "resource"}


def test_private_catalog_refused_unless_allowed(tmp_path: Path) -> None:
    root = write_catalog(tmp_path, classification="user_private")
    with pytest.raises(CatalogError, match="public"):
        CapabilityCatalog.load(root)
    assert len(CapabilityCatalog.load(root, allow_private=True)) == 5


def test_local_layer_is_never_read(tmp_path: Path) -> None:
    local = tmp_path / "_local"
    write_catalog(local)
    with pytest.raises(CatalogError, match="local-only"):
        CapabilityCatalog.load(local)


def test_resolve_and_reverse_index(tmp_path: Path) -> None:
    cat = CapabilityCatalog.load(write_catalog(tmp_path))
    assert cat.resolve("go").id == "go"
    assert cat.resolve("go:m.go").ir_v1 == "move_to_position"
    assert [x.ref for x in cat.by_ir_v1("send_message")] == ["talk:c.send"]
    with pytest.raises(CatalogError):
        cat.resolve("go:nope")
    with pytest.raises(CatalogError):
        cat.resolve("nope")


def test_judgment_rules(tmp_path: Path) -> None:
    cat = CapabilityCatalog.load(write_catalog(tmp_path))
    ready = assess_ref(cat, "go:m.go", "ground_vehicle")
    assert ready.conclusion == "ready" and ready.basis == "user_attested"
    assert assess_ref(cat, "talk:c.send").conclusion == "confirm"
    assert assess_ref(cat, "talk").conclusion == "partial"
    assert assess_ref(cat, "combo").conclusion == "composable"
    assert assess_ref(cat, "gone").conclusion == "develop"
    assert assess_ref(cat, "nope").conclusion == "develop"
    # platform class filter
    assert assess_ref(cat, "go:m.go", "air_vehicle").conclusion == "develop"
    # an interface with no IR counterpart needed by a tactic → IR extension
    assert assess_ref(cat, "go:m.raw", need_ir=True).conclusion == "ir_extension"
    assert assess_ref(cat, "go:m.raw").conclusion == "ready"


def test_combination_and_alternatives(tmp_path: Path) -> None:
    cat = CapabilityCatalog.load(write_catalog(tmp_path))
    # all of uses: worst wins; several ready parts are a composition
    assert assess(cat, ["go:m.go", "talk:c.send"]).conclusion == "confirm"
    assert assess(cat, ["go:m.go", "go:m.raw"]).conclusion == "composable"
    # alternatives: best wins, and the chosen one is recorded
    verdict = assess(cat, any_of=[["talk:c.send"], ["go:m.go"]])
    assert verdict.conclusion == "ready" and verdict.chosen_alternative == 1
    verdict = assess(cat, ["talk"], any_of=[["gone"], ["talk:c.send"]])
    assert verdict.conclusion == "confirm"
    assert [r.ref for r in verdict.blocking_refs()] == ["talk:c.send"]
    assert assess(cat).conclusion == "develop"


def test_summary_levels(tmp_path: Path) -> None:
    cat = CapabilityCatalog.load(write_catalog(tmp_path))
    ok = assess(cat, ["go:m.go"])
    confirm = assess(cat, ["talk:c.send"])
    missing = assess(cat, ["gone"])
    assert summarize([("required", ok, "g1")]).level == "ready"
    s = summarize(
        [("required", ok, "g1"), ("required", confirm, "g2"), ("enhancing", missing, "g3")]
    )
    assert s.level == "needs_confirmation" and s.blocking == ("g2",)
    assert s.counts["develop"] == 1
    assert summarize([("required", missing, "g3")]).level == "needs_development"


def test_gap_id_is_stable() -> None:
    assert gap_id("message_exchange:communication_action.send_message") == gap_id(
        "  message_exchange:communication_action.send_message "
    )
    assert gap_id("判断是否在城区").startswith("cap-gap-")
    assert gap_id("a") != gap_id("b")


# ---------------------------------------------------------------------- AFSIM 2.9 catalog
def test_repository_catalog_is_public_and_complete(catalog: CapabilityCatalog) -> None:
    assert catalog.catalog_id == "afsim_2_9_capabilities"
    assert {"mobility", "communication", "weapon_employment", "execution", "environment"} <= set(
        catalog.domains
    )
    # moved from resource management into the weapon part
    assert catalog.get("weapon_inventory_management").domain == "weapon_employment"


def test_every_ir_element_maps_and_resolves(catalog: CapabilityCatalog) -> None:
    unmapped, broken = [], []
    for name in ir_vocabulary():
        mapping = map_ir_element(catalog, name)
        if mapping.kind == "unmapped":
            unmapped.append(name)
        for ref in [*mapping.uses, *(r for alt in mapping.any_of for r in alt)]:
            try:
                catalog.resolve(ref)
            except CatalogError as exc:
                broken.append(f"{name}: {exc}")
    assert not unmapped, unmapped
    assert not broken, broken


def test_every_alias_reference_resolves(catalog: CapabilityCatalog) -> None:
    aliases = Aliases.default()
    refs = [r for uses in aliases.bindings.values() for r in uses]
    for entry in aliases.elements.values():
        refs += entry.get("uses", [])
        refs += [r for alt in entry.get("any_of", []) for r in alt]
    bad = []
    for ref in refs:
        try:
            catalog.resolve(ref)
        except CatalogError as exc:
            bad.append(str(exc))
    assert not bad, bad


def test_ir_binding_ids_map_to_catalog(catalog: CapabilityCatalog) -> None:
    for capability in SIM_CAPABILITIES:
        mapped = map_binding(capability)
        assert mapped, capability
        assert all(m in catalog for m in mapped), (capability, mapped)


def test_search_finds_tactical_phrases(catalog: CapabilityCatalog) -> None:
    ids = [c.id for c in catalog.search("无线电静默", domains=["communication"])]
    assert ids[0] == "comm_device_control"
    assert "terrain_classification" in [c.id for c in catalog.search("接近城区前转换")]


# ---------------------------------------------------------------------- armor example
# Expected conclusions follow analysis/2026-10-16-armor-example-catalog-coverage.md, updated
# for the environment domain added afterwards (terrain is now "confirm" instead of missing).
ARMOR_EXPECTED = {
    "follow_route": "ready",
    "bound_to_position": "ready",
    "hold_overwatch_position": "composable",
    "keep_station": "partial",
    "send_message": "confirm",
    "report_contact": "confirm",
    "set_blackboard": "partial",
    "blackboard": "partial",
    "message_received": "partial",
    "bounding_turn": "partial",
    "standoff_reached": "confirm",
    "track_value.nearest_hostile_range": "confirm",
    "terrain_value.class": "confirm",
    "terrain_value.distance_to_class": "confirm",
    "mover_value.route_complete": "confirm",
    "mover_value.at_destination": "ready",
    "mover_value.available": "ready",
    "time_in_state_gte": "confirm",
}


def test_armor_example_matches_manual_check(catalog: CapabilityCatalog) -> None:
    ir = json.loads(ARMOR.read_text(encoding="utf-8"))
    used = used_elements(ir)
    dependencies = {n for n, u in used.items() if u.kind == "dependency"}
    assert dependencies == {
        "dependency:route_following_ground",
        "dependency:visual_search",
        "dependency:voice_data",
    }
    elements = {n: u for n, u in used.items() if u.kind != "dependency"}
    assert set(elements) == set(ARMOR_EXPECTED)
    got = {
        name: assess_ir_element(catalog, name, "ground_vehicle")[1].conclusion for name in elements
    }
    assert got == ARMOR_EXPECTED
    # the leader-position problem is visible: keep_station needs messages, tracks or rigid follow
    mapping, verdict = assess_ir_element(catalog, "keep_station", "ground_vehicle")
    assert mapping.kind == "alias" and len(verdict.alternatives) == 3
    for dep in dependencies:
        verdict = assess(
            catalog, map_binding(dep.split(":", 1)[1]), platform_class="ground_vehicle"
        )
        assert verdict.conclusion == "partial"


def test_ir_extension_only_when_ir_cannot_reach(catalog: CapabilityCatalog) -> None:
    # 0.2.0 entry: IR mapping never recorded → not an IR gap
    affiliation = "track_identification:track_state.affiliation"
    assert assess_ref(catalog, affiliation, need_ir=True).conclusion == "confirm"
    # no direct link, but used inside an aliased IR element (nearest_hostile_range)
    assert (
        assess_ref(catalog, "target_tracking:track_state.latitude", need_ir=True).conclusion
        == "ready"
    )
    # 0.3.0 interface with ir_v1: null that no IR element reaches → IR extension
    power = "comm_device_control:communication_action.set_comm_power"
    assert assess_ref(catalog, power, need_ir=True).conclusion == "ir_extension"
    assert assess_ref(catalog, power).conclusion == "confirm"
