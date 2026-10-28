"""Conversation threads for the web UI: drive the agent in the background, collect events.

The browser polls ``Thread.events`` (append-only, numbered). Events are derived from the
agent state (messages, todos) plus pending interrupts, so a thread can be rebuilt from the
checkpointer after a server restart.

Event types::

    user          {text}
    ai            {text}
    tool_call     {call_id, name, args}
    tool_result   {call_id, name, text, result?}     result = parsed StageResult JSON
    todos         {todos: [{content, status}]}
    interrupt     {kind: approval|ask_human|other, interrupt_id, requests?, question?, options?}
    status        {status: running|idle|paused|error, detail?}
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Thread:
    id: str
    title: str
    created: float = field(default_factory=time.time)
    events: list[dict[str, Any]] = field(default_factory=list)
    status: str = "idle"
    pending: list[dict[str, Any]] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    last_todos: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def emit(self, etype: str, /, **data: Any) -> None:
        self.events.append({"seq": len(self.events), "type": etype, "time": time.time(), **data})

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "created": self.created,
            "status": self.status,
            "events": len(self.events),
        }


# ============================================================================ message → events
def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return str(content)


def message_events(thread: Thread, messages: list[Any]) -> None:
    """Emit events for messages not seen before (by id, falling back to position)."""
    for index, m in enumerate(messages):
        key = getattr(m, "id", None) or f"pos{index}"
        if key in thread.seen:
            continue
        thread.seen.add(key)
        kind = getattr(m, "type", "")
        if kind == "human":
            thread.emit("user", text=_text(m.content))
        elif kind == "ai":
            text = _text(m.content).strip()
            if text:
                thread.emit("ai", text=text)
            for c in getattr(m, "tool_calls", None) or []:
                thread.emit(
                    "tool_call", call_id=c.get("id"), name=c.get("name"), args=c.get("args")
                )
        elif kind == "tool":
            text = _text(m.content)
            event: dict[str, Any] = {
                "call_id": getattr(m, "tool_call_id", None),
                "name": getattr(m, "name", None),
                "text": text,
            }
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict) and "status" in parsed and "stage" in parsed:
                    event["result"] = parsed
            except (ValueError, TypeError):
                pass
            thread.emit("tool_result", **event)


def todo_events(thread: Thread, todos: Any) -> None:
    if not todos:
        return
    key = json.dumps(todos, ensure_ascii=False, sort_keys=True, default=str)
    if key != thread.last_todos:
        thread.last_todos = key
        thread.emit("todos", todos=todos)


def normalize_interrupt(intr: Any) -> dict[str, Any]:
    value = getattr(intr, "value", intr)
    iid = getattr(intr, "id", None)
    if isinstance(value, dict) and "action_requests" in value:
        configs = {
            c.get("action_name"): c.get("allowed_decisions")
            for c in value.get("review_configs") or []
            if isinstance(c, dict)
        }
        requests = [
            {
                "name": r.get("name"),
                "args": r.get("args"),
                "description": r.get("description"),
                "allowed": configs.get(r.get("name")) or ["approve", "edit", "reject"],
            }
            for r in value["action_requests"]
        ]
        return {"kind": "approval", "interrupt_id": iid, "requests": requests}
    if isinstance(value, dict) and value.get("type") == "ask_human":
        return {
            "kind": "ask_human",
            "interrupt_id": iid,
            "question": value.get("question", ""),
            "options": list(value.get("options") or []),
        }
    return {"kind": "other", "interrupt_id": iid, "question": str(value)[:2000]}


# ============================================================================ runners
class AgentRunner:
    """Runs a compiled deep agent (LangGraph) for UI threads."""

    def __init__(self, agent: Any, *, model_name: str = "") -> None:
        self.agent = agent
        self.model_name = model_name

    def _config(self, thread: Thread) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread.id}, "recursion_limit": 400}

    def start(self, thread: Thread, text: str) -> None:
        from langchain_core.messages import HumanMessage

        self._spawn(thread, {"messages": [HumanMessage(text)]})

    def resume(self, thread: Thread, answer: dict[str, Any]) -> None:
        from langgraph.types import Command

        pending = thread.pending[0] if thread.pending else {}
        if pending.get("kind") == "approval":
            value: Any = {"decisions": answer.get("decisions") or [{"type": "approve"}]}
        else:
            value = answer.get("answer", "")
        iid = pending.get("interrupt_id")
        self._spawn(thread, Command(resume={iid: value} if iid else value))

    def refresh(self, thread: Thread) -> None:
        """Rebuild events from the checkpointer (thread reopened after a restart)."""
        snap = self.agent.get_state(self._config(thread))
        values = getattr(snap, "values", {}) or {}
        message_events(thread, values.get("messages", []))
        todo_events(thread, values.get("todos"))
        self._set_pending(thread, self._snapshot_interrupts(snap))

    # ---------------------------------------------------------------- internals
    def _spawn(self, thread: Thread, payload: Any) -> None:
        if not thread.lock.acquire(blocking=False):
            raise RuntimeError("该会话正在运行")
        thread.pending = []
        thread.status = "running"
        thread.emit("status", status="running")
        threading.Thread(target=self._drive, args=(thread, payload), daemon=True).start()

    def _drive(self, thread: Thread, payload: Any) -> None:
        config = self._config(thread)
        interrupts: list[Any] = []
        try:
            for chunk in self.agent.stream(payload, config, stream_mode="updates"):
                if isinstance(chunk, dict) and "__interrupt__" in chunk:
                    interrupts.extend(chunk["__interrupt__"] or [])
                snap = self.agent.get_state(config)
                values = getattr(snap, "values", {}) or {}
                message_events(thread, values.get("messages", []))
                todo_events(thread, values.get("todos"))
            snap = self.agent.get_state(config)
            values = getattr(snap, "values", {}) or {}
            message_events(thread, values.get("messages", []))
            todo_events(thread, values.get("todos"))
            self._set_pending(thread, interrupts or self._snapshot_interrupts(snap))
        except Exception as exc:  # noqa: BLE001 - shown in the UI
            thread.status = "error"
            thread.emit("status", status="error", detail=f"{type(exc).__name__}: {str(exc)[:500]}")
        finally:
            thread.lock.release()

    @staticmethod
    def _snapshot_interrupts(snap: Any) -> list[Any]:
        found = list(getattr(snap, "interrupts", ()) or ())
        if not found:
            for task in getattr(snap, "tasks", ()) or ():
                found.extend(getattr(task, "interrupts", ()) or ())
        return found

    @staticmethod
    def _set_pending(thread: Thread, interrupts: list[Any]) -> None:
        if thread.status == "error":
            return
        thread.pending = [normalize_interrupt(i) for i in interrupts]
        for p in thread.pending:
            thread.emit("interrupt", **p)
        thread.status = "paused" if thread.pending else "idle"
        thread.emit("status", status=thread.status)


class ThreadStore:
    """Threads in memory plus a small index file so the list survives restarts."""

    def __init__(self, runner: Any, index_path: Path | None = None) -> None:
        self.runner = runner
        self.index_path = index_path
        self.threads: dict[str, Thread] = {}
        if index_path and index_path.is_file():
            for item in json.loads(index_path.read_text(encoding="utf-8")):
                self.threads[item["id"]] = Thread(item["id"], item["title"], item["created"])

    def create(self, title: str) -> Thread:
        tid = f"t{time.strftime('%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        thread = Thread(tid, title[:60] or "新会话")
        self.threads[tid] = thread
        self._save()
        return thread

    def get(self, tid: str) -> Thread | None:
        thread = self.threads.get(tid)
        if thread is not None and not thread.events and hasattr(self.runner, "refresh"):
            try:
                self.runner.refresh(thread)
            except Exception:  # noqa: BLE001 - a fresh thread has no state yet
                pass
        return thread

    def list(self) -> list[dict[str, Any]]:
        return sorted(
            (t.summary() for t in self.threads.values()), key=lambda s: s["created"], reverse=True
        )

    def _save(self) -> None:
        if not self.index_path:
            return
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        data = [{"id": t.id, "title": t.title, "created": t.created} for t in self.threads.values()]
        self.index_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


# ============================================================================ demo runner
class DemoRunner:
    """Scripted runner for UI development and tests (no model, no LangGraph).

    ``steps`` is a list of callables ``(thread, payload) -> None`` run one per start/resume.
    """

    model_name = "demo"

    def __init__(self, steps: list[Callable[[Thread, Any], None]] | None = None) -> None:
        self.steps = steps or DEMO_STEPS
        self.cursor: dict[str, int] = {}

    def start(self, thread: Thread, text: str) -> None:
        thread.emit("user", text=text)
        self._next(thread, text)

    def resume(self, thread: Thread, answer: dict[str, Any]) -> None:
        thread.pending = []
        self._next(thread, answer)

    def _next(self, thread: Thread, payload: Any) -> None:
        i = self.cursor.get(thread.id, 0)
        self.cursor[thread.id] = i + 1
        thread.status = "running"
        thread.emit("status", status="running")
        step = self.steps[min(i, len(self.steps) - 1)]
        step(thread, payload)
        thread.status = "paused" if thread.pending else "idle"
        thread.emit("status", status=thread.status)


def _demo_result(stage: str, status: str, summary: str, **extra: Any) -> dict[str, Any]:
    return {"stage": stage, "status": status, "summary": summary, **extra}


def _demo_1(thread: Thread, _payload: Any) -> None:
    thread.emit(
        "todos",
        todos=[
            {"content": "了解流程与扩展接入", "status": "completed"},
            {"content": "检查已有模型包", "status": "in_progress"},
            {"content": "二维仿真", "status": "pending"},
            {"content": "导出到 AFSIM", "status": "pending"},
        ],
    )
    thread.emit(
        "tool_call",
        call_id="c1",
        name="falcon_check",
        args={"package": "/runs/crow-research/armor-eval-03/models/armor_tc01_sheet_py9_fix4"},
    )
    thread.emit(
        "tool_result",
        call_id="c1",
        name="falcon_check",
        text="",
        result=_demo_result(
            "falcon_check",
            "failed",
            "4 个问题",
            problems=[
                "- PROBE002 roles[0].state_machine.states[3]: platoon_leader 在情况不变时来回切换：reporting → bounding_overwatch → reporting"
            ],
            next_steps=["falcon_fix"],
        ),
    )
    thread.emit(
        "tool_call",
        call_id="c2",
        name="simulate",
        args={"package": "/runs/crow-research/armor-eval-03/models/armor_tc01_sheet_py9_fix4"},
    )
    thread.emit(
        "tool_result",
        call_id="c2",
        name="simulate",
        text="",
        result=_demo_result(
            "simulate",
            "failed",
            "0/2 个想定通过",
            data={
                "scenarios": [
                    {
                        "id": "sc01_full_mission",
                        "passed": False,
                        "failed": ["ex01_pl_urban: anchor never occurred (measured=None)"],
                    }
                ]
            },
        ),
    )
    thread.emit(
        "ai",
        text="检查发现排长车在 reporting 和 bounding_overwatch 之间来回切换。这涉及战术意图，需要你决定。",
    )
    thread.emit(
        "tool_call",
        call_id="c3",
        name="ask_human",
        args={
            "question": "车辆受损时，应继续交替跃进还是撤出到开阔地？",
            "options": ["继续交替跃进", "撤出到开阔地"],
        },
    )
    thread.pending = [
        {
            "kind": "ask_human",
            "interrupt_id": "i1",
            "question": "车辆受损时，应继续交替跃进还是撤出到开阔地？",
            "options": ["继续交替跃进", "撤出到开阔地"],
        }
    ]
    thread.emit("interrupt", **thread.pending[0])


def _demo_2(thread: Thread, payload: Any) -> None:
    answer = payload.get("answer", "") if isinstance(payload, dict) else str(payload)
    thread.emit("tool_result", call_id="c3", name="ask_human", text=f"用户回答：{answer}")
    thread.emit("ai", text=f"按你的决定（{answer}）修正后，准备导出到 AFSIM。")
    thread.emit(
        "tool_call",
        call_id="c4",
        name="platform_export",
        args={"package": "/runs/demo/pkg", "out_dir": "/runs/demo/afsim", "backend": "afsim"},
    )
    thread.pending = [
        {
            "kind": "approval",
            "interrupt_id": "i2",
            "requests": [
                {
                    "name": "platform_export",
                    "args": {
                        "package": "/runs/demo/pkg",
                        "out_dir": "/runs/demo/afsim",
                        "backend": "afsim",
                    },
                    "description": "导出并在 AFSIM 上运行",
                    "allowed": ["approve", "edit", "reject"],
                }
            ],
        }
    ]
    thread.emit("interrupt", **thread.pending[0])


def _demo_3(thread: Thread, payload: Any) -> None:
    decision = (
        (payload.get("decisions") or [{}])[0].get("type")
        if isinstance(payload, dict)
        else "approve"
    )
    if decision == "reject":
        thread.emit("tool_result", call_id="c4", name="platform_export", text="用户拒绝了这次调用")
        thread.emit("ai", text="已取消导出。")
        return
    thread.emit(
        "tool_result",
        call_id="c4",
        name="platform_export",
        text="",
        result=_demo_result(
            "platform_export", "not_configured", "平台后端 afsim 尚未接入（AFSIM 模板映射开发中）"
        ),
    )
    thread.emit(
        "todos",
        todos=[
            {"content": "了解流程与扩展接入", "status": "completed"},
            {"content": "检查已有模型包", "status": "completed"},
            {"content": "二维仿真", "status": "completed"},
            {"content": "导出到 AFSIM", "status": "completed"},
        ],
    )
    thread.emit("ai", text="AFSIM 后端尚未接入，流程到此结束。")


DEMO_STEPS = [_demo_1, _demo_2, _demo_3]
