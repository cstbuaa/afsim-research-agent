"""Contract between the co-simulation runner and an external simulation engine.

An engine adapter is one Python class (see ``EngineAdapter``). Everything that
crosses the boundary uses canonical units: metres, seconds, m/s, and headings
in degrees clockwise from north with +x east and +y north. The adapter is
responsible for converting to and from the engine's own conventions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass
class TrackObservation:
    """One detected object as seen by an entity's own sensors."""

    id: str
    x: float
    y: float
    hostile: bool = True
    range_m: float | None = None  # computed from positions when omitted


@dataclass
class EntityObservation:
    x: float
    y: float
    heading_deg: float
    speed_mps: float
    alive: bool = True
    damaged: bool = False
    altitude_m: float = 0.0
    fuel_remaining: float = 1.0
    # Mover flags for the most recent movement command:
    # available, at_destination, route_complete, moving
    mover: dict[str, Any] = field(default_factory=dict)
    # Per logical component id (as in behavior_spec.yaml), e.g.
    # {"vehicle_sight": {"available": True, "on": True}, "platoon_net": {"available": True}}
    components: dict[str, dict[str, Any]] = field(default_factory=dict)
    tracks: list[TrackObservation] = field(default_factory=list)
    # {"class": "open", "distance_to": {"urban": 670.0}, "slope_deg": 0.0,
    #  "line_of_sight_to_nearest_hostile": False}
    terrain: dict[str, Any] = field(default_factory=dict)
    # Only used when the engine delivers messages (messaging == "engine"):
    # [{"message": id, "fields": {...}, "sender": engine_entity_id}]
    inbox: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Observation:
    time: float
    entities: dict[str, EntityObservation]


@dataclass
class EntitySetup:
    engine_id: str
    role: str
    instance: int
    capability_class: str
    components: dict[str, Any]  # resolved bindings for this engine (component, settings)
    position: tuple[float, float]
    heading_deg: float
    speed_mps: float


@dataclass
class ScenarioSetup:
    scenario: dict[str, Any]
    entities: list[EntitySetup]
    routes: dict[str, list[tuple[float, float]]]
    waypoints: dict[str, tuple[float, float]]
    terrain: dict[str, Any] | None
    time_step_s: float
    duration_s: float
    random_seed: int


@dataclass
class Command:
    """A neutral action from the action catalog, with arguments resolved to numbers.

    Besides the catalog arguments the runner adds engine-ready fields, e.g.
    ``keep_station`` gets ``leader_entity``, ``forward_m``, ``right_m``;
    ``follow_route`` gets ``points``; ``move_to_position`` gets ``point``.
    """

    entity: str
    command: str
    args: dict[str, Any]
    role: str
    instance: int
    behavior: str


@dataclass
class OutgoingMessage:
    sender: str
    recipients: list[str]
    message: str
    fields: dict[str, Any]
    latency_s: float
    reliability: float


class EngineAdapter(Protocol):
    """Implement this for the lab engine (an in-process Python API, a C++ plugin
    wrapped with pybind11, a socket client or a file exchange all fit)."""

    #: "adapter": the runner delivers messages (latency, reliability, comm events);
    #: "engine": the engine's own comms model delivers them via ``inbox``.
    messaging: Literal["adapter", "engine"]

    def reset(self, setup: ScenarioSetup) -> Observation:
        """Create the entities and return the observation at t=0."""
        ...

    def apply_commands(self, commands: list[Command]) -> None:
        """Hand this tick's commands to the engine (they act from the next step)."""
        ...

    def send_messages(self, messages: list[OutgoingMessage]) -> None:
        """Only called when ``messaging == "engine"``."""
        ...

    def apply_event(self, event: dict[str, Any]) -> bool:
        """Scenario events such as spawn_hostile. Return False if unsupported."""
        ...

    def step(self, dt: float) -> Observation:
        """Advance the engine by ``dt`` seconds and return the new observation."""
        ...

    def close(self) -> None: ...
