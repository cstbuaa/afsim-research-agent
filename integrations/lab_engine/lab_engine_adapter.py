"""Template: adapter between the co-simulation runner and the lab engine.

Copy this file, fill in every ``TODO(lab)``, then run:

    python -m afsim_research_agent.ir cosim <package> \\
        --engine integrations/lab_engine/lab_engine_adapter.py:make_engine \\
        --engine-name lab_engine --trace-dir runs/

Contract (see afsim_research_agent/ir/cosim/engine.py):
* Everything we exchange is in canonical units: metres in a local east-north
  frame, seconds, m/s, heading in degrees clockwise from north.
* ``reset`` creates the entities and returns the t=0 observation.
* Each step the runner calls ``apply_commands`` (and ``send_messages`` when the
  engine models comms), then ``step(dt)``, which must return the observation
  after advancing exactly ``dt`` seconds.
* Logical component ids in observations (``components``) are the ids used in
  behavior_spec.yaml (e.g. ``vehicle_sight``, ``platoon_net``); the mapping to
  engine components comes from ``bindings/lab_engine.json`` via ``setup``.

Verify your adapter with the reference scenarios first: results should be close
to ``python -m afsim_research_agent.ir cosim <package> --engine reference``;
differences should be explainable by the lab engine's more detailed physics.
"""

from __future__ import annotations

from typing import Any

from afsim_research_agent.ir.cosim import (
    Command,
    EntityObservation,
    Observation,
    OutgoingMessage,
    ScenarioSetup,
    TrackObservation,
)
from afsim_research_agent.ir.cosim.convert import (
    LocalFrame,
    heading_from_engine,
    heading_to_engine,
    speed_from_engine,
    speed_to_engine,
)

# TODO(lab): set the engine's conventions.
HEADING_CONVENTION = "north_cw_deg"  # north_cw_deg | north_cw_rad | east_ccw_deg | east_ccw_rad
SPEED_UNIT = "m/s"  # m/s | km/h | knots | mph
GEO_ORIGIN: tuple[float, float] | None = None  # (lat0, lon0) if the engine uses lat/lon


class LabEngineAdapter:
    #: "engine" if the lab engine's comms model should deliver messages
    #: (realistic latency/loss), "adapter" to let the runner deliver them.
    messaging = "engine"

    def __init__(
        self, ir: dict[str, Any], context: dict[str, Any], options: dict[str, Any]
    ) -> None:
        self.ir = ir
        self.context = context
        self.frame = LocalFrame(*GEO_ORIGIN) if GEO_ORIGIN else None
        self.entity_ids: list[str] = []
        self.api: Any = None
        # TODO(lab): connect to the engine, e.g.
        #   import lab_engine
        #   self.api = lab_engine.Session(config_path=..., step_mode="external")

    # -- frame helpers ----------------------------------------------------------
    def _to_local(self, a: float, b: float) -> tuple[float, float]:
        return self.frame.to_local(a, b) if self.frame else (a, b)

    def _to_engine(self, x: float, y: float) -> tuple[float, float]:
        return self.frame.to_geodetic(x, y) if self.frame else (x, y)

    # -- contract ------------------------------------------------------------------
    def reset(self, setup: ScenarioSetup) -> Observation:
        # TODO(lab): load terrain matching setup.terrain (or an equivalent engine map),
        # register routes/waypoints if the engine has native route objects.
        for ent in setup.entities:
            self.entity_ids.append(ent.engine_id)
            x, y = self._to_engine(*ent.position)
            # ent.components: logical id -> {"component": <engine component name>, "settings": {...}}
            # TODO(lab): create the platform, e.g.
            #   self.api.create_entity(ent.engine_id, template=..., x=x, y=y,
            #       heading=heading_to_engine(ent.heading_deg, HEADING_CONVENTION),
            #       speed=speed_to_engine(ent.speed_mps, SPEED_UNIT))
            _ = (x, y)
        return self._observe()

    def apply_commands(self, commands: list[Command]) -> None:
        for cmd in commands:
            handler = getattr(self, f"_cmd_{cmd.command}", None)
            if handler is None:
                raise NotImplementedError(f"lab adapter has no mapping for '{cmd.command}'")
            handler(cmd)

    def send_messages(self, messages: list[OutgoingMessage]) -> None:
        for msg in messages:
            # TODO(lab): hand to the engine's comms model; it must later appear in the
            # recipients' EntityObservation.inbox as {"message", "fields", "sender"}.
            _ = msg

    def apply_event(self, event: dict[str, Any]) -> bool:
        if event["type"] == "spawn_hostile":
            x, y = self._to_engine(*event["position"])
            # TODO(lab): self.api.create_entity(event.get("target_id") or event["id"], side="red", x=x, y=y)
            _ = (x, y)
            return False  # return True once implemented
        return False  # comm events: only needed when messaging == "engine"

    def step(self, dt: float) -> Observation:
        # TODO(lab): self.api.step(dt)
        return self._observe()

    def close(self) -> None:
        # TODO(lab): self.api.close()
        pass

    # -- observation -------------------------------------------------------------------
    def _observe(self) -> Observation:
        entities: dict[str, EntityObservation] = {}
        now = 0.0  # TODO(lab): self.api.time()
        for engine_id in self.entity_ids:
            # TODO(lab): read the entity state, e.g. s = self.api.state(engine_id)
            s: dict[str, Any] = {}
            x, y = self._to_local(s.get("x", 0.0), s.get("y", 0.0))
            entities[engine_id] = EntityObservation(
                x=x,
                y=y,
                heading_deg=heading_from_engine(s.get("heading", 0.0), HEADING_CONVENTION),
                speed_mps=speed_from_engine(s.get("speed", 0.0), SPEED_UNIT),
                alive=s.get("alive", True),
                mover={
                    # flags for the latest movement command issued to this entity
                    "available": s.get("mobility_ok", True),
                    "at_destination": s.get("arrived", False),
                    "route_complete": s.get("route_done", False),
                },
                components={
                    # logical ids from behavior_spec.yaml -> state
                    # "vehicle_sight": {"available": True, "on": True},
                    # "platoon_net": {"available": True},
                },
                tracks=[
                    TrackObservation(
                        t["id"], *self._to_local(t["x"], t["y"]), hostile=t.get("hostile", True)
                    )
                    for t in s.get("tracks", [])
                ],
                terrain={
                    # "class": self.api.terrain_class(x, y),
                    # "distance_to": {"urban": self.api.distance_to_class(x, y, "urban")},
                },
                inbox=s.get("inbox", []),
            )
        return Observation(time=now, entities=entities)

    # -- command mappings (one per neutral action the models use) ----------------
    def _cmd_follow_route(self, cmd: Command) -> None:
        points = [self._to_engine(*p) for p in cmd.args["points"]]
        speed = cmd.args.get("speed")
        # TODO(lab): self.api.follow_route(cmd.entity, points, speed_to_engine(speed, SPEED_UNIT) if speed else None)
        _ = (points, speed)

    def _cmd_keep_station(self, cmd: Command) -> None:
        # Station relative to the leader: forward_m ahead (+) / behind (-), right_m right (+) / left (-).
        # TODO(lab): native formation keeping, or set heading/speed each tick toward the slot.
        _ = (cmd.args["leader_entity"], cmd.args.get("forward_m"), cmd.args.get("right_m"))

    def _cmd_bound_to_position(self, cmd: Command) -> None:
        # Move cmd.args["distance"] metres along the current heading, then stop.
        pass  # TODO(lab)

    def _cmd_halt(self, cmd: Command) -> None:
        pass  # TODO(lab): self.api.stop(cmd.entity)

    _cmd_hold_overwatch_position = _cmd_halt

    def _cmd_report_contact(self, cmd: Command) -> None:
        pass  # the message itself is delivered via send_messages / the runner

    def _cmd_send_message(self, cmd: Command) -> None:
        pass  # same as above


def make_engine(
    ir: dict[str, Any], context: dict[str, Any], options: dict[str, Any]
) -> LabEngineAdapter:
    return LabEngineAdapter(ir, context, options)


__all__ = ["LabEngineAdapter", "make_engine", "heading_to_engine", "speed_to_engine"]
