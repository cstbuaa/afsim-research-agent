"""F2–F6 and confirmation, end to end with a scripted model."""

from __future__ import annotations

import asyncio
import json

import pytest
import yaml
from falcon_fixtures import CLAUSES, crow_result, ev, gold_script, write_request

from afsim_research_agent.falcon.__main__ import main as falcon_main
from afsim_research_agent.falcon.confirm import apply_confirmations, package_status
from afsim_research_agent.falcon.errors import FalconError
from afsim_research_agent.falcon.evaluate import evaluate_package
from afsim_research_agent.falcon.llm import ModelRunner, ScriptedModel
from afsim_research_agent.falcon.pipeline import run_draft
from afsim_research_agent.falcon.requirements import decompose
from afsim_research_agent.falcon.yamlpatch import set_key
from afsim_research_agent.ir.loader import load_spec
from afsim_research_agent.ir.pipeline import build_package


def _draft(tmp_path, **kw):
    request = write_request(tmp_path, crow_result())
    model = ScriptedModel(gold_script(**kw))
    result = asyncio.run(run_draft(request, model))
    return result, model


def _spec(pkg):
    return load_spec(pkg / "behavior_spec.yaml").data


@pytest.fixture(scope="module")
def drafted(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("falcon")
    result, model = _draft(tmp)
    return result, model


# --------------------------------------------------------------------------- F2


def test_requirements_keep_only_verbatim_clauses_and_stated_numbers():
    from falcon_fixtures import REQUIREMENT

    from afsim_research_agent.falcon.models import FalconRequest

    request = FalconRequest(
        requirement=REQUIREMENT,
        model_id="x",
        crow_result="c.json",
        user_parameters={"travel_speed": {"value": 20, "unit": "km/h"}},
    )
    answers = [
        {
            "clauses": [
                *CLAUSES,
                {"text": "保持五十米间距并加速", "kind": "behavior"},  # not verbatim
                {
                    "text": "与敌保持安全距离",
                    "kind": "constraint",
                    "user_values": [{"name": "standoff", "value": 800, "unit": "m"}],  # invented
                },
            ]
        }
    ]
    model = ScriptedModel({"requirements": answers})
    clauses, values, notes = asyncio.run(decompose(request, ModelRunner(model)))
    assert len(model.calls) == 2  # retried once because of the non-verbatim clause
    assert [c.id for c in clauses][:3] == ["rq_01", "rq_02", "rq_03"]
    assert all(c.text in REQUIREMENT for c in clauses)
    names = {(v.name, v.origin) for v in values}
    assert ("column_interval", "rq_03") in names and ("travel_speed", "falcon_request") in names
    assert not any(v.name == "standoff" for v in values)
    assert any("不在条款原文中" in n for n in notes)


# --------------------------------------------------------------------------- draft


def test_draft_writes_package_and_passes_l1_l2(drafted):
    result, _ = drafted
    pkg = result.package_dir
    assert result.status == "draft_valid", result.problems
    for name in (
        "falcon_request.yaml",
        "evidence_package.json",
        "behavior_spec.yaml",
        "confirmations.yaml",
        "falcon_review.md",
        "falcon_trace.json",
        "validation_report.json",
    ):
        assert (pkg / name).exists(), name
    assert not pkg.with_name(pkg.name + ".partial").exists()
    report = json.loads((pkg / "validation_report.json").read_text(encoding="utf-8"))
    assert report["l2"]["passed"]


def test_basis_rules_enforced(drafted):
    result, _ = drafted
    params = _spec(result.package_dir)["parameters"]
    basis = {name: p["basis"] for name, p in params.items()}
    assert basis["wedge_depth"] == {
        "status": "explicit",
        "evidence": [ev("wedge", 1)],
        "confirmation": "pending",
    }
    assert basis["wedge_half_width"]["status"] == "inferred"
    assert "不在证据范围" in basis["wedge_half_width"]["rationale"]
    assert basis["column_interval"]["status"] == "user_specified"
    assert basis["column_interval"]["confirmation"] == "confirmed"
    assert basis["station_tolerance"]["status"] == "unknown"
    assert "evidence" not in basis["station_tolerance"]
    assert basis["travel_speed_open"]["status"] == "unknown"
    assert basis["travel_speed_urban"]["status"] == "unknown"
    # nothing derived from sources is pre-confirmed
    for name, b in basis.items():
        if b["status"] != "user_specified":
            assert b["confirmation"] == "pending", name
    # every cited evidence id exists and its source is listed as a reference
    pkg = json.loads((result.package_dir / "evidence_package.json").read_text(encoding="utf-8"))
    ids = {e["evidence_id"] for e in pkg["evidence"]}
    spec = _spec(result.package_dir)
    cited = {e for p in params.values() for e in p["basis"].get("evidence", [])}
    assert cited <= ids
    refs = {r["source_id"]: r for r in spec["provenance"]["references"]}
    assert refs["local_notes"]["classification"] == "user_private"
    assert spec["provenance"]["evidence_package"]["content_hash"] == pkg["content_hash"]


def test_review_materials(drafted):
    result, _ = drafted
    pkg = result.package_dir
    conf = yaml.safe_load((pkg / "confirmations.yaml").read_text(encoding="utf-8"))
    items = conf["items"]
    categories = [i["category"] for i in items]
    order = ["待确认参数", "推断规则", "冲突证据", "假设"]
    assert categories == sorted(categories, key=order.index)
    assert all(i["decision"] == "pending" for i in items)
    targets = {i["target"] for i in items}
    assert "parameters.wedge_depth" in targets and "parameters.column_interval" not in targets
    assert any(i["kind"] == "assumption" for i in items)
    standoff = next(i for i in items if i["target"] == "parameters.standoff_distance")
    assert standoff["evidence"][0]["source"] == "本地资料"
    review = (pkg / "falcon_review.md").read_text(encoding="utf-8")
    assert "条款覆盖" in review and "⚠ 未覆盖" not in review
    assert "value_out_of_range" in review and "user_specified_unbacked" in review
    trace = json.loads((pkg / "falcon_trace.json").read_text(encoding="utf-8"))
    assert trace["status"] == "draft_valid" and not trace["uncovered_clauses"]
    assert all("prompt" not in call for call in trace["calls"])
    assert {c["stage"] for c in trace["calls"]} >= {"requirements", "skeleton", "section:control"}
    assert "not.a.concept" not in _spec(pkg)["model"]["domain_concepts"]


def test_repair_regenerates_only_failing_section(tmp_path):
    result, model = _draft(tmp_path, broken_control_first=True)
    assert result.status == "draft_valid", result.problems
    stages = [stage for stage, _ in model.calls]
    assert stages.count("section:control") == 2
    for section in ("parameters", "structure", "conditions", "behaviors", "verification"):
        assert stages.count(f"section:{section}") == 1
    feedback = [prompt for stage, prompt in model.calls if stage == "section:control"][1]
    assert "leader_contact_tree" in feedback
    trace = json.loads((result.package_dir / "falcon_trace.json").read_text(encoding="utf-8"))
    assert trace["repairs"][0]["sections"] == ["control"]


def test_repair_gives_up_and_marks_invalid(tmp_path):
    script = gold_script()
    from falcon_fixtures import control_answer

    script["section:control"] = [control_answer(drop_tree="leader_contact_tree")]
    request = write_request(tmp_path, crow_result())
    result = asyncio.run(run_draft(request, ScriptedModel(script), max_repair=2))
    assert result.status == "draft_invalid" and result.exit_code == 4
    assert result.package_dir.exists()
    review = (result.package_dir / "falcon_review.md").read_text(encoding="utf-8")
    assert "未解决的错误" in review


def test_refusal_leaves_nothing_and_crash_leaves_only_a_record(tmp_path):
    request = write_request(tmp_path, crow_result(ready=False))
    with pytest.raises(FalconError):
        asyncio.run(run_draft(request, ScriptedModel(gold_script())))
    assert not (tmp_path / "models").exists()
    request = write_request(tmp_path, crow_result())
    script = gold_script()
    script["section:behaviors"] = [RuntimeError("model service down")]
    with pytest.raises(RuntimeError):
        asyncio.run(run_draft(request, ScriptedModel(script)))
    left = sorted(p.name for p in (tmp_path / "models").iterdir())
    assert left == ["armor_platoon_v2.failure.json"]
    record = json.loads((tmp_path / "models" / left[0]).read_text(encoding="utf-8"))
    assert "model service down" in record["error"] and record["last_stage"] == "section:conditions"


# --------------------------------------------------------------------------- confirm


def _decide(pkg, decide):
    path = pkg / "confirmations.yaml"
    header = path.read_text(encoding="utf-8").split("items:")[0]
    conf = yaml.safe_load(path.read_text(encoding="utf-8"))
    for item in conf["items"]:
        decide(item)
    path.write_text(
        header + yaml.safe_dump(conf, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def test_confirm_then_build_passes(tmp_path):
    result, _ = _draft(tmp_path)
    pkg = result.package_dir
    spec_path = pkg / "behavior_spec.yaml"
    spec_path.write_text(
        spec_path.read_text(encoding="utf-8").replace(
            "parameters:\n", "parameters:\n  # reviewer note: keep this comment\n", 1
        ),
        encoding="utf-8",
    )
    assert not package_status(pkg)["ready_to_build"]

    def decide(item):
        if item["target"] == "parameters.station_tolerance":
            item["decision"] = "override"
            item["override"] = {"value": 35, "unit": "m"}
            item["note"] = "按排内经验"
        else:
            item["decision"] = "confirm"

    _decide(pkg, decide)
    assert falcon_main(["confirm", str(pkg), "--by", "reviewer_a"]) == 0
    text = spec_path.read_text(encoding="utf-8")
    assert "# reviewer note: keep this comment" in text
    params = _spec(pkg)["parameters"]
    assert params["station_tolerance"]["value"] == 35
    assert params["station_tolerance"]["basis"]["status"] == "user_specified"
    assert "用户覆盖" in params["station_tolerance"]["basis"]["rationale"]
    assert params["wedge_depth"]["basis"]["confirmed_by"] == "reviewer_a"
    status = package_status(pkg)
    assert status["ready_to_build"], status["blocking"]
    build = build_package(pkg)
    assert build.status == "passed", [
        d.render() for d in build.diagnostics if d.severity.value == "error"
    ]
    # applying again changes nothing
    report = apply_confirmations(pkg, "reviewer_a")
    assert not report.applied and not report.skipped


def test_confirm_reject_and_hand_edit(tmp_path):
    result, _ = _draft(tmp_path)
    pkg = result.package_dir
    spec_path = pkg / "behavior_spec.yaml"
    text = spec_path.read_text(encoding="utf-8")
    # the user changes a value by hand after the list was generated
    text = set_key(text, "parameters.bound_distance", "value", 250)
    spec_path.write_text(text, encoding="utf-8")

    def decide(item):
        if item["target"] == "parameters.contact_range":
            item["decision"] = "reject"
        elif item["target"] == "parameters.bound_distance":
            item["decision"] = "confirm"
        elif item["kind"] != "parameter" and item["kind"] != "assumption":
            item["decision"] = "override"  # not allowed for rules

    _decide(pkg, decide)
    report = apply_confirmations(pkg, "reviewer_b")
    skipped = dict(report.skipped)
    assert "手工修改" in skipped["parameters.bound_distance"]
    assert any("只有参数可以 override" in reason for reason in skipped.values())
    params = _spec(pkg)["parameters"]
    assert params["contact_range"]["basis"]["confirmation"] == "rejected"
    assert any("contact_range" in b for b in report.blocking)


def test_cli_draft_refusal_exit_code(tmp_path, capsys):
    request = write_request(tmp_path, crow_result(ready=False))
    assert falcon_main(["draft", str(request)]) == 2
    assert "FAL001" in capsys.readouterr().err


def test_evaluate_metrics_before_and_after_build(tmp_path, capsys):
    from falcon_fixtures import EXAMPLE

    result, _ = _draft(tmp_path)
    pkg = result.package_dir
    assert falcon_main(["evaluate", str(pkg), "--reference", str(EXAMPLE.parent)]) == 0
    m = json.loads((pkg / "falcon_eval.json").read_text(encoding="utf-8"))
    assert m["draft_status"] == "draft_valid" and m["l1_l2_passed_within_3_rounds"]
    assert m["clause_coverage"] == 1.0 and m["user_specified_downgrades"] == 1
    assert m["l2_passed"] == m["l2_total"] > 0 and "build_status" not in m
    assert m["vs_reference"]["patterns"]["only_draft"] == []
    assert "条款覆盖率" in capsys.readouterr().out
    _decide(pkg, lambda item: item.update(decision="confirm"))
    apply_confirmations(pkg, "reviewer_c")
    assert build_package(pkg).status == "passed"
    m = evaluate_package(pkg)
    assert m["build_status"] == "passed" and all(s["passed"] for s in m["l3"].values())


# --------------------------------------------------------------------------- online-eval fixes


def test_sections_use_json_mode_and_retry_quotes_errors(monkeypatch):
    from afsim_research_agent.falcon.llm import structured_method
    from afsim_research_agent.falcon.models import ConditionsSection

    assert structured_method("section:conditions") == "json_mode"
    assert structured_method("evidence.extract") == "json_schema"
    monkeypatch.setenv("FALCON_STRUCTURED_METHOD", "json_schema")
    assert structured_method("section:conditions") == "json_schema"

    bad = {"conditions": [{"id": "c1"}]}  # missing expression
    good = {"conditions": [{"id": "c1", "expression": {"literal": True}}]}
    model = ScriptedModel({"section:conditions": [bad, good]})
    answer = asyncio.run(ModelRunner(model)("section:conditions", "p", ConditionsSection))
    assert answer.conditions[0].expression == {"literal": True}
    retry_prompt = model.calls[1][1]
    assert "conditions.0.expression" in retry_prompt and "Field required" in retry_prompt


def test_later_sections_may_add_parameters(tmp_path):
    from falcon_fixtures import conditions_answer

    script = gold_script()
    cond = conditions_answer()
    cond["new_parameters"] = [
        {
            "name": "contact_range",
            "type": "distance",
            "value": 9,
            "unit": "km",
            "basis": {"status": "explicit"},
        },
        {
            "name": "extra_gap",
            "type": "distance",
            "value": 40,
            "unit": "m",
            "basis": {"status": "unknown"},
        },
    ]
    script["section:conditions"] = [cond]
    request = write_request(tmp_path, crow_result())
    result = asyncio.run(run_draft(request, ScriptedModel(script)))
    params = _spec(result.package_dir)["parameters"]
    assert params["contact_range"]["value"] == 1.5  # first definition (parameters section) wins
    assert params["extra_gap"]["basis"] == {"status": "unknown", "confirmation": "pending"}


def test_section_prompts_list_referenceable_ids(tmp_path):
    result, model = _draft(tmp_path)
    control = next(p for s, p in model.calls if s == "section:control")
    assert '"wingman[2]"' in control and "coord_bounding__tree" in control
    assert '"follow_route_open"' in control  # behaviors defined by the behaviors section
    verification = next(p for s, p in model.calls if s == "section:verification")
    assert '"tree_nodes"' in verification and "leader_open_root" in verification
    assert "只输出一个 JSON 对象" in control


def _verification_with_slips() -> dict:
    """The slips seen in the armor-eval-03 v3 run: 'failed' and 'count'."""
    from falcon_fixtures import gold_script

    good = gold_script()["section:verification"]
    good = good[0] if isinstance(good, list) else good
    data = (
        good.model_dump(mode="json", by_alias=True, exclude_none=True)
        if hasattr(good, "model_dump")
        else json.loads(json.dumps(good))
    )
    data["tests"][0]["expect"]["tree_status"] = "failed"
    data["scenarios"][0]["expect"].append(
        {"id": "e_msg", "observable": "messages_sent", "message": "m", "count": 1}
    )
    return data


def test_mechanical_slips_are_fixed_and_recorded():
    from afsim_research_agent.falcon.models import VerificationSection

    model = ScriptedModel({"section:verification": [_verification_with_slips()]})
    run = ModelRunner(model)
    answer = asyncio.run(run("section:verification", "p", VerificationSection))
    assert answer.tests[0].expect.tree_status == "failure"
    assert answer.scenarios[0].expect[-1].gte == 1
    assert len(model.calls) == 1  # fixed without a retry
    fixes = run.records[-1].fixes
    assert any("'failed' → 'failure'" in f for f in fixes) and any(
        "count → gte" in f for f in fixes
    )


def test_section_failure_salvages_valid_items_instead_of_aborting():
    from afsim_research_agent.falcon.models import VerificationSection

    data = _verification_with_slips()
    data["tests"][1]["given"] = "broken"  # not fixable
    model = ScriptedModel({"section:verification": [data]})
    run = ModelRunner(model)
    answer = asyncio.run(run("section:verification", "p", VerificationSection))
    assert len(model.calls) == 2  # one retry, then salvage
    assert len(answer.tests) == len(data["tests"]) - 1
    assert run.records[-1].salvaged and any("已丢弃 tests[1]" in f for f in run.records[-1].fixes)
    retry_prompt = model.calls[1][1]
    assert "tests.1.given" in retry_prompt  # the retry sees the real error, not a truncated dump


def test_non_section_stage_still_fails_loudly():
    from afsim_research_agent.falcon.errors import FalconError
    from afsim_research_agent.falcon.models import ConditionsSection

    model = ScriptedModel({"skeleton": [{"conditions": "x"}]})
    with pytest.raises(FalconError) as err:
        asyncio.run(ModelRunner(model)("skeleton", "p", ConditionsSection))
    assert err.value.code == "FAL004"


def test_langchain_wrapped_error_is_unwrapped():
    from pydantic import ValidationError

    from afsim_research_agent.falcon.llm import describe_error, parse_json_text
    from afsim_research_agent.falcon.models import ConditionsSection

    try:
        ConditionsSection.model_validate({"conditions": [{"id": "c"}]})
    except ValidationError as inner:
        wrapped = ValueError("Failed to parse ... {" + "x" * 500)
        wrapped.__cause__ = inner
        assert "conditions.0.expression" in describe_error(wrapped)
    assert parse_json_text('```json\n{"a": 1}\n```') == {"a": 1}


# --------------------------------------------------------------------------- v3 online-eval fixes


def test_autofix_repairs_mechanical_slips():
    from afsim_research_agent.falcon.autofix import autofix
    from afsim_research_agent.ir.catalog import load_catalog

    data = {
        "roles": [
            {
                "id": "lead",
                "dependencies": {"comms": [{"id": "radio", "capability": "radio"}]},
                "blackboards": [
                    {
                        "id": "mission",
                        "variables": [{"name": "fm", "type": "string", "default": "w"}],
                    }
                ],
            },
            {"id": "wing", "dependencies": {"comms": [{"id": "radio", "capability": "radio"}]}},
        ],
        "conditions": [
            {
                "id": "c",
                "expression": {
                    "All": [
                        {"comm_value": {"component": "radio", "property": "available"}},
                        {
                            "compare": {
                                "left": {"blackboard_value": "mission.fm"},
                                "operator": "eq",
                                "right": {"literal": "w"},
                            }
                        },
                    ]
                },
            }
        ],
        "behaviors": [
            {
                "id": "b",
                "precondition": {"comm_value": "available"},
                "actions": [{"command": "wait", "issue_policy": "every_tick"}],
            }
        ],
        "tests": [{"id": "t", "expect": {"issued_actions": ["b"]}}],
    }
    notes = autofix(data, load_catalog())
    expr = data["conditions"][0]["expression"]
    assert "all" in expr and expr["all"][0]["comm_value"] == {
        "comm": "radio",
        "property": "available",
    }
    assert data["behaviors"][0]["precondition"] == {
        "comm_value": {"property": "available", "comm": "radio"}
    }
    assert data["behaviors"][0]["actions"][0]["issue_policy"] == "once_per_activation"
    wing_vars = data["roles"][1]["blackboards"][0]["variables"]
    assert wing_vars == [{"name": "fm", "type": "string", "default": "w"}]
    assert data["tests"][0]["expect"]["issued_actions"] == ["wait"]
    assert all(n.rule == "auto_fix" for n in notes) and len(notes) >= 6


def test_repair_keeps_the_best_round(tmp_path):
    from falcon_fixtures import control_answer

    worse = control_answer(drop_tree="leader_contact_tree")
    dropped = {t["id"] for t in worse["trees"][:1]}
    worse["trees"] = [t for t in worse["trees"] if t["id"] not in dropped]
    script = gold_script()
    script["section:control"] = [control_answer(drop_tree="leader_contact_tree"), worse]
    request = write_request(tmp_path, crow_result())
    result = asyncio.run(run_draft(request, ScriptedModel(script), max_repair=2))
    trace = json.loads((result.package_dir / "falcon_trace.json").read_text(encoding="utf-8"))
    first = trace["repairs"][0]["errors"]
    assert len(result.problems) == first  # rolled back to the initial draft
    assert any(n["rule"] == "repair_rollback" for n in trace["enforcement"])


def test_verification_is_repaired_in_the_final_round(tmp_path):
    from falcon_fixtures import control_answer

    script = gold_script()
    script["section:control"] = [control_answer(drop_tree="leader_contact_tree")]
    good_ver = script["section:verification"]
    good_ver = good_ver[0] if isinstance(good_ver, list) else good_ver
    bad_ver = json.loads(
        json.dumps(
            good_ver
            if isinstance(good_ver, dict)
            else good_ver.model_dump(mode="json", by_alias=True)
        )
    )
    bad_ver["scenarios"] = bad_ver["scenarios"][:1]
    bad_ver["scenarios"][0]["traces_to"] = []
    script["section:verification"] = [bad_ver, good_ver]
    model = ScriptedModel(script)
    request = write_request(tmp_path, crow_result())
    asyncio.run(run_draft(request, model, max_repair=2))
    ver_calls = [s for s, _ in model.calls if s == "section:verification"]
    assert len(ver_calls) == 2  # deferred in round 1, repaired in round 2


def test_skeleton_requires_shared_phase_states():
    from falcon_fixtures import skeleton_answer

    from afsim_research_agent.falcon.models import RequirementClause, SkeletonDraft
    from afsim_research_agent.falcon.skeleton import check_skeleton
    from afsim_research_agent.ir.catalog import load_catalog

    clauses = [
        RequirementClause.model_validate({"id": f"rq_{i:02d}", **c})
        for i, c in enumerate(CLAUSES, 1)
    ]
    good = SkeletonDraft.model_validate(skeleton_answer())
    problems, _, _ = check_skeleton(good, clauses, load_catalog(), None)
    assert not [p for p in problems if "阶段状态" in p]
    bad = skeleton_answer()
    for role in bad["roles"]:
        if role.get("superior"):
            for state in role["states"]:
                state["id"] = "wing_" + state["id"]
            role["initial_state"] = role["states"][0]["id"]
    problems, _, _ = check_skeleton(
        SkeletonDraft.model_validate(bad), clauses, load_catalog(), None
    )
    assert any("阶段状态" in p for p in problems)


def test_generated_elements_name_their_role(tmp_path):
    from afsim_research_agent.falcon.assemble import generated_ids

    request = write_request(tmp_path, crow_result())
    result = asyncio.run(run_draft(request, ScriptedModel(gold_script())))
    data = yaml.safe_load((result.package_dir / "behavior_spec.yaml").read_text(encoding="utf-8"))
    coord = next(c for c in data["coordination"] if c["pattern"] == "report_and_wait")
    usage = generated_ids(data)["usage"]
    cid = coord["id"]
    assert usage[f"{cid}__send_report"] == coord["reporter"]
    assert usage[f"{cid}__await_ack"] == coord["reporter"]
    assert usage[f"{cid}__acknowledge"] == coord["receiver"]
