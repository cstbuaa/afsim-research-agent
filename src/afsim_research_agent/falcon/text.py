"""Text helpers shared by Falcon stages (no heavy imports)."""

from __future__ import annotations

import re

_DASHES = str.maketrans(
    {"–": "-", "—": "-", "‑": "-", "−": "-", "“": '"', "”": '"', "‘": "'", "’": "'"}
)


def squash(text: str) -> str:
    """Comparison form: no whitespace, lower case, unified dashes and quotes."""
    return re.sub(r"\s+", "", text.translate(_DASHES)).lower().replace("\u00ad", "")
