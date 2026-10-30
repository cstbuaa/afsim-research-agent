"""Exporting the capability set from the lab project, and the action / tactic libraries."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from afsim_research_agent.capability import CapabilityCatalog
from afsim_research_agent.capability.__main__ import main as cli
from afsim_research_agent.capability.lab_export import LabExporter, scan_tree, write_export
from afsim_research_agent.capability.libraries import (
    CapabilitySetActionLibrary,
    CapabilitySetTacticLibrary,
    setup_defaults,
)
from afsim_research_agent.orchestrator.extensions import ExtensionRegistry


def _w(root: Path, rel: str, data) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


@pytest.fixture
def lab(tmp_path: Path) -> Path:
    """A miniature lab project in the formats the real one uses (they differ per slice)."""
    root = tmp_path / "lab"
    _w(
        root,
        "catalog/movement_capabilities.yaml",
        {
            "schema_version": "x",
            "capabilities": [
                {
                    "id": "movement.read_speed",
                    "description": "Read speed.",
                    "outputs": [{"name": "speed_mps", "type": "number", "units": "m/s"}],
                    "afsim_binding": "afsim_2_9.example.read_speed",
                }
            ],
        },
    )
    _w(
        root,
        "actions/movement/move_to_point.action.yaml",
        {
            "schema_version": "apc-action/v1",
            "id": "movement.move_to_point",
            "description": "Move to a point.",
            "lifecycle": {"states": ["IDLE", "RUNNING", "SUCCESS", "FAILURE", "CANCELLED"]},
            "resource_claims": [{"resource": "locomotion", "mode": "exclusive"}],
            "parameters": {
                "required": ["latitude_deg"],
                "properties": {"latitude_deg": {"type": "number", "units": "deg"}},
            },
            "afsim_binding": "afsim_2_9.example.move",
            "unmapped": [
                "Route completion uses a scenario endpoint.",
                "Adapter writes apc_example_key via WsfExample.",
                "Defaults come from C:\\example\\model\\x.txt",
            ],
        },
    )
    _w(
        root,
        "schema/blackboard/movement_blackboard.schema.yaml",
        {
            "fields": {
                "self.speed_mps": {"type": "number", "units": "m/s", "afsim": "apc_example"}
            },
        },
    )
    _w(
        root,
        "catalog/engagement_capabilities.yaml",
        {
            "capabilities": [{"id": "engagement.fire", "description": "受控射击。"}],
        },
    )
    _w(
        root,
        "actions/engagement/fire.action.yaml",
        {
            "id": "engagement.fire",
            "role": "MEMBER",
            "failures": ["NO_AMMUNITION"],
        },
    )
    _w(
        root,
        "schema/blackboard/engagement.schema.yaml",
        {
            "weapon_classes": ["EXAMPLE_GUN_99MM"],
            "namespaces": {"weapons.inventory": ["ammunition", "ready"]},
        },
    )
    _w(
        root,
        "arbitration/engagement.policy.yaml",
        {
            "lease_timeout_s": 3.0,
            "resources": {"fire_control": {"mode": "exclusive"}},
        },
    )
    _w(
        root,
        "tactics/engagement/fire_once.subtree.yaml",
        {
            "id": "engagement.fire_once",
            "root": {
                "type": "sequence",
                "children": [
                    {"type": "action", "ref": "engagement.fire"},
                    {"type": "action", "ref": "engagement.inner_step"},
                    {"type": "condition", "ref": "task.valid"},
                ],
            },
        },
    )
    _w(
        root,
        "tactics/engagement/fire_while_holding.subtree.yaml",
        {
            "id": "engagement.fire_while_holding",
            "root": {
                "type": "parallel",
                "success_policy": "one",
                "children": [
                    {
                        "type": "repeat",
                        "until_done": True,
                        "child": {"type": "action", "ref": "engagement.fire"},
                    },
                    {"type": "condition", "ref": "task.valid"},
                ],
            },
        },
    )
    _w(
        root,
        "behavior_trees/engagement/firing.tree.yaml",
        {
            "id": "engagement.firing",
            "role": "MEMBER",
            "root": {"type": "subtree", "ref": "engagement.fire_once"},
        },
    )
    _w(
        root,
        "missions/multiweapon_engagement.fsm.yaml",
        {
            "initial_state": "FIRING",
            "states": ["FIRING", "COMPLETE"],
            "transitions": [{"from": "FIRING", "to": "COMPLETE", "when": "done"}],
        },
    )
    # never read
    _w(root, "bindings/afsim_2_9/movement_binding.yaml", {"secret": "apc_example"})
    (root / "model").mkdir()
    (root / "model" / "x.txt").write_text(
        "platform_type APC_EXAMPLE WSF_EXAMPLE\n", encoding="utf-8"
    )
    return root


def test_export_normalizes_and_scrubs(lab: Path, tmp_path: Path) -> None:
    exporter = LabExporter(lab)
    result = exporter.export()
    move = result["actions"]["movement"]
    action = move["actions"][0]
    assert action["id"] == "movement.move_to_point" and action["status"] == "implemented"
    assert action["statuses"][0] == "IDLE" and action["parameters"]["latitude_deg"]["required"]
    assert action["limitations"] == ["Route completion uses a scenario endpoint."]
    assert "afsim_binding" not in action and move["primitives"][0]["id"] == "movement.read_speed"
    assert move["blackboard"]["fields"]["self.speed_mps"] == {"type": "number", "units": "m/s"}
    fires = result["actions"]["engagement"]
    # a primitive that restates an action is folded into it
    assert [a["id"] for a in fires["actions"]] == ["engagement.fire"] and not fires["primitives"]
    assert fires["actions"][0]["description"] == "受控射击。"
    assert fires["resources"]["fire_control"]["lease_s"] == 3.0
    assert [s["id"] for s in fires["tactic_steps"]] == ["engagement.inner_step"]
    tactics = result["tactics"]["engagement"]
    assert tactics["conditions"] == ["task.valid"]
    assert tactics["missions"][0]["id"] == "engagement.multiweapon_engagement"
    assert tactics["state_trees"][0]["uses"] == {"subtrees": ["engagement.fire_once"]}
    held = next(t for t in tactics["subtrees"] if t["id"] == "engagement.fire_while_holding")
    assert held["root"]["success_policy"] == "one"
    assert held["root"]["children"][0] == {
        "type": "repeat",
        "until_done": True,
        "child": {"type": "action", "action": "engagement.fire"},
    }
    reasons = {r for _, r, _ in exporter.report.dropped}
    assert {"native_type", "path"} <= reasons
    assert exporter.report.files_read == 11  # bindings/ and model/ are never read
    out = tmp_path / "set"
    write_export(result, out)
    assert scan_tree(out) == []
    text = "\n".join(p.read_text(encoding="utf-8") for p in out.rglob("*.yaml"))
    assert "99MM" not in text and "apc_" not in text and "afsim_2_9." not in text
    # deterministic, and loadable as a capability set
    again = tmp_path / "again"
    write_export(LabExporter(lab).export(), again)
    for path in out.rglob("*.yaml"):
        assert path.read_bytes() == (again / path.relative_to(out)).read_bytes()
    catalog = CapabilityCatalog.load(out)
    assert catalog.get("engagement.inner_step").status == "partial"


def test_export_command_writes_only_changes_and_checks(lab: Path, tmp_path: Path, capsys) -> None:
    out = tmp_path / "set"
    assert cli(["export-lab", "--lab", str(lab), "--out", str(out)]) == 0
    assert cli(["export-lab", "--lab", str(lab), "--out", str(out), "--check"]) == 0
    assert "与仓库能力集一致" in capsys.readouterr().out
    _w(lab, "actions/movement/stop.action.yaml", {"id": "movement.stop"})
    assert cli(["export-lab", "--lab", str(lab), "--out", str(out), "--check"]) == 1
    assert not (out / "action_set" / "movement.yaml").read_text("utf-8").count("movement.stop")


def test_an_identifier_that_is_not_public_stops_the_export(lab: Path) -> None:
    _w(lab, "actions/movement/bad.action.yaml", {"id": "movement.apc_private"})
    with pytest.raises(ValueError, match="not public"):
        LabExporter(lab).export()


# ---------------------------------------------------------------------- libraries
def test_action_and_tactic_libraries_over_the_repository_set(capability_set) -> None:
    catalog = capability_set
    actions = CapabilitySetActionLibrary(catalog)
    names = {a.name for a in actions.actions("ground_vehicle")}
    assert "movement.move_to_point" in names and "formation.command_standoff" in names
    assert "movement.read_speed" not in names  # primitives are building blocks, not actions
    assert actions.actions("air_vehicle") == []
    spec = actions.get("movement.move_to_point")
    assert spec.parameters["latitude_deg"].endswith("（必填）") and spec.native == {}
    tactics = CapabilitySetTacticLibrary(catalog)
    mission = tactics.get("formation_standoff.formation_standoff_tracking")
    assert [p["phase"] for p in mission.phases][:3] == [
        "ASSEMBLE_SEARCH",
        "SEARCH_ADVANCE",
        "CONTACT_DEPLOY",
    ]
    assert {"movement.follow_route", "formation.command_standoff"} <= set(mission.actions)
    hits = tactics.match("三车编队搜索前进，接触后展开横队，与目标保持距离")
    assert hits[0].name == "formation_standoff.formation_standoff_tracking"
    assert tactics.match("长机分配射击目标")[0].name.startswith("threat_allocation.")


def test_defaults_fill_only_empty_slots(capability_set) -> None:
    registry = ExtensionRegistry()
    setup_defaults(registry)
    assert registry.action_library.name == "capability-set-actions"
    assert registry.check() == []
    mine = object.__new__(CapabilitySetTacticLibrary)
    other = ExtensionRegistry(tactic_library=mine)
    setup_defaults(other)
    assert other.tactic_library is mine and other.action_library is not None


def test_format_1_1_semantics_are_exported_and_checked(lab: Path) -> None:
    """Field semantics, contracts with reads / writes / result, tactic-step contract files,
    subtree results and mission trigger meanings (capability set 1.1)."""
    _w(
        lab,
        "schema/blackboard/perception_blackboard.schema.yaml",
        {
            "fields": {
                "perception.target.valid": {
                    "type": "boolean",
                    "description": "目标有效",
                    "initial": False,
                },
                "perception.request.id": {
                    "type": "string",
                    "description": "请求",
                    "internal": True,
                },
            }
        },
    )
    _w(
        lab,
        "actions/perception/select.action.yaml",
        {
            "id": "perception.select",
            "description": "选择目标",
            "blackboard": {
                "reads": ["perception.request.id"],
                "writes": ["perception.target.valid"],
                "updated": "on_success",
                "write_semantics": {"perception.target.valid": {"on_success": True}},
            },
            "result": {"success": "已选定", "failure": "没有目标"},
            "persistent": False,
        },
    )
    _w(
        lab,
        "schema/blackboard/formation_standoff_blackboard.schema.yaml",
        {
            "fields": {
                "formation.slot_error_m": {"type": "number", "description": "站位误差"},
                "formation.shape": {"type": "string", "enum": ["A", "B"], "description": "未实现"},
            }
        },
    )
    _w(
        lab,
        "actions/formation_standoff/line_slots.action.yaml",
        {
            "id": "formation.line_slots",
            "kind": "tactic_step",
            "description": "占领横队站位",
            "resource_claims": [
                {"resource": "locomotion", "mode": "exclusive", "scope": "followers"}
            ],
            "blackboard": {
                "reads": ["perception.target.valid"],
                "writes": ["formation.slot_error_m"],
                "updated": "while_running",
                "write_semantics": {"formation.slot_error_m": {"reset_on_start": 999.0}},
            },
            "result": {"running": "未到位", "success": "到位"},
            "persistent": False,
        },
    )
    _w(
        lab,
        "tactics/formation_standoff/deploy.subtree.yaml",
        {
            "id": "formation_standoff.deploy",
            "description": "展开",
            "result": {"running": "未到位", "success": "到位"},
            "persistent": False,
            "root": {"type": "action", "ref": "formation.line_slots"},
        },
    )
    _w(
        lab,
        "missions/formation_standoff_example.fsm.yaml",
        {
            "id": "formation_standoff_example",
            "initial_state": "DEPLOY",
            "states": {
                "DEPLOY": {
                    "tree": "formation_standoff.deploy_state",
                    "transitions": [
                        {"on": "READY", "to": "DONE"},
                        {"on": "SHAPE_OK", "to": "DONE"},
                        {"on": "STOP", "to": "DONE"},
                    ],
                },
                "DONE": {"terminal": True},
            },
            "events": {
                "READY": {"meaning": {"tree": "success"}, "description": "展开完成"},
                "SHAPE_OK": {"meaning": {"expression": {"bb": "formation.shape"}}},
            },
        },
    )
    exporter = LabExporter(lab)
    result = exporter.export()
    acts = result["actions"]["formation_standoff"]
    assert acts["actions"] == []
    (step,) = acts["tactic_steps"]
    assert step["id"] == "formation.line_slots" and step["kind"] == "tactic_step"
    assert step["used_by"] == ["formation_standoff"]
    assert step["writes"] == [
        {"field": "formation.slot_error_m", "updated": "while_running", "reset_on_start": 999.0}
    ]
    assert step["result"]["success"] == "到位" and step["persistent"] is False
    assert step["resources"][0]["scope"] == "followers"
    fields = acts["blackboard"]["fields"]
    assert fields["formation.slot_error_m"]["written_by"] == ["formation.line_slots"]
    assert fields["formation.slot_error_m"]["updated"] == "while_running"
    assert fields["formation.slot_error_m"]["reset_on_start"] == 999.0
    assert fields["formation.shape"]["written_by"] == []  # annotated slice: no writer
    perc = result["actions"]["perception"]["blackboard"]["fields"]
    assert perc["perception.target.valid"]["written_by"] == ["perception.select"]
    assert perc["perception.target.valid"]["initial"] is False
    sub = result["tactics"]["formation_standoff"]["subtrees"][0]
    assert sub["result"] == {"running": "未到位", "success": "到位"}
    assert sub["interface"] == {
        "reads": ["perception.target.valid"],
        "writes": ["formation.slot_error_m"],
        "resources": ["locomotion"],
    }
    (mission,) = result["tactics"]["formation_standoff"]["missions"]
    assert mission["events"]["READY"] == {"meaning": {"tree": "success"}, "description": "展开完成"}
    index = {s["id"]: s for s in result["index"]["slices"]}
    assert index["formation_standoff"]["semantics"] is True
    assert result["index"]["format_version"] == "1.1.0"
    problems = [p for _, p in exporter.report.warnings]
    assert "触发条件缺少含义：STOP" in problems
    assert "SHAPE_OK 读取的字段没有写入者：formation.shape" in problems
    # a slice without annotated contracts claims nothing about writers
    eng = result["actions"]["engagement"]["blackboard"]["fields"]
    assert all("written_by" not in f for f in eng.values())


def test_flat_mission_names_its_state_trees(lab: Path) -> None:
    """Missions with a flat state list name their state trees in ``state_trees``."""
    _w(
        lab,
        "missions/centralized_threat_allocation.fsm.yaml",
        {
            "initial_state": "ASSESSING",
            "states": ["ASSESSING", "COMPLETE"],
            "transitions": [{"from": "ASSESSING", "to": "COMPLETE", "when": "done"}],
            "events": {"done": {"meaning": {"tree": "success"}, "description": "完成"}},
            "state_trees": {"ASSESSING": "threat_allocation.leader_assessing"},
        },
    )
    result = LabExporter(lab).export()
    (mission,) = [
        m
        for m in result["tactics"]["threat_allocation"]["missions"]
        if m["id"].endswith("centralized_threat_allocation")
    ]
    assert mission["state_trees"] == {"ASSESSING": "threat_allocation.leader_assessing"}
