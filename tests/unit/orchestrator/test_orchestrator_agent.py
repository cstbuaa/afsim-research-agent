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


def test_default_agent_sees_v3_tools_and_writes_only_notes(tmp_path) -> None:
    """The served agent: no old-flow tools, and the file tools cannot change stage products
    (a hand-edited model package bypassed every check, r3 11-04); notes can be written."""
    from langchain_core.messages import HumanMessage
    from langgraph.checkpoint.memory import InMemorySaver

    from afsim_research_agent.orchestrator.graph import build_agent
    from afsim_research_agent.orchestrator.testing import ScriptedChatModel, call
    from afsim_research_agent.orchestrator.tools import LEGACY_TOOLS

    root = tmp_path / "data"
    pkg = root / "runs" / "m" / "models" / "p"
    pkg.mkdir(parents=True)
    (pkg / "tactic_sheet.md").write_text("原稿\n", encoding="utf-8")
    ws = Workspace(root.resolve())
    model = ScriptedChatModel(
        turns=[
            [
                call(
                    "edit_file",
                    {
                        "file_path": "/runs/m/models/p/tactic_sheet.md",
                        "old_string": "原稿",
                        "new_string": "手改",
                    },
                    "c1",
                ),
                call("write_file", {"file_path": "/runs/notes/n.md", "content": "笔记"}, "c2"),
            ],
            "完成。",
        ]
    )
    agent = build_agent(model, workspace=ws, checkpointer=InMemorySaver(), interrupt=False)
    agent.invoke({"messages": [HumanMessage("试")]}, {"configurable": {"thread_id": "t-perm"}})
    assert not set(model.seen_tools) & LEGACY_TOOLS
    assert {"falcon_draft3", "simulate2d", "afsim_run"} <= set(model.seen_tools)
    assert (pkg / "tactic_sheet.md").read_text(encoding="utf-8") == "原稿\n"
    assert (root / "runs" / "notes" / "n.md").read_text(encoding="utf-8").strip() == "笔记"
