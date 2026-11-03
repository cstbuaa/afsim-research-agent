"""Mechanical repair of ENTRY_TRUE transitions: the trigger waits until the state's
actions have written their fields (falcon.v3.autofix)."""

from __future__ import annotations

from afsim_research_agent.falcon.sheet.blocks import Block
from afsim_research_agent.falcon.v3.autofix import GUARD, entry_guards


def _blocks():
    return [
        Block("model", None, "model: {display_name: x}\n", 1),
        Block(
            "mermaid",
            "car",
            "stateDiagram-v2\n  [*] --> GO\n  GO --> HOLD : arrived\n"
            "  HOLD --> WATCH : near or seen\n  WATCH --> DONE : cancelled\n",
            5,
        ),
    ]


def test_entry_true_triggers_wait_for_the_state_actions() -> None:
    out, notes = entry_guards(
        _blocks(), [("car", "GO", "HOLD", "arrived"), ("car", "HOLD", "WATCH", "_c1")]
    )
    assert len(notes) == 2
    assert f"GO --> HOLD : arrived and {GUARD}" in out[1].text
    assert f"HOLD --> WATCH : (near or seen) and {GUARD}" in out[1].text
    assert "WATCH --> DONE : cancelled\n" in out[1].text


def test_nothing_matching_leaves_the_sheet_as_it_was() -> None:
    blocks = _blocks()
    out, notes = entry_guards(blocks, [("car", "GO", "OTHER", "arrived")])
    assert notes == [] and [b.text for b in out] == [b.text for b in blocks]
