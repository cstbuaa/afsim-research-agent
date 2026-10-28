"""Conformance checker for engine hosts implementing ``tbe-step/1``.

Runs a fixed request sequence against ``host:port`` and checks envelopes, error
handling, time semantics and basic movement semantics. It needs no behavior
model and no engine-specific knowledge, so it can validate a native bridge
before any tactical model runs on it.
"""

from __future__ import annotations

import math
import socket
from dataclasses import dataclass
from typing import Any

from afsim_research_agent.ir.cosim.protocol import PROTOCOL, recv_frame, request, send_frame

SETUP: dict[str, Any] = {
    "scenario": {"id": "protocol_check", "environment": {"duration": {"value": 60, "unit": "s"}}},
    "entities": [
        {
            "engine_id": "e1",
            "role": "probe",
            "instance": 0,
            "capability_class": "ground_vehicle",
            "components": {},
            "position": [0.0, 0.0],
            "heading_deg": 90.0,
            "speed_mps": 0.0,
        },
        {
            "engine_id": "e2",
            "role": "probe",
            "instance": 1,
            "capability_class": "ground_vehicle",
            "components": {},
            "position": [0.0, -50.0],
            "heading_deg": 90.0,
            "speed_mps": 0.0,
        },
    ],
    "routes": {"r1": [[0.0, 0.0], [500.0, 0.0]]},
    "waypoints": {},
    "terrain": {
        "default_class": "open",
        "regions": [{"class": "urban", "x_min": 300, "x_max": 400, "y_min": -100, "y_max": 100}],
    },
    "time_step_s": 1.0,
    "duration_s": 60.0,
    "random_seed": 1,
}
REQUIRED_ENTITY_FIELDS = ("x", "y", "heading_deg", "speed_mps")


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


class _Client:
    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.run_id = "protocol-check"
        self.seq = 0

    def call(self, kind: str, body: dict[str, Any], *, seq: int | None = None) -> dict[str, Any]:
        if seq is None:
            self.seq += 1
            seq = self.seq
        send_frame(self.sock, request(self.run_id, seq, kind, body))
        return recv_frame(self.sock)

    def close(self) -> None:
        self.sock.close()


def _ok(reply: dict[str, Any], kind: str, seq: int) -> str | None:
    if reply.get("protocol") != PROTOCOL:
        return f"protocol is {reply.get('protocol')!r}"
    if reply.get("sequence_id") != seq:
        return f"sequence_id {reply.get('sequence_id')} != {seq}"
    if not reply.get("ok"):
        return f"error {reply.get('error')}"
    if reply.get("type") != f"{kind}Result":
        return f"type {reply.get('type')!r}"
    return None


def _error(reply: dict[str, Any], code: str) -> str | None:
    if reply.get("ok") or reply.get("type") != "Error":
        return "expected an Error response"
    got = (reply.get("error") or {}).get("code")
    return None if got == code else f"error code {got!r}, expected {code!r}"


def _obs_problems(obs: dict[str, Any], ids: list[str]) -> list[str]:
    problems = []
    if not isinstance(obs.get("time"), int | float):
        problems.append("observation.time missing")
    ents = obs.get("entities") or {}
    for eid in ids:
        ent = ents.get(eid)
        if ent is None:
            problems.append(f"entity {eid} missing")
            continue
        for name in REQUIRED_ENTITY_FIELDS:
            if not isinstance(ent.get(name), int | float):
                problems.append(f"{eid}.{name} missing or not a number")
        terrain = ent.get("terrain") or {}
        if terrain and not isinstance(terrain.get("class", ""), str):
            problems.append(f"{eid}.terrain.class must be a string")
        for cls, dist in (terrain.get("distance_to") or {}).items():
            if dist is not None and not isinstance(dist, int | float):
                problems.append(f"{eid}.terrain.distance_to.{cls} must be a number or null")
    return problems


def run_checks(host: str, port: int, *, timeout: float = 30.0) -> list[CheckResult]:
    results: list[CheckResult] = []

    def record(name: str, problem: str | None) -> bool:
        results.append(CheckResult(name, problem is None, problem or ""))
        return problem is None

    c = _Client(host, port, timeout)
    try:
        reply = c.call("Hello", {"client": "protocol-check", "versions": [PROTOCOL]})
        problem = _ok(reply, "Hello", c.seq)
        body = reply.get("body") or {}
        if problem is None and body.get("messaging") not in ("adapter", "engine"):
            problem = "Hello.messaging must be 'adapter' or 'engine'"
        if not record("hello", problem):
            return results
        commands = set(body.get("commands") or [])
        record(
            "hello declares follow_route and halt",
            None if {"follow_route", "halt"} <= commands else f"commands {sorted(commands)}",
        )

        record(
            "step before load is BAD_STATE",
            _error(c.call("Step", {"dt": 1.0, "requested_time": 1.0}), "BAD_STATE"),
        )
        record("unknown request type is BAD_REQUEST", _error(c.call("Teleport", {}), "BAD_REQUEST"))
        record(
            "repeated sequence_id is BAD_REQUEST",
            _error(c.call("GetState", {}, seq=c.seq), "BAD_REQUEST"),
        )

        reply = c.call("LoadScenario", {"setup": SETUP})
        problem = _ok(reply, "LoadScenario", c.seq)
        body = reply.get("body") or {}
        if problem is None:
            if sorted(body.get("entities") or []) != ["e1", "e2"]:
                problem = f"entities {body.get('entities')}"
            else:
                obs_problems = _obs_problems(body.get("observation") or {}, ["e1", "e2"])
                problem = "; ".join(obs_problems) or None
        if not record("load scenario", problem):
            return results
        start = body["observation"]
        record("initial time is 0", None if abs(start["time"]) < 1e-6 else f"time {start['time']}")
        e1 = start["entities"]["e1"]
        record(
            "initial position honoured",
            None if math.hypot(e1["x"], e1["y"]) < 1.0 else f"e1 at {e1['x']},{e1['y']}",
        )

        reply = c.call(
            "ApplyActions",
            {
                "commands": [
                    {
                        "entity": "e1",
                        "command": "follow_route",
                        "args": {"route": "r1", "points": SETUP["routes"]["r1"], "speed": 5.0},
                        "role": "probe",
                        "instance": 0,
                        "behavior": "check",
                    },
                    {
                        "entity": "e2",
                        "command": "halt",
                        "args": {},
                        "role": "probe",
                        "instance": 1,
                        "behavior": "check",
                    },
                    {
                        "entity": "e2",
                        "command": "teleport_unknown",
                        "args": {},
                        "role": "probe",
                        "instance": 1,
                        "behavior": "check",
                    },
                ]
            },
        )
        problem = _ok(reply, "ApplyActions", c.seq)
        body = reply.get("body") or {}
        if problem is None:
            rejected = body.get("rejected") or []
            if [r.get("index") for r in rejected] != [2]:
                problem = f"expected only command index 2 rejected, got {rejected}"
        record("apply actions accepts known and rejects unknown commands", problem)

        last = 0.0
        monotonic = None
        obs = start
        for k in range(1, 11):
            reply = c.call("Step", {"dt": 1.0, "requested_time": float(k)})
            problem = _ok(reply, "Step", c.seq)
            if problem:
                monotonic = f"step {k}: {problem}"
                break
            body = reply["body"]
            actual = body.get("actual_sim_time")
            if not isinstance(actual, int | float) or actual + 1e-9 < k:
                monotonic = f"step {k}: actual_sim_time {actual} < requested {k}"
                break
            if actual + 1e-9 < last:
                monotonic = f"step {k}: time went backwards"
                break
            if body.get("requested_time") != float(k):
                monotonic = f"step {k}: requested_time not echoed"
                break
            obs_problems = _obs_problems(body.get("observation") or {}, ["e1", "e2"])
            if obs_problems:
                monotonic = f"step {k}: " + "; ".join(obs_problems)
                break
            if not isinstance(body.get("done"), bool):
                monotonic = f"step {k}: done must be boolean"
                break
            last = actual
            obs = body["observation"]
        record("ten steps: time reaches requested time and never goes back", monotonic)

        e1, e2 = obs["entities"]["e1"], obs["entities"]["e2"]
        record(
            "follow_route moves the entity along the route",
            None
            if e1["x"] > 10.0 and abs(e1["y"]) < 10.0
            else f"e1 at ({e1['x']:.1f}, {e1['y']:.1f})",
        )
        record(
            "halt keeps the entity in place",
            None
            if math.hypot(e2["x"], e2["y"] + 50.0) < 5.0
            else f"e2 at ({e2['x']:.1f}, {e2['y']:.1f})",
        )

        reply = c.call("RunUntil", {"time": 20.0})
        problem = _ok(reply, "RunUntil", c.seq)
        if problem is None and reply["body"].get("actual_sim_time", 0) + 1e-9 < 20.0:
            problem = f"actual_sim_time {reply['body'].get('actual_sim_time')}"
        record("run until reaches the target time", problem)

        record(
            "run until in the past is BAD_REQUEST",
            _error(c.call("RunUntil", {"time": 5.0}), "BAD_REQUEST"),
        )

        reply = c.call("GetState", {})
        if reply.get("ok"):
            got = reply["body"]["observation"]["time"]
            record(
                "get state reports current time",
                None if abs(got - last) < 1e-6 or got >= 20.0 - 1e-6 else f"time {got}",
            )
        else:
            record("get state reports current time", _error(reply, "UNSUPPORTED"))

        reply = c.call("Reset", {})
        problem = _ok(reply, "Reset", c.seq)
        if problem is None:
            obs = reply["body"]["observation"]
            e1 = obs["entities"]["e1"]
            if abs(obs["time"]) > 1e-6 or math.hypot(e1["x"], e1["y"]) > 1.0:
                problem = f"after reset time={obs['time']} e1=({e1['x']},{e1['y']})"
        record("reset restores the initial state", problem)

        reply = c.call("Terminate", {})
        record("terminate", _ok(reply, "Terminate", c.seq))
    except (OSError, ConnectionError, ValueError, KeyError) as exc:
        results.append(CheckResult("transport", False, f"{type(exc).__name__}: {exc}"))
    finally:
        c.close()
    return results
