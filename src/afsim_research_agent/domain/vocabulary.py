"""Controlled tactical vocabulary: glossary, matching and query expansion."""

from __future__ import annotations

import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from afsim_research_agent.domain.doctrine import DoctrineCatalog

_DATA = Path(__file__).with_name("data") / "tactical_vocabulary.toml"


@dataclass(frozen=True)
class Concept:
    id: str
    category: str
    preferred: str
    en: tuple[str, ...] = ()
    zh: tuple[str, ...] = ()
    doctrine: tuple[str, ...] = ()
    scholarly: tuple[str, ...] = ()

    @property
    def surface_forms(self) -> tuple[str, ...]:
        return (self.preferred, *self.en, *self.zh)


@dataclass
class QueryExpansion:
    """Deterministic additions derived from matched concepts."""

    concepts: list[Concept] = field(default_factory=list)
    doctrine_queries: list[str] = field(default_factory=list)
    scholarly_queries: list[str] = field(default_factory=list)
    chinese_queries: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class TacticalVocabulary:
    def __init__(self, concepts: list[Concept], doctrine: DoctrineCatalog | None = None) -> None:
        self.concepts = concepts
        self._doctrine = doctrine or DoctrineCatalog.default()

    @classmethod
    @cache
    def default(cls) -> TacticalVocabulary:
        return cls.from_file(_DATA)

    @classmethod
    def from_file(cls, path: Path) -> TacticalVocabulary:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return cls(
            [
                Concept(
                    id=item["id"],
                    category=item.get("category", "other"),
                    preferred=item["preferred"],
                    en=tuple(item.get("en", [])),
                    zh=tuple(item.get("zh", [])),
                    doctrine=tuple(item.get("doctrine", [])),
                    scholarly=tuple(item.get("scholarly", [])),
                )
                for item in data.get("concept", [])
            ]
        )

    def match(self, texts: Iterable[str]) -> list[Concept]:
        haystack = "\n".join(texts).lower()
        matched: list[Concept] = []
        for concept in self.concepts:
            for form in concept.surface_forms:
                if _contains(haystack, form.lower()):
                    matched.append(concept)
                    break
        return matched

    def glossary(self, concepts: list[Concept]) -> str:
        """Compact glossary block for the planner prompt."""
        lines = []
        for concept in concepts:
            parts = [concept.preferred]
            if concept.en:
                parts.append("aka " + ", ".join(concept.en[:4]))
            if concept.zh:
                parts.append("中文 " + "、".join(concept.zh[:3]))
            if concept.doctrine:
                current = [self._doctrine.current(d)[0] for d in concept.doctrine]
                parts.append("doctrine " + ", ".join(dict.fromkeys(current)))
            if concept.scholarly:
                parts.append("research terms " + "; ".join(concept.scholarly[:3]))
            lines.append("- " + " | ".join(parts))
        return "\n".join(lines)

    def expand(
        self,
        texts: Iterable[str],
        *,
        existing_doctrine: Iterable[str] = (),
        max_each: int = 3,
    ) -> QueryExpansion:
        texts = list(texts)
        expansion = QueryExpansion(concepts=self.match(texts))
        topical = [c for c in expansion.concepts if c.category in {"unit", "cooperation"}]
        aspects = [
            c for c in expansion.concepts if c.category in {"formation", "movement", "decision",
                                                           "terrain", "modeling"}
        ]

        # Doctrine: current editions of publications tied to matched concepts,
        # each searched together with its own concept, plus successors of any
        # superseded designation already mentioned.
        pairs: list[tuple[str, str]] = []
        for concept in [*topical, *aspects]:
            pairs.extend((designation, concept.preferred) for designation in concept.doctrine)
        for text in [*texts, *existing_doctrine]:
            for old, current, verified in self._doctrine.superseded_in(text):
                pairs.insert(0, (current, ""))
                expansion.notes.append(
                    f"{old} is superseded by {current}"
                    + ("" if verified else " (lineage not yet verified)")
                )
        seen: set[str] = set()
        for designation, subject in pairs:
            current = self._doctrine.current(designation)[0]
            if current in seen:
                continue
            seen.add(current)
            expansion.doctrine_queries.append(f"{current} {subject}".strip())
            if len(expansion.doctrine_queries) >= max_each:
                break

        # Scholarly: combine one topical and one aspect research term.
        topical_terms = _interleave([c.scholarly for c in topical])
        aspect_terms = _interleave([c.scholarly for c in aspects])
        for topic in topical_terms or [""]:
            for aspect in aspect_terms[:1] or [""]:
                query = " ".join(part for part in (topic, aspect) if part)
                if query and query not in expansion.scholarly_queries:
                    expansion.scholarly_queries.append(query)
                if len(expansion.scholarly_queries) >= max_each:
                    break
            if len(expansion.scholarly_queries) >= max_each:
                break

        # Chinese strings for manual CNKI/Wanfang searching.
        zh_topics = [c.zh[0] for c in topical if c.zh]
        zh_aspects = [c.zh[0] for c in aspects if c.zh]
        for topic in zh_topics or [""]:
            for aspect in zh_aspects or [""]:
                query = " ".join(part for part in (topic, aspect) if part)
                if query and query not in expansion.chinese_queries:
                    expansion.chinese_queries.append(query)
                if len(expansion.chinese_queries) >= max_each:
                    break
            if len(expansion.chinese_queries) >= max_each:
                break
        return expansion


def _interleave(groups: list[tuple[str, ...]]) -> list[str]:
    """First term of every concept, then second terms, …"""
    result: list[str] = []
    for index in range(max((len(group) for group in groups), default=0)):
        for group in groups:
            if index < len(group) and group[index] not in result:
                result.append(group[index])
    return result


def _contains(haystack: str, needle: str) -> bool:
    if not needle:
        return False
    if needle.isascii() and len(needle) <= 4:
        # Short ASCII forms (BT, FSM, SAF, WSF) must match as whole words.
        import re

        return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack) is not None
    return needle in haystack
