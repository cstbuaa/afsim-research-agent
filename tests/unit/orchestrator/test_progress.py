"""Progress lines of a running tool reach the stream writer (thread-safe), and nothing
happens outside a tool call."""

from __future__ import annotations

import asyncio
import sys
import types

from afsim_research_agent import progress
from afsim_research_agent.orchestrator import tools
from afsim_research_agent.orchestrator.results import StageResult


def test_report_outside_a_tool_call_does_nothing() -> None:
    progress.report("无人接收")  # no sink: no error
    assert progress.stage_label("collect_round_2") == "检索并下载资料（第 2 轮）"
    assert progress.stage_label("plan") == "研究规划"


def test_tool_progress_and_thread_reports_reach_the_writer(monkeypatch) -> None:
    got: list[dict] = []
    fake = types.ModuleType("langgraph.config")
    fake.get_stream_writer = lambda: got.append  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "langgraph", types.ModuleType("langgraph"))
    monkeypatch.setitem(sys.modules, "langgraph.config", fake)
    monkeypatch.setattr(tools, "HEARTBEAT_S", 0.05)

    def stage(ws, **kwargs):  # runs in a worker thread
        progress.report("检索资料：3 组检索词")
        import time

        time.sleep(0.15)
        return StageResult("demo", "ok", "完成")

    spec = tools.ToolSpec("demo", stage, tools.NoArgs, "demo", "meta")
    run = tools._async_runner(spec, None, None, None, None)
    out = asyncio.run(run())
    assert '"status": "ok"' in out or '"ok"' in out
    kinds = [e["kind"] for e in got]
    assert kinds[0] == "start" and kinds[-1] == "end"
    assert any(e["text"] == "检索资料：3 组检索词" for e in got)
    assert "heartbeat" in kinds
    assert all(e["type"] == "workbench_progress" and e["tool"] == "demo" for e in got)
    progress.report("调用结束后不再转发")
    assert got[-1]["kind"] == "end"
