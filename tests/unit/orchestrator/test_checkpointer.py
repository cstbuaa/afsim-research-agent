import asyncio

import pytest

pytest.importorskip("langgraph.checkpoint.sqlite.aio")

from afsim_research_agent.orchestrator.checkpointer import sqlite_checkpointer  # noqa: E402


def test_checkpoints_survive_a_new_saver(tmp_path, monkeypatch) -> None:
    from langgraph.checkpoint.base import empty_checkpoint

    monkeypatch.setenv("WORKBENCH_CHECKPOINT_DB", str(tmp_path / "cp.sqlite"))
    config = {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}

    async def write() -> None:
        async with sqlite_checkpointer() as saver:
            cp = empty_checkpoint()
            cp["channel_values"] = {"messages": ["hello"]}
            await saver.aput(config, cp, {"source": "input", "step": 0}, {})

    async def read():
        async with sqlite_checkpointer() as saver:
            return [t async for t in saver.alist(config)]

    asyncio.run(write())
    rows = asyncio.run(read())  # a fresh saver, as after a server restart
    assert len(rows) == 1 and rows[0].checkpoint["channel_values"]["messages"] == ["hello"]
