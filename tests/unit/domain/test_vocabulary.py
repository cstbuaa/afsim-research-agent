"""Tests for the tactical vocabulary and doctrine catalog."""

from __future__ import annotations

from pathlib import Path

import pytest

from afsim_research_agent.domain import DoctrineCatalog, TacticalVocabulary, normalize_designation
from afsim_research_agent.domain.doctrine import find_designations


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("atp3-20.15", "ATP 3-20.15"),
        ("see FM 3-90 now", "FM 3-90"),
        ("Tank_Platoon_ARN44282-ATP_3-20.15-000-WEB-1_(July_2025).pdf", "ATP 3-20.15"),
        ("ATP%203-21.71", "ATP 3-21.71"),
        ("AJP-3.2 Land Operations", "AJP 3.2"),
        ("fm4-01.45(05).pdf", "FM 4-01.45"),
        ("ATP 2-01.3", "ATP 2-01.3"),
        ("FMS 3 radio", None),
        ("nothing", None),
    ],
)
def test_designations_are_normalized(text: str, expected: str | None) -> None:
    assert normalize_designation(text) == expected


def test_supersession_chain_and_verification_flag() -> None:
    catalog = DoctrineCatalog.default()

    assert catalog.current("FM 3-20.15") == ("ATP 3-20.15", [catalog.get("FM 3-20.15")])
    assert catalog.superseded_in("FM 7-92 and ATP 3-20.98") == [("FM 7-92", "ATP 3-20.98", True)]
    assert catalog.superseded_in("FM 3-21.71 platoon") == [("FM 3-21.71", "ATP 3-21.71", False)]
    assert catalog.current("ATP 3-20.15")[1] == []


def test_catalog_survives_cycles(tmp_path: Path) -> None:
    path = tmp_path / "c.toml"
    path.write_text(
        '[[publication]]\ndesignation="FM 1"\nsuperseded_by="FM 2"\n'
        '[[publication]]\ndesignation="FM 2"\nsuperseded_by="FM 1"\n'
    )

    assert DoctrineCatalog.from_file(path).current("FM 1")[0] in {"FM 1", "FM 2"}


def test_every_vocabulary_doctrine_entry_is_a_valid_designation() -> None:
    vocabulary = TacticalVocabulary.default()

    for concept in vocabulary.concepts:
        for designation in concept.doctrine:
            assert normalize_designation(designation) == designation, concept.id
    assert len({c.id for c in vocabulary.concepts}) == len(vocabulary.concepts)


@pytest.mark.parametrize(
    ("text", "concept"),
    [
        ("研究装甲车在不同地形情况下的编队方式", "unit.tank_platoon"),
        ("四机有人/无人协同编队", "coop.mumt"),
        ("UAV swarm task allocation", "coop.task_allocation"),
        ("行为树建模", "model.behavior_tree"),
    ],
)
def test_matching_in_both_languages(text: str, concept: str) -> None:
    assert concept in {c.id for c in TacticalVocabulary.default().match([text])}


def test_short_acronyms_need_word_boundaries() -> None:
    ids = {c.id for c in TacticalVocabulary.default().match(["the subtitle is safe"])}

    assert "model.behavior_tree" not in ids  # "bt" inside "subtitle"
    assert "model.cgf" not in ids  # "saf" inside "safe"


def test_expansion_produces_current_doctrine_scholarly_and_chinese_queries() -> None:
    expansion = TacticalVocabulary.default().expand(
        ["研究装甲车在不同地形情况下的编队方式", "per FM 3-20.15"]
    )

    assert expansion.doctrine_queries[0] == "ATP 3-20.15"
    assert "ATP 2-01.3 terrain analysis" in expansion.doctrine_queries
    assert "armored vehicle formation control" in expansion.scholarly_queries
    assert "坦克排 编队" in expansion.chinese_queries
    assert expansion.notes == ["FM 3-20.15 is superseded by ATP 3-20.15"]
    assert not find_designations("no manuals here")
