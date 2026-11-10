"""Pilot generator (formation + perception): every skeleton yields a model that passes the
offline checks (L1, L2 tests, 2D scenarios), and the members never claim locomotion."""

import re
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from afsim_research_agent.training.pilot_fp import SKELETONS, build, sample, sheet


@pytest.mark.parametrize("skeleton", SKELETONS)
def test_each_skeleton_builds_a_valid_model(tmp_path: Path, skeleton: str) -> None:
    row = build(sample(skeleton, 3), tmp_path)
    assert row["status"] == "draft_valid", row
    assert row["l2"][0] == row["l2"][1] > 0
    assert row["l3"] and all(x[1] for x in row["l3"])


def test_members_leave_locomotion_to_the_leader() -> None:
    for seed in range(1, 6):
        text, _ = sheet(sample("K4", seed))
        member = re.search(r"```python member\n(.*?)```", text, re.S)
        assert member and "movement." not in member.group(1) and "formation." not in member.group(1)


def test_sampling_is_reproducible_and_states_the_module_numbers() -> None:
    a, b = sample("K4", 7), sample("K4", 7)
    assert a == b and a.model_id == b.model_id
    assert a.withdraw == "keep" and "desired_range_m" in a.stated
    assert all(sample("K4", i).desired_range_m >= 800 for i in range(1, 31))
    k5 = sample("K5", 2)
    assert "search_limit_s" in k5.stated and k5.search_limit_s


def test_requirement_facts_state_exactly_the_stated_numbers() -> None:
    from afsim_research_agent.training.backtranslate import check, facts, numbers

    s = sample("K4", 4)
    text = "；".join(facts(s))
    assert not check(s, text), check(s, text)
    for key in ("wing_offset_m", "column_spacing_m"):
        if key not in s.stated:
            assert f"{getattr(s, key):g} 米" not in text or key == "column_spacing_m"
    assert check(s, "三车搜索前进。") and any("缺少数值" in p for p in check(s, "三车搜索前进。"))
    assert any("标识符" in p for p in check(s, "进入 STANDOFF_TRACK 状态"))
    assert numbers(sample("K5", 2))["search_limit_s"]


def test_check_accepts_synonyms_and_chinese_numerals():
    from afsim_research_agent.training.backtranslate import check

    s = sample("K6", 2)
    s = replace(s, wait_start=True, reacquire_timeout_s=10.0, stated=["reacquire_timeout_s"])
    assert check(s, "初始待机，十秒内重新发现就继续跟踪") == []
    assert any("缺少数值" in p for p in check(s, "初始待机，很快重新发现就继续跟踪"))


def test_check_flags_added_fire():
    from afsim_research_agent.training.backtranslate import check

    s = replace(sample("K1", 1), stated=[])
    assert any("加入了" in p for p in check(s, "发现敌车后长车停止间射击准备"))


@pytest.mark.parametrize("skeleton", ["K1", "K4", "K6"])
def test_render_prompts_round_trip(skeleton):
    from afsim_research_agent.training.backtranslate import facts
    from afsim_research_agent.training.render import TASKS, render

    s = sample(skeleton, 3)
    text, request = sheet(s)
    out = render(text, request, "；".join(facts(s)), facts(s))
    assert [x.task for x in out] == list(TASKS)
    assert all(x.ok for x in out), [x.problems for x in out]
    assert "```yaml model" in out[0].target and "```python conditions" in out[1].target
    assert out[2].target.startswith("```yaml verification")
    # the structure prompt comes before the obligations; the later ones carry them
    assert "cancel_shutdown" not in out[0].prompt and "cancel_shutdown" in out[1].prompt


def test_injected_errors_are_caught_and_fixed():
    import json as _json

    from afsim_research_agent.training.backtranslate import facts
    from afsim_research_agent.training.inject import injections, make

    s = sample("K2", 1)
    text, request = sheet(s)
    injs = injections(text)
    assert {i.rule for i in injs} >= {"E1", "E2", "E4", "E5", "E8", "E10"}
    done = [make(text, request, "；".join(facts(s)), facts(s), i) for i in injs]
    ok = [x for x in done if x.ok]
    assert len(ok) >= len(done) // 2, [(x.rule, x.problems) for x in done if not x.ok]
    for x in ok:
        assert x.failed
        last = _json.loads(x.steps[-1]["target"])
        assert last["action"] == "edit" and last["args"]["changes"]
        if x.stage == "behaviour":
            assert _json.loads(x.steps[0]["target"])["action"] == "diagnose"


def test_score_accepts_the_gold_answers():
    import json as _json

    from afsim_research_agent.training.backtranslate import facts
    from afsim_research_agent.training.inject import injections, make
    from afsim_research_agent.training.render import render
    from afsim_research_agent.training.score import score_repair, score_sheet

    s = sample("K2", 1)
    text, request = sheet(s)
    req = "；".join(facts(s))
    pair = {"tactic_sheet": text, "falcon_request": yaml.safe_dump(request), "requirement": req}
    for x in render(text, request, req, facts(s)):
        res = score_sheet(x.task, pair, x.target)
        assert res["parsed"] and res["verified"], (x.task, res)
    assert not score_sheet("control", pair, "```python conditions\nx = 1\n```")["verified"]
    injs = injections(text)
    k = next(i for i, inj in enumerate(injs) if inj.rule == "E5")
    smp = make(text, request, req, facts(s), injs[k])
    rows = [
        {"id": f"x-{k:02d}-E5-s{j}", "target": st["target"], "meta": {"stage": smp.stage}}
        for j, st in enumerate(smp.steps)
    ]
    first = score_repair(rows[0], pair, None, rows[0]["target"])
    assert first["accepted"] and first["action"] == "diagnose"
    second = score_repair(rows[1], pair, _json.loads(rows[0]["target"]), rows[1]["target"])
    assert second["solved"]
    assert not score_repair(rows[1], pair, _json.loads(rows[0]["target"]), "不知道")["parsed"]


def test_repair_v2_looks_first_and_scores_with_replay():
    import json as _json

    from afsim_research_agent.training.backtranslate import facts
    from afsim_research_agent.training.inject import injections, make
    from afsim_research_agent.training.score import score_repair

    s = sample("K2", 1)
    text, request = sheet(s)
    req = "；".join(facts(s))
    pair = {"tactic_sheet": text, "falcon_request": yaml.safe_dump(request), "requirement": req}
    both = injections(text, "both")
    assert len(both) >= len(injections(text))
    assert len({(i.block, i.before, i.after) for i in both}) == len(both)
    k = next(i for i, inj in enumerate(both) if inj.rule == "E5")
    smp = make(text, request, req, facts(s), both[k], look=True)
    assert smp.ok, smp.problems
    acts = [_json.loads(st["target"])["action"] for st in smp.steps]
    assert acts == ["show", "diagnose", "edit"]
    assert both[k].block in smp.steps[1]["prompt"]
    rows = [
        {"id": f"x-{k:02d}-E5-s{j}", "target": st["target"], "meta": {"stage": smp.stage}}
        for j, st in enumerate(smp.steps)
    ]
    gold = [_json.loads(r["target"]) for r in rows]
    for j, r in enumerate(rows):
        res = score_repair(r, pair, gold[:j], r["target"], injset="both")
        assert res["accepted"], (j, res)
    assert res["solved"]
