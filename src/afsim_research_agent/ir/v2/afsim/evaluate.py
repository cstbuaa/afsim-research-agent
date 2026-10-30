"""Run a compiled package with mission.exe and judge the evidence.

Evidence is the console of each run: the ``IR_STATE|<time>|<platform>|<state>`` lines the
generated state machines write, and AFSIM's error markers. A run passes when AFSIM
completes without errors, every platform's state sequence matches the run's expectation,
and — when the run names the handwritten version's console — the sequence equals the
reference sequence; transition time differences are reported, and with
``expect.max_interval_delta_s`` the time spent in each state must stay that close to the
reference (a requirement such as "start searching after 10 s" shows as a state left early).
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

IR_STATE = re.compile(r"^IR_STATE\|([^|]+)\|([^|]+)\|([^|\r\n]+)", re.M)
IR_ACT = re.compile(r"^IR_ACT\|([^|]+)\|([^|]+)\|([^|\r\n]+)", re.M)
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
    obligations: list[dict[str, Any]] = field(default_factory=list)  # verdicts

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
        for v in self.obligations:
            if v.get("judged", True):
                mark = "ok" if v["passed"] else "FAIL"
                lines.append(f"  [{mark}] 需求约束 {v['id']}：{v['detail']}")
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


def judge(
    run: dict[str, Any],
    console: str,
    reference_console: str | None,
    obligations: list[dict[str, Any]] | None = None,
) -> RunJudgement:
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
    for pattern in (run.get("expect") or {}).get("markers") or []:
        if not re.search(pattern, console, re.M):
            j.problems.append(f"console has no line matching {pattern!r}")
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
            limit = (run.get("expect") or {}).get("max_interval_delta_s")
            if limit is not None:
                for i in range(1, len(first) - 1):
                    stay = first[i + 1][0] - first[i][0]
                    ref_stay = j.reference[i + 1][0] - j.reference[i][0]
                    if abs(stay - ref_stay) > float(limit):
                        j.problems.append(
                            f"{names[0]} stayed {stay:g}s in {first[i][1]} (entered at "
                            f"{first[i][0]:g}s), the handwritten version {ref_stay:g}s; "
                            f"more than {float(limit):g}s apart"
                        )
    elif ref:
        j.problems.append("the handwritten reference console was not found")
    if obligations:
        from afsim_research_agent.ir.v2.obligations import Obligation, check

        obs = [Obligation.model_validate(o) for o in obligations]
        acts = [(float(t), p, a.strip()) for t, p, a in IR_ACT.findall(console)]
        events = [(float(e["at_s"]), str(e["event"])) for e in run.get("events") or []]
        verdicts = check(obs, acts, events, end_s=float(run.get("end_time_s") or 0) or None)
        j.obligations = [asdict(v) for v in verdicts]
        for v in verdicts:
            if v.judged and not v.passed:
                j.problems.append(f"requirement obligation {v.id}: {v.detail}")
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
    for rec in (manifest.get("recordings") or {}).values():
        path = package / rec
        print(f"replay: {path.resolve() if path.exists() else str(path) + ' (not written)'}")
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
    return judge(run, console, ref_text, manifest.get("obligations") or [])


def _report(package: Path, results: list[RunJudgement]) -> None:
    data = [{**asdict(r), "passed": r.passed} for r in results]
    (package / "output" / "report.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
