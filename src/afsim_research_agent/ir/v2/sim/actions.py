"""Generic action models of the IR 2.0 simulation.

A capability set says which model executes each of its actions in the alignment file
(``[sim."<action id>"] model = "<name>"`` plus options), and which blackboard fields the
models' generic keys are (``[sim_fields]``). Completion criteria come from the action
contract (``success``: consecutive samples, tolerances), so the models stay generic and
the repository holds no platform names.

A model returns ``(status, failure_code)`` with status RUNNING, SUCCESS or FAILURE.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from afsim_research_agent.ir.v2.sim.world import (
    Vehicle,
    World,
    bearing_deg,
    heading_error,
)

RUNNING, SUCCESS, FAILURE = "running", "success", "failure"
Status = tuple[str, str | None]


@dataclass
class ActionContext:
    world: World
    vehicle: Vehicle
    blackboard: dict[str, Any]
    fields: dict[str, str]  # generic key → blackboard field
    params: dict[str, Any]  # resolved, in contract units
    success: dict[str, Any]  # contract completion criteria
    options: dict[str, Any]
    shared: dict[str, Any]  # per vehicle, across actions (e.g. the track snapshot)
    memory: dict[str, Any] = field(default_factory=dict)  # per activation
    # capability set 1.1: the fields the contract says the action writes (None: not stated);
    # a model writing any other field is not what the platform does — the write is dropped
    allowed: set[str] | None = None
    dropped: set[str] = field(default_factory=set)

    @property
    def now(self) -> float:
        return self.world.now

    def write(self, key: str, value: Any) -> None:
        path = self.fields.get(key, f"sim.{key}")
        if (
            self.allowed is not None
            and path not in self.allowed
            and not path.startswith("sim.")
            and not key.startswith("exec.")
        ):
            self.dropped.add(path)
            return
        self.blackboard[path] = value

    def read(self, key: str, default: Any = None) -> Any:
        return self.blackboard.get(self.fields.get(key, f"sim.{key}"), default)

    def put(self, path: str, value: Any) -> None:
        """Write a capability-set field by its own path (team models; ``[sim_fields]`` can
        still redirect it)."""
        self.write(path, value) if path in self.fields else self._raw(path, value)

    def get(self, path: str, default: Any = None) -> Any:
        return self.blackboard.get(self.fields.get(path, path), default)

    def _raw(self, path: str, value: Any) -> None:
        if self.allowed is not None and path not in self.allowed:
            self.dropped.add(path)
            return
        self.blackboard[path] = value

    def param(self, option: str, default_name: str, default: Any = None) -> Any:
        name = self.options.get(option, default_name)
        return self.params.get(name, default)

    def samples(self, ok: bool) -> bool:
        """Count consecutive samples meeting the criterion (contract ``consecutive_samples``)."""
        need = int(self.success.get("consecutive_samples", 1) or 1)
        self.memory["ok"] = self.memory.get("ok", 0) + 1 if ok else 0
        return self.memory["ok"] >= need


class ActionModel:
    def start(self, ctx: ActionContext) -> Status:
        return self.update(ctx)

    def update(self, ctx: ActionContext) -> Status:  # pragma: no cover - interface
        raise NotImplementedError

    def cancel(self, ctx: ActionContext) -> None:
        return None


# ---------------------------------------------------------------------- movement
def _mobility(ctx: ActionContext) -> Status | None:
    if not ctx.vehicle.mobility_available:
        return FAILURE, "MOBILITY_UNAVAILABLE"
    return None


class _Mover(ActionModel):
    def cancel(self, ctx: ActionContext) -> None:
        ctx.vehicle.cmd_speed = 0.0


class MoveToPoint(_Mover):
    def start(self, ctx: ActionContext) -> Status:
        lat = ctx.param("latitude_param", "latitude_deg")
        lon = ctx.param("longitude_param", "longitude_deg")
        if not isinstance(lat, int | float) or not isinstance(lon, int | float):
            return FAILURE, "INVALID_PARAMETER"
        ctx.memory["target"] = ctx.world.geo.to_xy(float(lat), float(lon))
        return self.update(ctx)

    def update(self, ctx: ActionContext) -> Status:
        if (bad := _mobility(ctx)) is not None:
            return bad
        tol = float(ctx.param("tolerance_param", "arrival_tolerance_m", 5.0) or 5.0)
        speed = float(ctx.param("speed_param", "target_speed_mps", 8.0) or 0.0)
        tx, ty = ctx.memory["target"]
        v = ctx.vehicle
        dx, dy = tx - v.x, ty - v.y
        rng = math.hypot(dx, dy)
        ctx.write("move.range_to_target", rng)
        ctx.write("move.bearing_to_target", bearing_deg(dx, dy))
        if rng <= tol:
            v.cmd_speed = 0.0
        else:
            v.cmd_heading = bearing_deg(dx, dy)
            # slow down so that the vehicle can stop inside the tolerance
            v.cmd_speed = min(speed, math.sqrt(max(0.0, 2 * v.accel * (rng - tol / 2))))
        stopped = v.speed <= float(ctx.success.get("maximum_speed_mps", 0.1))
        return (SUCCESS, None) if ctx.samples(rng <= tol and stopped) else (RUNNING, None)


class FollowRoute(_Mover):
    def start(self, ctx: ActionContext) -> Status:
        name = ctx.param("route_param", "route_name")
        route = ctx.world.routes.get(str(name)) if name else None
        if not route:
            return FAILURE, "ROUTE_NOT_FOUND"
        ctx.memory["route"] = list(route)
        ctx.memory["index"] = 0
        return self.update(ctx)

    def update(self, ctx: ActionContext) -> Status:
        if (bad := _mobility(ctx)) is not None:
            return bad
        v = ctx.vehicle
        route = ctx.memory["route"]
        tol = float(ctx.options.get("waypoint_tolerance_m", 10.0))
        speed = float(ctx.options.get("speed_mps", 8.0))
        while ctx.memory["index"] < len(route) - 1:
            wx, wy = route[ctx.memory["index"]]
            if math.hypot(wx - v.x, wy - v.y) > tol:
                break
            ctx.memory["index"] += 1
        wx, wy = route[ctx.memory["index"]]
        dx, dy = wx - v.x, wy - v.y
        rng = math.hypot(dx, dy)
        final = ctx.memory["index"] == len(route) - 1
        if final and rng <= tol:
            v.cmd_speed = 0.0
        else:
            v.cmd_heading = bearing_deg(dx, dy)
            v.cmd_speed = speed if not final else min(speed, math.sqrt(2 * v.accel * rng))
        stopped = v.speed <= float(ctx.success.get("maximum_speed_mps", 0.1))
        done = final and rng <= tol and stopped
        return (SUCCESS, None) if ctx.samples(done) else (RUNNING, None)


class HoldPosition(_Mover):
    def start(self, ctx: ActionContext) -> Status:
        lat = ctx.param("latitude_param", "latitude_deg")
        lon = ctx.param("longitude_param", "longitude_deg")
        if isinstance(lat, int | float) and isinstance(lon, int | float):
            ctx.memory["target"] = ctx.world.geo.to_xy(float(lat), float(lon))
        else:
            ctx.memory["target"] = (ctx.vehicle.x, ctx.vehicle.y)
        return self.update(ctx)

    def update(self, ctx: ActionContext) -> Status:
        if (bad := _mobility(ctx)) is not None:
            return bad
        tol = float(ctx.param("tolerance_param", "arrival_tolerance_m", 3.0) or 3.0)
        tx, ty = ctx.memory["target"]
        v = ctx.vehicle
        dx, dy = tx - v.x, ty - v.y
        rng = math.hypot(dx, dy)
        if rng <= tol:
            v.cmd_speed = 0.0
        else:
            v.cmd_heading = bearing_deg(dx, dy)
            v.cmd_speed = min(4.0, math.sqrt(2 * v.accel * rng))
        return RUNNING, None  # persistent: ends only by cancellation or failure


class SetSpeed(_Mover):
    def update(self, ctx: ActionContext) -> Status:
        if (bad := _mobility(ctx)) is not None:
            return bad
        target = ctx.param("speed_param", "target_speed_mps")
        if not isinstance(target, int | float) or target < 0:
            return FAILURE, "INVALID_PARAMETER"
        ctx.vehicle.cmd_speed = float(target)
        tol = float(ctx.success.get("speed_error_tolerance_mps", 0.25))
        ok = abs(ctx.vehicle.speed - float(target)) <= tol
        return (SUCCESS, None) if ctx.samples(ok) else (RUNNING, None)


class Stop(_Mover):
    def update(self, ctx: ActionContext) -> Status:
        if (bad := _mobility(ctx)) is not None:
            return bad
        ctx.vehicle.cmd_speed = 0.0
        ok = ctx.vehicle.speed <= float(ctx.success.get("maximum_speed_mps", 0.1))
        return (SUCCESS, None) if ctx.samples(ok) else (RUNNING, None)


class TurnToHeading(_Mover):
    def update(self, ctx: ActionContext) -> Status:
        if (bad := _mobility(ctx)) is not None:
            return bad
        target = ctx.param("heading_param", "target_heading_deg_true")
        if not isinstance(target, int | float):
            return FAILURE, "INVALID_PARAMETER"
        ctx.vehicle.cmd_heading = float(target) % 360.0
        tol = float(ctx.success.get("smallest_heading_error_tolerance_deg", 2.0))
        ok = abs(heading_error(float(target), ctx.vehicle.heading)) <= tol
        return (SUCCESS, None) if ctx.samples(ok) else (RUNNING, None)


# ---------------------------------------------------------------------- perception
def _sensor(ctx: ActionContext):
    s = ctx.vehicle.sensor
    return s if s is not None and s.exists else None


class SensorSwitch(ActionModel):
    def __init__(self, on: bool) -> None:
        self.on = on

    def update(self, ctx: ActionContext) -> Status:
        sensor = _sensor(ctx)
        if sensor is None or not sensor.operational:
            return FAILURE, "SENSOR_UNAVAILABLE"
        sensor.commanded_on = self.on
        if self.on and ctx.read("search.started_at") is None:
            ctx.write("search.started_at", ctx.now)
        return (SUCCESS, None) if sensor.on == self.on else (RUNNING, None)


class UpdateTracks(ActionModel):
    """Refresh the local track picture. Tracks persist after the last detection until
    they are older than ``track_timeout_s`` (option, default 2.5 s), as track lists do.
    As on the platform, the step succeeds without a working sensor: it then gets no new
    detections and the tracks it holds age out, so a selected target is lost a few
    seconds after the sensor fails (the target step reports that, not this one)."""

    def update(self, ctx: ActionContext) -> Status:
        sensor = _sensor(ctx)
        timeout = float(ctx.options.get("track_timeout_s", 2.5))
        memory = ctx.shared.setdefault("track_memory", {})
        if sensor is not None and sensor.operational:
            for t in ctx.world.visible(ctx.vehicle):
                memory[t["id"]] = (t, ctx.now)
        tracks = {tid: t for tid, (t, seen) in memory.items() if ctx.now - seen <= timeout}
        ctx.shared["tracks"] = tracks
        ctx.shared["snapshot_time"] = ctx.now
        enemies = [t for t in tracks.values() if t["affiliation"] == "ENEMY"]
        ctx.write("tracks.snapshot_time", ctx.now)
        ctx.write("tracks.raw_count", len(tracks))
        ctx.write("tracks.enemy_count", len(enemies))
        ctx.write("search.scan_count", int(ctx.read("search.scan_count", 0) or 0) + 1)
        return SUCCESS, None


def _publish(ctx: ActionContext, track: dict[str, Any]) -> None:
    ctx.write("selected.valid", True)
    ctx.write("selected.id", track["id"])
    ctx.write("selected.name", track["id"])
    ctx.write("selected.affiliation", track["affiliation"])
    for key in ("latitude_deg", "longitude_deg"):
        ctx.write(f"selected.{key[:3]}", track[key])
    ctx.write("selected.speed", track["speed_mps"])
    ctx.write("selected.heading", track["heading_deg_true"])
    ctx.write("selected.range", track["range_m"])
    ctx.write("selected.bearing", track["bearing_deg_true"])
    ctx.write("selected.updated", ctx.now)
    ctx.write("selected.age", 0.0)
    ctx.write("selected.lost_at", None)


class SelectNearest(ActionModel):
    def update(self, ctx: ActionContext) -> Status:
        if "tracks" not in ctx.shared:
            return FAILURE, "TRACK_SOURCE_UNAVAILABLE"
        enemies = [t for t in ctx.shared["tracks"].values() if t["affiliation"] == "ENEMY"]
        if not enemies:
            return FAILURE, "NO_VALID_TRACK"
        best = min(enemies, key=lambda t: (t["range_m"], t["id"]))
        _publish(ctx, best)
        return SUCCESS, None


class MaintainSelected(ActionModel):
    def update(self, ctx: ActionContext) -> Status:
        if "tracks" not in ctx.shared:
            return FAILURE, "TRACK_SOURCE_UNAVAILABLE"
        sid = ctx.read("selected.id")
        track = ctx.shared["tracks"].get(sid) if sid is not None else None
        if track is not None:
            _publish(ctx, track)
            return SUCCESS, None
        # keep last-known data, mark invalid, remember when the loss started
        ctx.write("selected.valid", False)
        if ctx.read("selected.lost_at") is None:
            ctx.write("selected.lost_at", ctx.now)
        updated = ctx.read("selected.updated")
        if isinstance(updated, int | float):
            ctx.write("selected.age", ctx.now - updated)
        return FAILURE, "TARGET_LOST"


class ClearSelected(ActionModel):
    def update(self, ctx: ActionContext) -> Status:
        ctx.write("selected.valid", False)
        ctx.write("selected.id", None)
        ctx.write("selected.lost_at", None)
        return SUCCESS, None


# ---------------------------------------------------------------------- formation
class FormationSlots(ActionModel):
    """Followers taking the slots of a formation shape (``mode`` option): their error to
    the shape's slots starts at ``initial_error_m`` when the shape changes and shrinks at
    ``converge_mps`` to ``floor_m``. With ``success_within_m`` the step succeeds once the
    error is within it (as the platform's formation steps do); without it, it keeps
    running. The followers are not simulated vehicles, only their slot errors are; which
    fields receive the error is decided by the contract (a shape whose step writes no
    error field leaves them as they are); ``keys`` names the generic keys written (default:
    the left / right slot errors)."""

    KEYS = ("formation.left_error", "formation.right_error")

    def start(self, ctx: ActionContext) -> Status:
        state = ctx.shared.setdefault("formation", {})
        mode = str(ctx.options.get("mode", "FORMATION"))
        if state.get("mode") != mode:
            state.update(
                mode=mode, error=float(ctx.options.get("initial_error_m", 60.0)), at=ctx.now
            )
        if ctx.options.get("write_mode"):
            # only when the real platform is known to write the shape this way: a field the
            # simulation sets but the platform does not would let a guessed condition pass
            ctx.write("formation.mode", mode)
        return self.update(ctx)

    def update(self, ctx: ActionContext) -> Status:
        state = ctx.shared.setdefault("formation", {"error": 0.0, "at": ctx.now})
        dt = max(0.0, ctx.now - float(state.get("at", ctx.now)))
        floor = float(ctx.options.get("floor_m", 2.0))
        rate = float(ctx.options.get("converge_mps", 4.0))
        state["error"] = max(floor, float(state.get("error", floor)) - rate * dt)
        state["at"] = ctx.now
        away = ctx.options.get("away_m")
        for key in ctx.options.get("keys") or self.KEYS:
            # ``away_m``: the error fields measure another shape's slots (e.g. the line,
            # while this shape is the column) and the followers are away from them
            ctx.write(key, float(away) if away is not None else state["error"])
        done = ctx.options.get("success_within_m")
        if done is not None and isinstance(ctx.params.get("ready_tolerance_m"), int | float):
            done = ctx.params["ready_tolerance_m"]  # the step's own readiness parameter
        if done is not None and state["error"] <= float(done):
            return SUCCESS, None
        return RUNNING, None


class StandoffCommand(ActionModel):
    """Range band of the selected target against ``desired_range_m`` ± ``range_tolerance_m``
    (the step's parameters; options ``desired_m`` / ``tolerance_m`` otherwise); the
    band is what standoff conditions read. Like the platform step, each update succeeds
    once the command is given (not when the range is in the band) and fails with
    NO_TARGET without a valid target; ``persistent: true`` keeps it running instead."""

    def update(self, ctx: ActionContext) -> Status:
        # the step's parameters when the model gives them, else the configured band
        desired = float(ctx.params.get("desired_range_m") or ctx.options.get("desired_m", 500.0))
        tol = float(ctx.params.get("range_tolerance_m") or ctx.options.get("tolerance_m", 50.0))
        rng = ctx.read("selected.range")
        if not ctx.read("selected.valid") or not isinstance(rng, int | float):
            zone = "NO_TARGET"
        elif rng > desired + tol:
            zone = "TOO_FAR"
        elif rng < desired - tol:
            zone = "TOO_CLOSE"
        else:
            zone = "IN_BAND"
        ctx.write("formation.range_zone", zone)
        if ctx.options.get("persistent"):
            return RUNNING, None
        if zone == "NO_TARGET":
            return FAILURE, "NO_TARGET"
        mode = {"TOO_FAR": "APPROACH", "TOO_CLOSE": "WITHDRAW"}.get(zone, "HOLD")
        ctx.write("formation.command_mode", mode)
        return SUCCESS, None


MODELS: dict[str, Callable[[], ActionModel]] = {
    "formation_slots": FormationSlots,
    "standoff_command": StandoffCommand,
    "move_to_point": MoveToPoint,
    "follow_route": FollowRoute,
    "hold_position": HoldPosition,
    "set_speed": SetSpeed,
    "stop": Stop,
    "turn_to_heading": TurnToHeading,
    "sensor_on": lambda: SensorSwitch(True),
    "sensor_off": lambda: SensorSwitch(False),
    "update_tracks": UpdateTracks,
    "select_nearest": SelectNearest,
    "maintain_selected": MaintainSelected,
    "clear_selected": ClearSelected,
}

# generic blackboard keys the world keeps current every update
VEHICLE_KEYS = (
    "self.lat",
    "self.lon",
    "self.heading",
    "self.speed",
    "self.mobility",
    "sensor.name",
    "sensor.exists",
    "sensor.on",
    "sensor.operational",
)


def sync_vehicle(world: World, vehicle: Vehicle, bb: dict[str, Any], fields: dict[str, str]):
    def put(key: str, value: Any) -> None:
        bb[fields.get(key, f"sim.{key}")] = value

    lat, lon = world.geo.to_latlon(vehicle.x, vehicle.y)
    put("self.lat", lat)
    put("self.lon", lon)
    put("self.heading", vehicle.heading)
    put("self.speed", vehicle.speed)
    put("self.mobility", vehicle.mobility_available)
    s = vehicle.sensor
    put("sensor.name", s.name if s else "")
    put("sensor.exists", bool(s and s.exists))
    put("sensor.on", bool(s and s.on))
    put("sensor.operational", bool(s and s.operational))
