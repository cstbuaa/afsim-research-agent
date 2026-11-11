"""Team models of the 2D simulation (ir.v2.sim.team): task expiry, weapon selection order,
shot limits, radio faults and the shared-target fallback / resync."""

from __future__ import annotations

from afsim_research_agent.ir.v2.sim import team as T
from afsim_research_agent.ir.v2.sim.actions import FAILURE, MODELS, RUNNING, SUCCESS, ActionContext
from afsim_research_agent.ir.v2.sim.world import Hostile, Sensor, Vehicle, World


def _world() -> World:
    w = World()
    for name, y in (("leader_0", 0.0), ("member_0", 100.0), ("member_1", -100.0)):
        w.vehicles[name] = Vehicle(name, 0.0, y, sensor=Sensor(range_m=5000.0))
    w.hostiles = [Hostile("red_1", 1500.0, 100.0), Hostile("red_2", 1600.0, -100.0)]
    w.team.leader = "leader_0"
    return w


class _Team:
    def __init__(self, w: World) -> None:
        self.w = w
        self.bb = {n: {} for n in w.vehicles}
        self.shared = {n: {} for n in w.vehicles}

    def run(self, who: str, model: str, **options) -> tuple[str, str | None]:
        ctx = ActionContext(
            world=self.w,
            vehicle=self.w.vehicles[who],
            blackboard=self.bb[who],
            fields={},
            params={},
            success={},
            options=options,
            shared=self.shared[who],
        )
        m = MODELS[model]()
        return m.start(ctx)

    def sync(self) -> None:
        for n in self.w.vehicles:
            T.sync_team(self.w, n, self.bb[n])

    def allocate(self) -> None:
        for step in (
            "alloc_collect_targets",
            "alloc_score_threats",
            "alloc_collect_members",
            "alloc_filter_members",
            "alloc_build_candidates",
            "alloc_allocate",
            "alloc_broadcast",
        ):
            assert self.run("leader_0", step) == (SUCCESS, None), step
        self.sync()

    def prepare(self, who: str) -> None:
        for step in ("engage_validate", "engage_collect_status", "engage_fire_safety"):
            assert self.run(who, step) == (SUCCESS, None), step

    def advance(self, dt: float) -> None:
        self.w.step(dt)


def test_one_target_per_member_and_the_task_expires_three_seconds_after_the_send():
    t = _Team(_world())
    t.allocate()
    assert t.bb["leader_0"]["allocation.publication.sent_count"] == 2
    targets = {t.bb[m]["allocation.execution_task.target_id"] for m in ("member_0", "member_1")}
    assert targets == {"red_1", "red_2"}
    assert t.bb["member_0"]["allocation.received_assignment.valid"] is True
    t.advance(4.0)  # no resend within 3 s
    assert t.run("member_0", "engage_validate") == (FAILURE, "TASK_EXPIRED")
    assert t.run("member_0", "alloc_expire") == (SUCCESS, None)
    assert t.bb["member_0"]["allocation.received_assignment.valid"] is False
    assert t.bb["member_0"]["allocation.execution_task.intent"] == "HOLD"


def test_weapons_must_be_filtered_then_scored_then_selected_before_firing():
    t = _Team(_world())
    t.allocate()
    t.prepare("member_0")
    assert t.run("member_0", "engage_score") == (FAILURE, "INVALID_PARAMETER")  # no filter
    assert t.run("member_0", "engage_fire") == (FAILURE, "NO_WEAPON_SELECTED")
    for step in ("engage_filter", "engage_score", "engage_select", "engage_aim", "engage_fire"):
        assert t.run("member_0", step) == (SUCCESS, None), step
    assert t.bb["member_0"]["engagement.selected.primary_weapon_id"] == "gun"
    assert t.bb["member_0"]["engagement.shots.request_count"] == 1
    # the round is in flight / the gun reloads: firing again at once fails
    assert t.run("member_0", "engage_fire") == (FAILURE, "WEAPON_NOT_READY")
    assert t.run("member_0", "engage_observe")[0] == RUNNING
    t.advance(1.0)
    assert t.run("member_0", "engage_observe") == (SUCCESS, None)
    assert t.bb["member_0"]["damage.assessment.normalized_result"] == "DAMAGED"


def test_firing_late_on_a_task_fails_task_expired_and_three_shots_per_epoch():
    w = _world()
    w.hits_to_kill = 99  # the target survives: the plan (and its epoch) stays the same
    t = _Team(w)
    t.allocate()
    t.prepare("member_0")
    for step in ("engage_filter", "engage_score", "engage_select"):
        t.run("member_0", step)
    t.advance(3.5)  # selection and aiming took longer than the task's life
    assert t.run("member_0", "engage_fire") == (FAILURE, "TASK_EXPIRED")
    for _ in range(3):  # the leader resends every 2 s: the member fires within the life
        t.allocate()
        assert t.run("member_0", "engage_validate") == (SUCCESS, None)
        assert t.run("member_0", "engage_fire") == (SUCCESS, None)
        t.advance(2.0)
    t.allocate()
    t.run("member_0", "engage_validate")
    assert t.run("member_0", "engage_fire") == (FAILURE, "ENGAGEMENT_LIMIT_REACHED")
    assert t.run("member_0", "engage_reattack") == (FAILURE, "ENGAGEMENT_LIMIT_REACHED")
    assert t.bb["member_0"]["damage.assessment.reattack_allowed"] is False
    assert t.w.hostile("red_1").hits == 3


def test_a_friend_in_the_line_of_fire_and_a_radio_fault():
    w = _world()
    w.hostiles = w.hostiles[:1]  # one target: both members get red_1
    w.vehicles["member_1"].x, w.vehicles["member_1"].y = 700.0, 105.0  # between m0 and red_1
    t = _Team(w)
    t.allocate()
    t.run("member_0", "engage_validate")
    t.run("member_0", "engage_collect_status")
    assert t.run("member_0", "engage_fire_safety") == (FAILURE, "FRIENDLY_IN_LINE_OF_FIRE")
    w.vehicles["member_0"].radio_operational = False
    t.advance(4.0)
    t.allocate()  # filter: member_0 is not eligible, so only member_1 gets a task
    assert t.bb["leader_0"]["formation.members.eligible_count"] == 1
    assert t.run("member_0", "engage_validate") == (FAILURE, "TASK_EXPIRED")


def test_shared_target_fallback_and_resync_after_two_fresh_broadcasts():
    w = _world()
    t = _Team(w)

    def leader_publish() -> None:
        t.run("member_0", "share_publish_local")
        assert t.run("leader_0", "share_update_fused") == (SUCCESS, None)
        assert t.run("leader_0", "share_select") == (SUCCESS, None)
        assert t.run("leader_0", "share_broadcast") == (SUCCESS, None)
        t.sync()

    leader_publish()
    assert t.run("member_0", "share_adopt") == (SUCCESS, None)
    assert t.bb["member_0"]["track_sharing.execution_target.source"] == "SHARED"
    w.vehicles["member_0"].radio_operational = False  # link lost
    t.advance(4.0)
    t.run("leader_0", "share_broadcast")
    t.sync()
    assert t.bb["member_0"]["track_sharing.shared_target.valid"] is False
    assert t.run("member_0", "share_fallback") == (SUCCESS, None)
    assert t.bb["member_0"]["track_sharing.execution_target.source"] == "LOCAL"
    w.vehicles["member_0"].radio_operational = True  # back
    leader_publish()
    assert t.run("member_0", "share_adopt") == (FAILURE, "NO_VALID_SHARED_TARGET")
    assert t.run("member_0", "share_resync") == (FAILURE, "STALE_REPORT")  # 1 fresh message
    t.advance(1.0)
    leader_publish()
    assert t.bb["member_0"]["track_sharing.recovery.fresh_message_count"] == 2
    assert t.run("member_0", "share_resync") == (SUCCESS, None)
    assert t.bb["member_0"]["track_sharing.execution_target.source"] == "SHARED"


def test_a_failed_expectation_names_the_failing_leaves_of_the_stuck_instance():
    from afsim_research_agent.ir.v2.sim.run import Check, SimResult

    r = SimResult("s")
    r.checks = [Check("m", False, "visited [...]", "member_0")]
    r.states = {"member_0": [(0.0, "WAIT"), (5.0, "VALIDATING"), (5.0, "SELECTING")]}
    r.failures = {
        "member_0": [
            (5.0, "VALIDATING", "engagement.evaluate_fire_safety", "FRIENDLY_IN_LINE_OF_FIRE"),
            (5.0, "SELECTING", "engagement.score_weapon_candidates", "INVALID_PARAMETER"),
            (6.0, "SELECTING", "engagement.score_weapon_candidates", "INVALID_PARAMETER"),
        ]
    }
    text = r.render()
    assert "member_0 stayed in SELECTING from 5s" in text
    assert "engagement.evaluate_fire_safety FRIENDLY_IN_LINE_OF_FIRE @5s (VALIDATING)" in text
    assert text.count("INVALID_PARAMETER") == 1 and "side by side" in text
