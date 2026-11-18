"""Crow → Falcon v3 bridge: the user's tactic selection becomes the v3 drafting input."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from afsim_research_agent.falcon.v3 import crow_bridge as B
from afsim_research_agent.falcon.v3.draft import V3Request


def _step(phase, do, until):
    return SimpleNamespace(phase=phase, do=do, until=until)


def _fake_tactic(profile=None):
    card = SimpleNamespace(
        id="tc_01",
        name="集中分配打击",
        narrative="排长车评估威胁并分配目标，成员车按任务交战。",
        roles=[
            SimpleNamespace(role="排长车", members=[]),
            SimpleNamespace(role="成员车", members=["2号", "3号"]),
        ],
        geometry=[SimpleNamespace(formation="横队", when="接敌", spacing="50 米")],
        exceptions=[SimpleNamespace(situation="任务过期", then="停火保持")],
        assumptions=["开火上限 3 次（参考值）"],
        capability_needs=[
            SimpleNamespace(maps_to=["threat_allocation.broadcast_assignments"]),
            SimpleNamespace(maps_to=["x:engagement.fire_selected_weapon", "unknown.thing"]),
        ],
    )
    steps = [
        _step("评估", "评估威胁并分配目标", "下发任务"),
        _step("交战", "瞄准开火", "目标被摧毁"),
    ]
    part = SimpleNamespace(card=card, phases=None, whole=True, steps=lambda: list(enumerate(steps)))
    return SimpleNamespace(
        parts=[part], user_notes="优先打近的", profile=profile, source_label="tactics#tc_01"
    )


def _contracts():
    actions = {
        "threat_allocation.broadcast_assignments": SimpleNamespace(slice="threat_allocation"),
        "engagement.fire_selected_weapon": SimpleNamespace(slice="engagement"),
    }
    return SimpleNamespace(actions=actions, tactics={}, slices={})


def test_the_tactic_in_words_names_every_clause() -> None:
    text = B.tactic_text(_fake_tactic())
    for piece in ("tc_01_s1 【评估】", "tc_01_s2 【交战】", "tc_01_g1 队形：横队", "tc_01_x1 例外"):
        assert piece in text
    assert "不要另行设计" in text and "用户选择备注：优先打近的" in text
    assert "成员车（2号、3号）" in text


def test_roles_follow_the_profile_and_the_role_map() -> None:
    forces = [
        SimpleNamespace(role_hint="排长车", count=1, platform_class="ground_vehicle"),
        SimpleNamespace(role_hint="成员车", count=2, platform_class="ground_vehicle"),
        SimpleNamespace(role_hint="徒步分队", count=1, platform_class=None),
    ]
    tactic = _fake_tactic(SimpleNamespace(own_forces=forces))
    roles, notes = B.derive_roles(tactic, {})
    assert set(roles) == {"leader", "member", "role_3"} and roles["member"].count == 2
    roles, _ = B.derive_roles(tactic, {"排长车": "lead", "成员": "wing"})
    assert {"lead", "wing"} <= set(roles)
    # the profile's English role hints
    english = [
        SimpleNamespace(role_hint="platoon_leader_vehicle", count=1, platform_class=None),
        SimpleNamespace(role_hint="member_vehicles", count=3, platform_class=None),
    ]
    roles, _ = B.derive_roles(_fake_tactic(SimpleNamespace(own_forces=english)), {})
    assert set(roles) == {"leader", "member"} and roles["member"].count == 3
    # no profile: the cards' roles (members counted)
    roles, _ = B.derive_roles(_fake_tactic(), {})
    assert roles["member"].count == 2 and roles["leader"].count == 1


def test_slices_come_from_crow_capability_needs() -> None:
    assert B.derive_slices(_fake_tactic(), _contracts()) == ["threat_allocation", "engagement"]


def test_a_request_needs_a_requirement_or_a_selection(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        V3Request.model_validate({"model_id": "x"})
    req = V3Request.model_validate(
        {"model_id": "x", "tactic_selection": "run/tactics", "role_map": {"排长车": "leader"}}
    )
    assert B.resolve_selection(req, tmp_path / "r.yaml") == tmp_path / "run/tactics"
    doc = B.make_request(
        tmp_path / "run" / "tactics", "strike_crow", role_map={"排长车": "leader"}, base=tmp_path
    )
    assert doc["tactic_selection"] == "run/tactics" and doc["role_map"] == {"排长车": "leader"}
    V3Request.model_validate(doc)


def test_apply_selection_folds_the_tactic_into_the_request(tmp_path, monkeypatch) -> None:
    import afsim_research_agent.falcon.tactic as T

    monkeypatch.setattr(T, "load_selection", lambda path: _fake_tactic())
    tactic = _fake_tactic()
    tactic.requirement = lambda stated: stated or "排长车与成员车协同打击"
    tactic.reference_assumptions = lambda: [("tc_01", "开火上限 3 次（参考值）")]
    monkeypatch.setattr(T, "load_selection", lambda path: tactic)
    req = V3Request.model_validate({"model_id": "strike_crow", "tactic_selection": "t"})
    out, got, notes = B.apply_selection(req, tmp_path / "r.yaml", _contracts())
    assert got is tactic
    assert out.requirement.startswith("排长车与成员车协同打击") and "tc_01_s2" in out.requirement
    assert set(out.roles) == {"leader", "member"} and out.slices == [
        "threat_allocation",
        "engagement",
    ]
    assert out.display_name == "集中分配打击"
    assert any("参考值" in n for n in notes) and any("Crow" in n for n in notes)


def _need(need, source, status, maps_to, why="", notes=None):
    return SimpleNamespace(
        need=need,
        source=source,
        status=status,
        label=status,
        maps_to=maps_to,
        why=why,
        notes=notes or [],
    )


def _mapped_tactic():
    tactic = _fake_tactic()
    tactic.parts[0].card.capability_needs = [
        _need(
            "向成员车广播射击指令或自由交战模式",
            "steps[0].until",
            "simplified",
            ["threat_allocation.broadcast_assignments"],
            notes=["理想通信"],
        ),
        _need("无人机前出侦察", "steps[1].do", "develop", ["uav.launch"]),
    ]
    return tactic


GAPS = {
    "broadcast_free_fire_mode": {"description": "排长车向成员车广播自由交战模式指令"},
    "uav_scout": {"description": "无人机前出侦察并回传目标"},
}


def test_capability_map_matches_the_catalog_first() -> None:
    entries = B.capability_map(_mapped_tactic(), _contracts())
    assert [e["missing"] for e in entries] == [False, True]
    assert [e["elements"] for e in entries] == [["tc_01_s1"], ["tc_01_s2"]]
    assert B.element_ids("tc_02", "steps[0].do, steps[2].until") == ["tc_02_s1", "tc_02_s3"]
    assert B.deferred_elements(entries) == {"tc_01_s2"}
    text = B.capability_text(entries, "skip")
    assert "threat_allocation.broadcast_assignments" in text and "无人机前出侦察" in text
    assert "本次忽略" in text and "不得有 gaps" in text
    assert "写成 gaps" in B.capability_text(entries, "wait")
    # only the phases taken count
    tactic = _mapped_tactic()
    tactic.parts[0].whole, tactic.parts[0].phases = False, ["交战"]
    tactic.parts[0].steps = lambda: [(1, _step("交战", "瞄准开火", "目标被摧毁"))]
    assert [e["need"] for e in B.capability_map(tactic, _contracts())] == ["无人机前出侦察"]


def test_missing_capabilities_skip_or_wait() -> None:
    entries = B.capability_map(_mapped_tactic(), _contracts())
    # skip: no gaps at all; the mapped one names the actions, the missing one is left out
    skip = dict(B.gap_problems(GAPS, entries, mode="skip"))
    assert set(skip) == {"broadcast_free_fire_mode", "uav_scout"}
    assert "threat_allocation.broadcast_assignments" in skip["broadcast_free_fire_mode"]
    assert "本次忽略" in skip["uav_scout"]
    # wait: the missing one stays as a gap for capability development
    wait = dict(B.gap_problems(GAPS, entries, mode="wait"))
    assert set(wait) == {"broadcast_free_fire_mode"}


def test_skipped_elements_leave_the_clauses_and_are_recorded(tmp_path: Path) -> None:
    tactic = _mapped_tactic()
    clause = lambda cid: SimpleNamespace(id=cid, text=cid, kind="behavior", evidence=[])  # noqa: E731
    tactic.clauses = lambda: [clause("tc_01_s1"), clause("tc_01_s2"), clause("tc_01_x1")]
    ids = [c["id"] for c in B.tactic_clauses(tactic, skip={"tc_01_s2"})]
    assert ids == ["tc_01_s1", "tc_01_x1"]
    assert "暂缺能力，本次忽略" in B.tactic_text(tactic, deferred={"tc_01_s2"})
    entries = B.capability_map(tactic, _contracts())
    tactic.selected_document = lambda: "selected: []\n"
    tactic.crow_gaps_document = lambda: ""
    B.write_crow_files(tmp_path, tactic, entries, "skip")
    doc = yaml.safe_load((tmp_path / B.DEFERRED_FILE).read_text(encoding="utf-8"))
    assert doc["mode"] == "skip" and doc["left_out"] == ["tc_01_s2"]
    assert doc["needs"][0]["need"] == "无人机前出侦察"
    assert (
        B.make_request(tmp_path, "x", missing_capabilities="wait")["missing_capabilities"] == "wait"
    )
    assert "drafting" not in B.make_request(tmp_path, "x")
    assert B.make_request(tmp_path, "x", drafting="skeleton")["drafting"] == "skeleton"


def test_a_real_crow_selection_loads(tmp_path: Path) -> None:
    """With the Crow package importable (the project environment): a tactics directory as
    crow.propose / crow.select write it becomes the v3 input."""
    pm = pytest.importorskip("afsim_research_agent.crow.proposal_models")
    sel = pytest.importorskip("afsim_research_agent.crow.select")
    from afsim_research_agent.ir.v2.contracts import Contracts

    card = pm.TacticCard(
        id="tc_01",
        name="集中分配打击",
        summary="排长分配目标，成员交战",
        narrative="排长车评估威胁并给每辆成员车分配一个目标，成员车按任务瞄准开火。" * 2,
        roles=[
            pm.RoleAssignment(role="排长车"),
            pm.RoleAssignment(role="成员车", members=["2号", "3号"]),
        ],
        steps=[
            pm.Step(phase="分配", do="评估威胁并分配目标", until="任务下发"),
            pm.Step(phase="交战", do="瞄准开火", until="目标被摧毁或开火三次"),
        ],
        exceptions=[pm.ExceptionItem(situation="任务过期", then="停火保持")],
    )
    result = pm.ProposalResult(
        requirement="排长车与两辆成员车协同打击",
        max_candidates=3,
        max_innovative=0,
        candidates=[card],
    )
    tactics = tmp_path / "run" / "tactics"
    tactics.mkdir(parents=True)
    (tactics / "tactic_candidates.yaml").write_text(
        yaml.safe_dump(result.model_dump(mode="json"), allow_unicode=True), encoding="utf-8"
    )
    doc, _ = sel.build_selection(result, ["tc_01"], rejected=[], note="", by="tester")
    (tactics / "tactic_selection.yaml").write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    req_path = tmp_path / "run" / "falcon3_strike_crow.yaml"
    req_path.write_text(
        yaml.safe_dump(
            B.make_request(tactics, "strike_crow", base=req_path.parent), allow_unicode=True
        ),
        encoding="utf-8",
    )
    request = V3Request.load(req_path)
    out, tactic, _notes = B.apply_selection(request, req_path, Contracts.load())
    assert "tc_01_s1 【分配】" in out.requirement and set(out.roles) == {"leader", "member"}
    clauses = B.tactic_clauses(tactic)
    assert [c["id"] for c in clauses] == ["tc_01_s1", "tc_01_s2", "tc_01_x1"]
    B.write_crow_files(tmp_path, tactic)
    assert (tmp_path / "selected_tactics.yaml").is_file()
    assert any("选定战术" in line for line in B.review_section(tactic))
