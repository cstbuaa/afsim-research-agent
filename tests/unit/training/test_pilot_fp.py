"""Pilot generator (formation + perception): every skeleton yields a model that passes the
offline checks (L1, L2 tests, 2D scenarios), and the members never claim locomotion."""

import re
from dataclasses import replace
from pathlib import Path

import pytest

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
