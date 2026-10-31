"""Run a compiled package with mission.exe and judge the evidence.

Evidence is the console of each run (with the mission's own log, the event_output file and
the generated behavior file to place errors): the ``IR_STATE|<time>|<platform>|<state>`` lines the
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


# where an issue comes from: the drafted model (its states, conditions, trees), the run
# (scenario evidence), or the infrastructure (binding, compiler, lab files): a drafting
# model cannot repair the last kind by editing its sheet
MODEL, RUN, INFRASTRUCTURE = "model", "run", "infrastructure"

# rules a run configuration may switch off (expect.rules); a switched-off rule reports a
# caveat instead of failing the run
RULES = {
    # the run completed but nothing happened: no action started, no platform left its
    # initial state
    "inert_is_failure": True,
    "no_evidence_is_failure": True,  # no platform of the model wrote any state line
}


@dataclass
class Issue:
    """One finding of a run, with a code and the data behind it (see ``render``)."""

    code: str
    source: str = RUN
    platform: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        d, p = self.data, self.platform
        match self.code:
            case "AFSIM_ERROR":
                where = ""
                if d.get("state"):
                    where = f" ({d.get('role')} state {d['state']}"
                    where += f", action {d['action']})" if d.get("action") else ")"
                head = f"AFSIM {d.get('severity', 'ERROR')} [{d.get('kind')}]"
                return f"{head}: {d.get('message')}{where}"
            case "NOT_COMPLETED":
                return "AFSIM did not complete the simulation"
            case "IR_LOOP":
                return (
                    f"{p}: transitions kept firing from {d['state']} within one update (a "
                    "cycle of conditions that are all true); the update was cut after 8 "
                    "state changes"
                )
            case "NO_STATE_OUTPUT":
                return f"{p} wrote no IR_STATE lines"
            case "NO_EVIDENCE":
                return "no platform of the model wrote a state line (no evidence)"
            case "INERT":
                return (
                    "the run completed but nothing happened: no action of the model started "
                    "and no platform left its initial state (inert)"
                )
            case "SEQUENCE":
                return f"{p} visited {d['got']}, expected {d['expected']}"
            case "MARKER":
                return f"console has no line matching {d['pattern']!r}"
            case "EVENTS":
                return (
                    f"event expectation {d['kind']}"
                    + (f" by {d['by']}" if d.get("by") else "")
                    + (f" on {d['target']}" if d.get("target") else "")
                    + f": {d['count']} seen, at least {d['min']} expected"
                )
            case "REFERENCE_SEQUENCE":
                return (
                    f"{p} sequence {d['got']} differs from the handwritten version {d['reference']}"
                )
            case "INTERVAL":
                return (
                    f"{p} stayed {d['stay']:g}s in {d['state']} (entered at {d['entered']:g}s), "
                    f"the handwritten version {d['ref_stay']:g}s; more than {d['limit']:g}s apart"
                )
            case "REFERENCE_MISSING":
                return "the handwritten reference console was not found"
            case "OBLIGATION":
                return f"requirement obligation {d['id']}: {d['detail']}"
        return f"{self.code}: {d}"


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
    issues: list[Issue] = field(default_factory=list)  # the problems, with codes
    caveats: list[str] = field(default_factory=list)  # findings of switched-off rules
    # platform → {"fired": n, "hits": n, "kills": n, "targets": {target: hits}}
    engagements: dict[str, dict[str, Any]] = field(default_factory=dict)

    def add(self, issue: Issue, rules: dict[str, bool] | None = None, rule: str = "") -> None:
        if rule and not {**RULES, **(rules or {})}.get(rule, True):
            self.caveats.append(issue.render())
            return
        self.issues.append(issue)
        self.problems.append(issue.render())

    @property
    def infrastructure(self) -> bool:
        """The run failed for a reason the drafted model cannot repair."""
        return any(i.source == INFRASTRUCTURE for i in self.issues)

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
        for name, e in self.engagements.items():
            lines.append(f"  {name}: fired {e['fired']}, hits {e['hits']}, kills {e['kills']}")
        lines += [f"  problem: {p}" for p in self.problems]
        lines += [f"  caveat: {c}" for c in self.caveats]
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


ERROR_AT = re.compile(r"^\s*'([^']+)', line (\d+)(?:, near column (\d+))?", re.M)
EVENT_LINE = re.compile(r"^(\d+(?:\.\d+)?) ([A-Z_]+) (.*)$")


def parse_errors(console: str) -> list[dict[str, Any]]:
    """AFSIM's ERROR / FATAL lines with the file position printed under them, and the stage:
    ``input`` when the input files could not be processed, otherwise ``runtime``."""
    lines = console.splitlines()
    stage = "input" if "Could not process input files" in console else "runtime"
    out: list[dict[str, Any]] = []
    for i, ln in enumerate(lines):
        m = re.search(r"\*\*\*\*\* (ERROR|FATAL): (.*)$", ln)
        if not m or "Could not process input files" in m.group(2):
            continue
        err: dict[str, Any] = {
            "severity": m.group(1),
            "message": m.group(2).strip(),
            "stage": stage,
            "kind": _error_kind(m.group(2)),
        }
        for nxt in lines[i + 1 : i + 3]:
            at = ERROR_AT.match(nxt)
            if at:
                err.update(file=at.group(1), line=int(at.group(2)), column=int(at.group(3) or 0))
                break
        if err not in out:
            out.append(err)
    return out


def _error_kind(message: str) -> str:
    if message.startswith("Duplicate next_state"):
        return "duplicate_transition"
    if message.startswith(("Could not find", "Unknown type", "Undefined")):
        return "unknown_reference"
    if "script" in message.lower() or "Unable to" in message:
        return "script"
    if "Unexpected" in message or "Bad value" in message or "expected" in message:
        return "syntax"
    return "other"


def parse_events(text: str) -> list[dict[str, Any]]:
    """Records of an event_output file: time, event, the first two names (actor, target),
    and the fields the judge uses (weapon, result)."""
    out: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for ln in text.splitlines():
        m = EVENT_LINE.match(ln)
        if m:
            words = m.group(3).split()
            current = {
                "t": float(m.group(1)),
                "event": m.group(2),
                "actor": words[0] if words else "",
                "target": words[1] if len(words) > 1 and not words[1].endswith(":") else "",
                "text": m.group(3),
            }
            if current["event"].startswith("WEAPON_") and len(words) > 2:
                current["weapon"] = words[2]
            out.append(current)
        elif current is not None:
            current["text"] += " " + ln.strip()
    for e in out:
        r = re.search(r"\bResult: (\w+)", e["text"])
        if r:
            e["result"] = r.group(1)
    return out


def locate(
    error: dict[str, Any], behavior_file: str, behavior_text: str, manifest: dict[str, Any]
) -> dict[str, Any]:
    """Which role, state and action of the model an AFSIM error points into: only for errors
    in the generated behavior file (from the enclosing advanced_behavior node, or the state of
    the role's state machine)."""
    if not error.get("file") or Path(str(error["file"])).name != Path(behavior_file).name:
        return {}
    lines = behavior_text.splitlines()
    nodes = manifest.get("nodes") or {}
    processors = manifest.get("processors") or {}
    state = None
    for ln in reversed(lines[: int(error.get("line") or 0)]):
        word = ln.split()
        if not word:
            continue
        if word[0] == "advanced_behavior" and len(word) > 1 and state is None:
            node = nodes.get(word[1])
            if node:
                return dict(node)
        if word[0] == "end_state" and state is None:
            return {}
        if word[0] == "state" and len(word) > 1 and state is None:
            state = word[1]
        if word[0] == "processor" and len(word) > 1:
            proc = word[1]
            role = processors.get(proc)
            return {"role": role, "state": state} if role and state else {}
    return {}


def judge(
    run: dict[str, Any],
    console: str,
    reference_console: str | None,
    obligations: list[dict[str, Any]] | None = None,
    role_actions: dict[str, list[str]] | None = None,  # role → actions its trees run
    event_actions: dict[str, dict[str, list[str]]] | None = None,  # role → event → actions
    events_text: str | None = None,  # the run's event_output file
    where: Any = None,  # error → {role, state, action} (see locate)
    log_text: str | None = None,  # mission's own log file (log_file output/$(CASE).log)
) -> RunJudgement:
    expect = run.get("expect") or {}
    rules = dict(expect.get("rules") or {})
    # the console says "Simulation complete"; the mission log ends with "complete <time> ..."
    done = "Simulation complete" in console or bool(
        log_text and re.search(r"\bcomplete \d", log_text)
    )
    j = RunJudgement(run=run["id"], completed=done)
    j.errors = [m.group(0) for m in ERRORS.finditer(console)]
    errs = parse_errors(console)
    for err in errs:
        pos = where(err) if where else {}
        # input errors come from the generated files and the lab files the binding names:
        # the compiler or the binding is wrong, not the drafted sheet; a runtime error inside
        # a state's node may come from the values the model gave
        source = MODEL if err["stage"] == "runtime" and pos.get("state") else INFRASTRUCTURE
        j.add(Issue("AFSIM_ERROR", source, None, {**err, **pos}))
    if any(e["stage"] == "input" for e in errs):
        # the input was refused: nothing ran, so markers, sequences and obligations would
        # only repeat that
        return j
    if not j.completed and not j.errors:
        j.add(Issue("NOT_COMPLETED", RUN))
    j.states = parse_states(console)
    first_loop: dict[tuple[str, str], float] = {}
    for t, platform, state in re.findall(r"IR_LOOP\|([^|]+)\|([^|]+)\|([^\r\n]+)", console):
        first_loop.setdefault((platform, state.strip()), float(t))
    for (platform, state), t in sorted(first_loop.items())[:5]:
        # the states entered in that update: the path the transitions kept taking
        chain = [s for ts, s in j.states.get(platform, []) if ts == t][:12]
        j.add(Issue("IR_LOOP", MODEL, platform, {"state": state, "t": t, "chain": chain}))
    names = [p["name"] for p in run["platforms"]]
    if j.completed and not j.errors:
        if not any(n in j.states for n in names):
            j.add(Issue("NO_EVIDENCE", MODEL), rules, "no_evidence_is_failure")
        elif (
            names
            and not IR_ACT.search(console)
            and all(len(j.states.get(n, [])) <= 1 for n in names)
        ):
            j.add(Issue("INERT", MODEL), rules, "inert_is_failure")
    for name in names:
        if name not in j.states:
            j.add(Issue("NO_STATE_OUTPUT", MODEL, name))
    expected = expect.get("sequence")
    if expected is not None:
        j.expected = list(expected)
        for name in names:
            got = [s for _, s in j.states.get(name, [])]
            if got != j.expected:
                j.add(Issue("SEQUENCE", MODEL, name, {"got": got, "expected": j.expected}))
    notes = dict(expect.get("marker_notes") or {})
    for pattern in expect.get("markers") or []:
        if not re.search(pattern, console, re.M):
            j.add(Issue("MARKER", RUN, None, {"pattern": pattern, "note": notes.get(pattern)}))
    if events_text is not None:
        _judge_events(j, run, parse_events(events_text))
    ref = run.get("reference")
    if ref and reference_console is not None:
        j.reference = parse_reference(reference_console, ref["state_pattern"])
        first = j.states.get(names[0], [])
        if [s for _, s in first] != [s for _, s in j.reference]:
            j.add(
                Issue(
                    "REFERENCE_SEQUENCE",
                    MODEL,
                    names[0],
                    {"got": [s for _, s in first], "reference": [s for _, s in j.reference]},
                )
            )
        else:
            for (tg, sg), (tr, _) in zip(first[1:], j.reference[1:], strict=False):
                j.time_deltas.append(
                    {"transition": f"-> {sg}", "generated": tg, "reference": tr, "delta": tg - tr}
                )
            limit = expect.get("max_interval_delta_s")
            if limit is not None:
                for i in range(1, len(first) - 1):
                    stay = first[i + 1][0] - first[i][0]
                    ref_stay = j.reference[i + 1][0] - j.reference[i][0]
                    if abs(stay - ref_stay) > float(limit):
                        j.add(
                            Issue(
                                "INTERVAL",
                                MODEL,
                                names[0],
                                {
                                    "state": first[i][1],
                                    "stay": stay,
                                    "ref_stay": ref_stay,
                                    "entered": first[i][0],
                                    "limit": float(limit),
                                },
                            )
                        )
    elif ref:
        j.add(Issue("REFERENCE_MISSING", INFRASTRUCTURE))
    if obligations:
        from afsim_research_agent.ir.v2.obligations import Obligation, check

        obs = [Obligation.model_validate(o) for o in obligations]
        acts = [(float(t), p, a.strip()) for t, p, a in IR_ACT.findall(console)]
        events = [(float(e["at_s"]), str(e["event"])) for e in run.get("events") or []]
        capable = None
        if role_actions:
            capable = {
                p["name"]: set(role_actions.get(p["role"], []))
                for p in run.get("platforms") or []
                if "role" in p
            }
        after = None
        if event_actions:
            after = {
                p["name"]: {e: set(a) for e, a in event_actions.get(p["role"], {}).items()}
                for p in run.get("platforms") or []
                if "role" in p
            }
        verdicts = check(
            obs,
            acts,
            events,
            end_s=float(run.get("end_time_s") or 0) or None,
            capable=capable,
            after_event=after,
        )
        j.obligations = [asdict(v) for v in verdicts]
        for v in verdicts:
            if v.judged and not v.passed:
                j.add(Issue("OBLIGATION", MODEL, None, {"id": v.id, "detail": v.detail}))
    return j


def _judge_events(j: RunJudgement, run: dict[str, Any], events: list[dict[str, Any]]) -> None:
    """Engagement statistics of the model's platforms and the run's event expectations
    (``expect.events``), from AFSIM's standard event output."""
    roles = {p["name"]: p.get("role") for p in run.get("platforms") or []}
    for name in roles:
        j.engagements[name] = {"fired": 0, "hits": 0, "kills": 0, "targets": {}}
    for e in events:
        stats = j.engagements.get(e["actor"])
        if stats is None:
            continue
        if e["event"] == "WEAPON_FIRED":
            stats["fired"] += 1
        elif e["event"] == "WEAPON_HIT":
            stats["hits"] += 1
            stats["targets"][e["target"]] = stats["targets"].get(e["target"], 0) + 1
            if e.get("result") in ("KILLED", "DESTROYED"):
                stats["kills"] += 1
    for exp in (run.get("expect") or {}).get("events") or []:
        kind, by, target = exp.get("kind"), exp.get("by"), exp.get("target")
        count = sum(
            1
            for e in events
            if e["event"] == kind
            and (by is None or roles.get(e["actor"]) == by)
            and (target is None or e["target"] == target or e["actor"] == target)
            and (exp.get("result") is None or e.get("result") == exp["result"])
        )
        if count < int(exp.get("min", 1)):
            j.add(
                Issue(
                    "EVENTS",
                    MODEL,
                    None,
                    {
                        "kind": kind,
                        "by": by,
                        "target": target,
                        "min": int(exp.get("min", 1)),
                        "count": count,
                        "note": exp.get("note"),
                    },
                )
            )


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
    case = re.sub(r"[^A-Za-z0-9_]+", "_", str(run["id"])).strip("_")  # the run's $(CASE)
    out = package / "output"

    def text(path: Path) -> str | None:
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else None

    behavior_file = str(manifest.get("behavior_file") or "")
    behavior_text = text(package / behavior_file) if behavior_file else None
    return judge(
        run,
        console,
        ref_text,
        manifest.get("obligations") or [],
        role_actions=manifest.get("role_actions"),
        event_actions=manifest.get("event_actions"),
        events_text=text(out / f"{case}.evt"),
        log_text=text(out / f"{case}.log"),
        where=(lambda e: locate(e, behavior_file, behavior_text or "", manifest))
        if behavior_text
        else None,
    )


def _report(package: Path, results: list[RunJudgement]) -> None:
    data = [{**asdict(r), "passed": r.passed} for r in results]
    (package / "output" / "report.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
