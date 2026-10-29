"""Run a compiled package with mission.exe and judge the evidence.

Evidence is the console of each run: the ``IR_STATE|<time>|<platform>|<state>`` lines the
generated state machines write, and AFSIM's error markers. A run passes when AFSIM
completes without errors, every platform's state sequence matches the run's expectation,
and — when the run names the handwritten version's console — the sequence equals the
reference sequence; transition time differences are reported.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

IR_STATE = re.compile(r"^IR_STATE\|([^|]+)\|([^|]+)\|([^|\r\n]+)", re.M)
ERRORS = re.compile(r"\*\*\*\*\* (ERROR|FATAL)[^\r\n]*")


@dataclass
class RunJudgement:
    run: str
    completed: bool
    errors: list[str] = field(default_factory=list)
    states: dict[str, list[tuple[float, str]]] = field(default_factory=dict)
    expected: list[str] | None = None
    reference: list[tuple[float, str]] | None = None
    problems: list[str] = field(default_factory=list)
    time_deltas: list[dict[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.completed and not self.errors and not self.problems

    def render(self) -> str:
        lines = [f"run {self.run}: {'PASS' if self.passed else 'FAIL'}"]
        if not self.completed:
            lines.append("  AFSIM did not complete the simulation")
        lines += [f"  {e}" for e in self.errors[:10]]
        for name, seq in self.states.items():
            lines.append(f"  {name}: " + " -> ".join(f"{s}@{t:g}" for t, s in seq))
        if self.reference is not None:
            lines.append("  reference: " + " -> ".join(f"{s}@{t:g}" for t, s in self.reference))
        for d in self.time_deltas:
            lines.append(
                f"  {d['transition']}: generated {d['generated']:g}s, "
                f"reference {d['reference']:g}s ({d['delta']:+g}s)"
            )
        lines += [f"  problem: {p}" for p in self.problems]
        return "\n".join(lines)


def compress(seq: list[tuple[float, str]]) -> list[tuple[float, str]]:
    out: list[tuple[float, str]] = []
    for t, s in seq:
        if not out or out[-1][1] != s:
            out.append((t, s))
    return out


def parse_states(console: str) -> dict[str, list[tuple[float, str]]]:
    states: dict[str, list[tuple[float, str]]] = {}
    for t, platform, state in IR_STATE.findall(console):
        states.setdefault(platform, []).append((float(t), state.strip()))
    return {p: compress(seq) for p, seq in states.items()}


def parse_reference(console: str, pattern: str) -> list[tuple[float, str]]:
    rx = re.compile(pattern, re.M)
    seq = []
    for m in rx.finditer(console):
        state = m.group("state") if "state" in rx.groupindex else m.group(1)
        t = float(m.group("t")) if "t" in rx.groupindex else float(len(seq))
        seq.append((t, state.strip()))
    return compress(seq)


def judge(run: dict[str, Any], console: str, reference_console: str | None) -> RunJudgement:
    j = RunJudgement(run=run["id"], completed="Simulation complete" in console)
    j.errors = [m.group(0) for m in ERRORS.finditer(console)]
    j.states = parse_states(console)
    names = [p["name"] for p in run["platforms"]]
    for name in names:
        if name not in j.states:
            j.problems.append(f"{name} wrote no IR_STATE lines")
    expected = (run.get("expect") or {}).get("sequence")
    if expected is not None:
        j.expected = list(expected)
        for name in names:
            got = [s for _, s in j.states.get(name, [])]
            if got != j.expected:
                j.problems.append(f"{name} visited {got}, expected {j.expected}")
    ref = run.get("reference")
    if ref and reference_console is not None:
        j.reference = parse_reference(reference_console, ref["state_pattern"])
        first = j.states.get(names[0], [])
        if [s for _, s in first] != [s for _, s in j.reference]:
            j.problems.append(
                f"{names[0]} sequence {[s for _, s in first]} differs from the handwritten "
                f"version {[s for _, s in j.reference]}"
            )
        else:
            for (tg, sg), (tr, _) in zip(first[1:], j.reference[1:], strict=False):
                j.time_deltas.append(
                    {"transition": f"-> {sg}", "generated": tg, "reference": tr, "delta": tg - tr}
                )
    elif ref:
        j.problems.append("the handwritten reference console was not found")
    return j


def run_package(package: Path, mission: str, timeout_s: float = 300.0) -> list[RunJudgement]:
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    out = package / "output"
    out.mkdir(exist_ok=True)
    results = []
    for run_file, run in manifest["runs"].items():
        proc = subprocess.run(
            [mission, run_file],
            cwd=package,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
        console = proc.stdout + proc.stderr
        (out / f"{Path(run_file).stem}_console.log").write_text(console, encoding="utf-8")
        results.append(_judge_saved(package, run, console))
    _report(package, results)
    return results


def judge_package(package: Path) -> list[RunJudgement]:
    """Judge consoles already saved under <package>/output (e.g. run elsewhere)."""
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    results = []
    for run_file, run in manifest["runs"].items():
        path = package / "output" / f"{Path(run_file).stem}_console.log"
        console = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        results.append(_judge_saved(package, run, console))
    _report(package, results)
    return results


def _judge_saved(package: Path, run: dict[str, Any], console: str) -> RunJudgement:
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    ref = run.get("reference")
    ref_text = None
    if ref:
        root = Path(manifest.get("model_root") or "")
        path = Path(ref["console"])
        path = path if path.is_absolute() else root / path
        if path.exists():
            ref_text = path.read_text(encoding="utf-8", errors="replace")
    return judge(run, console, ref_text)


def _report(package: Path, results: list[RunJudgement]) -> None:
    data = [{**asdict(r), "passed": r.passed} for r in results]
    (package / "output" / "report.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
