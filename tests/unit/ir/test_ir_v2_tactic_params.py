"""Public parameters of tactic-set subtrees: declared with type / units / range / category,
filled into the inner actions when the model uses the subtree (normalize), shown to the
drafting model (Falcon catalog), and exported from the lab files (lab_export).

A synthetic capability set is used (no lab content)."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from afsim_research_agent.capability.lab_export import LabExporter
from afsim_research_agent.falcon.v3 import catalog
from afsim_research_agent.falcon.v3.semantics import bound_by_name
from afsim_research_agent.ir.v2 import Contracts, normalize
from afsim_research_agent.ir.v2.contracts import param_ref, param_spec
from afsim_research_agent.ir.v2.from_tactic import mission_to_ir


def _w(root: Path, rel: str, data) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


SPEED = {
    "type": "number",
    "units": "m/s",
    "default": 6,
    "minimum": 0,
    "maximum": 12,
    "category": "design",
    "effect": "跃进速度",
}


@pytest.fixture
def contracts(tmp_path: Path) -> Contracts:
    root = tmp_path / "set"
    _w(
        root,
        "index.yaml",
        {
            "format": "CapabilitySet",
            "format_version": "1.0.0",
            "catalog": {"id": "demo_set", "platform_classes": ["ground_vehicle"]},
            "slices": [
                {
                    "id": "nav",
                    "action_file": "action_set/nav.yaml",
                    "tactic_file": "tactic_set/nav.yaml",
                }
            ],
        },
    )
    _w(
        root,
        "action_set/nav.yaml",
        {
            "resources": {"drive": {"mode": "exclusive"}},
            "actions": [
                {
                    "id": "nav.follow",
                    "status": "implemented",
                    "resources": [{"resource": "drive", "mode": "exclusive"}],
                    "parameters": {
                        "route_name": {"type": "string", "required": True, "category": "scenario"},
                        "speed_mps": {**SPEED, "default": 8, "maximum": 15},
                    },
                },
                {
                    "id": "nav.slots",
                    "status": "implemented",
                    "parameters": {
                        "formation": {
                            "type": "string",
                            "enum": ["COLUMN", "LINE_ABREAST"],
                            "default": "COLUMN",
                            "category": "design",
                        },
                        "spacing_m": {"type": "number", "units": "m", "default": 20},
                    },
                },
            ],
        },
    )
    _w(
        root,
        "tactic_set/nav.yaml",
        {
            "subtrees": [
                {
                    "id": "nav.bound",
                    "parameters": {"speed_mps": SPEED},
                    "root": {
                        "type": "action",
                        "action": "nav.follow",
                        "params": {"speed_mps": {"$param": "speed_mps"}},
                    },
                },
                {
                    "id": "nav.plan_and_bound",
                    "parameters": {
                        "formation": {
                            "type": "string",
                            "enum": ["COLUMN", "LINE_ABREAST"],
                            "default": "COLUMN",
                            "category": "design",
                        },
                        "spacing_m": {"type": "number", "units": "m", "minimum": 5},
                        "speed_mps": SPEED,
                    },
                    "root": {
                        "type": "sequence",
                        "children": [
                            {
                                "type": "action",
                                "action": "nav.slots",
                                "params": {
                                    "formation": {"$param": "formation"},
                                    "spacing_m": {"$param": "spacing_m"},
                                },
                            },
                            {
                                "type": "subtree",
                                "subtree": "nav.bound",
                                "params": {"speed_mps": {"$param": "speed_mps"}},
                            },
                        ],
                    },
                },
            ],
            "state_trees": [],
            "missions": [
                {
                    "id": "nav.advance",
                    "initial_state": "MOVING",
                    "parameters": {"speed_mps": SPEED, "legs": 2},
                    "states": ["MOVING", "COMPLETE"],
                    "terminal_states": ["COMPLETE"],
                    "state_trees": {"MOVING": "nav.plan_and_bound"},
                    "transitions": [{"from": "MOVING", "to": "COMPLETE", "when": "ARRIVED"}],
                }
            ],
        },
    )
    return Contracts.load(root)


def _spec(state_tree, params=None) -> dict:
    spec = {
        "schema_version": "2.0",
        "model": {"id": "demo", "display_name": "演示", "version": "0.1.0", "intent": "跃进"},
        "roles": [{"id": "lead", "capability_class": "ground_vehicle"}],
        "parameters": params or {},
        "events": [{"id": "done", "source": "external"}],
        "subtrees": [],
        "state_trees": {"lead": {"GO": "go"}},
        "missions": {
            "lead": {
                "initial_state": "GO",
                "states": ["GO", "COMPLETE"],
                "terminal": ["COMPLETE"],
                "transitions": [{"from": "GO", "to": "COMPLETE", "on": "done"}],
            }
        },
    }
    if isinstance(state_tree, str):
        spec["state_trees"]["lead"]["GO"] = state_tree
    else:
        spec["subtrees"].append({"id": "go", "root": state_tree})
    return spec


def _call(**params) -> dict:
    return {"type": "tactic", "tactic": "nav.plan_and_bound", "params": params}


def _leaves(node: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if node.get("type") == "action":
        out[node["action"]] = node.get("params") or {}
    for kid in node.get("children") or []:
        out.update(_leaves(kid))
    return out


def _errors(result) -> list[str]:
    return [d.render() for d in result.diagnostics if d.severity.value == "error"]


def test_spec_reading_keeps_older_bare_defaults() -> None:
    assert param_spec(500) == {"default": 500}
    assert param_spec({"value": 5, "unit": "m"}) == {"default": {"value": 5, "unit": "m"}}
    assert param_spec({"type": "number", "unit": "m"})["units"] == "m"
    assert param_ref({"$param": "x"}) == "x" and param_ref({"x": 1}) is None


def test_call_values_fill_inner_actions(contracts: Contracts) -> None:
    tree = _call(formation="LINE_ABREAST", spacing_m={"value": 0.1, "unit": "km"}, speed_mps=10)
    result = normalize(_spec(tree), contracts)
    assert result.ok, _errors(result)
    root = result.model["roles"]["lead"]["states"]["GO"]["tree"]
    assert root["params"] == {"formation": "LINE_ABREAST", "spacing_m": 100.0, "speed_mps": 10}
    leaves = _leaves(root)
    assert leaves["nav.slots"] == {"formation": "LINE_ABREAST", "spacing_m": 100.0}
    assert leaves["nav.follow"] == {"speed_mps": 10}  # through the nested subtree


def test_defaults_and_unbound_parameters(contracts: Contracts) -> None:
    result = normalize(_spec(_call()), contracts)
    assert result.ok, _errors(result)
    leaves = _leaves(result.model["roles"]["lead"]["states"]["GO"]["tree"])
    # spacing_m has no default: the argument is left out and the contract default applies
    assert leaves["nav.slots"] == {"formation": "COLUMN"}
    assert leaves["nav.follow"] == {"speed_mps": 6}


def test_model_parameters_pass_by_reference(contracts: Contracts) -> None:
    params = {"cruise": {"type": "speed", "value": 9, "unit": "m/s"}}
    result = normalize(_spec(_call(speed_mps="$cruise"), params), contracts)
    assert result.ok, _errors(result)
    leaves = _leaves(result.model["roles"]["lead"]["states"]["GO"]["tree"])
    assert leaves["nav.follow"] == {"speed_mps": {"parameter_ref": "cruise"}}


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"speed_mps": 20}, "ACT002"),  # above the declared maximum
        ({"formation": "WEDGE"}, "ACT002"),  # not one of the choices
        ({"spacing_m": {"value": 3, "unit": "s"}}, "UNIT001"),
        ({"depth": 2}, "TAC001"),  # not a public parameter
    ],
)
def test_call_values_are_checked(contracts: Contracts, params, code) -> None:
    result = normalize(_spec(_call(**params)), contracts)
    assert not result.ok and code in {d.code for d in result.diagnostics}


def test_direct_state_tree_takes_model_parameters_by_name(contracts: Contracts) -> None:
    params = {
        "speed_mps": {"type": "speed", "value": 4, "unit": "m/s"},
        "spacing_m": {"type": "distance", "value": 40, "unit": "m"},
    }
    result = normalize(_spec("nav.plan_and_bound", params), contracts)
    assert result.ok, _errors(result)
    leaves = _leaves(result.model["roles"]["lead"]["states"]["GO"]["tree"])
    assert leaves["nav.slots"]["spacing_m"] == {"parameter_ref": "spacing_m"}
    assert leaves["nav.follow"] == {"speed_mps": {"parameter_ref": "speed_mps"}}
    spec = _spec("nav.plan_and_bound", params)
    assert bound_by_name(spec, contracts.tactics) == {"formation", "spacing_m", "speed_mps"}


def test_mission_reference_values_reach_its_trees(contracts: Contracts) -> None:
    spec = _spec("nav.plan_and_bound")
    spec["missions"]["lead"] = {"tactic": "nav.advance", "params": {"speed_mps": 3}}
    del spec["state_trees"]["lead"]
    spec["events"] = []
    result = normalize(spec, contracts)
    states = result.model["roles"]["lead"]["states"]
    assert _leaves(states["MOVING"]["tree"])["nav.follow"] == {"speed_mps": 3}
    raw = mission_to_ir(contracts, "nav.advance")
    assert raw["parameters"]["speed_mps"]["value"] == 6
    assert raw["parameters"]["speed_mps"]["type"] == "speed"


def test_catalog_shows_categories_and_parameter_arguments(contracts: Contracts) -> None:
    text = catalog.details(contracts, ["nav"])
    assert "[设计] number m/s 默认 6 >=0 <=12（跃进速度）" in text
    assert "route_name: [想定] string 必填" in text
    lines = catalog.tree_python(contracts, contracts.tactics["nav.plan_and_bound"].raw["root"])
    assert "nav.slots(formation=formation, spacing_m=spacing_m)" in "\n".join(lines)
    assert 'tactic("nav.bound", speed_mps=speed_mps)' in "\n".join(lines)


def test_lab_export_keeps_parameters_and_arguments() -> None:
    exporter = LabExporter.__new__(LabExporter)
    raw = {"speed_mps": {**SPEED, "x_internal": 1}, "legs": 2}
    out = exporter._parameters(copy.deepcopy(raw))
    assert out["speed_mps"]["category"] == "design" and "x_internal" not in out["speed_mps"]
    assert out["legs"] == {"default": 2}
    acc: dict[str, set[str]] = {"actions": set(), "conditions": set(), "subtrees": set()}
    node = exporter._node(
        "nav",
        {
            "type": "sequence",
            "children": [
                {"type": "action", "ref": "follow", "params": {"speed_mps": {"$param": "v"}}},
                {"type": "subtree", "ref": "bound", "params": {"speed_mps": 3}},
            ],
        },
        acc,
    )
    assert node["children"][0] == {
        "type": "action",
        "action": "nav.follow",
        "params": {"speed_mps": {"$param": "v"}},
    }
    assert node["children"][1]["params"] == {"speed_mps": 3}
