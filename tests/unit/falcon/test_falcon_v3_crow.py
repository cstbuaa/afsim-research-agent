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
