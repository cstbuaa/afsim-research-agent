"""Replay API of the workbench, served by ``langgraph dev`` next to the agent
(``langgraph.json`` → ``http.app``).

Routes (local browser only; the frontend proxies ``/workbench/*`` to this server):

- ``GET /workbench/replays`` — recordings and 2D replays under the replay roots, newest
  first: ``[{id, source, title, size, mtime}]``;
- ``GET /workbench/replay?id=<id>`` — one replay as ``replay/1`` JSON
  (:mod:`afsim_research_agent.replay.build`);
- ``GET /workbench/info`` — the orchestrator's model name, this server's process id and start
  time (which server process answers, and with which model);
- ``GET /workbench/scenarios`` — the scenario files the agent wrote (``*/scenarios/*.yaml``)
  under the same roots, newest first: ``[{id, title, dir, size, mtime}]``;
- ``POST /workbench/open_folder`` — body ``{"id": <id of a replay or a scenario>}``: opens the
  folder that holds that file in the computer's own file manager and answers
  ``{"opened": <absolute path>}``. The server runs on the user's machine beside AFSIM, so
  this is the only way the browser can reach a local folder. Only a folder is ever opened,
  never a file (a file would be handed to whatever program is registered for it), and only
  inside the roots below.

Replay roots: ``<data>/runs`` and the directories in ``WORKBENCH_REPLAY_ROOTS`` (``;``
separated; e.g. local lab runs). An id is ``<root index>:<path relative to that root>``; a
path that leaves its root is refused. AFSIM recordings (``.aer``, Mystic's input) are decoded
here; the run console next to them gives action starts and results, the package manifest
the role names, ``output/report.json`` the states and verdicts of older runs.

Nothing here goes to the agent or to a model service: AFSIM run directories live under
``_local`` (which the agent cannot read) and are served to the browser only.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

MAX_AER_BYTES = 400 * 1024 * 1024
_CACHE: OrderedDict[tuple[str, float], dict[str, Any]] = OrderedDict()
_LOCK = threading.Lock()


def roots() -> list[Path]:
    from afsim_research_agent.orchestrator.workspace import Workspace

    out = [Workspace.from_settings().root / "runs"]
    for part in (os.environ.get("WORKBENCH_REPLAY_ROOTS") or "").split(";"):
        if part.strip():
            out.append(Path(part.strip()).resolve())
    return out


def _resolve(rid: str, bases: list[Path]) -> Path:
    index, _, rel = rid.partition(":")
    if not index.isdigit() or int(index) >= len(bases) or not rel:
        raise ValueError("bad replay id")
    base = bases[int(index)].resolve()
    path = (base / rel).resolve()
    if base not in path.parents or not path.is_file():
        raise ValueError("no such replay")
    if not (path.suffix == ".aer" or path.name.endswith(".replay.json")):
        raise ValueError("not a replay")
    return path


def _resolve_any(rid: str, bases: list[Path]) -> Path:
    """The file an id names, under its root. Any suffix: scenarios as well as replays."""
    index, _, rel = rid.partition(":")
    if not index.isdigit() or int(index) >= len(bases) or not rel:
        raise ValueError("bad id")
    base = bases[int(index)].resolve()
    path = (base / rel).resolve()
    if base != path and base not in path.parents:
        raise ValueError("path leaves its root")
    if not path.exists():
        raise ValueError("no such file")
    return path


def list_scenarios(bases: list[Path] | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """Scenario files the agent wrote (``<tactics>/scenarios/<name>.yaml``), newest first."""
    bases = bases if bases is not None else roots()
    found = []
    for i, base in enumerate(bases):
        if not base.is_dir():
            continue
        for pattern in ("*/scenarios/*.yaml", "*/scenarios/*.yml"):
            for p in base.glob("**/" + pattern):
                try:
                    st = p.stat()
                except OSError:
                    continue
                rel = p.relative_to(base).as_posix()
                found.append(
                    {
                        "id": f"{i}:{rel}",
                        "title": f"{p.parent.parent.name} / {p.name}",
                        "dir": str(p.parent),
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                    }
                )
    found.sort(key=lambda r: r["mtime"], reverse=True)
    return found[:limit]


def _launch(folder: Path) -> None:
    """Show a folder in the computer's own file manager (no shell, the path as an argument)."""
    import subprocess
    import sys

    if sys.platform == "win32":
        os.startfile(str(folder))  # type: ignore[attr-defined]  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.run(["/usr/bin/open", str(folder)], check=False)
    else:
        subprocess.run(["xdg-open", str(folder)], check=False)


def open_folder(rid: str, bases: list[Path] | None = None) -> Path:
    """Open the folder holding the file ``rid`` names; returns the folder."""
    bases = bases if bases is not None else roots()
    path = _resolve_any(rid, bases)
    folder = path if path.is_dir() else path.parent
    _launch(folder)
    return folder


def list_replays(bases: list[Path] | None = None, limit: int = 300) -> list[dict[str, Any]]:
    bases = bases if bases is not None else roots()
    found = []
    for i, base in enumerate(bases):
        if not base.is_dir():
            continue
        for pattern, source in (("*.aer", "afsim"), ("*.replay.json", "2d")):
            for p in base.rglob(pattern):
                try:
                    st = p.stat()
                except OSError:
                    continue
                rel = p.relative_to(base).as_posix()
                found.append(
                    {
                        "id": f"{i}:{rel}",
                        "source": source,
                        "title": _title(p, source),
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                    }
                )
    found.sort(key=lambda r: r["mtime"], reverse=True)
    return found[:limit]


def _title(path: Path, source: str) -> str:
    if source == "2d":
        return f"二维 · {path.parent.name} / {path.name.removesuffix('.replay.json')}"
    run_dir = path.parent.parent if path.parent.name == "output" else path.parent
    return f"AFSIM · {run_dir.parent.name} / {run_dir.name}"


def _sidecars(aer: Path) -> tuple[str | None, dict[str, str], list[dict[str, Any]], dict]:
    """Console text, platform labels, verdicts and report states for a recording."""
    out_dir = aer.parent
    run_dir = out_dir.parent if out_dir.name == "output" else out_dir
    case = aer.stem
    console = None
    for p in (out_dir / f"run_{case}_console.log", *sorted(out_dir.glob("*console*.log"))):
        if p.is_file():
            console = p.read_text(encoding="utf-8", errors="replace")
            break
    labels: dict[str, str] = {}
    manifest = run_dir / "manifest.json"
    if manifest.is_file():
        from afsim_research_agent.replay.build import labels_from_run

        data = json.loads(manifest.read_text(encoding="utf-8"))
        for run in (data.get("runs") or {}).values():
            labels.update(labels_from_run(run.get("platforms") or []))
    verdicts: list[dict[str, Any]] = []
    states: dict[str, Any] = {}
    report = out_dir / "report.json"
    if report.is_file():
        for r in json.loads(report.read_text(encoding="utf-8")) or []:
            for v in r.get("obligations") or []:
                verdicts.append(
                    {
                        "id": v.get("id"),
                        "passed": bool(v.get("passed")),
                        "judged": bool(v.get("judged", True)),
                        "detail": str(v.get("detail") or ""),
                    }
                )
            for issue in r.get("problems") or []:
                verdicts.append(
                    {"id": "运行检查", "passed": False, "judged": True, "detail": issue}
                )
            states.update(r.get("states") or {})
    return console, labels, verdicts, states


def build_replay(path: Path) -> dict[str, Any]:
    key = (str(path), path.stat().st_mtime)
    with _LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    if path.suffix == ".aer":
        from afsim_research_agent.replay.build import from_aer, from_console

        console, labels, verdicts, states = _sidecars(path)
        sensor = os.environ.get("WORKBENCH_SENSOR_RANGE_M")
        sensor_m = float(sensor) if sensor else None
        if path.stat().st_size > MAX_AER_BYTES:
            # too big to decode here (older runs recorded every aux-data change): the run
            # console has the 1 Hz positions, states and actions the player needs
            if not console:
                raise ValueError("recording too large and no run console next to it")
            replay = from_console(
                console, labels=labels, sensor_range_m=sensor_m, title=_title(path, "afsim")
            )
            replay["note"] = "记录文件过大，回放取自运行控制台（每秒位置、状态、动作）"
        else:
            replay = from_aer(
                path,
                console=console,
                labels=labels,
                sensor_range_m=sensor_m,
                title=_title(path, "afsim"),
            )
        if not replay["states"] and states:
            replay["states"] = {
                labels.get(k, k): [[float(t), s] for t, s in v] for k, v in states.items()
            }
        replay["verdicts"] = verdicts
    else:
        replay = json.loads(path.read_text(encoding="utf-8"))
    with _LOCK:
        _CACHE[key] = replay
        while len(_CACHE) > 8:
            _CACHE.popitem(last=False)
    return replay


async def _list(request: Request) -> JSONResponse:
    return JSONResponse(list_replays())


async def _get(request: Request) -> JSONResponse:
    import anyio

    rid = request.query_params.get("id", "")
    try:
        path = _resolve(rid, roots())
        replay = await anyio.to_thread.run_sync(build_replay, path)
    except (ValueError, OSError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse(replay)


_STARTED = time.time()


def server_info() -> dict[str, Any]:
    from afsim_research_agent.orchestrator.graph import orchestrator_model_name

    return {
        "orchestrator_model": orchestrator_model_name(),
        "pid": os.getpid(),
        "started": _STARTED,
    }


async def _info(request: Request) -> JSONResponse:
    return JSONResponse(server_info())


async def _scenarios(request: Request) -> JSONResponse:
    return JSONResponse(list_scenarios())


async def _open_folder(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except (ValueError, TypeError):
        body = {}
    rid = str((body or {}).get("id") or request.query_params.get("id") or "")
    try:
        folder = open_folder(rid)
    except (ValueError, OSError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"opened": str(folder)})


app = Starlette(
    routes=[
        Route("/workbench/info", _info, methods=["GET"]),
        Route("/workbench/replays", _list, methods=["GET"]),
        Route("/workbench/replay", _get, methods=["GET"]),
        Route("/workbench/scenarios", _scenarios, methods=["GET"]),
        Route("/workbench/open_folder", _open_folder, methods=["POST"]),
    ]
)
