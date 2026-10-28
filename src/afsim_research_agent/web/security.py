"""URL validation used before every outbound webpage request."""

from __future__ import annotations

import contextlib
import ipaddress
import socket
import threading
from collections.abc import Iterator, Sequence
from urllib.parse import urlparse

_ALLOWED_SCHEMES = frozenset({"http", "https"})

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class URLValidationError(ValueError):
    """Raised when a URL cannot be fetched safely."""


class DNSResolutionError(URLValidationError):
    """Raised when a hostname cannot be resolved at all (not a policy rejection)."""


def parse_networks(values: Sequence[str]) -> tuple[IPNetwork, ...]:
    """Parse CIDR strings such as ``198.18.0.0/15``; blank entries are ignored."""
    return tuple(ipaddress.ip_network(value.strip()) for value in values if value.strip())


def _normalize(address: IPAddress) -> IPAddress:
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return address.ipv4_mapped
        if address.sixtofour is not None:
            return address.sixtofour
    return address


def _is_blocked(
    address: IPAddress,
    allowed_networks: Sequence[IPNetwork] = (),
) -> bool:
    address = _normalize(address)
    for network in allowed_networks:
        if address.version == network.version and address in network:
            return False
    return not address.is_global


def resolve_public_ips(
    url: str,
    *,
    allowed_networks: Sequence[IPNetwork] = (),
) -> list[str]:
    """Resolve a HTTP URL and reject any non-public DNS answer.

    ``allowed_networks`` is an explicit opt-in for proxy setups that answer DNS
    with addresses from a reserved range (for example a TUN-mode proxy using the
    ``198.18.0.0/15`` fake-IP pool). It should never include loopback or
    link-local ranges.
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        msg = f"URL scheme must be http or https, got {parsed.scheme!r}"
        raise URLValidationError(msg)
    if not parsed.hostname:
        raise URLValidationError("URL is missing a hostname")

    try:
        hostname = parsed.hostname.encode("idna").decode("ascii")
        answers = socket.getaddrinfo(
            hostname,
            None,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except (UnicodeError, socket.gaierror) as exc:
        msg = f"Could not resolve URL hostname {parsed.hostname!r}: {exc}"
        raise DNSResolutionError(msg) from exc

    addresses: list[str] = []
    for answer in answers:
        raw_address = str(answer[4][0]).split("%", 1)[0]
        address = ipaddress.ip_address(raw_address)
        if _is_blocked(address, allowed_networks):
            msg = f"URL hostname {parsed.hostname!r} resolves to blocked address {address}"
            raise URLValidationError(msg)
        if raw_address not in addresses:
            addresses.append(raw_address)

    if not addresses:
        msg = f"URL hostname {parsed.hostname!r} resolved to no addresses"
        raise DNSResolutionError(msg)
    return addresses


_PIN_STATE = threading.local()
_INSTALL_LOCK = threading.Lock()
_active_pins = 0
_original_factory = None


def _pinned_create_connection(address: tuple[str, int], *args: object, **kwargs: object):
    host, port = address
    pins: dict[str, list[str]] = getattr(_PIN_STATE, "pins", {})
    allowed_ips = pins.get(host)
    if not allowed_ips:
        return _original_factory(address, *args, **kwargs)  # type: ignore[misc]
    last_error: OSError | None = None
    for allowed_ip in allowed_ips:
        try:
            return _original_factory((allowed_ip, port), *args, **kwargs)  # type: ignore[misc]
        except OSError as exc:
            last_error = exc
    raise last_error or OSError("No validated IP address is available")


@contextlib.contextmanager
def pinned_dns(hostname: str, allowed_ips: list[str]) -> Iterator[None]:
    """Make urllib3 connect only to addresses that passed URL validation.

    Pins are thread-local, so concurrent downloads do not serialize on a global
    lock; the patched connection factory is installed while at least one pin is
    active and the original is restored when the last one exits.
    """
    global _active_pins, _original_factory
    from urllib3.util import connection as urllib3_connection

    with _INSTALL_LOCK:
        if _active_pins == 0:
            _original_factory = urllib3_connection.create_connection
            urllib3_connection.create_connection = _pinned_create_connection
        _active_pins += 1
    pins: dict[str, list[str]] = getattr(_PIN_STATE, "pins", None) or {}
    _PIN_STATE.pins = pins
    previous = pins.get(hostname)
    pins[hostname] = list(allowed_ips)
    try:
        yield
    finally:
        if previous is None:
            pins.pop(hostname, None)
        else:
            pins[hostname] = previous
        with _INSTALL_LOCK:
            _active_pins -= 1
            if _active_pins == 0:
                urllib3_connection.create_connection = _original_factory
                _original_factory = None
