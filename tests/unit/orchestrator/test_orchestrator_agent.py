"""The real deep agent with a scripted model: tools visible, approval pause, ask_human."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

deepagents = pytest.importorskip("deepagents")
pytest.importorskip("langgraph.checkpoint.memory")
tools_mod = pytest.importorskip("langchain_core.tools")
if not hasattr(tools_mod.StructuredTool, "from_function") or not getattr(
    deepagents, "__version__", None
):
    pytest.skip("deepagents / langchain not installed (stubs only)", allow_module_level=True)

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "falcon"))

from test_orchestrator_stages import _drafted  # noqa: E402

from afsim_research_agent.orchestrator.cli import _check  # noqa: E402
from afsim_research_agent.orchestrator.workspace import Workspace  # noqa: E402


def test_offline_check_passes(tmp_path) -> None:
    root = tmp_path / "data"
    (root / "runs").mkdir(parents=True)
    ws = Workspace(root.resolve())
    pkg = _drafted(ws)
    assert _check(ws, pkg) == 0


def test_offline_check_without_package(tmp_path) -> None:
    root = tmp_path / "data"
    (root / "runs").mkdir(parents=True)
    assert _check(Workspace(root.resolve()), None) == 0
