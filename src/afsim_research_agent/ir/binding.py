"""Backend binding: logical capabilities -> backend components (``resolved_context.json``).

Bindings are the only place where backend-specific names appear (design R1, §24).
A package may override or extend defaults with ``bindings/<backend>.json``::

    {"capabilities": {"visual_search": {"component": "sim.VisualSensor",
                                         "settings": {"range_m": 3000}}}}
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from afsim_research_agent.ir.diagnostics import Collector, Diagnostic

BINDING_VERSION = "bindings-2026.10.1"

# Built-in 2D simulation components and their default settings.
SIM_CAPABILITIES: dict[str, dict[str, Any]] = {
    "route_following_ground": {
        "component": "sim.GroundMover",
        "settings": {"max_speed_mps": 15.0, "max_accel_mps2": 2.0, "turn_rate_dps": 45.0},
    },
    "route_following_air": {
        "component": "sim.AirMover",
        "settings": {"max_speed_mps": 250.0, "max_accel_mps2": 20.0, "turn_rate_dps": 15.0},
    },
    "route_following_surface": {
        "component": "sim.SurfaceMover",
        "settings": {"max_speed_mps": 12.0, "max_accel_mps2": 0.5, "turn_rate_dps": 10.0},
    },
    "visual_search": {"component": "sim.RangeSensor", "settings": {"range_m": 2000.0}},
    "thermal_search": {"component": "sim.RangeSensor", "settings": {"range_m": 3000.0}},
    "radar_search": {"component": "sim.RangeSensor", "settings": {"range_m": 40000.0}},
    "voice_data": {"component": "sim.Radio", "settings": {}},
    "data_link": {"component": "sim.Radio", "settings": {}},
    "direct_fire": {"component": "sim.Weapon", "settings": {"quantity": 40}},
    "guided_missile": {"component": "sim.Weapon", "settings": {"quantity": 4}},
    "fuel_tracking": {"component": "sim.FuelTank", "settings": {"burn_per_s": 0.0001}},
    "mission_processor": {"component": "sim.Processor", "settings": {}},
}

PLATFORM_TEMPLATES = {
    "python_sim": {
        "ground_vehicle": "sim.ground_vehicle",
        "air_vehicle": "sim.air_vehicle",
        "surface_vessel": "sim.surface_vessel",
        "static_node": "sim.static_node",
    }
}

BACKENDS = ("python_sim", "btcpp_xml", "mermaid")


def _components(role: dict[str, Any]) -> list[tuple[str, str, str, bool]]:
    """(logical id, kind, capability, required) for every dependency of a role."""
    deps = role["dependencies"]
    out: list[tuple[str, str, str, bool]] = []
    if deps["mover"]:
        out.append(("mover", "mover", deps["mover"]["capability"], deps["mover"]["required"]))
    if deps["energy"]:
        out.append(("energy", "energy", deps["energy"]["capability"], deps["energy"]["required"]))
    for kind in ("sensors", "comms", "weapons", "processors"):
        for comp in deps[kind]:
            out.append((comp["id"], kind, comp["capability"], comp["required"]))
    return out


def load_overrides(package_dir: Path | None, backend: str) -> dict[str, Any]:
    if package_dir is None:
        return {}
    path = package_dir / "bindings" / f"{backend}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("capabilities", {})


def bind(
    ir: dict[str, Any], backend: str, overrides: dict[str, Any] | None = None
) -> tuple[dict[str, Any] | None, list[Diagnostic]]:
    diag = Collector()
    if backend not in BACKENDS:
        diag.error("BIND001", "backend", f"unknown backend '{backend}'; known: {list(BACKENDS)}")
        return None, diag.items
    overrides = overrides or {}
    roles: dict[str, Any] = {}
    for role in ir["roles"]:
        components: dict[str, Any] = {}
        for logical, kind, capability, required in _components(role):
            path = f"roles.{role['id']}.dependencies.{logical}"
            if backend == "python_sim":
                base = SIM_CAPABILITIES.get(capability)
                override = overrides.get(capability)
                if base is None and override is None:
                    if required:
                        diag.error(
                            "DEP001",
                            path,
                            f"required capability '{capability}' has no {backend} binding",
                            suggestion=f"add bindings/{backend}.json or use one of "
                            f"{sorted(SIM_CAPABILITIES)}",
                        )
                    else:
                        diag.warning(
                            "BIND001",
                            path,
                            f"optional capability '{capability}' is unbound; degrade",
                        )
                    continue
                merged = {
                    "component": (override or {}).get("component") or base["component"],  # type: ignore[index]
                    "settings": {
                        **(base or {}).get("settings", {}),
                        **(override or {}).get("settings", {}),
                    },
                }
            else:
                merged = {"component": f"port:{capability}", "settings": {}}
            components[logical] = {"kind": kind, "capability": capability, **merged}
        template = PLATFORM_TEMPLATES.get(backend, {}).get(
            role["capability_class"], f"abstract:{role['capability_class']}"
        )
        roles[role["id"]] = {"platform_template": template, "components": components}
    context = {
        "backend": backend,
        "binding_version": BINDING_VERSION,
        "ir_hash": ir["ir_hash"],
        "roles": roles,
    }
    return (None if diag.has_errors else context), diag.items


def bind_external(
    ir: dict[str, Any], engine: str, capabilities: dict[str, Any]
) -> tuple[dict[str, Any] | None, list[Diagnostic]]:
    """Bind logical capabilities to an external (lab) engine from ``bindings/<engine>.json``.

    ``capabilities`` maps capability -> {"component": engine name, "settings": {...}}.
    Required capabilities without an entry are DEP001 errors.
    """
    diag = Collector()
    roles: dict[str, Any] = {}
    for role in ir["roles"]:
        components: dict[str, Any] = {}
        for logical, kind, capability, required in _components(role):
            entry = capabilities.get(capability)
            path = f"roles.{role['id']}.dependencies.{logical}"
            if not entry or not entry.get("component"):
                if required:
                    diag.error(
                        "DEP001",
                        path,
                        f"required capability '{capability}' has no {engine} binding",
                        suggestion=f'add "capabilities": {{"{capability}": {{"component": ...}}}} '
                        f"to bindings/{engine}.json",
                    )
                else:
                    diag.warning("BIND001", path, f"optional capability '{capability}' is unbound")
                continue
            components[logical] = {
                "kind": kind,
                "capability": capability,
                "component": entry["component"],
                "settings": dict(entry.get("settings", {})),
            }
        roles[role["id"]] = {
            "platform_template": capabilities.get(f"platform:{role['capability_class']}", {}).get(
                "component", f"{engine}:{role['capability_class']}"
            ),
            "components": components,
        }
    context = {
        "backend": engine,
        "binding_version": BINDING_VERSION,
        "ir_hash": ir["ir_hash"],
        "roles": roles,
    }
    return (None if diag.has_errors else context), diag.items
