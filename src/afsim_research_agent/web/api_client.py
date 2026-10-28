"""Small shared HTTP client for scholarly JSON/XML APIs.

It adds what every provider needs and nothing else: an identifying
User-Agent, an explicit proxy, a minimum interval between calls (polite rate
limiting), retries for 429/5xx, and secret-free error messages.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from typing import Protocol

import requests

from afsim_research_agent.web.search import SearchError

_RETRYABLE = frozenset({429, 500, 502, 503, 504})


class HttpGetter(Protocol):
    def get(self, url: str, **kwargs: object) -> requests.Response: ...


class ApiClient:
    """Rate-limited GET with retries; one instance per provider."""

    def __init__(
        self,
        name: str,
        *,
        session: HttpGetter | None = None,
        headers: Mapping[str, str] | None = None,
        proxy_url: str | None = None,
        min_interval: float = 0.0,
        timeout: float = 30.0,
        max_retries: int = 2,
        backoff_seconds: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self._session = session or requests.Session()
        self._headers = {
            "User-Agent": "AFSIMResearchAgent/0.3 (academic literature research)",
            **dict(headers or {}),
        }
        self._proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        self._min_interval = min_interval
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._last_call = float("-inf")

    def get(self, url: str, params: Mapping[str, object] | None = None) -> requests.Response:
        attempt = 0
        while True:
            attempt += 1
            self._wait_turn()
            try:
                response = self._session.get(
                    url,
                    params=dict(params or {}),
                    headers=self._headers,
                    timeout=self._timeout,
                    proxies=self._proxies,
                )
            except requests.RequestException as exc:
                if attempt <= self._max_retries:
                    self._sleep(self._backoff * attempt)
                    continue
                raise SearchError(f"{self.name} request failed: {type(exc).__name__}") from exc
            status = response.status_code
            if status == 200:
                return response
            if status in _RETRYABLE and attempt <= self._max_retries:
                retry_after = _float(response.headers.get("Retry-After")) if hasattr(
                    response, "headers"
                ) else None
                self._sleep(min(30.0, retry_after or self._backoff * attempt))
                continue
            raise SearchError(f"{self.name} returned HTTP {status}")

    def get_json(self, url: str, params: Mapping[str, object] | None = None) -> object:
        response = self.get(url, params)
        try:
            return response.json()
        except ValueError as exc:
            raise SearchError(f"{self.name} returned invalid JSON") from exc

    def _wait_turn(self) -> None:
        with self._lock:
            wait = self._last_call + self._min_interval - self._clock()
            if wait > 0:
                self._sleep(wait)
            self._last_call = self._clock()


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None
