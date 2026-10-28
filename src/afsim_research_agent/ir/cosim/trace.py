"""Run traces (JSON Lines) and judging a trace against L3 expectations.

The same format is written by the co-simulation runner and can be written by
a lab engine running a natively compiled model, so both kinds of run are judged
by identical expectations. One JSON object per line:

    {"type": "header", "model": "...", "ir_hash": "sha256:...", "scenario": "...",
     "entities": {"tank_01": "platoon_leader[0]", ...}}
    {"type": "step", "time": 12.0,
     "entities": {"tank_01": {"x": 100.0, "y": 0.0, "speed": 8.3, "heading": 90.0,
                              "state": "travel_open", "formation": "wedge",
                              "nearest_hostile_m": 1620.0}}}
    {"type": "event", "time": 60.0, "id": "hostile_appears"}
    {"type": "message", "time": 139.0, "sender": "tank_02",
     "message": "coord_contact_report__report"}
    {"type": "action", "time": 139.0, "entity": "tank_02", "command": "report_contact"}
    {"type": "detection", "time": 77.0, "entity": "tank_01"}

``step`` lines are the state after the step at ``time``. Units are canonical
(m, s, m/s, degrees clockwise from north).
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from afsim_research_agent.ir.cosim.runner import slot_position
from afsim_research_agent.ir.sim.l3 import Sample, ScenarioResult, evaluate_trace


@dataclass
class _TraceEntity:
    key: str
    role: str


@dataclass
class RecordedTrace:
    entities: list[_TraceEntity] = field(default_factory=list)
    event_times: dict[str, float] = field(default_factory=dict)
    sent_log: list[tuple[float, str, str]] = field(default_factory=list)
    action_log: list[tuple[float, str, str]] = field(default_factory=list)
    detect_time: dict[str, float] = field(default_factory=dict)


class TraceError(ValueError):
    pass


def write_trace(path: Path, lines: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")


def judge_trace(ir: dict[str, Any], scenario: Mapping[str, Any], path: Path) -> ScenarioResult:
    lines = [
        json.loads(raw) for raw in path.read_text(encoding="utf-8").splitlines() if raw.strip()
    ]
    if not lines or lines[0].get("type") != "header":
        raise TraceError("trace must start with a header line")
    header = lines[0]
    if header.get("ir_hash") != ir["ir_hash"]:
        raise TraceError("trace was recorded for a different normalized IR (ir_hash mismatch)")
    if header.get("scenario") != scenario["id"]:
        raise TraceError(
            f"trace is for scenario '{header.get('scenario')}', not '{scenario['id']}'"
        )
    keys: dict[str, str] = dict(header["entities"])
    roles = {r["id"] for r in ir["roles"]}
    trace = RecordedTrace()
    for engine_id, key in keys.items():
        role = key.split("[")[0]
        if role not in roles:
            raise TraceError(f"unknown role in entity mapping: {key}")
        trace.entities.append(_TraceEntity(key, role))
    formations = {f["id"]: f for f in ir["formations"]}
    leaders = _leader_roles(ir)
    samples: list[Sample] = []
    for line in lines[1:]:
        kind = line.get("type")
        t = float(line.get("time", 0.0))
        if kind == "step":
            ents = {keys[eid]: data for eid, data in line["entities"].items() if eid in keys}
            samples.append(_sample(t, ents, formations, leaders))
        elif kind == "event":
            trace.event_times.setdefault(line["id"], t)
        elif kind == "message":
            trace.sent_log.append((t, keys.get(line["sender"], line["sender"]), line["message"]))
        elif kind == "action":
            trace.action_log.append((t, keys.get(line["entity"], line["entity"]), line["command"]))
        elif kind == "detection":
            trace.detect_time.setdefault(keys.get(line["entity"], line["entity"]), t)
        else:
            raise TraceError(f"unknown trace line type '{kind}'")
    if not samples:
        raise TraceError("trace has no step lines")
    return evaluate_trace(ir, scenario, trace, samples, duration_s=samples[-1].t)


def _leader_roles(ir: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for role in ir["roles"]:
        superior = role["command"].get("superior")
        if superior:
            out[role["id"]] = superior
    for coord in ir["coordination"]:
        if coord["pattern"] == "leader_follower":
            out[coord["followers"]] = coord["leader"]
    return out


def _sample(
    t: float,
    ents: dict[str, dict[str, Any]],
    formations: dict[str, Any],
    leaders: dict[str, str],
) -> Sample:
    rms: dict[str, float | None] = {}
    for fid, form in formations.items():
        errors = []
        for slot in form["slots"]:
            key = f"{slot['role']}[{slot['instance']}]"
            leader_key = f"{leaders.get(slot['role'], '')}[0]"
            if key not in ents or leader_key not in ents:
                continue
            leader = ents[leader_key]
            pos = slot_position(
                form,
                slot["role"],
                slot["instance"],
                leader["x"],
                leader["y"],
                leader.get("heading", 0.0),
            )
            if pos is not None:
                errors.append(math.hypot(pos[0] - ents[key]["x"], pos[1] - ents[key]["y"]))
        rms[fid] = math.sqrt(sum(e * e for e in errors) / len(errors)) if errors else None
    return Sample(
        t=t,
        states={k: v.get("state") for k, v in ents.items()},
        positions={k: (float(v["x"]), float(v["y"])) for k, v in ents.items()},
        speeds={k: float(v.get("speed", 0.0)) for k, v in ents.items()},
        formations={k: v.get("formation", "") for k, v in ents.items()},
        formation_rms=rms,
        hostile_ranges={
            k: float(v["nearest_hostile_m"])
            for k, v in ents.items()
            if v.get("nearest_hostile_m") is not None
        },
    )
