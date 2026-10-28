"""Client side of ``tbe-step/1``: an ``EngineAdapter`` that drives a remote engine host."""

from __future__ import annotations

import socket
import uuid
from typing import Any, Literal

from afsim_research_agent.ir.cosim.engine import (
    Command,
    Observation,
    OutgoingMessage,
    ScenarioSetup,
)
from afsim_research_agent.ir.cosim.protocol import (
    PROTOCOL,
    ProtocolError,
    decode_observation,
    encode_command,
    encode_message,
    encode_setup,
    recv_frame,
    request,
    send_frame,
)


class RemoteEngineAdapter:
    """Connects to an engine host (e.g. a C++ bridge on the simulation machine)."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout_s: float = 60.0,
        client: str = "afsim-research-agent",
        strict_commands: bool = True,
    ) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout_s)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.run_id = uuid.uuid4().hex
        self.sequence_id = 0
        self.strict_commands = strict_commands
        self.warnings: list[str] = []
        self.last_time = 0.0
        hello = self.call("Hello", {"client": client, "versions": [PROTOCOL]})
        self.engine_info = hello
        self.messaging: Literal["adapter", "engine"] = hello.get("messaging", "adapter")
        self.supported_commands = set(hello.get("commands") or [])
        self.supported_events = set(hello.get("events") or [])

    # -- transport --------------------------------------------------------------------
    def call(self, kind: str, body: dict[str, Any]) -> dict[str, Any]:
        self.sequence_id += 1
        send_frame(self.sock, request(self.run_id, self.sequence_id, kind, body))
        reply = recv_frame(self.sock)
        if reply.get("protocol") != PROTOCOL:
            raise ProtocolError("VERSION", f"unexpected protocol {reply.get('protocol')!r}")
        if reply.get("sequence_id") != self.sequence_id or reply.get("run_id") != self.run_id:
            raise ProtocolError(
                "BAD_REQUEST", "response does not match the request (sequence/run id)"
            )
        if not reply.get("ok"):
            err = reply.get("error") or {}
            raise ProtocolError(
                err.get("code", "ENGINE_ERROR"), err.get("message", "unknown error")
            )
        if reply.get("type") != f"{kind}Result":
            raise ProtocolError("BAD_REQUEST", f"expected {kind}Result, got {reply.get('type')}")
        return reply.get("body") or {}

    # -- EngineAdapter -------------------------------------------------------------
    def reset(self, setup: ScenarioSetup) -> Observation:
        body = self.call("LoadScenario", {"setup": encode_setup(setup)})
        obs = decode_observation(body["observation"])
        self.last_time = obs.time
        return obs

    def apply_commands(self, commands: list[Command]) -> None:
        if not commands:
            return
        body = self.call("ApplyActions", {"commands": [encode_command(c) for c in commands]})
        rejected = body.get("rejected") or []
        if rejected:
            text = "; ".join(
                f"{commands[r['index']].command}: {r.get('reason', '')}" for r in rejected
            )
            if self.strict_commands:
                raise ProtocolError("UNSUPPORTED", f"engine rejected commands: {text}")
            self.warnings.append(text)

    def send_messages(self, messages: list[OutgoingMessage]) -> None:
        self.call("SendMessages", {"messages": [encode_message(m) for m in messages]})

    def apply_event(self, event: dict[str, Any]) -> bool:
        if self.supported_events and event.get("type") not in self.supported_events:
            return False
        return bool(self.call("ApplyEvent", {"event": event}).get("supported", False))

    def step(self, dt: float) -> Observation:
        requested = self.last_time + dt
        body = self.call("Step", {"dt": dt, "requested_time": requested})
        obs = decode_observation(body["observation"])
        actual = float(body.get("actual_sim_time", obs.time))
        if actual + 1e-9 < requested:
            self.warnings.append(f"engine stopped at {actual} before requested {requested}")
        for warning in body.get("warnings") or []:
            self.warnings.append(str(warning))
        self.last_time = actual
        return obs

    def close(self) -> None:
        try:
            self.call("Terminate", {})
        except (OSError, ProtocolError):
            pass
        finally:
            self.sock.close()
