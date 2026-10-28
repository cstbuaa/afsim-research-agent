"""Deterministic identity keys used to collapse duplicate sources.

Three levels are applied during collection, cheapest first:

1. normalized URL (tracking parameters, fragments and arXiv abs/pdf variants);
2. DOI;
3. SHA-256 of the downloaded PDF bytes or extracted webpage text, which
   catches the same document hosted on different mirrors.

Claim-level deduplication belongs to evidence merging and is not done here.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

_TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "ref",
        "ref_src",
        "spm",
        "xstg",
        "si",
        "feature",
    }
)
_ARXIV_ABS = re.compile(r"^/abs/(?P<id>[^/?#]+?)(?:v\d+)?/?$")
_ARXIV_PDF = re.compile(r"^/pdf/(?P<id>[^/?#]+?)(?:v\d+)?(?:\.pdf)?/?$")


def normalize_url(url: str) -> str:
    """Return a canonical URL used only as a deduplication key."""
    parsed = urlparse(url.strip())
    scheme = (parsed.scheme or "https").lower()
    if scheme == "http":
        scheme = "https"
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = f":{parsed.port}" if parsed.port and parsed.port not in {80, 443} else ""
    path = parsed.path or "/"
    if host in {"arxiv.org", "export.arxiv.org"}:
        host = "arxiv.org"
        match = _ARXIV_ABS.match(path) or _ARXIV_PDF.match(path)
        if match:
            path = f"/abs/{match.group('id')}"
    if len(path) > 1:
        path = path.rstrip("/")
    query = urlencode(
        sorted(
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() not in _TRACKING_PARAMS and not key.lower().startswith("utm_")
        )
    )
    return urlunparse((scheme, f"{host}{port}", path, "", query, ""))


def preferred_fetch_url(url: str) -> str:
    """Rewrite landing pages that have a stable full-text twin (arXiv abs → pdf)."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
        match = _ARXIV_ABS.match(parsed.path)
        if match:
            return f"https://arxiv.org/pdf/{match.group('id')}"
    return url


def normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    value = doi.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "doi:"):
        if value.startswith(prefix):
            value = value[len(prefix) :]
    return value or None
