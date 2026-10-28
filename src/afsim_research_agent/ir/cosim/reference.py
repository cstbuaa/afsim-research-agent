"""Reference engine: the built-in 2D physics behind the ``EngineAdapter`` contract.

It serves two purposes: a worked example of an adapter, and a conformance
check — a co-simulation against this engine must reproduce the built-in L3
results, which proves the runner, the observation mapping and the command
resolution before a real lab engine is connected.
"""

from __future__ import annotations

import math
from typing import Any, Literal

from afsim_research_agent.ir.cosim.engine import (
    Command,
    EntityObservation,
    Observation,
    OutgoingMessage,
    ScenarioSetup,
    TrackObservation,
)
from afsim_research_agent.ir.runtime import Delivery
from afsim_research_agent.ir.sim.world import Hostile, Pending, SimWorld, _q


class ReferenceEngine:
    def __init__(
        self,
        ir: dict[str, Any],
        context: dict[str, Any],
        *,
        messaging: Literal["adapter", "engine"] = "adapter",
    ) -> None:
        self.ir = ir
        self.context = context
        self.messaging = messaging
        self.world: SimWorld | None = None
        self.ids: dict[str, Any] = {}
        self.terrain_classes: list[str] = []

    # -- contract ---------------------------------------------------------
    def reset(self, setup: ScenarioSetup) -> Observation:
        world = SimWorld(self.ir, self.context, setup.scenario)
        world.dt = setup.time_step_s
        for spec in setup.entities:
            ent = next(
                e for e in world.entities if e.role == spec.role and e.instance == spec.instance
            )
            ent.x, ent.y = spec.position
            ent.heading = ent.target_heading = spec.heading_deg
            ent.speed = ent.target_speed = spec.speed_mps
            self.ids[spec.engine_id] = ent
        self.world = world
        self.terrain_classes = sorted(
            {r.cls for r in world.terrain.regions} | {world.terrain.default_class}
        )
        return self._observe()

    def apply_commands(self, commands: list[Command]) -> None:
        assert self.world is not None
        for cmd in commands:
            self.world._apply(self.ids[cmd.entity], cmd.command, cmd.args)

    def send_messages(self, messages: list[OutgoingMessage]) -> None:
        assert self.world is not None
        w = self.world
        for msg in messages:
            sender = self.ids[msg.sender]
            loss = max(w._loss(msg.message), 1.0 - msg.reliability)
            for rid in msg.recipients:
                if loss > 0 and w.rng.random() < loss:
                    continue
                w.queue.append(
                    Pending(
                        w.now + msg.latency_s,
                        w.now,
                        rid,
                        Delivery(msg.message, dict(msg.fields), sender.role, sender.instance),
                    )
                )

    def apply_event(self, event: dict[str, Any]) -> bool:
        assert self.world is not None
        kind = event["type"]
        if kind == "spawn_hostile":
            x, y = event["position"]
            self.world.hostiles.append(
                Hostile(event.get("target_id") or event["id"], float(x), float(y))
            )
            return True
        if kind == "remove_hostile":
            for h in self.world.hostiles:
                if h.id == event.get("target_id"):
                    h.alive = False
            return True
        if kind in ("comm_degradation", "message_loss"):
            now = self.world.now
            only = event.get("message") if kind == "message_loss" else None
            self.world.comm_loss.append(
                (now, now + _q(event.get("duration")), float(event.get("loss", 1.0)), only)
            )
            return True
        return False

    def step(self, dt: float) -> Observation:
        assert self.world is not None
        w = self.world
        for ent in w.entities:
            w._move(ent)
            ent.fuel = max(0.0, ent.fuel - ent.fuel_burn * dt)
        w.now += dt
        w.tick_index += 1
        return self._observe()

    def close(self) -> None:
        pass

    # -- observation --------------------------------------------------------
    def _observe(self) -> Observation:
        assert self.world is not None
        w = self.world
        out: dict[str, EntityObservation] = {}
        for engine_id, ent in self.ids.items():
            reach = max(
                (r for s, r in ent.sensor_ranges.items() if ent.sensors_on.get(s)), default=0.0
            )
            tracks = [
                TrackObservation(h.id, h.x, h.y, True, math.hypot(h.x - ent.x, h.y - ent.y))
                for h in w.hostiles
                if h.alive and math.hypot(h.x - ent.x, h.y - ent.y) <= reach
            ]
            components: dict[str, dict[str, Any]] = {}
            for sid, rng in ent.sensor_ranges.items():
                components[sid] = {"available": True, "on": ent.sensors_on.get(sid, False)}
            for wid, qty in ent.weapons.items():
                components[wid] = {
                    "available": qty > 0,
                    "ready": qty > 0,
                    "quantity_remaining": qty,
                }
            for comm in self.ir_role(ent.role)["dependencies"]["comms"]:
                components[comm["id"]] = {"available": ent.has_comms}
            inbox: list[dict[str, Any]] = []
            if self.messaging == "engine":
                due = [
                    p
                    for p in w.queue
                    if p.recipient == engine_id
                    and p.deliver_at <= w.now + 1e-9
                    and p.sent_at < w.now
                ]
                w.queue = [p for p in w.queue if p not in due]
                sender_ids = {(e.role, e.instance): k for k, e in self.ids.items()}
                inbox = [
                    {
                        "message": p.delivery.message,
                        "fields": p.delivery.fields,
                        "sender": sender_ids.get(
                            (p.delivery.sender_role, p.delivery.sender_instance), ""
                        ),
                    }
                    for p in due
                ]
            out[engine_id] = EntityObservation(
                x=ent.x,
                y=ent.y,
                heading_deg=ent.heading,
                speed_mps=ent.speed,
                alive=ent.alive,
                fuel_remaining=ent.fuel,
                mover={
                    "available": ent.alive,
                    "at_destination": ent.at_destination,
                    "route_complete": ent.route_complete,
                    "moving": ent.speed > 0.1,
                },
                components=components,
                tracks=tracks,
                terrain={
                    "class": w.terrain.class_at(ent.x, ent.y),
                    "distance_to": {
                        c: w.terrain.distance_to_class(ent.x, ent.y, c)
                        for c in self.terrain_classes
                    },
                },
                inbox=inbox,
            )
        return Observation(time=w.now, entities=out)

    def ir_role(self, role_id: str) -> dict[str, Any]:
        return next(r for r in self.ir["roles"] if r["id"] == role_id)
