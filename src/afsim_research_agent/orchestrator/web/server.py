"""A small local web server for talking to the agent (standard library only).

Binds to 127.0.0.1 by default. Routes::

    GET  /                               the single-page UI
    GET  /api/info                       model, tools, extension status
    GET  /api/threads                    thread list
    POST /api/threads        {title}     new thread
    GET  /api/threads/<id>/events?after=N
    POST /api/threads/<id>/messages {text}
    POST /api/threads/<id>/resume   {answer} | {decisions}
    GET  /api/files?path=/runs/...       directory listing or file text (workspace rules apply)
"""

from __future__ import annotations

import json
import mimetypes
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from afsim_research_agent.orchestrator.extensions import get_registry
from afsim_research_agent.orchestrator.tools import TOOL_SPECS
from afsim_research_agent.orchestrator.web.session import ThreadStore
from afsim_research_agent.orchestrator.workspace import PROTECTED_PART, Workspace, WorkspaceError

MAX_FILE_BYTES = 400_000
TEXT_SUFFIXES = {
    ".md",
    ".yaml",
    ".yml",
    ".json",
    ".txt",
    ".py",
    ".xml",
    ".log",
    ".csv",
    ".toml",
    ".mmd",
    ".html",
}


def _static(name: str) -> bytes:
    return (
        resources.files("afsim_research_agent.orchestrator.web")
        .joinpath("static", name)
        .read_bytes()
    )


class App:
    def __init__(self, store: ThreadStore, workspace: Workspace, *, model_name: str = "") -> None:
        self.store = store
        self.ws = workspace
        self.model_name = model_name

    # ---------------------------------------------------------------- handlers
    def info(self) -> dict[str, Any]:
        reg = get_registry()
        return {
            "model": self.model_name,
            "workspace": "data/",
            "tools": [
                {
                    "name": s.name,
                    "group": s.group,
                    "description": s.description,
                    "uses_model": s.uses_model,
                    "needs_approval": s.needs_approval,
                }
                for s in TOOL_SPECS
            ],
            "extensions": reg.status(),
            "extension_problems": reg.check(),
        }

    def files(self, path: str) -> dict[str, Any]:
        full = self.ws.existing(path or "/runs")
        shown = self.ws.show(full)
        if full.is_dir():
            entries = []
            for p in sorted(full.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                if p.name.startswith(".") or p.name == PROTECTED_PART:
                    continue
                entries.append(
                    {
                        "name": p.name,
                        "path": self.ws.show(p),
                        "dir": p.is_dir(),
                        "size": p.stat().st_size if p.is_file() else None,
                    }
                )
            return {"path": shown, "dir": True, "entries": entries[:500]}
        size = full.stat().st_size
        if full.suffix.lower() not in TEXT_SUFFIXES:
            return {
                "path": shown,
                "dir": False,
                "size": size,
                "text": None,
                "note": "非文本文件，未显示内容",
            }
        data = full.read_bytes()[:MAX_FILE_BYTES]
        return {
            "path": shown,
            "dir": False,
            "size": size,
            "suffix": full.suffix.lower(),
            "text": data.decode("utf-8", errors="replace"),
            "truncated": size > MAX_FILE_BYTES,
        }


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AfsimAgentUI/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet console
            return

        # -------------------------------------------------------- helpers
        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data: Any, status: int = 200) -> None:
            body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        def _error(self, status: int, message: str) -> None:
            self._json({"error": message}, status)

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        # -------------------------------------------------------- routes
        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            query = parse_qs(url.query)
            try:
                if url.path in ("/", "/index.html"):
                    return self._send(200, _static("index.html"), "text/html; charset=utf-8")
                if url.path.startswith("/static/"):
                    name = url.path.removeprefix("/static/")
                    if not re.fullmatch(r"[\w.-]+", name):
                        return self._error(404, "not found")
                    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
                    return self._send(200, _static(name), ctype)
                if url.path == "/api/info":
                    return self._json(app.info())
                if url.path == "/api/threads":
                    return self._json(app.store.list())
                m = re.fullmatch(r"/api/threads/([\w-]+)/events", url.path)
                if m:
                    thread = app.store.get(m.group(1))
                    if thread is None:
                        return self._error(404, "没有这个会话")
                    after = int((query.get("after") or ["0"])[0])
                    return self._json(
                        {
                            "thread": thread.summary(),
                            "pending": thread.pending,
                            "events": thread.events[after:],
                        }
                    )
                if url.path == "/api/files":
                    return self._json(app.files((query.get("path") or [""])[0]))
                return self._error(404, "not found")
            except WorkspaceError as exc:
                return self._error(400, str(exc))
            except FileNotFoundError:
                return self._error(404, "not found")

        def do_POST(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            try:
                body = self._body()
                if url.path == "/api/threads":
                    thread = app.store.create(str(body.get("title") or "新会话"))
                    return self._json(thread.summary(), 201)
                m = re.fullmatch(r"/api/threads/([\w-]+)/(messages|resume)", url.path)
                if m:
                    thread = app.store.get(m.group(1))
                    if thread is None:
                        return self._error(404, "没有这个会话")
                    if thread.status == "running":
                        return self._error(409, "该会话正在运行，请稍候")
                    if m.group(2) == "messages":
                        text = str(body.get("text") or "").strip()
                        if not text:
                            return self._error(400, "消息为空")
                        if thread.pending:
                            return self._error(409, "请先回答待处理的问题或批准请求")
                        app.store.runner.start(thread, text)
                    else:
                        if not thread.pending:
                            return self._error(409, "没有待处理的问题")
                        app.store.runner.resume(thread, body)
                    return self._json(thread.summary(), 202)
                return self._error(404, "not found")
            except (ValueError, json.JSONDecodeError) as exc:
                return self._error(400, str(exc))
            except RuntimeError as exc:
                return self._error(409, str(exc))

    return Handler


def serve(app: App, *, host: str = "127.0.0.1", port: int = 18765) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(app))
    server.daemon_threads = True
    return server


def index_path(ws: Workspace) -> Path:
    return ws.root / "runs" / "agent-ui" / "threads.json"
