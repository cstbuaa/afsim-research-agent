"""Capability set access layer and judgment engine (K1, capability set v1.0)."""

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
from afsim_research_agent.capability.catalog import default_catalog_root
from afsim_research_agent.capability.granularity import CapabilityPolicy, GroupTable
from afsim_research_agent.capability.ir_elements import used_elements

ROOT = Path(__file__).resolve().parents[3]
IR_CATALOG = ROOT / "src" / "afsim_research_agent" / "ir" / "catalog"
ARMOR = ROOT / "examples" / "armor_platoon_terrain_formation" / "normalized_ir.json"


@pytest.fixture(scope="module")
def catalog(capability_set) -> CapabilityCatalog:
    return capability_set


def ir_vocabulary() -> list[str]:
    actions = tomllib.loads((IR_CATALOG / "actions.toml").read_text(encoding="utf-8"))["action"]
    operands = tomllib.loads((IR_CATALOG / "operands.toml").read_text(encoding="utf-8"))
    names = [a["name"] for a in actions]
    names += [f"{k}.{p}" for k, v in operands["operand"].items() for p in v.get("properties", {})]
    names += list(operands.get("function", {}))
    return names + ["blackboard"]


# ---------------------------------------------------------------------- synthetic set
ALIGNMENT = """
[meta]
version = "test"
[entry.demo]
display_name = "演示切片"
[entry."demo.go"]
display_name = "前往"
ir_v1 = ["move_to_position"]
tactical_uses = ["跃进到指定位置"]
keywords = ["前往", "机动"]
[entry."demo.step"]
display_name = "内部步骤"
"""


def write_set(root: Path, *, classification: str = "public") -> Path:
    (root / "action_set").mkdir(parents=True)
    (root / "tactic_set").mkdir()
    index = {
        "format": "CapabilitySet",
        "format_version": "1.0.0",
        "catalog": {
            "id": "test_set",
            "platform_family": "demo",
            "platform_version": "1",
            "platform_classes": ["ground_vehicle"],
            "classification": classification,
        },
        "slices": [
            {
                "id": "demo",
                "display_name": "演示",
                "action_file": "action_set/demo.yaml",
                "tactic_file": "tactic_set/demo.yaml",
            }
        ],
    }
    actions = {
        "slice": {"id": "demo", "description": "演示切片"},
        "primitives": [{"id": "demo.read", "kind": "primitive", "status": "implemented"}],
        "actions": [
            {
                "id": "demo.go",
                "kind": "action",
                "status": "implemented",
                "description": "go somewhere",
                "parameters": {"x": {"type": "number", "units": "m", "required": True}},
            },
            {"id": "demo.broken", "kind": "action", "status": "unavailable"},
        ],
        "tactic_steps": [
            {"id": "demo.step", "kind": "tactic_step", "status": "implemented", "used_by": ["demo"]}
        ],
    }
    tactics = {
        "subtrees": [
            {
                "id": "demo.advance",
                "root": {"type": "sequence", "children": [{"type": "action", "action": "demo.go"}]},
                "uses": {"actions": ["demo.go"]},
            }
        ],
        "state_trees": [
            {
                "id": "demo.moving",
                "role": "LEADER",
                "root": {"type": "subtree", "subtree": "demo.advance"},
                "uses": {"subtrees": ["demo.advance"]},
            }
        ],
        "missions": [
            {
                "id": "demo.mission",
                "initial_state": "MOVING",
                "states": ["MOVING", "COMPLETE"],
                "transitions": [{"from": "MOVING", "to": "COMPLETE", "when": "arrived"}],
                "state_trees": {"MOVING": "demo.moving"},
            }
        ],
    }
    (root / "index.yaml").write_text(yaml.safe_dump(index, allow_unicode=True), encoding="utf-8")
    (root / "action_set" / "demo.yaml").write_text(
        yaml.safe_dump(actions, allow_unicode=True), encoding="utf-8"
    )
    (root / "tactic_set" / "demo.yaml").write_text(
        yaml.safe_dump(tactics, allow_unicode=True), encoding="utf-8"
    )
    (root.parent / "alignment.toml").write_text(ALIGNMENT, encoding="utf-8")
    return root


def load_set(root: Path, **kw) -> CapabilityCatalog:
    return CapabilityCatalog.load(root, alignment=root.parent / "alignment.toml", **kw)


def test_load_builds_one_capability_per_slice_and_entry(tmp_path: Path) -> None:
    cat = load_set(write_set(tmp_path / "set"))
    assert len(cat) == 5  # slice + primitive + 2 actions + tactic step
    assert cat.catalog_id == "test_set" and cat.platform == "demo 1"
    assert cat.get("demo").kind == "slice"
    go = cat.get("demo.go")
    assert go.display_name == "前往" and go.status == "available" and go.domain == "demo"
    assert go.platform_classes == ("ground_vehicle",) and go.ir_v1 == ("move_to_position",)
    assert cat.get("demo.broken").status == "unavailable"
    step = cat.get("demo.step")
    assert step.status == "partial" and "战术内部" in step.limitations[0]
    assert cat.get("demo.read").display_name == "demo.read"  # no annotation: id is the name
    assert cat.content_hash.startswith("sha256:")
    assert load_set(tmp_path / "set").content_hash == cat.content_hash
    assert list(cat.tactics) == ["demo"] and cat.tactics["demo"]["missions"][0]["id"]


def test_duplicate_ids_are_refused(tmp_path: Path) -> None:
    root = write_set(tmp_path / "set")
    path = root / "action_set" / "demo.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["primitives"].append({"id": "demo.go", "kind": "primitive"})
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(CatalogError, match="duplicate"):
        load_set(root)


def test_private_set_refused_unless_allowed(tmp_path: Path) -> None:
    root = write_set(tmp_path / "set", classification="user_private")
    with pytest.raises(CatalogError, match="public"):
        load_set(root)
    assert len(load_set(root, allow_private=True)) == 5


def test_local_layer_is_never_read(tmp_path: Path) -> None:
    root = write_set(tmp_path / "_local")
    with pytest.raises(CatalogError, match="local-only"):
        load_set(root)


def test_old_format_is_refused(tmp_path: Path) -> None:
    (tmp_path / "catalog").mkdir()
    (tmp_path / "capability_catalog.yaml").write_text("catalog: {}\n", encoding="utf-8")
    with pytest.raises(CatalogError, match="no capability set"):
        CapabilityCatalog.load(tmp_path)


def test_judgment_rules(tmp_path: Path) -> None:
    cat = load_set(write_set(tmp_path / "set"))
    ready = assess_ref(cat, "demo.go", "ground_vehicle")
    assert ready.conclusion == "ready" and ready.basis == "lab_implemented"
    assert assess_ref(cat, "demo.step").conclusion == "partial"
    assert assess_ref(cat, "demo.broken").conclusion == "develop"
    assert assess_ref(cat, "nope").conclusion == "develop"
    assert assess_ref(cat, "demo.go", "air_vehicle").conclusion == "develop"
    assert [c.id for c in cat.by_ir_v1("move_to_position")] == ["demo.go"]


def test_combination_and_alternatives(tmp_path: Path) -> None:
    cat = load_set(write_set(tmp_path / "set"))
    assert assess(cat, ["demo.go", "demo.step"]).conclusion == "partial"
    assert assess(cat, ["demo.go", "demo.read"]).conclusion == "composable"
    verdict = assess(cat, any_of=[["demo.broken"], ["demo.go"]])
    assert verdict.conclusion == "ready" and verdict.chosen_alternative == 1
    verdict = assess(cat, ["demo.step"], any_of=[["demo.broken"], ["demo.go"]])
    assert verdict.conclusion == "partial"
    assert assess(cat).conclusion == "develop"


def test_summary_levels(tmp_path: Path) -> None:
    cat = load_set(write_set(tmp_path / "set"))
    ok = assess(cat, ["demo.go"])
    missing = assess(cat, ["demo.broken"])
    assert summarize([("required", ok, "g1")]).level == "ready"
    s = summarize([("required", ok, "g1"), ("enhancing", missing, "g3")])
    assert s.level == "ready" and s.counts["develop"] == 1
    assert summarize([("required", missing, "g3")]).level == "needs_development"


def test_gap_id_is_stable() -> None:
    assert gap_id("movement.move_to_point") == gap_id("  movement.move_to_point ")
    assert gap_id("判断是否在城区").startswith("cap-gap-")
    assert gap_id("a") != gap_id("b")


# ---------------------------------------------------------------------- repository set
def test_repository_set_is_public_and_has_seven_slices(catalog: CapabilityCatalog) -> None:
    assert catalog.catalog_id == "armored_vehicle_afsim_2_9"
    assert set(catalog.domains) == {
        "movement",
        "perception",
        "communication_target_sharing",
        "threat_allocation",
        "engagement",
        "formation_standoff",
        "hierarchical_formation",
    }
    assert catalog.get("movement.move_to_point").display_name == "定点机动"
    assert all(c.status in ("available", "partial") for c in catalog.capabilities.values())
    assert {c.id for c in catalog.capabilities.values() if c.kind == "tactic_step"} == {
        "formation.column_slots",
        "formation.target_line_slots",
        "formation.command_standoff",
    }
    assert set(catalog.tactics) == set(catalog.domains)


def test_repository_set_contains_nothing_local(catalog: CapabilityCatalog) -> None:
    from afsim_research_agent.capability.lab_export import scan_tree

    assert scan_tree(default_catalog_root()) == []


def test_every_alignment_entry_and_reference_resolves(catalog: CapabilityCatalog) -> None:
    align = tomllib.loads((default_catalog_root() / "ir_alignment.toml").read_text("utf-8"))
    assert set(align["entry"]) == set(catalog.capabilities), "每个条目都要有中文名称"
    aliases = Aliases.default()
    refs = [r for uses in aliases.bindings.values() for r in uses]
    for entry in aliases.elements.values():
        refs += entry.get("uses", [])
        refs += [r for alt in entry.get("any_of", []) for r in alt]
    bad = [r for r in refs if r not in catalog]
    assert not bad, bad
    vocabulary = set(ir_vocabulary()) | {
        "formation_is",
        "bounding_turn",
        "standoff_reached",
        "in_group",
    }
    links = [n for c in catalog.capabilities.values() for n in c.ir_v1]
    assert set(links) <= vocabulary, set(links) - vocabulary


def test_every_entry_is_in_exactly_one_group(catalog: CapabilityCatalog) -> None:
    table = GroupTable.default()
    grouped = [c for g in table.groups.values() for c in g.capabilities | g.detail]
    assert len(grouped) == len(set(grouped))
    assert set(grouped) == set(catalog.capabilities)


def test_unmapped_ir_elements_are_grouped(catalog: CapabilityCatalog) -> None:
    """An IR element nothing implements must say which group it belongs to, so a coarse
    draft can decide between gap and simplification."""
    policy = CapabilityPolicy.make()
    missing = []
    for name in ir_vocabulary():
        if map_ir_element(catalog, name).kind == "unmapped" and policy.ir_group(name) is None:
            missing.append(name)
    assert not missing, missing


def test_core_ground_vehicle_vocabulary(catalog: CapabilityCatalog) -> None:
    fine = {
        n: assess_ir_element(catalog, n, "ground_vehicle")[1].conclusion for n in ir_vocabulary()
    }
    for name in (
        "move_to_position",
        "follow_route",
        "halt",
        "hold_overwatch_position",
        "turn_sensor_on",
        "report_contact",
        "track_value.current_target_valid",
    ):
        assert fine[name] == "ready", name
    for name in (
        "keep_station",
        "assume_formation",
        "fire_weapon",
        "track_value.nearest_hostile_range",
    ):
        assert fine[name] == "composable", name
    for name in (
        "terrain_value.class",
        "follow_route_with_offset",
        "move_to_platform",
        "send_message",
    ):
        assert fine[name] == "develop", name
    coarse = CapabilityPolicy.make()
    # outside the core groups, a coarse draft simplifies instead of reporting a gap
    assert (
        assess_ir_element(catalog, "send_message", "ground_vehicle", policy=coarse)[1].conclusion
        == "simplified"
    )
    assert (
        assess_ir_element(catalog, "terrain_value.class", "ground_vehicle", policy=coarse)[
            1
        ].conclusion
        == "develop"
    )


def test_role_dependencies(catalog: CapabilityCatalog) -> None:
    assert map_binding("route_following_ground") == ("movement.follow_route",)
    assert map_binding("route_following_air") == ()  # nothing in the lab flies
    assert "mission_processor" in Aliases.default().static_bindings


def test_search_finds_tactical_phrases(catalog: CapabilityCatalog) -> None:
    assert catalog.search("跃进到下一位置")[0].id == "movement.move_to_point"
    assert catalog.search("长机分配射击目标")[0].id == "threat_allocation.allocate_targets"


def test_ir_extension_when_ir_cannot_reach(catalog: CapabilityCatalog) -> None:
    # implemented, but no IR element uses it
    ref = "hierarchical_formation.elect_replacement_leader"
    assert assess_ref(catalog, ref).conclusion == "ready"
    assert assess_ref(catalog, ref, need_ir=True).conclusion == "ir_extension"
    # reached through a composition (nearest_hostile_range)
    assert (
        assess_ref(catalog, "perception.classify_affiliation", need_ir=True).conclusion == "ready"
    )


# ---------------------------------------------------------------------- armor example
ARMOR_EXPECTED = {
    "follow_route": "ready",
    "bound_to_position": "ready",
    "hold_overwatch_position": "ready",
    "keep_station": "composable",
    "send_message": "develop",
    "report_contact": "ready",
    "set_blackboard": "ready",
    "blackboard": "ready",
    "message_received": "develop",
    "bounding_turn": "develop",
    "standoff_reached": "partial",
    "track_value.nearest_hostile_range": "composable",
    "terrain_value.class": "develop",
    "terrain_value.distance_to_class": "develop",
    "mover_value.route_complete": "ready",
    "mover_value.at_destination": "ready",
    "mover_value.available": "ready",
    "time_in_state_gte": "ready",
}


def test_armor_example_against_lab_capabilities(catalog: CapabilityCatalog) -> None:
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
    for dep in dependencies:
        verdict = assess(
            catalog, map_binding(dep.split(":", 1)[1]), platform_class="ground_vehicle"
        )
        assert verdict.conclusion in ("ready", "composable")
