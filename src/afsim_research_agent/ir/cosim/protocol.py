"""Engine step protocol ``tbe-step/1``: framing, envelopes and codecs.

Transport: one TCP connection; every frame is a 4-byte big-endian unsigned
length followed by that many bytes of UTF-8 JSON. Requests and responses
alternate strictly (the client never pipelines), which keeps engine access on a
single thread on the server side.

Request  : {"protocol": "tbe-step/1", "run_id": str, "sequence_id": int,
            "type": <RequestType>, "body": {...}}
Response : {"protocol": "tbe-step/1", "run_id": str, "sequence_id": int (echo),
            "type": "<RequestType>Result" | "Error", "ok": bool,
            "body": {...}, "error": {"code": str, "message": str} | null}

Units are canonical: metres (local east-north frame), seconds, m/s, headings in
degrees clockwise from north. JSON cannot carry infinity: "no value" distances
are sent as null or omitted.
"""

from __future__ import annotations

import json
import math
import socket
import struct
from dataclasses import asdict
from typing import Any

from afsim_research_agent.ir.cosim.engine import (
    Command,
    EntityObservation,
    EntitySetup,
    Observation,
    OutgoingMessage,
    ScenarioSetup,
    TrackObservation,
)

PROTOCOL = "tbe-step/1"
MAX_FRAME_BYTES = 64 * 1024 * 1024
REQUEST_TYPES = (
    "Hello",
    "LoadScenario",
    "Reset",
    "ApplyActions",
    "SendMessages",
    "ApplyEvent",
    "Step",
    "RunUntil",
    "GetState",
    "Terminate",
)
ERROR_CODES = ("BAD_REQUEST", "BAD_STATE", "UNSUPPORTED", "VERSION", "ENGINE_ERROR")
END_REASONS = ("duration", "terminated", "engine")


class ProtocolError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- framing


def send_frame(sock: socket.socket, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    if len(data) > MAX_FRAME_BYTES:
        raise ProtocolError("BAD_REQUEST", "frame too large")
    sock.sendall(struct.pack(">I", len(data)) + data)


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = sock.recv(min(remaining, 1 << 20))
        if not chunk:
            raise ConnectionError("connection closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_frame(sock: socket.socket) -> dict[str, Any]:
    (size,) = struct.unpack(">I", _recv_exact(sock, 4))
    if size > MAX_FRAME_BYTES:
        raise ProtocolError("BAD_REQUEST", "frame too large")
    data = json.loads(_recv_exact(sock, size).decode("utf-8"))
    if not isinstance(data, dict):
        raise ProtocolError("BAD_REQUEST", "frame must be a JSON object")
    return data


def request(run_id: str, sequence_id: int, kind: str, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocol": PROTOCOL,
        "run_id": run_id,
        "sequence_id": sequence_id,
        "type": kind,
        "body": body,
    }


def response(req: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocol": PROTOCOL,
        "run_id": req.get("run_id", ""),
        "sequence_id": req.get("sequence_id", -1),
        "type": f"{req.get('type')}Result",
        "ok": True,
        "body": body,
        "error": None,
    }


def error_response(req: dict[str, Any], code: str, message: str) -> dict[str, Any]:
    return {
        "protocol": PROTOCOL,
        "run_id": req.get("run_id", ""),
        "sequence_id": req.get("sequence_id", -1),
        "type": "Error",
        "ok": False,
        "body": {},
        "error": {"code": code, "message": message},
    }


# --------------------------------------------------------------------------- codecs


def _finite(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_finite(v) for v in value]
    return value


def encode_setup(setup: ScenarioSetup) -> dict[str, Any]:
    return _finite(
        {
            "scenario": setup.scenario,
            "entities": [asdict(e) for e in setup.entities],
            "routes": {k: [list(p) for p in v] for k, v in setup.routes.items()},
            "waypoints": {k: list(v) for k, v in setup.waypoints.items()},
            "terrain": setup.terrain,
            "time_step_s": setup.time_step_s,
            "duration_s": setup.duration_s,
            "random_seed": setup.random_seed,
        }
    )


def decode_setup(data: dict[str, Any]) -> ScenarioSetup:
    return ScenarioSetup(
        scenario=data["scenario"],
        entities=[
            EntitySetup(
                engine_id=e["engine_id"],
                role=e["role"],
                instance=int(e["instance"]),
                capability_class=e["capability_class"],
                components=e.get("components") or {},
                position=(float(e["position"][0]), float(e["position"][1])),
                heading_deg=float(e["heading_deg"]),
                speed_mps=float(e["speed_mps"]),
            )
            for e in data["entities"]
        ],
        routes={
            k: [tuple(map(float, p)) for p in v] for k, v in (data.get("routes") or {}).items()
        },
        waypoints={k: tuple(map(float, v)) for k, v in (data.get("waypoints") or {}).items()},
        terrain=data.get("terrain"),
        time_step_s=float(data["time_step_s"]),
        duration_s=float(data["duration_s"]),
        random_seed=int(data.get("random_seed", 0)),
    )


def encode_observation(obs: Observation) -> dict[str, Any]:
    return _finite(
        {
            "time": obs.time,
            "entities": {eid: asdict(ent) for eid, ent in obs.entities.items()},
        }
    )


def decode_observation(data: dict[str, Any]) -> Observation:
    entities: dict[str, EntityObservation] = {}
    for eid, raw in (data.get("entities") or {}).items():
        terrain = dict(raw.get("terrain") or {})
        if "distance_to" in terrain:
            terrain["distance_to"] = {
                k: (math.inf if v is None else float(v)) for k, v in terrain["distance_to"].items()
            }
        entities[eid] = EntityObservation(
            x=float(raw["x"]),
            y=float(raw["y"]),
            heading_deg=float(raw["heading_deg"]),
            speed_mps=float(raw["speed_mps"]),
            alive=bool(raw.get("alive", True)),
            damaged=bool(raw.get("damaged", False)),
            altitude_m=float(raw.get("altitude_m", 0.0)),
            fuel_remaining=float(raw.get("fuel_remaining", 1.0)),
            mover=dict(raw.get("mover") or {}),
            components={k: dict(v) for k, v in (raw.get("components") or {}).items()},
            tracks=[
                TrackObservation(
                    id=str(t["id"]),
                    x=float(t["x"]),
                    y=float(t["y"]),
                    hostile=bool(t.get("hostile", True)),
                    range_m=None if t.get("range_m") is None else float(t["range_m"]),
                )
                for t in raw.get("tracks") or []
            ],
            terrain=terrain,
            inbox=list(raw.get("inbox") or []),
        )
    return Observation(time=float(data["time"]), entities=entities)


def encode_command(cmd: Command) -> dict[str, Any]:
    return _finite(asdict(cmd))


def decode_command(data: dict[str, Any]) -> Command:
    return Command(
        entity=data["entity"],
        command=data["command"],
        args=dict(data.get("args") or {}),
        role=data.get("role", ""),
        instance=int(data.get("instance", 0)),
        behavior=data.get("behavior", ""),
    )


def encode_message(msg: OutgoingMessage) -> dict[str, Any]:
    return _finite(asdict(msg))


def decode_message(data: dict[str, Any]) -> OutgoingMessage:
    return OutgoingMessage(
        sender=data["sender"],
        recipients=list(data["recipients"]),
        message=data["message"],
        fields=dict(data.get("fields") or {}),
        latency_s=float(data.get("latency_s", 0.0)),
        reliability=float(data.get("reliability", 1.0)),
    )
