from __future__ import annotations

import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from ir_specs import ROOT, example_data

from afsim_research_agent.ir.binding import bind
from afsim_research_agent.ir.cosim import CoSimRunner
from afsim_research_agent.ir.cosim.check import run_checks
from afsim_research_agent.ir.cosim.kinematic import KinematicEngine
from afsim_research_agent.ir.cosim.protocol import (
    PROTOCOL,
    decode_observation,
    encode_observation,
    recv_frame,
    request,
    send_frame,
)
from afsim_research_agent.ir.cosim.reference import ReferenceEngine
from afsim_research_agent.ir.cosim.remote import RemoteEngineAdapter
from afsim_research_agent.ir.cosim.server import serve
from afsim_research_agent.ir.normalize import normalize
from afsim_research_agent.ir.sim import run_scenario
from afsim_research_agent.ir.validate import validate_data


def _start(factory, connections: int = 1) -> int:
    ready, bound = threading.Event(), []
    thread = threading.Thread(
        target=serve,
        args=(factory,),
        kwargs={"port": 0, "ready": ready, "bound": bound, "max_connections": connections},
        daemon=True,
    )
    thread.start()
    assert ready.wait(5)
    return bound[0]


@pytest.fixture(scope="module")
def ir() -> dict[str, Any]:
    result = normalize(validate_data(example_data()))
    assert result.ir is not None
    return result.ir


def test_python_reference_host_passes_protocol_check() -> None:
    port = _start(KinematicEngine)
    results = run_checks("127.0.0.1", port)
    assert all(r.passed for r in results), [(r.name, r.detail) for r in results if not r.passed]
    assert len(results) >= 15


@pytest.mark.parametrize("messaging", ["adapter", "engine"])
def test_remote_cosim_matches_builtin(ir, messaging: str) -> None:
    ctx, _ = bind(ir, "python_sim")
    port = _start(
        lambda: ReferenceEngine(ir, ctx, messaging=messaging), connections=len(ir["scenarios"])
    )
    for scenario in ir["scenarios"]:
        builtin = run_scenario(ir, ctx, scenario)
        remote = CoSimRunner(
            ir, RemoteEngineAdapter("127.0.0.1", port), scenario, bindings=ctx
        ).run()
        assert [e.measured for e in remote.expectations] == [
            e.measured for e in builtin.expectations
        ]
        assert remote.final_states == builtin.final_states


def test_observation_codec_handles_infinity() -> None:
    from afsim_research_agent.ir.cosim import EntityObservation, Observation

    obs = Observation(
        3.0,
        {
            "a": EntityObservation(
                1.0,
                2.0,
                90.0,
                4.0,
                terrain={"class": "open", "distance_to": {"urban": float("inf")}},
            )
        },
    )
    data = encode_observation(obs)
    assert data["entities"]["a"]["terrain"]["distance_to"]["urban"] is None
    back = decode_observation(data)
    assert back.entities["a"].terrain["distance_to"]["urban"] == float("inf")


def test_server_rejects_bad_envelopes() -> None:
    port = _start(KinematicEngine)
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        send_frame(
            sock,
            {"protocol": "other/9", "run_id": "r", "sequence_id": 1, "type": "Hello", "body": {}},
        )
        assert recv_frame(sock)["error"]["code"] == "VERSION"
        send_frame(sock, request("r", 2, "GetState", {}))
        assert recv_frame(sock)["error"]["code"] == "BAD_STATE"  # Hello first
        send_frame(sock, request("r", 3, "Hello", {"versions": [PROTOCOL]}))
        assert recv_frame(sock)["ok"]
        send_frame(sock, request("other-run", 4, "GetState", {}))
        assert recv_frame(sock)["error"]["code"] == "BAD_REQUEST"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(
    shutil.which("cmake") is None or shutil.which("c++") is None, reason="no C++ toolchain"
)
def test_cpp_bridge_builds_and_conforms(ir, tmp_path: Path) -> None:
    src = ROOT / "integrations" / "engine_bridge_cpp"
    build = tmp_path / "build"
    subprocess.run(
        ["cmake", "-S", str(src), "-B", str(build), "-DCMAKE_BUILD_TYPE=Release"],
        check=True,
        capture_output=True,
    )
    subprocess.run(["cmake", "--build", str(build), "-j", "4"], check=True, capture_output=True)
    exe = next(p for p in build.rglob("tbe_bridge*") if p.is_file() and p.suffix in ("", ".exe"))
    port = _free_port()
    proc = subprocess.Popen(
        [str(exe), "--port", str(port), "--max-connections", "2"], stderr=subprocess.PIPE
    )
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        # the probe connection above counts as one; allow the check and one scenario
        results = run_checks("127.0.0.1", port)
        assert all(r.passed for r in results), [(r.name, r.detail) for r in results if not r.passed]
    finally:
        proc.kill()
        proc.wait(5)


def test_conformance_pack_round_trip(ir, tmp_path: Path) -> None:
    import json

    from afsim_research_agent.ir.conformance import compare_l2, export_pack

    counts = export_pack(ir, tmp_path / "pack")
    assert counts == {"l2_cases": len(ir["tests"]), "l3_scenarios": len(ir["scenarios"])}
    pack = json.loads((tmp_path / "pack" / "l2_cases.json").read_text(encoding="utf-8"))
    assert all(case["reference_passed"] for case in pack["cases"])
    native = {"ir_hash": pack["ir_hash"], "cases": {c["id"]: c["reference"] for c in pack["cases"]}}
    (tmp_path / "ok.json").write_text(json.dumps(native), encoding="utf-8")
    assert compare_l2(tmp_path / "pack", tmp_path / "ok.json") == []
    first = pack["cases"][0]["id"]
    native["cases"][first] = {"next_state": "nowhere"}
    (tmp_path / "bad.json").write_text(json.dumps(native), encoding="utf-8")
    problems = compare_l2(tmp_path / "pack", tmp_path / "bad.json")
    assert any("next_state" in p for p in problems) and any(
        "fired_transition not reported" in p for p in problems
    )
