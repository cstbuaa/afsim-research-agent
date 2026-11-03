"""The repair loop counts verification tests / scenarios to refuse edits that drop them;
an edit that leaves the block as a list or scalar must not crash it."""

from afsim_research_agent.falcon.sheet.blocks import Block
from afsim_research_agent.falcon.v3.fixer import _counts


def _block(text: str) -> Block:
    return Block("verification", None, text, 1, "")


def test_counts_of_a_mapping() -> None:
    text = "verification:\n  tests: [{id: a}]\n  scenarios: [{id: s, expect: [{id: e}]}]\n"
    assert _counts([_block(text)]) == {"tests": 1, "scenarios": 1, "expectations": 1}


def test_list_or_scalar_block_is_unreadable_not_a_crash() -> None:
    for text in ("- id: a\n- id: b\n", "just text\n"):
        assert _counts([_block(text)]) == {"tests": -1, "scenarios": -1, "expectations": -1}
