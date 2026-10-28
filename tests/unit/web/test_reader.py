"""Tests for auditable webpage retrieval and extraction."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from http_fakes import FakeResponse, FakeSession

import afsim_research_agent.web as web
from afsim_research_agent.web.fetch import FetchedResource
from afsim_research_agent.web.reader import (
    WebReadError,
    decode_html,
    extract_webpage,
    read_webpage,
)
from afsim_research_agent.web.security import URLValidationError

ARTICLE_HTML = """
<html>
  <head>
    <title>Formation control</title>
    <meta name="date" content="2026-01-02">
  </head>
  <body>
    <nav>Navigation that should not be evidence.</nav>
    <article>
      <p>Aircraft maintain a shared formation reference.</p>
      <p>Each wingman updates its command from local observations.</p>
    </article>
  </body>
</html>
"""


def test_web_package_exposes_reader_api() -> None:
    assert web.read_webpage is read_webpage
    assert web.WebReadError is WebReadError
    assert web.URLValidationError is URLValidationError


def test_extract_webpage_builds_traceable_document() -> None:
    retrieved_at = datetime(2026, 1, 3, tzinfo=UTC)

    document = extract_webpage(
        ARTICLE_HTML, url="https://example.com/article", retrieved_at=retrieved_at
    )

    assert document.source.title == "Formation control"
    assert document.source.uri == "https://example.com/article"
    assert document.source.published_at == datetime(2026, 1, 2)
    assert document.source.retrieved_at == retrieved_at
    expected_source_id = hashlib.sha256(document.source.uri.encode()).hexdigest()[:16]
    assert document.source.source_id == f"web:{expected_source_id}"
    assert document.paragraph_ids == ["p0001", "p0002"]
    assert document.text == (
        "Aircraft maintain a shared formation reference.\n\n"
        "Each wingman updates its command from local observations."
    )
    assert document.source.content_hash == hashlib.sha256(document.text.encode()).hexdigest()


def test_extract_webpage_rejects_page_without_main_content() -> None:
    with pytest.raises(WebReadError, match="main content") as raised:
        extract_webpage(
            "<html><body><script>onlyScript()</script></body></html>",
            url="https://example.com/empty",
        )

    assert raised.value.category == "no_extractable_text"


def test_read_webpage_uses_final_url_and_writes_snapshots(
    public_dns: list[str], tmp_path: Path
) -> None:
    session = FakeSession(
        [
            FakeResponse("https://example.com/s", status_code=301, headers={"Location": "/a"}),
            FakeResponse(
                "https://example.com/a",
                headers={"Content-Type": "text/html; charset=utf-8"},
                body=ARTICLE_HTML.encode(),
            ),
        ]
    )

    document = read_webpage("https://example.com/s", raw_dir=tmp_path, session=session)
    stem = f"{document.source.source_id.removeprefix('web:')}-{document.source.content_hash}"

    assert document.source.uri == "https://example.com/a"
    assert (tmp_path / f"{stem}.html").read_text() == ARTICLE_HTML
    assert document.source.uri in (tmp_path / f"{stem}.json").read_text()


def test_read_webpage_reports_pdf_as_unsupported(public_dns: list[str]) -> None:
    response = FakeResponse(
        "https://example.com/a", headers={"Content-Type": "application/pdf"}, body=b"%PDF-1.4"
    )

    with pytest.raises(WebReadError) as raised:
        read_webpage(response.url, session=FakeSession([response]))

    assert raised.value.category == "unsupported_content_type"


def test_read_webpage_carries_fetch_category(public_dns: list[str]) -> None:
    response = FakeResponse("https://example.com/missing", status_code=404)

    with pytest.raises(WebReadError, match="HTTP 404") as raised:
        read_webpage(response.url, session=FakeSession([response]))

    assert raised.value.category == "http_error"


def test_decode_html_falls_back_when_declared_charset_is_wrong() -> None:
    resource = FetchedResource(
        requested_url="u",
        final_url="u",
        status_code=200,
        content_type="text/html",
        kind="html",
        body="编队".encode(),
        charset="not-a-charset",
        attempts=1,
    )

    assert decode_html(resource) == "编队"
