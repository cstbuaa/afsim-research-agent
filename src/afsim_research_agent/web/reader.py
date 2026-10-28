"""Auditable webpage reading and main-content extraction."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import requests
from trafilatura import bare_extraction

from afsim_research_agent.models import SourceReference, WebDocument
from afsim_research_agent.web.fetch import FetchedResource, FetchError, FetchPolicy, fetch_url


class WebReadError(RuntimeError):
    """Raised when a webpage cannot be safely read or normalized."""

    def __init__(self, message: str, *, category: str = "unknown") -> None:
        super().__init__(message)
        self.category = category


def _parse_published_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def decode_html(resource: FetchedResource) -> str:
    """Decode HTML using the declared charset, falling back to UTF-8."""
    for encoding in (resource.charset, "utf-8"):
        if not encoding:
            continue
        try:
            return resource.body.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return resource.body.decode("utf-8", errors="replace")


def extract_webpage(
    html: str,
    *,
    url: str,
    status_code: int = 200,
    retrieved_at: datetime | None = None,
) -> WebDocument:
    """Extract main content from already-downloaded HTML."""
    extracted = bare_extraction(
        html,
        url=url,
        include_comments=False,
        with_metadata=True,
    )
    if extracted is None or not extracted.text:
        raise WebReadError(
            "Webpage contains no extractable main content", category="no_extractable_text"
        )

    paragraphs = [line.strip() for line in extracted.text.splitlines() if line.strip()]
    text = "\n\n".join(paragraphs)
    content_hash = hashlib.sha256(text.encode()).hexdigest()
    source_key = hashlib.sha256(url.encode()).hexdigest()[:16]
    source = SourceReference(
        source_id=f"web:{source_key}",
        kind="article",
        title=extracted.title or url,
        uri=url,
        published_at=_parse_published_at(extracted.date),
        retrieved_at=retrieved_at or datetime.now(UTC),
        content_hash=content_hash,
    )
    return WebDocument(
        source=source,
        text=text,
        paragraph_ids=[f"p{index:04d}" for index in range(1, len(paragraphs) + 1)],
        status_code=status_code,
    )


def write_snapshot(raw_dir: Path, document: WebDocument, html: str) -> Path:
    """Persist the original HTML and normalized document side by side."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    source_key = document.source.source_id.removeprefix("web:")
    stem = f"{source_key}-{document.source.content_hash}"
    html_path = raw_dir / f"{stem}.html"
    html_path.write_text(html, encoding="utf-8")
    (raw_dir / f"{stem}.json").write_text(
        document.model_dump_json(indent=2),
        encoding="utf-8",
    )
    return html_path


def read_webpage(
    url: str,
    *,
    policy: FetchPolicy | None = None,
    raw_dir: Path | None = None,
    retrieved_at: datetime | None = None,
    session: requests.Session | None = None,
) -> WebDocument:
    """Read one webpage into a traceable document and optionally persist it."""
    try:
        resource = fetch_url(url, policy=policy, session=session)
    except FetchError as exc:
        raise WebReadError(str(exc), category=exc.category) from exc
    if resource.kind != "html":
        raise WebReadError(
            f"Unsupported webpage content type: {resource.content_type or resource.kind}",
            category="unsupported_content_type",
        )
    html = decode_html(resource)
    document = extract_webpage(
        html,
        url=resource.final_url,
        status_code=resource.status_code,
        retrieved_at=retrieved_at,
    )
    if raw_dir is not None:
        write_snapshot(raw_dir, document, html)
    return document
