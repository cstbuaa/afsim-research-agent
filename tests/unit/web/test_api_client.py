"""Tests for the shared rate-limited API client."""

from __future__ import annotations

import pytest
import requests

from afsim_research_agent.web.api_client import ApiClient
from afsim_research_agent.web.search import SearchError


class Response:
    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self.headers = headers or {}

    def json(self) -> object:
        return {"ok": True}


class Session:
    def __init__(self, items: list[object]) -> None:
        self.items = items
        self.calls: list[dict[str, object]] = []

    def get(self, url: str, **kwargs: object) -> object:
        self.calls.append({"url": url, **kwargs})
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 2))
        self.now += seconds

    def __call__(self) -> float:
        return self.now


def client(items: list[object], **kwargs: object) -> tuple[ApiClient, Session, Clock]:
    session = Session(items)
    clock = Clock()
    return (
        ApiClient("Test", session=session, sleep=clock.sleep, clock=clock, **kwargs),
        session,
        clock,
    )


def test_minimum_interval_is_enforced_between_calls() -> None:
    api, _, clock = client([Response(200), Response(200)], min_interval=3.0)

    api.get_json("https://x")
    api.get_json("https://x")

    assert clock.sleeps == [3.0]


def test_retries_honour_retry_after_then_succeed() -> None:
    api, session, clock = client(
        [Response(429, {"Retry-After": "7"}), requests.ConnectionError("x"), Response(200)]
    )

    assert api.get_json("https://x") == {"ok": True}
    assert clock.sleeps == [7.0, 4.0]
    assert len(session.calls) == 3


def test_permanent_errors_are_not_retried() -> None:
    api, session, _ = client([Response(403)])

    with pytest.raises(SearchError, match="Test returned HTTP 403"):
        api.get("https://x")
    assert len(session.calls) == 1


def test_headers_and_proxy_are_sent() -> None:
    api, session, _ = client(
        [Response(200)], headers={"x-api-key": "k"}, proxy_url="http://127.0.0.1:7890"
    )

    api.get("https://x", {"q": "a"})

    call = session.calls[0]
    assert call["headers"]["x-api-key"] == "k"
    assert call["proxies"] == {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
    assert call["params"] == {"q": "a"}
