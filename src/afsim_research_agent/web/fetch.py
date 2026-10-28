"""One secure download path for every internet source.

The fetcher decides what a resource *is* from the response (``Content-Type``
header and the leading bytes), never from the URL suffix, so download
endpoints such as ``/download?id=42`` or ``arxiv.org/pdf/2401.01234`` are
handled like any other PDF.

Every failure is raised as :class:`FetchError` with a stable ``category`` and a
short, secret-free ``detail`` so the collection ledger can explain exactly why
a URL was not used.
"""

from __future__ import annotations

import contextlib
import ipaddress
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urljoin, urlparse

import requests

from afsim_research_agent.models import FailureCategory
from afsim_research_agent.web.security import (
    DNSResolutionError,
    IPNetwork,
    URLValidationError,
    pinned_dns,
    resolve_public_ips,
)

ResourceKind = Literal["pdf", "html"]

_MAX_REDIRECTS = 5
_DEFAULT_MAX_HTML_BYTES = 5_000_000
_DEFAULT_MAX_PDF_BYTES = 40_000_000
_HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_PDF_CONTENT_TYPES = frozenset({"application/pdf", "application/x-pdf"})
_AMBIGUOUS_CONTENT_TYPES = frozenset(
    {"", "application/octet-stream", "binary/octet-stream", "application/download",
     "application/force-download", "application/x-download"}
)
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_RETRY_AFTER_SECONDS = 10.0
_SNIFF_BYTES = 1024
_INTERNAL_HOST_SUFFIXES = (".local", ".internal", ".lan", ".localhost", ".home.arpa")

DEFAULT_USER_AGENT = "AFSIMResearchAgent/0.2 (academic literature research)"


def build_user_agent(contact: str | None) -> str:
    """Return an honest, identifiable User-Agent as most publishers request."""
    if contact and contact.strip():
        return f"AFSIMResearchAgent/0.2 (academic literature research; mailto:{contact.strip()})"
    return DEFAULT_USER_AGENT


class FetchError(RuntimeError):
    """A categorized, diagnosable download failure."""

    def __init__(
        self,
        category: FailureCategory,
        detail: str,
        *,
        url: str | None = None,
        status_code: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(f"{category}: {detail}")
        self.category: FailureCategory = category
        self.detail = detail
        self.url = url
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after


@dataclass(frozen=True)
class FetchPolicy:
    """Network policy shared by all downloads in one run."""

    connect_timeout: float = 10.0
    timeout: float = 30.0
    max_html_bytes: int = _DEFAULT_MAX_HTML_BYTES
    max_pdf_bytes: int = _DEFAULT_MAX_PDF_BYTES
    max_retries: int = 1
    backoff_seconds: float = 1.5
    user_agent: str = DEFAULT_USER_AGENT
    proxy_url: str | None = None
    allowed_networks: tuple[IPNetwork, ...] = ()


@dataclass(frozen=True)
class FetchedResource:
    """A downloaded and classified response body."""

    requested_url: str
    final_url: str
    status_code: int
    content_type: str
    kind: ResourceKind
    body: bytes
    charset: str | None
    attempts: int
    redirects: list[str] = field(default_factory=list)


def fetch_url(
    url: str,
    *,
    policy: FetchPolicy | None = None,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> FetchedResource:
    """Download one URL, retrying only transient failures."""
    active_policy = policy or FetchPolicy()
    client = session or requests.Session()
    # Proxies are only used when configured explicitly in the policy, so a
    # stray shell variable cannot silently change the network path.
    client.trust_env = False

    attempt = 0
    while True:
        attempt += 1
        try:
            return _fetch_once(url, policy=active_policy, client=client, attempt=attempt)
        except FetchError as exc:
            if not exc.retryable or attempt > active_policy.max_retries:
                raise
            delay = active_policy.backoff_seconds * (2 ** (attempt - 1))
            if exc.retry_after is not None:
                delay = max(delay, exc.retry_after)
            sleep(min(delay, _MAX_RETRY_AFTER_SECONDS))


def _fetch_once(
    url: str,
    *,
    policy: FetchPolicy,
    client: requests.Session,
    attempt: int,
) -> FetchedResource:
    current_url = url
    redirects: list[str] = []
    proxies = (
        {"http": policy.proxy_url, "https": policy.proxy_url} if policy.proxy_url else None
    )

    for _hop in range(_MAX_REDIRECTS + 1):
        hostname = urlparse(current_url).hostname
        addresses = _validate_destination(current_url, policy)
        if hostname is None:
            raise FetchError("security_rejected", "URL is missing a hostname", url=current_url)
        encoded_hostname = hostname.encode("idna").decode("ascii")

        try:
            with pinned_dns(encoded_hostname, addresses) if addresses else contextlib.nullcontext():
                response = client.get(
                    current_url,
                    timeout=(policy.connect_timeout, policy.timeout),
                    headers={
                        "User-Agent": policy.user_agent,
                        "Accept": (
                            "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.5"
                        ),
                        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
                    },
                    allow_redirects=False,
                    stream=True,
                    proxies=proxies,
                )
        except requests.Timeout as exc:
            raise FetchError(
                "timeout", f"no response within {policy.timeout:g}s", url=current_url,
                retryable=True,
            ) from exc
        except requests.exceptions.SSLError as exc:
            raise FetchError(
                "network_error", "TLS handshake or certificate verification failed",
                url=current_url,
            ) from exc
        except requests.ConnectionError as exc:
            raise FetchError(
                "network_error", "connection failed or was reset", url=current_url,
                retryable=True,
            ) from exc
        except requests.RequestException as exc:
            raise FetchError(
                "network_error", type(exc).__name__, url=current_url
            ) from exc

        if 300 <= response.status_code < 400:
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise FetchError(
                    "http_error", "redirect without Location header", url=current_url,
                    status_code=response.status_code,
                )
            redirects.append(current_url)
            current_url = urljoin(current_url, location)
            continue

        try:
            return _classify_response(
                response,
                requested_url=url,
                current_url=current_url,
                policy=policy,
                attempt=attempt,
                redirects=redirects,
            )
        finally:
            response.close()

    raise FetchError("http_error", f"more than {_MAX_REDIRECTS} redirects", url=url)


def _validate_destination(url: str, policy: FetchPolicy) -> list[str]:
    """Return pinned addresses, or ``[]`` when an explicit proxy resolves the host."""
    try:
        return resolve_public_ips(url, allowed_networks=policy.allowed_networks)
    except DNSResolutionError as exc:
        hostname = (urlparse(url).hostname or "").lower()
        if policy.proxy_url and _looks_public_hostname(hostname):
            # Local DNS may be filtered while the configured proxy can resolve
            # the name. Literal IPs and internal names are never delegated.
            return []
        raise FetchError("dns_error", "hostname could not be resolved", url=url) from exc
    except URLValidationError as exc:
        raise FetchError("security_rejected", str(exc), url=url) from exc


def _looks_public_hostname(hostname: str) -> bool:
    if not hostname or "." not in hostname or hostname.endswith(_INTERNAL_HOST_SUFFIXES):
        return False
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return True
    return False


def _classify_response(
    response: requests.Response,
    *,
    requested_url: str,
    current_url: str,
    policy: FetchPolicy,
    attempt: int,
    redirects: Sequence[str],
) -> FetchedResource:
    status = response.status_code
    if status >= 400:
        category: FailureCategory = "rate_limited" if status == 429 else "http_error"
        raise FetchError(
            category,
            f"HTTP {status}{_status_hint(status)}",
            url=current_url,
            status_code=status,
            retryable=status in _RETRYABLE_STATUS,
            retry_after=_retry_after(response.headers.get("Retry-After")),
        )

    content_type, charset = _parse_content_type(response.headers.get("Content-Type", ""))
    declared_length = _int_or_none(response.headers.get("Content-Length"))
    chunks = response.iter_content(chunk_size=64 * 1024)
    body = bytearray()
    for chunk in chunks:
        body.extend(chunk)
        if len(body) >= _SNIFF_BYTES:
            break

    kind = _sniff_kind(content_type, bytes(body[:_SNIFF_BYTES]))
    if kind is None:
        if content_type in _PDF_CONTENT_TYPES:
            raise FetchError(
                "invalid_pdf", "declared as PDF but has no %PDF signature", url=current_url,
                status_code=status,
            )
        raise FetchError(
            "unsupported_content_type",
            f"content type {content_type or 'unknown'!r} is neither HTML nor PDF",
            url=current_url,
            status_code=status,
        )

    limit = policy.max_pdf_bytes if kind == "pdf" else policy.max_html_bytes
    if declared_length is not None and declared_length > limit:
        raise FetchError(
            "too_large", f"declared size {declared_length} bytes exceeds {limit}",
            url=current_url, status_code=status,
        )
    if len(body) > limit:
        raise FetchError("too_large", f"body exceeds {limit} bytes", url=current_url)
    try:
        for chunk in chunks:
            body.extend(chunk)
            if len(body) > limit:
                raise FetchError("too_large", f"body exceeds {limit} bytes", url=current_url)
    except requests.RequestException as exc:
        raise FetchError(
            "network_error", "connection dropped while reading body", url=current_url,
            retryable=True,
        ) from exc

    return FetchedResource(
        requested_url=requested_url,
        final_url=str(getattr(response, "url", "") or current_url),
        status_code=status,
        content_type=content_type,
        kind=kind,
        body=bytes(body),
        charset=charset,
        attempts=attempt,
        redirects=list(redirects),
    )


def _sniff_kind(content_type: str, head: bytes) -> ResourceKind | None:
    if b"%PDF-" in head[:_SNIFF_BYTES]:
        return "pdf"
    if content_type in _HTML_CONTENT_TYPES:
        return "html"
    if content_type in _AMBIGUOUS_CONTENT_TYPES or content_type.startswith("text/"):
        lowered = head.lstrip()[:512].lower()
        if lowered.startswith((b"<!doctype html", b"<html")) or b"<html" in lowered:
            return "html"
    return None


def _parse_content_type(value: str) -> tuple[str, str | None]:
    parts = [part.strip() for part in value.split(";")]
    content_type = parts[0].lower() if parts else ""
    charset = None
    for part in parts[1:]:
        key, _, raw = part.partition("=")
        if key.strip().lower() == "charset" and raw.strip():
            charset = raw.strip().strip('"').lower()
    return content_type, charset


def _status_hint(status: int) -> str:
    return {
        401: " (login required)",
        403: " (access denied; site may block automated clients)",
        404: " (not found)",
        410: " (gone)",
        429: " (rate limited)",
        451: " (unavailable for legal reasons)",
    }.get(status, "")


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None

