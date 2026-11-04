"""Progress of long stages for the workbench: what is running right now.

A stage reports with :func:`report` (a short Chinese line); the orchestrator's tool runner
installs a sink for the duration of one tool call (:func:`sink`) that forwards the lines
to the browser as LangGraph custom stream events. Outside a tool call (CLI, tests, scripts)
nothing is installed and :func:`report` does nothing.

The lines go to the local browser only: they are not part of the agent's messages and are
never sent to a model service. Keep them free of native names and coordinates anyway.
"""

from __future__ import annotations

import contextvars
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

_SINK: contextvars.ContextVar[Callable[[str], None] | None] = contextvars.ContextVar(
    "afsim_progress_sink", default=None
)

# names of Crow research stages (crow.research timings) and Falcon model calls
LABELS = {
    "plan": "研究规划",
    "collect": "检索并下载资料",
    "embed": "建立资料索引",
    "answer": "从资料中整理证据",
    "profile": "任务画像",
}


def report(text: str) -> None:
    """Tell the browser what the running stage is doing (no-op outside a tool call)."""
    sink = _SINK.get()
    if sink is not None:
        try:
            sink(str(text)[:300])
        except Exception:  # noqa: BLE001 - progress must never break a stage
            pass


def stage_label(stage: str) -> str:
    """``collect_round_2`` → ``检索并下载资料（第 2 轮）``."""
    base, _, rest = stage.partition("_round_")
    label = LABELS.get(base, base)
    return f"{label}（第 {rest} 轮）" if rest else label


@contextmanager
def sink(callback: Callable[[str], None]) -> Iterator[None]:
    token = _SINK.set(callback)
    try:
        yield
    finally:
        _SINK.reset(token)


class Timer:
    def __init__(self) -> None:
        self.started = time.perf_counter()

    @property
    def seconds(self) -> int:
        return int(time.perf_counter() - self.started)
