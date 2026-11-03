"""Mechanical repair of ENTRY_TRUE transitions: the state's own tree event takes the
condition's place (falcon.v3.autofix)."""

from __future__ import annotations

import yaml

from afsim_research_agent.falcon.sheet.blocks import Block
from afsim_research_agent.falcon.v3.autofix import entry_events


def _blocks():
    return [
        Block(
            "model", None, "model: {display_name: x}\nevents:\n  cancelled: {source: external}\n", 1
        ),
        Block(
            "mermaid",
            "car",
            "stateDiagram-v2\n  [*] --> GO\n  GO --> HOLD : arrived\n  HOLD --> DONE : cancelled\n",
            5,
        ),
        Block(
            "conditions",
            None,
            "arrived = movement.derived.range_to_target_m <= 5.0\nslow = self.speed_mps < 1\n",
            9,
        ),
    ]


def test_entry_true_transition_becomes_the_tree_event() -> None:
    out, notes = entry_events(_blocks(), [("car", "GO", "HOLD", "arrived")])
    assert len(notes) == 1 and "go_done" in notes[0]
    doc = yaml.safe_load(out[0].text)
    assert doc["events"]["go_done"]["source"] == "tree" and "cancelled" in doc["events"]
    assert "GO --> HOLD : go_done" in out[1].text
    assert "arrived" not in out[2].text and "slow" in out[2].text  # unused condition dropped


def test_nothing_matching_leaves_the_sheet_as_it_was() -> None:
    blocks = _blocks()
    out, notes = entry_events(blocks, [("car", "GO", "OTHER", "arrived")])
    assert notes == [] and [b.text for b in out] == [b.text for b in blocks]
