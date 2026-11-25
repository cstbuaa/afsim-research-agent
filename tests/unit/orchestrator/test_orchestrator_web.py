"""Web UI server with the scripted demo runner (no model, no LangGraph)."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from afsim_research_agent.orchestrator.web.server import App, serve
from afsim_research_agent.orchestrator.web.session import (
    DemoRunner,
    Thread,
    ThreadStore,
    message_events,
    normalize_interrupt,
)
from afsim_research_agent.orchestrator.workspace import Workspace


@pytest.fixture
def base(tmp_path):
    root = tmp_path / "data"
    (root / "runs" / "r1").mkdir(parents=True)
    (root / "runs" / "r1" / "a.yaml").write_bytes(b"x: 1\n")  # no CRLF on Windows
    (root / "runs" / "_local").mkdir()
    ws = Workspace(root.resolve())
    store = ThreadStore(DemoRunner(), tmp_path / "threads.json")
    server = serve(App(store, ws, model_name="demo"), port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _req(url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_index_and_info(base) -> None:
    with urllib.request.urlopen(base + "/") as resp:
        assert b"AFSIM" in resp.read()
    status, info = _req(base + "/api/info")
    assert status == 200 and info["tools"] and "plan_compiler" in info["extensions"]


def test_demo_conversation_with_question_and_approval(base) -> None:
    _, t = _req(base + "/api/threads", {"title": "测试"})
    tid = t["id"]
    assert _req(f"{base}/api/threads/{tid}/resume", {"answer": "x"})[0] == 409  # nothing pending
    assert _req(f"{base}/api/threads/{tid}/messages", {"text": "检查模型包"})[0] == 202
    _, ev = _req(f"{base}/api/threads/{tid}/events?after=0")
    assert ev["pending"][0]["kind"] == "ask_human"
    types = [e["type"] for e in ev["events"]]
    assert {"user", "todos", "tool_call", "tool_result", "interrupt"} <= set(types)
    # cannot send a new message while a question is pending
    assert _req(f"{base}/api/threads/{tid}/messages", {"text": "再来"})[0] == 409
    assert _req(f"{base}/api/threads/{tid}/resume", {"answer": "撤出到开阔地"})[0] == 202
    _, ev2 = _req(f"{base}/api/threads/{tid}/events?after={len(ev['events'])}")
    assert ev2["pending"][0]["kind"] == "approval"
    assert _req(f"{base}/api/threads/{tid}/resume", {"decisions": [{"type": "approve"}]})[0] == 202
    _, ev3 = _req(f"{base}/api/threads/{tid}/events?after=0")
    assert ev3["thread"]["status"] == "idle" and not ev3["pending"]
    _, threads = _req(base + "/api/threads")
    assert threads[0]["id"] == tid


def test_file_api_respects_workspace(base) -> None:
    status, listing = _req(base + "/api/files?path=/runs")
    names = [e["name"] for e in listing["entries"]]
    assert status == 200 and "r1" in names and "_local" not in names
    status, f = _req(base + "/api/files?path=/runs/r1/a.yaml")
    assert f["text"] == "x: 1\n"
    assert _req(base + "/api/files?path=/runs/_local")[0] == 400
    assert _req(base + "/api/files?path=../../etc/passwd")[0] == 400


def test_events_from_messages_and_interrupts() -> None:
    class M:
        def __init__(self, type, content, id, **kw):
            self.type, self.content, self.id = type, content, id
            self.__dict__.update(kw)

    t = Thread("t", "x")
    msgs = [
        M("human", "需求", "1"),
        M(
            "ai",
            [{"type": "text", "text": "好的"}],
            "2",
            tool_calls=[{"id": "c", "name": "falcon_check", "args": {"package": "/runs/p"}}],
        ),
        M(
            "tool",
            '{"stage": "falcon_check", "status": "failed", "summary": "2 个问题"}',
            "3",
            tool_call_id="c",
            name="falcon_check",
        ),
    ]
    message_events(t, msgs)
    message_events(t, msgs)  # no duplicates
    assert [e["type"] for e in t.events] == ["user", "ai", "tool_call", "tool_result"]
    assert t.events[-1]["result"]["status"] == "failed"

    class Intr:
        id = "i1"
        value = {
            "action_requests": [{"name": "select_tactic", "args": {"choices": ["tc_01"]}}],
            "review_configs": [
                {"action_name": "select_tactic", "allowed_decisions": ["approve", "reject"]}
            ],
        }

    n = normalize_interrupt(Intr())
    assert n["kind"] == "approval" and n["requests"][0]["allowed"] == ["approve", "reject"]
    Intr.value = {"type": "ask_human", "question": "q", "options": ["a"]}
    assert normalize_interrupt(Intr())["kind"] == "ask_human"
    time.sleep(0)


def test_a_scenario_list_and_opening_its_folder(tmp_path, monkeypatch) -> None:
    from afsim_research_agent.orchestrator import workbench

    base = tmp_path / "runs"
    scn = base / "tactics_a" / "scenarios" / "approach.yaml"
    scn.parent.mkdir(parents=True)
    scn.write_text("meta: {}\n", encoding="utf-8")
    (base / "tactics_a" / "notes.yaml").write_text("x: 1\n", encoding="utf-8")  # not a scenario

    rows = workbench.list_scenarios([base])
    assert [r["id"] for r in rows] == ["0:tactics_a/scenarios/approach.yaml"]
    assert rows[0]["title"] == "tactics_a / approach.yaml"

    opened: list[str] = []
    monkeypatch.setattr(workbench, "_launch", lambda folder: opened.append(str(folder)))
    folder = workbench.open_folder(rows[0]["id"], [base])
    assert folder == scn.parent and opened == [str(scn.parent)]  # the folder, never the file


def test_opening_a_folder_outside_the_roots_is_refused(tmp_path, monkeypatch) -> None:
    from afsim_research_agent.orchestrator import workbench

    base = tmp_path / "runs"
    (base / "a").mkdir(parents=True)
    (tmp_path / "secret").mkdir()
    monkeypatch.setattr(workbench, "_launch", lambda folder: pytest.fail("must not launch"))
    for rid in ("0:../secret", "9:a", "0:", "0:missing.yaml"):
        with pytest.raises(ValueError):
            workbench.open_folder(rid, [base])
