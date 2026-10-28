"""Network-free HTTP fakes shared by unit tests."""

from __future__ import annotations


class FakeResponse:
    """Small requests-compatible streaming response."""

    def __init__(
        self,
        url: str,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
    ) -> None:
        self.url = url
        self.status_code = status_code
        self.headers = headers if headers is not None else {"Content-Type": "text/html"}
        self.body = body
        self.closed = False

    def iter_content(self, chunk_size: int):
        for index in range(0, len(self.body), chunk_size):
            yield self.body[index : index + chunk_size]

    def close(self) -> None:
        self.closed = True


class FakeSession:
    """Return queued responses (or raise queued exceptions) in order."""

    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        self.responses = list(responses)
        self.trust_env = True
        self.requests: list[dict[str, object]] = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.requests.append({"url": url, **kwargs})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
