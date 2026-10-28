"""Tests for URL validation before webpage retrieval."""

import socket

import pytest
from urllib3.util import connection as urllib3_connection

from afsim_research_agent.web.security import (
    URLValidationError,
    pinned_dns,
    resolve_public_ips,
)


def _dns_result(address: str) -> list[tuple[object, ...]]:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 0))]


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/file",
        "http:///missing-host",
    ],
)
def test_resolve_public_ips_rejects_invalid_targets(url: str) -> None:
    with pytest.raises(URLValidationError):
        resolve_public_ips(url)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "::1",
        "::ffff:127.0.0.1",
    ],
)
def test_resolve_public_ips_rejects_non_public_dns_answers(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: _dns_result(address))

    with pytest.raises(URLValidationError, match="blocked address"):
        resolve_public_ips("https://example.com/article")


def test_resolve_public_ips_returns_all_public_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = _dns_result("93.184.216.34") + _dns_result("2606:2800:220:1:248:1893:25c8:1946")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: answers)

    assert resolve_public_ips("https://example.com/article") == [
        "93.184.216.34",
        "2606:2800:220:1:248:1893:25c8:1946",
    ]


def test_resolve_public_ips_reports_dns_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_dns(*args: object, **kwargs: object) -> list[tuple[object, ...]]:
        raise socket.gaierror("not found")

    monkeypatch.setattr(socket, "getaddrinfo", fail_dns)

    with pytest.raises(URLValidationError, match="Could not resolve"):
        resolve_public_ips("https://missing.example")


def test_pinned_dns_uses_validated_address_and_restores_connection_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int]] = []

    def connect(address: tuple[str, int], *args: object, **kwargs: object) -> object:
        calls.append(address)
        return object()

    monkeypatch.setattr(urllib3_connection, "create_connection", connect)
    original = urllib3_connection.create_connection

    with pinned_dns("example.com", ["93.184.216.34"]):
        urllib3_connection.create_connection(("example.com", 443))

    assert calls == [("93.184.216.34", 443)]
    assert urllib3_connection.create_connection is original


def test_allowed_networks_admit_proxy_fake_ip_range(monkeypatch: pytest.MonkeyPatch) -> None:
    from afsim_research_agent.web.security import parse_networks

    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: _dns_result("198.18.0.9"))

    with pytest.raises(URLValidationError, match="blocked address"):
        resolve_public_ips("https://example.com/a")
    assert resolve_public_ips(
        "https://example.com/a", allowed_networks=parse_networks(["198.18.0.0/15"])
    ) == ["198.18.0.9"]


def test_allowed_networks_do_not_open_other_private_ranges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from afsim_research_agent.web.security import parse_networks

    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: _dns_result("127.0.0.1"))

    with pytest.raises(URLValidationError, match="blocked address"):
        resolve_public_ips(
            "https://example.com/a", allowed_networks=parse_networks(["198.18.0.0/15"])
        )


def test_dns_failure_is_a_distinct_subclass(monkeypatch: pytest.MonkeyPatch) -> None:
    from afsim_research_agent.web.security import DNSResolutionError

    def fail_dns(*args: object, **kwargs: object) -> list[tuple[object, ...]]:
        raise socket.gaierror("not found")

    monkeypatch.setattr(socket, "getaddrinfo", fail_dns)

    with pytest.raises(DNSResolutionError):
        resolve_public_ips("https://missing.example")


def test_pins_are_thread_local_and_do_not_serialize(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    seen: dict[str, tuple[str, int]] = {}
    inside = threading.Barrier(2, timeout=5)

    def connect(address: tuple[str, int], *args: object, **kwargs: object) -> object:
        seen[threading.current_thread().name] = address
        return object()

    monkeypatch.setattr(urllib3_connection, "create_connection", connect)
    original = urllib3_connection.create_connection

    def worker(ip: str) -> None:
        with pinned_dns("example.com", [ip]):
            inside.wait()  # both threads hold a pin at the same time
            urllib3_connection.create_connection(("example.com", 443))

    threads = [
        threading.Thread(target=worker, args=(ip,), name=ip)
        for ip in ("93.184.216.34", "93.184.216.35")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert seen == {
        "93.184.216.34": ("93.184.216.34", 443),
        "93.184.216.35": ("93.184.216.35", 443),
    }
    assert urllib3_connection.create_connection is original
