"""Durable checkpoints for the workbench agent server (``langgraph dev``).

The dev server keeps checkpoints in memory and pickles them to ``.langgraph_api/`` every
10 s. A pickle that no longer loads after a code change is dropped and then overwritten,
so old threads keep their records (the thread list and last values) but lose their
history; the frontend then opens them empty. ``langgraph.json`` points the server at
``sqlite_checkpointer`` instead: every checkpoint is committed to a SQLite file as it is
written, stored in a format that does not depend on this package's classes.

The file is ``.langgraph_api/checkpoints.sqlite`` under the server's working directory,
or ``WORKBENCH_CHECKPOINT_DB``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

DEFAULT_DB = Path(".langgraph_api") / "checkpoints.sqlite"


def checkpoint_db() -> Path:
    return Path(os.environ.get("WORKBENCH_CHECKPOINT_DB") or DEFAULT_DB)


@asynccontextmanager
async def sqlite_checkpointer(*_: Any) -> AsyncIterator[Any]:
    """Async SQLite checkpointer, created on first use (WAL mode: the server reads while
    a run writes)."""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    path = checkpoint_db()
    path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        await saver.conn.execute("PRAGMA journal_mode=WAL")
        await saver.setup()
        yield saver
