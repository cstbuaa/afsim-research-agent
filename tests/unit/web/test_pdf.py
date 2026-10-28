"""Tests for content-addressed PDF persistence."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from http_fakes import FakeResponse, FakeSession

from afsim_research_agent.web.fetch import FetchedResource
from afsim_research_agent.web.pdf import PDFDownloadError, download_pdf, save_pdf

BODY = b"%PDF-1.7\ntest"


def resource(body: bytes = BODY, kind: str = "pdf") -> FetchedResource:
    return FetchedResource(
        requested_url="https://example.org/a",
        final_url="https://example.org/a.pdf",
        status_code=200,
        content_type="application/pdf",
        kind=kind,  # type: ignore[arg-type]
        body=body,
        charset=None,
        attempts=1,
    )


def test_save_pdf_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    first = save_pdf(resource(), tmp_path)
    second = save_pdf(resource(), tmp_path)

    digest = hashlib.sha256(BODY).hexdigest()
    assert first == second
    assert first.path == tmp_path / f"{digest}.pdf"
    assert first.path.read_bytes() == BODY
    assert first.url == "https://example.org/a.pdf"


def test_save_pdf_rejects_non_pdf_bytes(tmp_path: Path) -> None:
    with pytest.raises(PDFDownloadError) as raised:
        save_pdf(resource(body=b"<html>", kind="html"), tmp_path)

    assert raised.value.category == "invalid_pdf"


def test_download_pdf_follows_sniffed_pdf_without_suffix(
    public_dns: list[str], tmp_path: Path
) -> None:
    response = FakeResponse(
        "https://example.org/download?id=1",
        headers={"Content-Type": "application/octet-stream"},
        body=BODY,
    )

    result = download_pdf(
        response.url, destination_dir=tmp_path, session=FakeSession([response])
    )

    assert result.content_hash == hashlib.sha256(BODY).hexdigest()


def test_download_pdf_reports_html_as_unsupported(
    public_dns: list[str], tmp_path: Path
) -> None:
    response = FakeResponse("https://example.org/a", body=b"<html><body>x</body></html>")

    with pytest.raises(PDFDownloadError) as raised:
        download_pdf(response.url, destination_dir=tmp_path, session=FakeSession([response]))

    assert raised.value.category == "unsupported_content_type"


def test_download_pdf_carries_fetch_category(public_dns: list[str], tmp_path: Path) -> None:
    response = FakeResponse("https://example.org/a.pdf", status_code=404)

    with pytest.raises(PDFDownloadError) as raised:
        download_pdf(response.url, destination_dir=tmp_path, session=FakeSession([response]))

    assert raised.value.category == "http_error"
