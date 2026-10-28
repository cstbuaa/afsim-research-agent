"""Reference server for ``tbe-step/1``: serves any ``EngineAdapter`` over TCP.

It is the behavioural reference for a native engine host (e.g. a C++ bridge):
the same requests must produce the same kinds of responses and errors. One
client at a time; every request is handled to completion on the serving thread,
so the wrapped engine is only ever touched by that thread.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable
from typing import Any

from afsim_research_agent.ir.cosim.engine import EngineAdapter, ScenarioSetup
from afsim_research_agent.ir.cosim.protocol import (
    PROTOCOL,
    REQUEST_TYPES,
    ProtocolError,
    decode_command,
    decode_message,
    decode_setup,
    encode_observation,
    error_response,
    recv_frame,
    response,
    send_frame,
)

COMMANDS = (
    "follow_route",
    "move_to_position",
    "keep_station",
    "bound_to_position",
    "halt",
    "hold_overwatch_position",
    "set_speed",
    "set_heading",
    "turn_sensor_on",
    "turn_sensor_off",
    "send_message",
    "report_contact",
    "assume_formation",
)


class Session:
    def __init__(self, factory: Callable[[], EngineAdapter], engine_name: str) -> None:
        self.factory = factory
        self.engine_name = engine_name
        self.engine: EngineAdapter | None = None
        self.setup: ScenarioSetup | None = None
        self.greeted = False
        self.last_sequence = 0
        self.run_id: str | None = None
        self.time = 0.0
        self.terminated = False

    def handle(self, req: dict[str, Any]) -> dict[str, Any]:
        try:
            self._check_envelope(req)
            kind = req["type"]
            body = req.get("body") or {}
            return response(req, getattr(self, f"_on_{kind}")(body))
        except ProtocolError as exc:
            return error_response(req, exc.code, exc.message)
        except (KeyError, TypeError, ValueError) as exc:
            return error_response(req, "BAD_REQUEST", f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - report engine failures to the client
            return error_response(req, "ENGINE_ERROR", f"{type(exc).__name__}: {exc}")

    def _check_envelope(self, req: dict[str, Any]) -> None:
        if req.get("protocol") != PROTOCOL:
            raise ProtocolError("VERSION", f"expected protocol {PROTOCOL}")
        # Every request consumes its sequence_id, including rejected ones.
        seq = req.get("sequence_id")
        if not isinstance(seq, int) or seq <= self.last_sequence:
            raise ProtocolError("BAD_REQUEST", "sequence_id must increase strictly")
        if self.run_id is None:
            self.run_id = req.get("run_id")
        elif req.get("run_id") != self.run_id:
            raise ProtocolError("BAD_REQUEST", "run_id changed within a connection")
        self.last_sequence = seq
        if req.get("type") not in REQUEST_TYPES:
            raise ProtocolError("BAD_REQUEST", f"unknown request type {req.get('type')!r}")
        if req["type"] != "Hello" and not self.greeted:
            raise ProtocolError("BAD_STATE", "send Hello first")
        if self.terminated:
            raise ProtocolError("BAD_STATE", "session terminated")

    def _need_engine(self) -> EngineAdapter:
        if self.engine is None:
            raise ProtocolError("BAD_STATE", "no scenario loaded")
        return self.engine

    def _on_Hello(self, body: dict[str, Any]) -> dict[str, Any]:
        if PROTOCOL not in (body.get("versions") or [PROTOCOL]):
            raise ProtocolError("VERSION", f"server speaks {PROTOCOL}")
        self.greeted = True
        probe = self.factory()
        return {
            "engine": self.engine_name,
            "engine_version": "reference",
            "protocol": PROTOCOL,
            "messaging": getattr(probe, "messaging", "adapter"),
            "commands": list(COMMANDS),
            "events": ["spawn_hostile", "remove_hostile", "comm_degradation", "message_loss"],
            "step": {"modes": ["dt", "until"]},
        }

    def _load(self) -> dict[str, Any]:
        assert self.setup is not None
        if self.engine is not None:
            self.engine.close()
        self.engine = self.factory()
        obs = self.engine.reset(self.setup)
        self.time = obs.time
        return {"entities": sorted(obs.entities), "observation": encode_observation(obs)}

    def _on_LoadScenario(self, body: dict[str, Any]) -> dict[str, Any]:
        self.setup = decode_setup(body["setup"])
        return self._load()

    def _on_Reset(self, body: dict[str, Any]) -> dict[str, Any]:
        if self.setup is None:
            raise ProtocolError("BAD_STATE", "no scenario loaded")
        if "seed" in body:
            self.setup.random_seed = int(body["seed"])
        return {"observation": self._load()["observation"]}

    def _on_ApplyActions(self, body: dict[str, Any]) -> dict[str, Any]:
        engine = self._need_engine()
        commands = [decode_command(c) for c in body.get("commands") or []]
        rejected = [
            {"index": i, "reason": f"unsupported command '{c.command}'"}
            for i, c in enumerate(commands)
            if c.command not in COMMANDS
        ]
        engine.apply_commands([c for c in commands if c.command in COMMANDS])
        return {"accepted": len(commands) - len(rejected), "rejected": rejected}

    def _on_SendMessages(self, body: dict[str, Any]) -> dict[str, Any]:
        engine = self._need_engine()
        messages = [decode_message(m) for m in body.get("messages") or []]
        if getattr(engine, "messaging", "adapter") != "engine":
            raise ProtocolError("UNSUPPORTED", "engine does not model messaging")
        engine.send_messages(messages)
        return {"accepted": len(messages)}

    def _on_ApplyEvent(self, body: dict[str, Any]) -> dict[str, Any]:
        return {"supported": bool(self._need_engine().apply_event(dict(body["event"])))}

    def _step_result(self, requested: float) -> dict[str, Any]:
        engine = self._need_engine()
        obs = None
        while obs is None or obs.time + 1e-9 < requested:
            dt = requested - (obs.time if obs else self.time)
            if self.setup and dt > self.setup.time_step_s + 1e-9:
                dt = self.setup.time_step_s
            obs = engine.step(max(dt, 1e-9))
        self.time = obs.time
        done = self.setup is not None and obs.time >= self.setup.duration_s - 1e-9
        return {
            "requested_time": requested,
            "actual_sim_time": obs.time,
            "observation": encode_observation(obs),
            "events": [],
            "warnings": [],
            "done": done,
            "end_reason": "duration" if done else None,
        }

    def _on_Step(self, body: dict[str, Any]) -> dict[str, Any]:
        dt = float(body["dt"])
        if dt <= 0:
            raise ProtocolError("BAD_REQUEST", "dt must be positive")
        requested = float(body.get("requested_time", self.time + dt))
        if requested + 1e-9 < self.time:
            raise ProtocolError("BAD_REQUEST", "requested_time is in the past")
        return self._step_result(requested)

    def _on_RunUntil(self, body: dict[str, Any]) -> dict[str, Any]:
        target = float(body["time"])
        if target + 1e-9 < self.time:
            raise ProtocolError("BAD_REQUEST", "time is in the past")
        return self._step_result(target)

    def _on_GetState(self, body: dict[str, Any]) -> dict[str, Any]:
        engine = self._need_engine()
        observe = getattr(engine, "_observe", None)
        if observe is None:
            raise ProtocolError("UNSUPPORTED", "engine cannot report state without stepping")
        return {"observation": encode_observation(observe())}

    def _on_Terminate(self, body: dict[str, Any]) -> dict[str, Any]:
        if self.engine is not None:
            self.engine.close()
        self.engine = None
        self.terminated = True
        return {}


def serve(
    factory: Callable[[], EngineAdapter],
    host: str = "127.0.0.1",
    port: int = 47100,
    *,
    engine_name: str = "reference",
    ready: threading.Event | None = None,
    max_connections: int | None = None,
    bound: list[int] | None = None,
) -> None:
    """Serve clients one after another. ``bound`` receives the actual port (use port 0)."""
    with socket.create_server((host, port)) as server:
        if bound is not None:
            bound.append(server.getsockname()[1])
        if ready is not None:
            ready.set()
        served = 0
        while max_connections is None or served < max_connections:
            conn, _ = server.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            served += 1
            session = Session(factory, engine_name)
            with conn:
                while True:
                    try:
                        req = recv_frame(conn)
                    except (ConnectionError, OSError):
                        break
                    except ProtocolError as exc:
                        send_frame(conn, error_response({}, exc.code, exc.message))
                        break
                    send_frame(conn, session.handle(req))
                    if session.terminated:
                        break
