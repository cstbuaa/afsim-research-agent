"""Doctrine designation parsing and supersession lookup."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from functools import cache
from pathlib import Path

_DATA = Path(__file__).with_name("data") / "doctrine_supersession.toml"
# Matches "ATP 3-20.15", "atp3-20.15", "ATP_3-20.15-000-WEB-1.pdf" (Army file names),
# "ATP%203-20.15" (URL-encoded) and "AJP-3.2"; the publication-number grammar stops
# before trailing file-name suffixes such as "-000".
_DESIGNATION = re.compile(
    r"(?<![A-Za-z0-9])(?P<series>FM|ATP|ADP|ADRP|ATTP|TC|JP|AJP|AAP|MCWP|MCDP|MCRP|AFDP"
    r"|AFTTP|NWP|NTTP)(?:[\s_\-]|%20)?(?P<number>\d+(?:-\d+(?:\.\d+)?|\.\d+(?:\.\d+)?)?)"
    r"(?![\d.]\d|\d)",
    re.IGNORECASE,
)
_STANAG = re.compile(r"(?<![A-Za-z0-9])STANAG(?:[\s_\-]|%20)?\d{3,4}(?!\d)", re.IGNORECASE)


def normalize_designation(text: str) -> str | None:
    """'atp3-20.15' -> 'ATP 3-20.15'; returns None when no designation is present."""
    match = _DESIGNATION.search(text or "")
    if not match:
        return None
    return f"{match.group('series').upper()} {match.group('number')}"


def has_designation(text: str) -> bool:
    return bool(_DESIGNATION.search(text or "") or _STANAG.search(text or ""))


def find_designations(text: str) -> list[str]:
    seen: list[str] = []
    for match in _DESIGNATION.finditer(text or ""):
        value = f"{match.group('series').upper()} {match.group('number')}"
        if value not in seen:
            seen.append(value)
    return seen


@dataclass(frozen=True)
class Publication:
    designation: str
    title: str
    superseded_by: str | None
    verified: bool
    notes: str = ""
    source: str = ""


class DoctrineCatalog:
    def __init__(self, publications: list[Publication]) -> None:
        self._by_designation = {pub.designation: pub for pub in publications}

    @classmethod
    @cache
    def default(cls) -> DoctrineCatalog:
        return cls.from_file(_DATA)

    @classmethod
    def from_file(cls, path: Path) -> DoctrineCatalog:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        publications = []
        for item in data.get("publication", []):
            designation = normalize_designation(item["designation"])
            if designation is None:
                continue
            successor = item.get("superseded_by")
            publications.append(
                Publication(
                    designation=designation,
                    title=item.get("title", ""),
                    superseded_by=normalize_designation(successor) if successor else None,
                    verified=bool(item.get("verified", False)),
                    notes=item.get("notes", ""),
                    source=item.get("source", ""),
                )
            )
        return cls(publications)

    def get(self, designation: str) -> Publication | None:
        normalized = normalize_designation(designation)
        return self._by_designation.get(normalized) if normalized else None

    def current(self, designation: str) -> tuple[str, list[Publication]]:
        """Follow the supersession chain; return the current designation and the chain."""
        normalized = normalize_designation(designation) or designation
        chain: list[Publication] = []
        seen = {normalized}
        while True:
            publication = self._by_designation.get(normalized)
            if publication is None or publication.superseded_by is None:
                return normalized, chain
            chain.append(publication)
            normalized = publication.superseded_by
            if normalized in seen:  # malformed cycle
                return normalized, chain
            seen.add(normalized)

    def superseded_in(self, text: str) -> list[tuple[str, str, bool]]:
        """(old designation, current designation, all links verified) found in text."""
        results = []
        for designation in find_designations(text):
            current, chain = self.current(designation)
            if chain:
                results.append((designation, current, all(p.verified for p in chain)))
        return results
