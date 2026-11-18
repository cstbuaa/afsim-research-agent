# ruff: noqa: F811 - pytest fixtures imported from test_falcon_v3
"""Drafting from a verified skeleton (falcon.v3.skeleton): the model's choice becomes a whole
sheet; the sheets for the formation requirements pass the offline checks."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from test_falcon_v3 import _stub_langchain, set_root  # noqa: F401, F811 - fixtures

from afsim_research_agent.falcon.v3.skeleton import SkeletonChoice, build, to_spec

EVENTS = {"cancelled": "上级结束任务", "withdraw_order": "上级撤离命令", "fire_order": "开火命令"}

# the formation requirements of the 11-16 runs (formation328 ×5, tc02, tc02_b, tc02_c)
CHOICES = {
    "assemble_delay_reacquire": SkeletonChoice(
        applicable=True, assemble=True, search_delay_s=10, lost_hold=True,
        on_timeout="resume", reacquire_timeout_s=30, leader_role="leader",
        member_role="wingman", implements={"rq_01": ["leader.ASSEMBLE"],
                                           "rq_02": ["leader.ADVANCE", "leader.SEARCH_ADVANCE"]},
    ),
    "share_withdraw": SkeletonChoice(
        applicable=True, lost_hold=True, desired_range_m=3000, withdraw="stop",
        withdraw_threshold_m=2000, share_target=True, leader_role="leader", member_role="member",
        implements={"tc_02_s2": ["leader.STANDOFF_TRACK"], "tc_02_s3": ["leader.SHUTDOWN"]},
    ),
    "withdraw_order": SkeletonChoice(
        applicable=True, lost_hold=True, desired_range_m=3000, withdraw="stop",
        withdraw_threshold_m=2000, stop_events=["withdraw_order"], leader_role="leader",
        member_role="member",
    ),
    "advance_to_standoff": SkeletonChoice(
        applicable=True, desired_range_m=3000, withdraw="stop", withdraw_threshold_m=2000,
        leader_role="leader", member_role="member",
    ),
}  # fmt: skip


def test_the_choice_sets_the_modules_and_names_the_roles():
    s = to_spec(CHOICES["assemble_delay_reacquire"], EVENTS)
    assert (s.skeleton, s.assemble, s.search_delay_s, s.names) == (
        "K3", True, 10, {"leader": "leader", "member": "wingman"}
    )  # fmt: skip
    text, notes, _ = build(
        CHOICES["assemble_delay_reacquire"], events=EVENTS, clauses=["rq_01", "rq_02"]
    )
    leader = re.search(r"```mermaid leader\n(.*?)```", text, re.S).group(1)
    assert "ASSEMBLE --> ADVANCE : column_ready" in leader
    assert "ADVANCE --> SEARCH_ADVANCE : search_delay_up" in leader
    assert "```python wingman" in text and "```python member" not in text
    tree = re.search(r"def ADVANCE\(\):\n(.*?)\n\n", text, re.S).group(1)
    assert "follow_route" in tree and "search" not in tree  # the search waits for the timer
    model = yaml.safe_load(re.search(r"```yaml model\n(.*?)```", text, re.S).group(1))
    assert model["events"]["search_delay_up"] == {
        "after": "10 s", "description": "前出到时，开始搜索"
    }  # fmt: skip
    assert model["implements"]["rq_02"] == ["leader.ADVANCE", "leader.SEARCH_ADVANCE"]
    ver = yaml.safe_load(re.search(r"```yaml verification\n(.*?)```", text, re.S).group(1))
    assert {t["given"]["role"] for t in ver["tests"]} == {"leader", "wingman"}
    assert all(set(sc["vehicles"]) == {"leader", "wingman"} for sc in ver["scenarios"])
    assert any("保持距离" in n and "待复核" in n for n in notes)  # the range was not stated


def test_bad_implements_and_events_are_dropped_with_a_note():
    choice = CHOICES["withdraw_order"].model_copy(
        update={"implements": {"x9": ["leader.SEARCH_ADVANCE"], "rq_01": ["leader.FLY"]},
                "stop_events": ["withdraw_order", "nope"]}
    )  # fmt: skip
    text, notes, _ = build(choice, events=EVENTS, clauses=["rq_01"])
    assert "implements:" not in text
    assert any("x9" in n for n in notes) and any("leader.FLY" in n for n in notes)
    assert "* --> SHUTDOWN : withdraw_order" in text and "nope" not in text
    assert "t_leader_withdraw_order" in text


def test_share_target_publishes_while_deploying_and_tracking():
    text, _, _ = build(CHOICES["share_withdraw"], events=EVENTS)
    for st in ("CONTACT_DEPLOY", "STANDOFF_TRACK"):
        body = re.search(rf"def {st}\(\):\n(.*?)\n\n", text, re.S).group(1)
        assert "leader_publish_shared_target" in body and 'parallel("all")' in body
    assert "communication_target_sharing" in text
    assert "withdraw_threshold_m:" in text and "user_specified" in text


def _capability_set() -> bool:
    try:
        from afsim_research_agent.ir.v2.contracts import Contracts

        Contracts.load(None)
    except Exception:  # noqa: BLE001 - the capability set is not on this machine
        return False
    return True


@pytest.mark.skipif(not _capability_set(), reason="needs the capability set")
@pytest.mark.parametrize("name", sorted(CHOICES))
def test_each_formation_choice_builds_a_valid_model(tmp_path: Path, name: str) -> None:
    from afsim_research_agent.falcon.v3.draft import compile_package

    text, _, request = build(CHOICES[name], events=EVENTS)
    used = {"cancelled", *CHOICES[name].stop_events}
    request["scenario_events"] = {k: v for k, v in EVENTS.items() if k in used}
    (tmp_path / "tactic_sheet.md").write_text(text, encoding="utf-8")
    (tmp_path / "falcon_request.yaml").write_text(
        yaml.safe_dump(request, allow_unicode=True), encoding="utf-8"
    )
    res = compile_package(
        tmp_path / "tactic_sheet.md", tmp_path / "falcon_request.yaml", tmp_path / "pkg"
    )
    c = res.check
    detail = (
        [p.render() for p in c.problems][:6]
        + [(x["id"], x.get("render", "")[:600]) for x in c.l3 if not x["passed"]]
        + [t for t in c.l2 if not t["passed"]][:4]
    )
    assert res.status == "draft_valid", detail


def test_run_draft3_drafts_from_the_skeleton_without_structure_or_control(
    set_root: Path, tmp_path: Path
):
    import asyncio

    _stub_langchain()
    from afsim_research_agent.falcon.llm import ScriptedModel
    from afsim_research_agent.falcon.v3.draft import run_draft3

    request = tmp_path / "req.yaml"
    request.write_text(
        yaml.safe_dump(
            {
                "requirement": "三车编队集结后沿路线搜索前进，发现目标后展开并保持距离跟踪。",
                "model_id": "sk_demo",
                "roles": {
                    "leader": {"capability_class": "ground_vehicle", "count": 1},
                    "wingman": {"capability_class": "ground_vehicle", "count": 2},
                },
                "capability_set": str(set_root),
                "drafting": "skeleton",
                "extract_obligations": False,
                "scenario_events": {"cancelled": "上级结束任务"},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    choice = CHOICES["assemble_delay_reacquire"].model_dump() | {"leader_role": "lead"}
    script = {
        "requirements": [
            {"clauses": [{"text": "三车编队集结后沿路线搜索前进", "kind": "behavior"}]}
        ],
        "v3:skeleton": [choice],
    }
    model = ScriptedModel(script)
    result = asyncio.run(run_draft3(request, model, out_dir=tmp_path / "out"))
    stages = [s for s, _ in model.calls]
    assert "v3:skeleton" in stages and "v3:structure" not in stages and "v3:control" not in stages
    pkg = result.package_dir
    sheet = (pkg / "tactic_sheet.md").read_text(encoding="utf-8")
    assert "```mermaid leader" in sheet and "```python wingman" in sheet  # unknown name → role
    assert "assemble: true" in (pkg / "skeleton_choice.yaml").read_text(encoding="utf-8")


def test_stop_events_leave_the_timer_tests_of_later_roles_alone():
    text, _, _ = build(CHOICES["withdraw_order"], events=EVENTS)
    ver = yaml.safe_load(re.search(r"```yaml verification\n(.*?)```", text, re.S).group(1))
    t = next(t for t in ver["tests"] if t["id"] == "t_member_03_reacquire_search")
    assert t["given"]["events"] == ["reacquire_time_up"] and t["given"]["time_in_state_s"] == 31


def test_the_skeleton_adds_the_advance_route_and_drops_an_unknown_start_order(
    set_root: Path, tmp_path: Path
):
    import asyncio

    _stub_langchain()
    from afsim_research_agent.falcon.llm import ScriptedModel
    from afsim_research_agent.falcon.v3.draft import run_draft3

    request = tmp_path / "req.yaml"
    request.write_text(
        yaml.safe_dump(
            {
                "requirement": "三车编队集结后原地展开警戒。",
                "model_id": "sk_route",
                "roles": {
                    "leader": {"capability_class": "ground_vehicle", "count": 1},
                    "wingman": {"capability_class": "ground_vehicle", "count": 2},
                },
                "capability_set": str(set_root),
                "drafting": "skeleton",
                "extract_obligations": False,
                "scenario_events": {"cancelled": "上级结束任务"},
                "scenario_interface": {"points": {"post": {"anchor": "own_start"}}},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    choice = CHOICES["assemble_delay_reacquire"].model_dump() | {"wait_start": True}
    script = {
        "requirements": [{"clauses": [{"text": "三车编队集结后原地展开警戒", "kind": "behavior"}]}],
        "v3:skeleton": [choice],
    }
    result = asyncio.run(run_draft3(request, ScriptedModel(script), out_dir=tmp_path / "out"))
    pkg = result.package_dir
    req = yaml.safe_load((pkg / "falcon_request.yaml").read_text(encoding="utf-8"))
    assert req["scenario_interface"]["routes"]["approach"]["end"] == "ahead"
    sheet = (pkg / "tactic_sheet.md").read_text(encoding="utf-8")
    assert "WAIT_START" not in sheet and "start_order" not in sheet


def test_a_strike_choice_builds_the_strike_skeleton_and_maps_tactic_role_names():
    choice = SkeletonChoice(
        applicable=True, family="strike", reattack=True, cease_order="nope",
        leader_role="leader", member_role="member",
        implements={"rq_01": ["lead_vehicle.ALLOCATING"], "rq_02": ["wingman_A.FIRING"],
                    "rq_03": ["member.SEARCH"]},
    )  # fmt: skip
    text, notes, request = build(choice, events=EVENTS, clauses=["rq_01", "rq_02", "rq_03"])
    assert "```mermaid leader" in text and "def ALLOCATING():" in text
    model = yaml.safe_load(re.search(r"```yaml model\n(.*?)```", text, re.S).group(1))
    assert model["implements"] == {"rq_01": ["leader.ALLOCATING"], "rq_02": ["member.FIRING"]}
    assert "threat_allocation" in model["slices"]
    assert any("nope" in n for n in notes) and any("member.SEARCH" in n for n in notes)
    assert {o["id"] for o in request["obligations"]} >= {"allocate_before_fire", "cease_on_cancel"}


def test_formation_implements_with_tactic_role_names_are_mapped_not_dropped():
    choice = CHOICES["withdraw_order"].model_copy(
        update={"implements": {"tc_02_s1": ["lead_vehicle.STANDOFF_TRACK", "wingman_A.TRACK"]}}
    )
    text, notes, _ = build(choice, events=EVENTS, clauses=["tc_02_s1"])
    model = yaml.safe_load(re.search(r"```yaml model\n(.*?)```", text, re.S).group(1))
    assert model["implements"] == {"tc_02_s1": ["leader.STANDOFF_TRACK", "member.TRACK"]}


def test_a_strike_role_name_that_is_not_an_id_falls_back_to_the_skeleton_role(
    set_root: Path, tmp_path: Path
):
    import asyncio

    _stub_langchain()
    from afsim_research_agent.falcon.llm import ScriptedModel
    from afsim_research_agent.falcon.v3.draft import run_draft3

    request = tmp_path / "req.yaml"
    request.write_text(
        yaml.safe_dump(
            {
                "requirement": "排长车分配目标，成员车交战。",
                "model_id": "sk_strike",
                "capability_set": str(set_root),
                "drafting": "skeleton",
                "extract_obligations": False,
                "scenario_events": {"cancelled": "上级结束任务"},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    choice = SkeletonChoice(
        applicable=True, family="strike", leader_role="leader", member_role="成员车"
    ).model_dump()
    script = {
        "requirements": [{"clauses": [{"text": "排长车分配目标，成员车交战", "kind": "behavior"}]}],
        "v3:skeleton": [choice],
    }
    result = asyncio.run(run_draft3(request, ScriptedModel(script), out_dir=tmp_path / "out"))
    sheet = (result.package_dir / "tactic_sheet.md").read_text(encoding="utf-8")
    assert "```python member" in sheet and "成员车_" not in sheet


def test_clauses_the_skeleton_does_not_carry_are_left_out_and_declared(
    set_root: Path, tmp_path: Path
):
    import asyncio

    _stub_langchain()
    from afsim_research_agent.falcon.llm import ScriptedModel
    from afsim_research_agent.falcon.v3.draft import run_draft3

    request = tmp_path / "req.yaml"
    request.write_text(
        yaml.safe_dump(
            {
                "requirement": "三车编队集结后沿路线搜索前进；通信中断时各车独立行动。",
                "model_id": "sk_left",
                "roles": {
                    "leader": {"capability_class": "ground_vehicle", "count": 1},
                    "wingman": {"capability_class": "ground_vehicle", "count": 2},
                },
                "capability_set": str(set_root),
                "drafting": "skeleton",
                "extract_obligations": False,
                "scenario_events": {"cancelled": "上级结束任务"},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    choice = CHOICES["assemble_delay_reacquire"].model_dump() | {
        "implements": {"rq_01": ["leader.ASSEMBLE"]}
    }
    script = {
        "requirements": [
            {"clauses": [{"text": "三车编队集结后沿路线搜索前进", "kind": "behavior"},
                         {"text": "通信中断时各车独立行动", "kind": "behavior"}]}
        ],
        "v3:skeleton": [choice],
    }  # fmt: skip
    result = asyncio.run(run_draft3(request, ScriptedModel(script), out_dir=tmp_path / "out"))
    assert not any("IMPL_MISSING" in p.render() for p in result.check.problems)
    doc = yaml.safe_load((result.package_dir / "skeleton_choice.yaml").read_text(encoding="utf-8"))
    assert [x["clause"] for x in doc["left_out"]] == ["rq_02"]
