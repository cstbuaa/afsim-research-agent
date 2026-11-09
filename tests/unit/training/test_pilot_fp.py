"""Pilot generator (formation + perception): every skeleton yields a model that passes the
offline checks (L1, L2 tests, 2D scenarios), and the members never claim locomotion."""

import re
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
    assert "withdraw_threshold_m" in a.stated
    k5 = sample("K5", 2)
    assert "search_limit_s" in k5.stated and k5.search_limit_s
