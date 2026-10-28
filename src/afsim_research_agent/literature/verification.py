"""Verify candidate metadata against an authoritative DOI record.

Search providers sometimes attach the wrong DOI, truncate titles or omit
retraction status. Before download, each DOI is looked up once (OpenAlex
single-work lookups are free): missing fields are filled, retractions are
flagged, and a DOI whose record has a different title is detached so it cannot
produce a false citation.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from difflib import SequenceMatcher

from afsim_research_agent.literature.models import SourceCandidate
from afsim_research_agent.models import SearchHit

DoiLookup = Callable[[str], SearchHit | None]
_MIN_TITLE_SIMILARITY = 0.6


def normalize_title(title: str) -> str:
    return " ".join(re.findall(r"[0-9a-z一-鿿]+", (title or "").lower()))


def title_similarity(left: str, right: str) -> float:
    a, b = normalize_title(left), normalize_title(right)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0 if min(len(a), len(b)) >= 20 else 0.8
    return SequenceMatcher(None, a, b).ratio()


class MetadataVerifier:
    def __init__(self, lookup: DoiLookup, *, name: str = "openalex") -> None:
        self._lookup = lookup
        self.name = name
        self._cache: dict[str, SearchHit | None] = {}

    def verify(self, candidate: SourceCandidate) -> None:
        meta = candidate.metadata
        doi = meta.doi
        if not doi or self.name in meta.verified_by:
            return
        if doi not in self._cache:
            try:
                self._cache[doi] = self._lookup(doi)
            except Exception:  # noqa: BLE001 - verification is best effort
                candidate.notes.append(
                    f"DOI {doi} could not be verified ({self.name} lookup failed)"
                )
                return
        record = self._cache[doi]
        if record is None:
            candidate.notes.append(f"DOI {doi} not found in {self.name}; DOI detached")
            meta.doi = None
            return
        if title_similarity(candidate.title, record.title) < _MIN_TITLE_SIMILARITY:
            candidate.notes.append(
                f"DOI {doi} resolves to a different work ({record.title[:80]!r}); DOI detached"
            )
            meta.doi = None
            meta.external_ids.pop("doi", None)
            return
        verified = record.metadata
        meta.verified_by.append(self.name)
        meta.is_retracted = verified.is_retracted
        meta.authors = meta.authors or verified.authors
        meta.year = meta.year or verified.year
        meta.venue = meta.venue or verified.venue
        meta.publisher = meta.publisher or verified.publisher
        meta.institutions = meta.institutions or verified.institutions
        meta.document_type = meta.document_type or verified.document_type
        if verified.cited_by_count is not None:
            meta.cited_by_count = max(meta.cited_by_count or 0, verified.cited_by_count)
        meta.pdf_url = meta.pdf_url or verified.pdf_url
        meta.abstract = meta.abstract or verified.abstract
        for key, value in verified.external_ids.items():
            meta.external_ids.setdefault(key, value)
