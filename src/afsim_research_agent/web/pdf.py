"""Content-addressed PDF persistence on top of the shared fetcher."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import requests

from afsim_research_agent.web.fetch import FetchedResource, FetchError, FetchPolicy, fetch_url


class PDFDownloadError(RuntimeError):
    """Raised when a PDF cannot be downloaded and validated."""

    def __init__(self, message: str, *, category: str = "unknown") -> None:
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class DownloadedPDF:
    """A persisted PDF and its stable content identity."""

    url: str
    path: Path
    content_hash: str


def save_pdf(resource: FetchedResource, destination_dir: Path) -> DownloadedPDF:
    """Write a fetched PDF under its SHA-256 so mirrors collapse to one file."""
    if resource.kind != "pdf" or b"%PDF-" not in resource.body[:1024]:
        raise PDFDownloadError(
            "Downloaded content has an invalid PDF signature", category="invalid_pdf"
        )
    digest = hashlib.sha256(resource.body).hexdigest()
    destination_dir.mkdir(parents=True, exist_ok=True)
    path = destination_dir / f"{digest}.pdf"
    if not path.exists():
        path.write_bytes(resource.body)
    return DownloadedPDF(url=resource.final_url, path=path, content_hash=digest)


def download_pdf(
    url: str,
    *,
    destination_dir: Path,
    policy: FetchPolicy | None = None,
    session: requests.Session | None = None,
) -> DownloadedPDF:
    """Download one PDF after validating every redirect target."""
    try:
        resource = fetch_url(url, policy=policy, session=session)
    except FetchError as exc:
        raise PDFDownloadError(str(exc), category=exc.category) from exc
    if resource.kind != "pdf":
        raise PDFDownloadError(
            f"Unsupported PDF content type: {resource.content_type or resource.kind}",
            category="unsupported_content_type",
        )
    return save_pdf(resource, destination_dir)
