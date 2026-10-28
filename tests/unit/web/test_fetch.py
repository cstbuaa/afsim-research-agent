"""Tests for the unified, categorized fetcher."""

from __future__ import annotations

import contextlib
import ipaddress

import pytest
import requests
from http_fakes import FakeResponse, FakeSession

from afsim_research_agent.web import fetch
from afsim_research_agent.web.fetch import FetchError, FetchPolicy, build_user_agent, fetch_url
from afsim_research_agent.web.security import DNSResolutionError, URLValidationError

PDF = b"%PDF-1.7\n" + b"x" * 2000
HTML = b"<!DOCTYPE html><html><body><p>hello</p></body></html>"
NO_WAIT = FetchPolicy(backoff_seconds=0, max_retries=2)


def no_sleep(seconds: float) -> None:
    return None


@pytest.mark.parametrize(
    ("url", "headers"),
    [
        ("https://arxiv.org/pdf/2401.01234", {"Content-Type": "application/pdf"}),
        ("https://example.org/download?id=7", {"Content-Type": "application/octet-stream"}),
        ("https://example.org/file", {}),
        ("https://example.org/report.aspx", {"Content-Type": "text/html"}),
    ],
)
def test_pdf_is_recognized_by_signature_not_url_suffix(
    public_dns: list[str], url: str, headers: dict[str, str]
) -> None:
    session = FakeSession([FakeResponse(url, headers=headers, body=PDF)])

    resource = fetch_url(url, session=session)

    assert resource.kind == "pdf"
    assert resource.body == PDF
    assert session.trust_env is False


def test_html_is_recognized_and_charset_kept(public_dns: list[str]) -> None:
    response = FakeResponse(
        "https://example.org/a",
        headers={"Content-Type": "text/html; charset=GBK"},
        body=HTML,
    )

    resource = fetch_url(response.url, session=FakeSession([response]))

    assert resource.kind == "html"
    assert resource.charset == "gbk"
    assert response.closed


def test_html_without_content_type_is_sniffed(public_dns: list[str]) -> None:
    response = FakeResponse("https://example.org/a", headers={}, body=HTML)

    assert fetch_url(response.url, session=FakeSession([response])).kind == "html"


def test_declared_pdf_without_signature_is_invalid(public_dns: list[str]) -> None:
    response = FakeResponse(
        "https://example.org/a.pdf",
        headers={"Content-Type": "application/pdf"},
        body=b"<html>blocked</html>",
    )

    with pytest.raises(FetchError) as raised:
        fetch_url(response.url, session=FakeSession([response]))

    assert raised.value.category == "invalid_pdf"


def test_unsupported_content_type_is_categorized(public_dns: list[str]) -> None:
    response = FakeResponse(
        "https://example.org/a.mp4", headers={"Content-Type": "video/mp4"}, body=b"\x00" * 10
    )

    with pytest.raises(FetchError) as raised:
        fetch_url(response.url, session=FakeSession([response]))

    assert raised.value.category == "unsupported_content_type"
    assert "video/mp4" in raised.value.detail


def test_forbidden_is_not_retried_and_keeps_status(public_dns: list[str]) -> None:
    session = FakeSession([FakeResponse("https://example.mil/a", status_code=403)])

    with pytest.raises(FetchError) as raised:
        fetch_url("https://example.mil/a", session=session, policy=NO_WAIT, sleep=no_sleep)

    assert raised.value.category == "http_error"
    assert raised.value.status_code == 403
    assert "automated" in raised.value.detail
    assert len(session.requests) == 1


def test_transient_errors_are_retried_then_succeed(public_dns: list[str]) -> None:
    session = FakeSession(
        [
            FakeResponse("https://example.org/a", status_code=503),
            requests.ConnectionError("reset"),
            FakeResponse("https://example.org/a", headers={"Content-Type": "text/html"}, body=HTML),
        ]
    )
    waits: list[float] = []

    resource = fetch_url(
        "https://example.org/a", session=session, policy=NO_WAIT, sleep=waits.append
    )

    assert resource.attempts == 3
    assert len(waits) == 2


def test_rate_limit_respects_retry_after_and_gives_up(public_dns: list[str]) -> None:
    responses = [
        FakeResponse("https://example.org/a", status_code=429, headers={"Retry-After": "4"})
        for _ in range(3)
    ]
    waits: list[float] = []

    with pytest.raises(FetchError) as raised:
        fetch_url(
            "https://example.org/a",
            session=FakeSession(responses),
            policy=NO_WAIT,
            sleep=waits.append,
        )

    assert raised.value.category == "rate_limited"
    assert waits == [4.0, 4.0]


def test_timeout_is_categorized(public_dns: list[str]) -> None:
    session = FakeSession([requests.Timeout("t")] * 3)

    with pytest.raises(FetchError) as raised:
        fetch_url("https://example.org/a", session=session, policy=NO_WAIT, sleep=no_sleep)

    assert raised.value.category == "timeout"


@pytest.mark.parametrize(
    ("headers", "body"),
    [
        ({"Content-Type": "application/pdf", "Content-Length": "999999"}, PDF),
        ({"Content-Type": "application/pdf"}, PDF + b"y" * 5000),
    ],
)
def test_size_limits_are_enforced(
    public_dns: list[str], headers: dict[str, str], body: bytes
) -> None:
    response = FakeResponse("https://example.org/a.pdf", headers=headers, body=body)

    with pytest.raises(FetchError) as raised:
        fetch_url(
            response.url,
            session=FakeSession([response]),
            policy=FetchPolicy(max_pdf_bytes=4096),
        )

    assert raised.value.category == "too_large"


def test_every_redirect_is_revalidated(public_dns: list[str]) -> None:
    session = FakeSession(
        [
            FakeResponse("https://a.org/x", status_code=302, headers={"Location": "/y"}),
            FakeResponse("https://a.org/y", headers={"Content-Type": "application/pdf"}, body=PDF),
        ]
    )

    resource = fetch_url("https://a.org/x", session=session)

    assert public_dns == ["https://a.org/x", "https://a.org/y"]
    assert resource.redirects == ["https://a.org/x"]


def test_private_destination_is_security_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    def reject(url: str, **kwargs: object) -> list[str]:
        raise URLValidationError("resolves to blocked address 10.0.0.1")

    monkeypatch.setattr(fetch, "resolve_public_ips", reject)

    with pytest.raises(FetchError) as raised:
        fetch_url("https://intranet.example/a", session=FakeSession([]))

    assert raised.value.category == "security_rejected"


def test_dns_failure_is_categorized_without_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(url: str, **kwargs: object) -> list[str]:
        raise DNSResolutionError("Could not resolve")

    monkeypatch.setattr(fetch, "resolve_public_ips", fail)

    with pytest.raises(FetchError) as raised:
        fetch_url("https://blocked.example.org/a", session=FakeSession([]))

    assert raised.value.category == "dns_error"


def test_explicit_proxy_resolves_names_local_dns_cannot(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(url: str, **kwargs: object) -> list[str]:
        raise DNSResolutionError("Could not resolve")

    monkeypatch.setattr(fetch, "resolve_public_ips", fail)
    monkeypatch.setattr(fetch, "pinned_dns", lambda *args: contextlib.nullcontext())
    session = FakeSession(
        [FakeResponse("https://upload.wikimedia.org/a.pdf", headers={}, body=PDF)]
    )

    resource = fetch_url(
        "https://upload.wikimedia.org/a.pdf",
        session=session,
        policy=FetchPolicy(proxy_url="http://127.0.0.1:7890"),
    )

    assert resource.kind == "pdf"
    assert session.requests[0]["proxies"] == {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
    }


@pytest.mark.parametrize("url", ["https://10.0.0.5/a", "https://printer.local/a"])
def test_proxy_never_delegates_literal_or_internal_hosts(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    def fail(url: str, **kwargs: object) -> list[str]:
        raise DNSResolutionError("Could not resolve")

    monkeypatch.setattr(fetch, "resolve_public_ips", fail)

    with pytest.raises(FetchError) as raised:
        fetch_url(url, session=FakeSession([]), policy=FetchPolicy(proxy_url="http://p:1"))

    assert raised.value.category == "dns_error"


def test_allowed_networks_are_passed_to_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def resolve(url: str, **kwargs: object) -> list[str]:
        seen.update(kwargs)
        return ["198.18.0.7"]

    monkeypatch.setattr(fetch, "resolve_public_ips", resolve)
    monkeypatch.setattr(fetch, "pinned_dns", lambda *args: contextlib.nullcontext())
    networks = (ipaddress.ip_network("198.18.0.0/15"),)

    fetch_url(
        "https://example.org/a",
        session=FakeSession([FakeResponse("https://example.org/a", body=HTML)]),
        policy=FetchPolicy(allowed_networks=networks),
    )

    assert seen["allowed_networks"] == networks


def test_user_agent_identifies_operator_when_configured() -> None:
    assert "mailto:me@example.org" in build_user_agent("me@example.org")
    assert "mailto" not in build_user_agent(None)
