"""Shared pytest fixtures."""

from __future__ import annotations

import contextlib
import os

import pytest

from afsim_research_agent.web import fetch


@pytest.fixture(scope="session")
def capability_set():
    """The local capability set (exported from the lab project, never committed).

    Tests that need it are skipped on machines without it."""
    from afsim_research_agent.capability import CapabilityCatalog, CatalogError

    try:
        return CapabilityCatalog.load()
    except CatalogError:
        pytest.skip("本机没有能力集（见 datasets/platform_capabilities/README.md）")


@pytest.fixture
def public_dns(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Pretend every hostname resolves to a public address; record checked URLs."""
    checked: list[str] = []

    def resolve(url: str, **kwargs: object) -> list[str]:
        checked.append(url)
        return ["93.184.216.34"]

    monkeypatch.setattr(fetch, "resolve_public_ips", resolve)
    monkeypatch.setattr(fetch, "pinned_dns", lambda *args: contextlib.nullcontext())
    return checked


_ISOLATED_PREFIXES = (
    "CROW_",
    "OPENALEX_",
    "SEMANTIC_SCHOLAR_",
    "CORE_",
    "TAVILY_",
    "DASHSCOPE_",
    "PAPERQA_",
    "AFSIM_RESEARCH_",
)


@pytest.fixture(autouse=True)
def isolate_environment(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests must not see the developer's .env or shell configuration.

    Live tests keep the real environment. Unit tests get a clean slate and a
    single fetch worker so fetch order is deterministic.
    """
    if request.node.get_closest_marker("live"):
        return
    for name in list(os.environ):
        if name.startswith(_ISOLATED_PREFIXES):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CROW_FETCH_WORKERS", "1")
