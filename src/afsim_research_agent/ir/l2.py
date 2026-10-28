"""L2 single-step semantic tests (design §20), run on normalized IR without any backend."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from afsim_research_agent.ir.catalog import Catalog, load_catalog
from afsim_research_agent.ir.diagnostics import Collector, Diagnostic
from afsim_research_agent.ir.runtime import Delivery, DictWorld, RoleController, TickResult
from afsim_research_agent.ir.units import PHYSICAL_TYPES, infer_dimension, to_canonical


@dataclass
class TestOutcome:
    __test__ = False

    test_id: str
    role: str
    passed: bool
    failures: list[str]
    result: dict[str, Any]


@dataclass
class L2Report:
    outcomes: list[TestOutcome]
    coverage: dict[str, dict[str, list[str]]]
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(o.passed for o in self.outcomes) and not any(
            d.severity.value == "error" for d in self.diagnostics
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "tests": [
                {
                    "id": o.test_id,
                    "role": o.role,
                    "passed": o.passed,
                    "failures": o.failures,
                    "result": o.result,
                }
                for o in self.outcomes
            ],
            "transition_coverage": self.coverage,
        }


def defaults_from_catalog(catalog: Catalog) -> dict[str, dict[str, Any]]:
    return {
        name: {prop: p.default for prop, p in operand.properties.items()}
        for name, operand in catalog.operands.items()
    }


def _quantity(raw: Any) -> Any:
    if isinstance(raw, dict) and set(raw) == {"value", "unit"}:
        dim = infer_dimension(str(raw["unit"]))
        if dim is not None:
            return to_canonical(float(raw["value"]), str(raw["unit"]), dim)
    return raw


def _world_data(
    ir: dict[str, Any], role: dict[str, Any], given: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    data: dict[str, dict[str, Any]] = defaultdict(dict)
    sections = {
        "platform": "self_value",
        "tracks": "track_value",
        "terrain": "terrain_value",
        "formation": "formation_value",
        "team": "team_value",
        "simulation": "simulation_value",
    }
    for section, source in sections.items():
        for key, value in (given.get(section) or {}).items():
            data[source][key] = _quantity(value)
    deps = role["dependencies"]
    kinds = {
        **{c["id"]: "sensor_value" for c in deps["sensors"]},
        **{c["id"]: "weapon_value" for c in deps["weapons"]},
        **{c["id"]: "comm_value" for c in deps["comms"]},
    }
    for key, value in (given.get("components") or {}).items():
        comp, _, prop = key.partition(".")
        if comp == "mover":
            data["mover_value"][prop] = value
        elif comp in kinds:
            data[kinds[comp]][key] = _quantity(value)
    return data


def _param_override(ir: dict[str, Any], name: str, raw: Any) -> Any:
    param = ir["parameters"][name]
    if param["type"] in PHYSICAL_TYPES:
        if isinstance(raw, dict):
            return to_canonical(float(raw["value"]), str(raw["unit"]), param["type"])
        unit = param["original"]["unit"]
        return to_canonical(float(raw), unit, param["type"])
    return raw


def run_test(
    ir: dict[str, Any], test: dict[str, Any], catalog: Catalog | None = None
) -> TestOutcome:
    catalog = catalog or load_catalog()
    given, expect = test["given"], test["expect"]
    role_id = given.get("role") or ir["roles"][0]["id"]
    role = next(r for r in ir["roles"] if r["id"] == role_id)
    overrides = {
        name: _param_override(ir, name, raw)
        for name, raw in (given.get("parameters") or {}).items()
    }
    ctl = RoleController(ir, role_id, given.get("instance", 0), parameter_overrides=overrides)
    now = float(_quantity((given.get("simulation") or {}).get("time", 0.0)) or 0.0)
    if given.get("state"):
        ctl.state = given["state"]
    elapsed = given.get("elapsed") or {}
    ctl.state_entry = now - float(_quantity(elapsed.get("state", 0.0)) or 0.0)
    for key, value in (given.get("blackboard") or {}).items():
        parts = key.split(".")
        ctl.blackboard[parts[-2]][parts[-1]] = value
    ctl.coordination_overrides = dict(given.get("coordination") or {})
    _preactivate(ctl, elapsed, now)
    world = DictWorld(_world_data(ir, role, given), defaults_from_catalog(catalog))
    inbox = [
        Delivery(
            item["message"],
            dict(item.get("fields") or {}),
            item.get("sender_role", ""),
            item.get("sender_instance", 0),
        )
        for item in given.get("messages") or []
    ]
    try:
        result = ctl.tick(now, world, inbox)
    except (TypeError, ValueError, KeyError, ZeroDivisionError) as exc:
        # a malformed input (e.g. a nested value where a number is read) fails this test only
        message = f"test could not run: {type(exc).__name__}: {exc}"
        return TestOutcome(test["id"], role_id, False, [message], _empty_summary(ctl))
    failures = _compare(expect, result, ctl)
    return TestOutcome(test["id"], role_id, not failures, failures, _summary(result, ctl))


def _empty_summary(ctl: RoleController) -> dict[str, Any]:
    return {
        "previous_state": ctl.state,
        "next_state": None,
        "fired_transition": None,
        "tree_status": None,
        "selected_path": [],
        "issued_actions": [],
        "sent_messages": [],
        "blackboard": {},
        "issued_action_details": [],
        "sent_message_details": [],
        "ticked_nodes": [],
    }


def _preactivate(ctl: RoleController, elapsed: dict[str, Any], now: float) -> None:
    from afsim_research_agent.ir.runtime import _Activation

    for behavior_id, raw in elapsed.items():
        if behavior_id == "state" or behavior_id not in ctl.behaviors:
            continue
        seconds = float(_quantity(raw) or 0.0)
        behavior = ctl.behaviors[behavior_id]
        for tree in ctl.trees.values():
            stack = [tree["root"]]
            while stack:
                node = stack.pop()
                stack += node.get("children", [])
                if node["type"] == "behavior" and node["behavior"] == behavior_id:
                    ctl.activations[node["node_id"]] = _Activation(
                        active=True,
                        start=now - seconds,
                        issued=set(range(len(behavior["actions"]))),
                        memory={m["name"]: m["default"] for m in behavior["memory"]},
                    )


def _summary(result: TickResult, ctl: RoleController) -> dict[str, Any]:
    return {
        "previous_state": result.previous_state,
        "next_state": result.next_state,
        "fired_transition": result.fired_transition,
        "tree_status": result.tree_status,
        "selected_path": result.selected_path,
        "issued_actions": [a.command for a in result.issued_actions],
        "sent_messages": [m.message for m in result.sent_messages],
        "blackboard": {
            f"{b}.{k}": v for b, vals in ctl.blackboard.items() for k, v in vals.items()
        },
        "issued_action_details": [
            {
                "command": a.command,
                "behavior": a.behavior,
                "node_id": a.node_id,
                "arguments": a.arguments,
            }
            for a in result.issued_actions
        ],
        "sent_message_details": [
            {"message": m.message, "fields": m.fields} for m in result.sent_messages
        ],
        "ticked_nodes": result.ticked_nodes,
    }


def _compare(expect: dict[str, Any], result: TickResult, ctl: RoleController) -> list[str]:
    failures: list[str] = []
    checks = {
        "next_state": result.next_state,
        "fired_transition": result.fired_transition,
        "tree_status": result.tree_status,
        "selected_path": result.selected_path,
        "issued_actions": [a.command for a in result.issued_actions],
        "sent_messages": [m.message for m in result.sent_messages],
    }
    for key, actual in checks.items():
        wanted = expect.get(key)
        if wanted is not None and wanted != actual:
            failures.append(f"{key}: expected {wanted!r}, got {actual!r}")
    for key, wanted in (expect.get("blackboard") or {}).items():
        parts = key.split(".")
        actual = ctl.blackboard.get(parts[-2], {}).get(parts[-1])
        if actual != wanted:
            failures.append(f"blackboard {key}: expected {wanted!r}, got {actual!r}")
    return failures


def run_tests(ir: dict[str, Any], catalog: Catalog | None = None) -> L2Report:
    catalog = catalog or load_catalog()
    outcomes = [run_test(ir, test, catalog) for test in ir["tests"]]
    diag = Collector()
    for outcome in outcomes:
        if not outcome.passed:
            diag.error("TEST001", f"tests.{outcome.test_id}", "; ".join(outcome.failures))
    covered: dict[str, set[str]] = defaultdict(set)
    for outcome in outcomes:
        if outcome.result["fired_transition"]:
            covered[outcome.role].add(outcome.result["fired_transition"])
    coverage: dict[str, dict[str, list[str]]] = {}
    for role in ir["roles"]:
        machine = role["state_machine"]
        if machine is None:
            continue
        required = [
            tr["transition_id"]
            for state in machine["states"]
            for tr in state["transitions"]
            if not tr["fallback"] and "generated_from" not in tr
        ]
        missing = [t for t in required if t not in covered[role["id"]]]
        coverage[role["id"]] = {"covered": sorted(covered[role["id"]]), "missing": missing}
        for tid in missing:
            diag.error(
                "SCN001",
                f"roles.{role['id']}.transitions.{tid}",
                f"transition '{tid}' is not exercised by any L2 test",
            )
    return L2Report(outcomes, coverage, diag.items)
