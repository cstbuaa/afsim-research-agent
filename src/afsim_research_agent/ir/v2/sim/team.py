"""Team action models of the 2D simulation: centralized target allocation, weapon
engagement and target sharing over the radio (capability set slices threat_allocation,
engagement, communication_target_sharing).

They follow the contracts' stated semantics (``result`` texts and failure codes), so that
what only AFSIM used to show also shows in L2 / L3:

- a task sent by the leader is valid for ``TASK_LIFE_S`` (3 s) from the send; validating or
  firing on an expired task fails with TASK_EXPIRED;
- weapon scoring needs the filter's feasible weapons first (INVALID_PARAMETER), firing
  needs a selected weapon (NO_WEAPON_SELECTED), at most 3 shots per task epoch
  (ENGAGEMENT_LIMIT_REACHED), a reload interval (WEAPON_NOT_READY) and a round in flight
  (ROUND_IN_FLIGHT);
- radio messages are delivered only while the sender's and the recipient's radios work
  (scenario fault ``radio_operational``); followers count fresh broadcasts while on their
  local fallback and switch back after 2.

Field paths are the capability set's own (``ActionContext.put``); the alignment file names
which action runs which model (``[sim."<action>"] model = "<name>"``) and may set options
(weapons, ranges). Simplifications: hits are certain and a target is destroyed after
``World.hits_to_kill`` hits; tracks of a vehicle are the present hostiles within its sensor
range (its platform track picture, whether the search sensor was switched on or not)."""

from __future__ import annotations

import math
from typing import Any

from afsim_research_agent.ir.v2.sim.actions import FAILURE, RUNNING, SUCCESS, ActionContext
from afsim_research_agent.ir.v2.sim.actions import ActionModel as _Model
from afsim_research_agent.ir.v2.sim.world import Message, Shot, Vehicle, World

TASK_LIFE_S = 3.0  # an assignment is valid this long from the leader's send
SHARE_LIFE_S = 3.0  # a shared-target broadcast is fresh this long
TRACK_LIFE_S = 3.0  # fused tracks / reports
SHOTS_PER_EPOCH = 3
LINE_OF_FIRE_M = 30.0
WEAPONS = (  # default weapon fit (option ``weapons`` replaces it)
    {"id": "gun", "min_m": 0.0, "max_m": 3000.0, "ammo": 30, "reload_s": 2.0, "flight_s": 1.0},
    {"id": "missile", "min_m": 300.0, "max_m": 5000.0, "ammo": 4, "reload_s": 6.0,
     "flight_s": 4.0},
)  # fmt: skip

# fields the radio callbacks keep (sync_team): they change while no action of the
# receiving vehicle runs, so the L1 check STATE_FROZEN does not count on them staying put
RADIO_FIELDS = frozenset(
    {
        "allocation.execution_task.intent",
        "allocation.execution_task.target_id",
        "allocation.received_assignment.expires_at_s",
        "allocation.received_assignment.valid",
        "communication.link.last_rx_s",
        "formation.members.count",
        "threat.scoring.updated_at_s",
        "threat.targets.count",
        "track_sharing.execution_target.source",
        "track_sharing.recovery.fresh_message_count",
        "track_sharing.shared_target.valid",
    }
)

# what a failure code of these models means for a scenario or a sheet (render notes)
CODE_HINTS = {
    "FRIENDLY_IN_LINE_OF_FIRE": (
        f"a friendly vehicle is within {LINE_OF_FIRE_M:g} m of the line to the target: place "
        "the vehicles side by side across the direction of the target (set_scenario vehicles), "
        "or give the state a way out when the fire check fails"
    ),
    "TASK_EXPIRED": (
        f"an assignment is valid {TASK_LIFE_S:g} s from the leader's send: the leader must "
        "resend within that time, or the member needs a way out (expire / hold)"
    ),
    "NO_VALID_TARGET": "no enemy track: the state needs a way out when there are no targets",
    "OUT_OF_RANGE": "the target is out of the weapons' range",
    "NO_ELIGIBLE_MEMBER": "no member can take a task (radio or status)",
    "ENGAGEMENT_LIMIT_REACHED": f"at most {SHOTS_PER_EPOCH} shots per task epoch",
    "STALE_REPORT": "no fresh shared message (radio down or broadcasts too rare)",
}


# ---------------------------------------------------------------------- helpers
def picture(world: World, v: Vehicle) -> dict[str, tuple[Any, float]]:
    """Enemy tracks the vehicle holds: id → (hostile, range)."""
    s = v.sensor
    if s is not None and (not s.exists or not s.operational):
        return {}
    reach = s.range_m if s is not None else 6000.0
    out = {}
    for h in world.hostiles:
        if h.present(world.now) and h.affiliation == "ENEMY":
            rng = math.hypot(h.x - v.x, h.y - v.y)
            if rng <= reach:
                out[h.id] = (h, rng)
    return out


def _radio(world: World, name: str) -> bool:
    v = world.vehicles.get(name)
    return v is not None and v.radio_operational


def _others(world: World, me: str) -> list[str]:
    return [n for n in world.vehicles if n != me]


def _team_state(ctx: ActionContext) -> dict[str, Any]:
    return ctx.shared.setdefault("team", {})


def _live_task(ctx: ActionContext) -> Message | None:
    msg = ctx.world.team.assignments.get(ctx.vehicle.name)
    if msg is None or ctx.now > msg.sent_at + TASK_LIFE_S + 1e-9:
        return None
    return msg


def sync_team(world: World, name: str, bb: dict[str, Any]) -> None:
    """The radio callbacks of one vehicle, before its update: an assignment received is
    adopted (valid until ``expire_assignment`` clears it); a follower's shared target is
    valid while the last broadcast is fresh, and fresh broadcasts are counted while it is
    on its local fallback."""
    team = world.team
    seen = team.seen.setdefault(name, {})
    msg = team.assignments.get(name)
    if msg is not None and seen.get("assignment") != msg.seq:
        seen["assignment"] = msg.seq
        bb["allocation.received_assignment.valid"] = True
        bb["allocation.received_assignment.expires_at_s"] = msg.sent_at + TASK_LIFE_S
        bb["allocation.execution_task.intent"] = "ENGAGE"
        bb["allocation.execution_task.target_id"] = msg.target_id
    share = team.shared.get(name)
    if share is not None:
        bb["track_sharing.shared_target.valid"] = world.now <= share.sent_at + SHARE_LIFE_S
        bb["communication.link.last_rx_s"] = share.sent_at
        if seen.get("shared") != share.seq:
            seen["shared"] = share.seq
            if bb.get("track_sharing.execution_target.source") == "LOCAL":
                count = int(bb.get("track_sharing.recovery.fresh_message_count") or 0)
                bb["track_sharing.recovery.fresh_message_count"] = count + 1


# ---------------------------------------------------------------------- allocation
class _Leader(_Model):
    def leader_only(self, ctx: ActionContext) -> tuple[str, str] | None:
        lead = ctx.world.team.leader
        if lead is not None and ctx.vehicle.name != lead:
            return FAILURE, "UNAUTHORIZED_ROLE"
        return None


class CollectFusedTargets(_Leader):
    def update(self, ctx: ActionContext):
        if (bad := self.leader_only(ctx)) is not None:
            return bad
        threats = picture(ctx.world, ctx.vehicle)
        _team_state(ctx)["threats"] = threats
        ctx.put("threat.targets.count", len(threats))
        return (SUCCESS, None) if threats else (FAILURE, "NO_VALID_TARGET")


class ScoreThreats(_Leader):
    def update(self, ctx: ActionContext):
        if (bad := self.leader_only(ctx)) is not None:
            return bad
        if not _team_state(ctx).get("threats"):
            return FAILURE, "NO_VALID_TARGET"
        ctx.put("threat.scoring.updated_at_s", ctx.now)
        return SUCCESS, None


class CollectMemberStatus(_Leader):
    def update(self, ctx: ActionContext):
        if (bad := self.leader_only(ctx)) is not None:
            return bad
        members = _others(ctx.world, ctx.vehicle.name)
        _team_state(ctx)["members"] = members
        ctx.put("formation.members.count", len(members))
        return (SUCCESS, None) if members else (FAILURE, "STALE_INPUT")


class FilterEligibleMembers(_Leader):
    def update(self, ctx: ActionContext):
        if (bad := self.leader_only(ctx)) is not None:
            return bad
        mine = _radio(ctx.world, ctx.vehicle.name)
        members = _team_state(ctx).get("members") or []
        eligible = [m for m in members if mine and _radio(ctx.world, m)]
        _team_state(ctx)["eligible"] = eligible
        ctx.put("formation.members.eligible_count", len(eligible))
        return (SUCCESS, None) if eligible else (FAILURE, "NO_ELIGIBLE_MEMBER")


class BuildAllocationCandidates(_Leader):
    def update(self, ctx: ActionContext):
        if (bad := self.leader_only(ctx)) is not None:
            return bad
        st = _team_state(ctx)
        threats, eligible = st.get("threats") or {}, st.get("eligible") or []
        if not threats:
            return FAILURE, "NO_VALID_TARGET"
        if not eligible:
            return FAILURE, "NO_ELIGIBLE_MEMBER"
        st["candidates"] = [(m, t) for m in eligible for t in threats]
        ctx.put("allocation.candidates.count", len(st["candidates"]))
        return SUCCESS, None


class AllocateTargets(_Leader):
    """One target per member, highest threat (nearest to the leader) first, to the
    nearest free member; a member keeps its target while that target is still a threat."""

    def update(self, ctx: ActionContext):
        if (bad := self.leader_only(ctx)) is not None:
            return bad
        st = _team_state(ctx)
        cands = st.get("candidates") or []
        if not cands:
            return FAILURE, "NO_ELIGIBLE_MEMBER"
        threats = st.get("threats") or {}
        members = sorted({m for m, _ in cands})
        old: dict[str, str] = dict(st.get("plan") or {})
        plan = {m: t for m, t in old.items() if m in members and t in threats}
        order = sorted(threats, key=lambda t: threats[t][1])
        w = ctx.world
        for tid in order:
            free = [m for m in members if m not in plan]
            if not free:
                break
            if tid in plan.values():
                continue
            h = threats[tid][0]
            plan[
                min(free, key=lambda m: math.hypot(w.vehicles[m].x - h.x, w.vehicles[m].y - h.y))
            ] = tid
        for m in members:  # more members than targets: share the nearest target
            if m not in plan and order:
                plan[m] = order[0]
        if plan != old:
            st["epoch"] = int(st.get("epoch", 0)) + 1
        st["plan"] = plan
        ctx.put("allocation.assignments.count", len(plan))
        ctx.put("allocation.assignments.epoch", int(st.get("epoch", 0)))
        return SUCCESS, None


class BroadcastAssignments(_Leader):
    def update(self, ctx: ActionContext):
        if (bad := self.leader_only(ctx)) is not None:
            return bad
        st = _team_state(ctx)
        plan = st.get("plan") or {}
        if not plan:
            return FAILURE, "UNAUTHORIZED_ROLE"
        if not _radio(ctx.world, ctx.vehicle.name):
            return FAILURE, "COMM_UNAVAILABLE"
        team = ctx.world.team
        sent = 0
        for member, tid in plan.items():
            if _radio(ctx.world, member):
                team.assignments[member] = Message(
                    ctx.vehicle.name, tid, int(st.get("epoch", 0)), team.next_seq(), ctx.now
                )
                sent += 1
        ctx.put("allocation.publication.sent_count", sent)
        return SUCCESS, None


class AdoptAssignment(_Model):
    def update(self, ctx: ActionContext):
        ok = bool(ctx.get("allocation.received_assignment.valid"))
        return (SUCCESS, None) if ok else (FAILURE, "STALE_INPUT")


class ExpireAssignment(_Model):
    def update(self, ctx: ActionContext):
        exp = ctx.get("allocation.received_assignment.expires_at_s")
        if ctx.get("allocation.received_assignment.valid") and isinstance(exp, int | float):
            if ctx.now > float(exp) + 1e-9:
                ctx.put("allocation.received_assignment.valid", False)
                ctx.put("allocation.execution_task.intent", "HOLD")
                ctx.put("allocation.execution_task.target_id", "")
        return SUCCESS, None


# ---------------------------------------------------------------------- engagement
def _weapons(ctx: ActionContext) -> list[dict[str, Any]]:
    st = _team_state(ctx)
    if "weapons" not in st:
        fit = ctx.options.get("weapons") or WEAPONS
        st["weapons"] = [dict(w, ready_at=0.0, flight_until=0.0) for w in fit]
    return st["weapons"]


def _target(ctx: ActionContext) -> tuple[Any, float] | None:
    tid = ctx.get("engagement.task.target_id")
    if not tid:
        return None
    return picture(ctx.world, ctx.vehicle).get(str(tid))


def _epoch_shots(ctx: ActionContext) -> int:
    return int(ctx.get("engagement.shots.request_count") or 0)


class ValidateEngagementTask(_Model):
    def update(self, ctx: ActionContext):
        team = ctx.world.team
        msg = team.assignments.get(ctx.vehicle.name)
        if msg is None or ctx.get("allocation.execution_task.intent") not in (None, "ENGAGE"):
            ctx.put("engagement.task.valid", False)
            ctx.put("engagement.safety.authority_valid", False)
            return FAILURE, "NO_ENGAGE_AUTHORITY"
        if _live_task(ctx) is None:
            ctx.put("engagement.task.valid", False)
            ctx.put("engagement.safety.authority_valid", False)
            return FAILURE, "TASK_EXPIRED"
        st = _team_state(ctx)
        if st.get("task_epoch") != msg.epoch:
            st["task_epoch"] = msg.epoch
            ctx.blackboard["engagement.shots.request_count"] = 0  # new epoch: count restarts
        ctx.put("engagement.task.valid", True)
        ctx.put("engagement.task.target_id", msg.target_id)
        ctx.put("engagement.task.intent", "ENGAGE")
        ctx.put("engagement.task.allocation_epoch", msg.epoch)
        ctx.put("engagement.task.expires_at_s", msg.sent_at + TASK_LIFE_S)
        ctx.put("engagement.safety.authority_valid", True)
        return SUCCESS, None


class CollectWeaponStatus(_Model):
    def update(self, ctx: ActionContext):
        weapons = _weapons(ctx)
        tgt = _target(ctx)
        ctx.put("engagement.target.valid", tgt is not None)
        if tgt is not None:
            h, rng = tgt
            ctx.put("engagement.target.affiliation", h.affiliation)
            ctx.put("engagement.target.class", str(ctx.options.get("target_class", "ARMOR")))
            ctx.put("engagement.target.class_known", True)
            ctx.put("engagement.target.range_m", rng)
            ctx.put("engagement.target.age_s", 0.0)
        if not any(int(w["ammo"]) > 0 for w in weapons):
            return FAILURE, "NO_AMMUNITION"
        return SUCCESS, None


class EvaluateFireSafety(_Model):
    def update(self, ctx: ActionContext):
        tgt = _target(ctx)
        if tgt is None:
            return FAILURE, "TRACK_STALE"
        h, rng = tgt
        v = ctx.vehicle
        clear = True
        for name, other in ctx.world.vehicles.items():
            if name == v.name:
                continue
            # distance of the friend from the line of fire, and along it
            dx, dy = h.x - v.x, h.y - v.y
            ox, oy = other.x - v.x, other.y - v.y
            along = (ox * dx + oy * dy) / max(rng, 1e-9)
            off = abs(ox * dy - oy * dx) / max(rng, 1e-9)
            if 0.0 < along < rng and off <= LINE_OF_FIRE_M:
                clear = False
        protected = bool(ctx.options.get("protected_area_clear", True))
        ctx.put("engagement.safety.friendly_line_clear", clear)
        ctx.put("engagement.safety.protected_area_clear", protected)
        if not clear:
            return FAILURE, "FRIENDLY_IN_LINE_OF_FIRE"
        if not protected:
            return FAILURE, "PROTECTED_AREA"
        return SUCCESS, None


class FilterWeaponCandidates(_Model):
    def update(self, ctx: ActionContext):
        st = _team_state(ctx)
        st["feasible"] = []
        tgt = _target(ctx)
        if tgt is None:
            return FAILURE, "OUT_OF_RANGE"
        if not ctx.get("engagement.safety.friendly_line_clear", False):
            return FAILURE, "FRIENDLY_IN_LINE_OF_FIRE"
        _h, rng = tgt
        first = None
        for w in _weapons(ctx):
            why = None
            if not float(w["min_m"]) <= rng <= float(w["max_m"]):
                why = "OUT_OF_RANGE"
            elif int(w["ammo"]) <= 0:
                why = "NO_AMMUNITION"
            elif ctx.now < float(w["ready_at"]) - 1e-9:
                why = "WEAPON_NOT_READY"
            elif ctx.now < float(w["flight_until"]) - 1e-9:
                why = "ROUND_IN_FLIGHT"
            if why is None:
                st["feasible"].append(w["id"])
            first = first or why
        return (SUCCESS, None) if st["feasible"] else (FAILURE, first or "OUT_OF_RANGE")


class ScoreWeaponCandidates(_Model):
    def update(self, ctx: ActionContext):
        st = _team_state(ctx)
        if not st.get("feasible"):
            st["scored"] = []
            return FAILURE, "INVALID_PARAMETER"
        st["scored"] = list(st["feasible"])  # the default fit lists the preferred first
        return SUCCESS, None


class SelectWeapon(_Model):
    def update(self, ctx: ActionContext):
        scored = _team_state(ctx).get("scored") or []
        if not scored:
            return FAILURE, "INVALID_PARAMETER"
        ctx.put("engagement.selected.primary_weapon_id", scored[0])
        ctx.put("engagement.selected.backup_weapon_id", scored[1] if len(scored) > 1 else "")
        return SUCCESS, None


def _selected(ctx: ActionContext) -> dict[str, Any] | None:
    wid = ctx.get("engagement.selected.primary_weapon_id")
    return next((w for w in _weapons(ctx) if w["id"] == wid), None) if wid else None


class AimSelectedWeapon(_Model):
    def update(self, ctx: ActionContext):
        if not ctx.get("engagement.task.valid"):
            return FAILURE, "NO_ENGAGE_AUTHORITY"
        w = _selected(ctx)
        tgt = _target(ctx)
        if tgt is None:
            return FAILURE, "TRACK_STALE"
        _h, rng = tgt
        ctx.put("engagement.target.range_m", rng)
        ctx.put("engagement.target.age_s", 0.0)
        if w is None or not float(w["min_m"]) <= rng <= float(w["max_m"]):
            return FAILURE, "OUT_OF_ARC"
        return SUCCESS, None


class FireSelectedWeapon(_Model):
    def update(self, ctx: ActionContext):
        if _live_task(ctx) is None or not ctx.get("engagement.task.valid"):
            return FAILURE, "TASK_EXPIRED"
        w = _selected(ctx)
        if w is None:
            return FAILURE, "NO_WEAPON_SELECTED"
        if not ctx.get("engagement.safety.friendly_line_clear", False):
            return FAILURE, "FRIENDLY_IN_LINE_OF_FIRE"
        if _epoch_shots(ctx) >= SHOTS_PER_EPOCH:
            return FAILURE, "ENGAGEMENT_LIMIT_REACHED"
        if ctx.now < float(w["ready_at"]) - 1e-9 or int(w["ammo"]) <= 0:
            return FAILURE, "WEAPON_NOT_READY"
        if ctx.now < float(w["flight_until"]) - 1e-9:
            return FAILURE, "ROUND_IN_FLIGHT"
        tgt = _target(ctx)
        if tgt is None or not float(w["min_m"]) <= tgt[1] <= float(w["max_m"]):
            return FAILURE, "CANNOT_INTERCEPT"
        w["ammo"] = int(w["ammo"]) - 1
        w["ready_at"] = ctx.now + float(w["reload_s"])
        w["flight_until"] = ctx.now + float(w["flight_s"])
        shot = Shot(ctx.vehicle.name, w["id"], tgt[0].id, ctx.now, ctx.now + float(w["flight_s"]))
        ctx.world.team.shots.append(shot)
        _team_state(ctx)["last_shot"] = shot
        ctx.put("engagement.shots.request_count", _epoch_shots(ctx) + 1)
        ctx.put("engagement.shots.fired_at_s", ctx.now)
        return SUCCESS, None


class ObserveWeaponEffect(_Model):
    def update(self, ctx: ActionContext):
        shot: Shot | None = _team_state(ctx).get("last_shot")
        if shot is None or ctx.get("engagement.shots.fired_at_s") is None:
            return FAILURE, "INVALID_PARAMETER"
        if shot.result is None and ctx.now <= shot.impact_at + 5.0:
            return RUNNING, None
        ctx.put("damage.assessment.normalized_result", shot.result or "NO_EFFECT")
        ctx.put("damage.observations.observed_at_s", ctx.now)
        return SUCCESS, None


class EvaluateReattack(_Model):
    def update(self, ctx: ActionContext):
        if _epoch_shots(ctx) >= SHOTS_PER_EPOCH:
            code = "ENGAGEMENT_LIMIT_REACHED"
        elif ctx.get("damage.assessment.normalized_result") == "DESTROYED":
            code = "NO_ENGAGE_AUTHORITY"
        elif _live_task(ctx) is None:
            code = "TASK_EXPIRED"
        else:
            code = None
        ctx.put("damage.assessment.reattack_allowed", code is None)
        return (SUCCESS, None) if code is None else (FAILURE, code)


class CeaseFire(_Model):
    def update(self, ctx: ActionContext):
        return SUCCESS, None


# ---------------------------------------------------------------------- target sharing
class PublishLocalTracks(_Model):
    def update(self, ctx: ActionContext):
        team = ctx.world.team
        up = _radio(ctx.world, ctx.vehicle.name)
        ctx.put("communication.link.available", up)
        if not up or team.leader is None:
            return FAILURE, "COMM_UNAVAILABLE"
        tracks = picture(ctx.world, ctx.vehicle)
        if _radio(ctx.world, team.leader):
            for tid in tracks:
                team.reports[tid] = ctx.now
        ctx.put("communication.link.last_tx_s", ctx.now)
        ctx.put("communication.tx.sent_count", len(tracks))
        return SUCCESS, None


class LeaderOnly(_Model):
    """Report ingestion and association: done by the radio callback; the leaf succeeds on
    the leader."""

    def update(self, ctx: ActionContext):
        lead = ctx.world.team.leader
        return (
            (SUCCESS, None) if lead in (None, ctx.vehicle.name) else (FAILURE, "INVALID_PARAMETER")
        )


def _fused(ctx: ActionContext) -> dict[str, Any]:
    team = ctx.world.team
    out = {tid: h for tid, (h, _r) in picture(ctx.world, ctx.vehicle).items()}
    for tid, t in team.reports.items():
        h = ctx.world.hostile(tid)
        if h is not None and h.present(ctx.now) and ctx.now - t <= TRACK_LIFE_S:
            out[tid] = h
    return out


class UpdateFusedTracks(_Model):
    def update(self, ctx: ActionContext):
        lead = ctx.world.team.leader
        if lead not in (None, ctx.vehicle.name):
            return FAILURE, "STALE_REPORT"
        fused = _fused(ctx)
        ctx.put("track_sharing.fused_tracks.count", len(fused))
        if ctx.get("track_sharing.shared_target.target_id") not in fused:
            ctx.put("track_sharing.shared_target.valid", False)
        return (SUCCESS, None) if fused else (FAILURE, "STALE_REPORT")


class SelectSharedTarget(_Model):
    def update(self, ctx: ActionContext):
        fused = _fused(ctx)
        cur = ctx.get("track_sharing.shared_target.target_id")
        if not fused:
            ctx.put("track_sharing.shared_target.valid", False)
            return FAILURE, "NO_VALID_FUSED_TRACK"
        if cur not in fused:
            v = ctx.vehicle
            cur = min(fused, key=lambda t: math.hypot(fused[t].x - v.x, fused[t].y - v.y))
            epoch = int(ctx.get("track_sharing.shared_target.selection_epoch") or 0) + 1
            ctx.put("track_sharing.shared_target.selection_epoch", epoch)
        ctx.put("track_sharing.shared_target.valid", True)
        ctx.put("track_sharing.shared_target.target_id", cur)
        return SUCCESS, None


class BroadcastSharedTarget(_Model):
    def update(self, ctx: ActionContext):
        up = _radio(ctx.world, ctx.vehicle.name)
        ctx.put("communication.link.available", up)
        if not ctx.get("track_sharing.shared_target.valid"):
            return FAILURE, "NO_VALID_SHARED_TARGET"
        if not up:
            return FAILURE, "COMM_UNAVAILABLE"
        team = ctx.world.team
        tid = str(ctx.get("track_sharing.shared_target.target_id") or "")
        epoch = int(ctx.get("track_sharing.shared_target.selection_epoch") or 0)
        for other in _others(ctx.world, ctx.vehicle.name):
            if _radio(ctx.world, other):
                team.shared[other] = Message(ctx.vehicle.name, tid, epoch, team.next_seq(), ctx.now)
        ctx.put("communication.link.last_tx_s", ctx.now)
        return SUCCESS, None


def _shared_now(ctx: ActionContext) -> Message | None:
    msg = ctx.world.team.shared.get(ctx.vehicle.name)
    if msg is None or ctx.now > msg.sent_at + SHARE_LIFE_S + 1e-9:
        return None
    return msg


class AdoptSharedTarget(_Model):
    def update(self, ctx: ActionContext):
        msg = _shared_now(ctx)
        if msg is None or ctx.get("track_sharing.execution_target.source") == "LOCAL":
            return FAILURE, "NO_VALID_SHARED_TARGET"
        ctx.put("track_sharing.execution_target.source", "SHARED")
        ctx.put("track_sharing.execution_target.target_id", msg.target_id)
        return SUCCESS, None


class FallbackToLocalTarget(_Model):
    def update(self, ctx: ActionContext):
        own = ctx.read("selected.id") if ctx.read("selected.valid") else None
        if not own:
            tracks = picture(ctx.world, ctx.vehicle)
            own = min(tracks, key=lambda t: tracks[t][1]) if tracks else ""
        ctx.put("track_sharing.execution_target.source", "LOCAL")
        ctx.put("track_sharing.execution_target.target_id", own)
        ctx.put("track_sharing.recovery.fresh_message_count", 0)
        return SUCCESS, None


class ResynchronizeSharedTarget(_Model):
    def update(self, ctx: ActionContext):
        if ctx.get("track_sharing.execution_target.source") == "SHARED":
            return SUCCESS, None
        msg = _shared_now(ctx)
        if msg is None or int(ctx.get("track_sharing.recovery.fresh_message_count") or 0) < 2:
            return FAILURE, "STALE_REPORT"
        ctx.put("track_sharing.execution_target.source", "SHARED")
        ctx.put("track_sharing.execution_target.target_id", msg.target_id)
        ctx.put("track_sharing.recovery.fresh_message_count", 0)
        return SUCCESS, None


TEAM_MODELS = {
    "alloc_collect_targets": CollectFusedTargets,
    "alloc_score_threats": ScoreThreats,
    "alloc_collect_members": CollectMemberStatus,
    "alloc_filter_members": FilterEligibleMembers,
    "alloc_build_candidates": BuildAllocationCandidates,
    "alloc_allocate": AllocateTargets,
    "alloc_broadcast": BroadcastAssignments,
    "alloc_adopt": AdoptAssignment,
    "alloc_expire": ExpireAssignment,
    "engage_validate": ValidateEngagementTask,
    "engage_collect_status": CollectWeaponStatus,
    "engage_fire_safety": EvaluateFireSafety,
    "engage_filter": FilterWeaponCandidates,
    "engage_score": ScoreWeaponCandidates,
    "engage_select": SelectWeapon,
    "engage_aim": AimSelectedWeapon,
    "engage_fire": FireSelectedWeapon,
    "engage_observe": ObserveWeaponEffect,
    "engage_reattack": EvaluateReattack,
    "engage_cease": CeaseFire,
    "share_publish_local": PublishLocalTracks,
    "share_leader_leaf": LeaderOnly,
    "share_update_fused": UpdateFusedTracks,
    "share_select": SelectSharedTarget,
    "share_broadcast": BroadcastSharedTarget,
    "share_adopt": AdoptSharedTarget,
    "share_fallback": FallbackToLocalTarget,
    "share_resync": ResynchronizeSharedTarget,
}

# the alignment entries for a capability set with these slices (``ir_alignment.toml``)
DEFAULT_ALIGNMENT = {
    "threat_allocation.collect_fused_targets": "alloc_collect_targets",
    "threat_allocation.score_threats": "alloc_score_threats",
    "threat_allocation.collect_member_status": "alloc_collect_members",
    "threat_allocation.filter_eligible_members": "alloc_filter_members",
    "threat_allocation.build_allocation_candidates": "alloc_build_candidates",
    "threat_allocation.allocate_targets": "alloc_allocate",
    "threat_allocation.broadcast_assignments": "alloc_broadcast",
    "threat_allocation.adopt_assignment": "alloc_adopt",
    "threat_allocation.expire_assignment": "alloc_expire",
    "engagement.validate_engagement_task": "engage_validate",
    "engagement.collect_weapon_status": "engage_collect_status",
    "engagement.evaluate_fire_safety": "engage_fire_safety",
    "engagement.filter_weapon_candidates": "engage_filter",
    "engagement.score_weapon_candidates": "engage_score",
    "engagement.select_weapon": "engage_select",
    "engagement.aim_selected_weapon": "engage_aim",
    "engagement.fire_selected_weapon": "engage_fire",
    "engagement.observe_weapon_effect": "engage_observe",
    "engagement.evaluate_reattack": "engage_reattack",
    "engagement.cease_fire": "engage_cease",
    "target_sharing.publish_local_tracks": "share_publish_local",
    "target_sharing.ingest_track_report": "share_leader_leaf",
    "target_sharing.associate_track_report": "share_leader_leaf",
    "target_sharing.update_fused_tracks": "share_update_fused",
    "target_sharing.select_shared_target": "share_select",
    "target_sharing.broadcast_shared_target": "share_broadcast",
    "target_sharing.adopt_shared_target": "share_adopt",
    "target_sharing.fallback_to_local_target": "share_fallback",
    "target_sharing.resynchronize_shared_target": "share_resync",
}


def sim_spec(contracts: Any, action: str) -> dict[str, Any] | None:
    """The 2D model entry of an action: the alignment file's ``[sim."<action>"]``, else the
    team model these contracts' slices default to; None: no model (idealized)."""
    from afsim_research_agent.ir.v2.sim.actions import MODELS

    spec = contracts.semantics.actions.get(action)
    if spec is None and action in DEFAULT_ALIGNMENT and action in contracts.actions:
        spec = {"model": DEFAULT_ALIGNMENT[action]}
    return spec if spec is not None and spec.get("model") in MODELS else None


def _register() -> None:
    from afsim_research_agent.ir.v2.sim.actions import MODELS

    MODELS.update(TEAM_MODELS)


_register()
