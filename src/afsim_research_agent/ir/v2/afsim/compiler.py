"""Compile the IR 2.0 internal model to AFSIM 2.9 (plan B).

Every role becomes a ``WSF_STATE_MACHINE`` processor with one ``advanced_behavior_tree``
per state (design §5, §7). Composite and decorator nodes map one to one to native nodes;
action leaves come from the backend binding (:mod:`.binding`): either a generated leaf
around a binding script that calls the project's public interface, or the project's own
leaf fed by a generated inputs leaf that issues a fresh request per activation. Leaves
that are running when the state is left are cancelled in the state's ``on_exit``, and the
resources the contract releases on exit are released there.

Conventions of the generated input (all names start with ``ir_`` / ``IR_``):

- a ``IR_BOOT`` state enters the initial state, so its ``on_entry`` runs (AFSIM does not
  run the initial state's ``on_entry``);
- every ``on_entry`` writes ``IR_STATE|<time>|<platform>|<state>`` for the evaluator, and
  the first execution of each action leaf in a state writes ``IR_ACT|<time>|<platform>|<action>``
  (requirement obligations are judged on these), its first failure
  ``IR_FAIL|<time>|<platform>|<action>|FAILURE:<code>`` and, for script actions, its first
  success ``IR_DONE|<time>|<platform>|<action>`` (orderings across vehicles are judged on
  completions: a member may poll for its task before the leader sent it);
- every run samples every platform's position and speed each ``sample_s`` (default 1 s):
  ``IR_POS|<time>|<platform>|<side>|<north m>|<east m / cos(lat0)>|<speed m/s>|<heading>``
  relative to the first platform's start (``IR_POS_ORIGIN|<lat0>|<lon0>``), so the judge can
  check what the vehicles did (stopped, kept distance, moved in turn), not only what they
  called;
- every run writes an event-pipe recording ``output/<run>.aer`` for replay in mystic
  (``record = false`` in the run config turns it off) and, unless the run configures its own,
  an event log ``output/<run>.evt`` with the standard platform and weapon events the judge
  counts (``record_events = false`` turns it off);
- the manifest maps each generated behavior node to its role, state and action and each
  state-machine processor to its role, so an AFSIM error can be placed in the model;
- external events are auxiliary data ``ir_ev_<id>`` set by the run file and consumed by
  the transition that fires on them.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from afsim_research_agent.ir.units import infer_dimension, to_canonical
from afsim_research_agent.ir.v2.afsim.binding import Binding, RunConfig
from afsim_research_agent.ir.v2.contracts import Contracts
from afsim_research_agent.ir.v2.from_tactic import event_id
from afsim_research_agent.ir.v2.obligations import event_actions

AUX_GET = {
    "bool": "AuxDataBool",
    "int": "AuxDataInt",
    "double": "AuxDataDouble",
    "string": "AuxDataString",
}
OPS = {"eq": "==", "ne": "!=", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}
COMPOSITE_TAGS = {
    "sequence": "sequence",
    "sequence_with_memory": "sequence_with_memory",
    "selector": "selector",
    "selector_with_memory": "selector_with_memory",
    "priority_selector": "priority_selector",
    "weighted_random": "weighted_random",
    "parallel": "parallel",
}


class CompileError(ValueError):
    """The model cannot be expressed with this binding."""


# standard event_output events the judge reads (shots, hits, kills)
EVENT_OUTPUT = ("PLATFORM_ADDED", "PLATFORM_BROKEN", "WEAPON_FIRED", "WEAPON_HIT", "WEAPON_MISSED")


def ident(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", text).strip("_")


def num(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return f"{value}.0"
    return repr(float(value))


def lit(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return num(value)
    if value is None:
        return '""'
    text = str(value)
    if '"' in text or "\n" in text:
        raise CompileError(f"string literal {text!r} cannot be written to AFSIM script")
    return f'"{text}"'


def _traced(lines: list[str]) -> list[str]:
    """Debug build: every leaf writes IR_TICK|<time>|<leaf> when it is evaluated."""
    out: list[str] = []
    name = ""
    for line in lines:
        if line.startswith("advanced_behavior "):
            name = line.split()[1]
        tick = f'writeln("IR_TICK|", TIME_NOW, "|{name}");'
        if line.startswith("   precondition ") and line.endswith(" end_precondition"):
            line = line.replace("   precondition ", f"   precondition {tick} ", 1)
        out.append(line)
    return out


def subst(text: str, **values: str) -> str:
    """Replace ``{key}`` for the given keys only (scripts contain other braces)."""
    for key, value in values.items():
        text = text.replace("{" + key + "}", str(value))
    return text


MAX_HOPS = 8  # state changes per update (guards against cycles of true transitions)


def indent(text: str, pad: str) -> list[str]:
    return [pad + line if line.strip() else "" for line in text.strip("\n").splitlines()]


@dataclass
class _Leaf:
    name: str
    action: str
    kind: str
    request: str  # script expression of the request id
    cancel: str
    exit: str = ""
    enter: str = ""


@dataclass
class _State:
    leaves: list[_Leaf] = field(default_factory=list)
    guards: list[str] = field(default_factory=list)


@dataclass
class Package:
    behavior_file: str
    behavior: str
    runs: dict[str, str]
    manifest: dict[str, Any]


def _placeholders(model: dict[str, Any]) -> list[str]:
    found: set[str] = set()

    def walk(node: dict[str, Any] | None, where: str) -> None:
        if not node:
            return
        if "gap" in node:
            found.add(f"{node['gap']['id']} ({where})")
        for kid in node.get("children") or []:
            walk(kid, where)

    for rid, role in model["roles"].items():
        for state, body in role["states"].items():
            walk(body.get("tree"), f"{rid}.{state}")
    return sorted(found)


class Compiler:
    def __init__(
        self,
        model: dict[str, Any],
        contracts: Contracts,
        binding: Binding,
        *,
        trace: bool = False,
    ) -> None:
        self.trace = trace
        self.m = model
        self.c = contracts
        self.b = binding
        self.mid = ident(model["model"]["id"])
        self.P = f"ir_{self.mid}"
        self.scripts: list[str] = []
        self.behaviors: list[str] = []
        self.params = {k: v.get("value") for k, v in model["parameters"].items()}
        self.missing: set[str] = set()
        self.role_actions: dict[str, set[str]] = {}  # role → actions its trees run
        self.state_actions: dict[str, dict[str, set[str]]] = {}  # role → state → actions
        self.nodes: dict[str, dict[str, str]] = {}  # behavior node → role, state (, action)
        self.processors: dict[str, str] = {}  # state-machine processor → role

    # ------------------------------------------------------------------ package
    def package(self, runs: list[RunConfig], obligations: list[dict] | None = None) -> Package:
        placeholders = _placeholders(self.m)
        if placeholders:
            raise CompileError(
                "GAP002: the model still has gap placeholders; fill the gaps first: "
                + ", ".join(placeholders)
            )
        self._scenario_parameters(runs)
        processors = [self._role(rid, role) for rid, role in self.m["roles"].items()]
        if self.missing:
            raise CompileError("binding has no entry for: " + ", ".join(sorted(self.missing)))
        head = [
            f"# Generated from IR 2.0 model '{self.m['model']['id']}' - do not edit.",
            f"# capability set {self.m['capability']['set']}",
            f"#   {self.m['capability']['content_hash']}",
            f"# binding {self.b.meta.name}",
            "",
            *[f"include_once {inc}" for inc in self.b.meta.includes],
            "",
            *([self.b.meta.scripts.strip(), ""] if self.b.meta.scripts.strip() else []),
        ]
        behaviors = _traced(self.behaviors) if self.trace else self.behaviors
        behavior = "\n".join([*head, *self.scripts, *behaviors, *processors]) + "\n"
        name = f"{self.P}_behavior.txt"
        run_files = {f"run_{ident(r.id)}.txt": self._run(r, name) for r in runs}
        manifest = {
            "model": self.m["model"]["id"],
            "capability": self.m["capability"],
            "binding": self.b.meta.name,
            "model_root": self.b.meta.model_root,
            "behavior_file": name,
            "runs": {f: r.model_dump() for f, r in zip(run_files, runs, strict=True)},
            "recordings": {
                f: f"output/{ident(r.id)}.aer"
                for f, r in zip(run_files, runs, strict=True)
                if r.record
            },
            "obligations": list(obligations or []),
            "role_actions": {r: sorted(a) for r, a in sorted(self.role_actions.items())},
            "nodes": dict(sorted(self.nodes.items())),
            # the failure codes the capability set states for each action the model runs
            # (feedback names a code only when it is one of these)
            "failure_codes": {
                a: list(self.c.actions[a].failure_codes)
                for a in sorted(
                    set().union(*self.role_actions.values()) if self.role_actions else []
                )
                if a in self.c.actions
            },
            "processors": dict(sorted(self.processors.items())),
            # role → external event → actions of the states the event leads to (and after)
            "event_actions": {
                r: {e: sorted(a) for e, a in sorted(per.items())}
                for r, per in sorted(
                    event_actions(
                        {rid: role["mission"] for rid, role in self.m["roles"].items()},
                        self.state_actions,
                    ).items()
                )
            },
            "sha256": {
                name: hashlib.sha256(behavior.encode()).hexdigest(),
                **{f: hashlib.sha256(t.encode()).hexdigest() for f, t in run_files.items()},
            },
        }
        return Package(name, behavior, run_files, manifest)

    def write(self, pkg: Package, out: Path) -> list[Path]:
        if out.exists() and any(out.iterdir()):
            raise CompileError(f"{out} is not empty; generated packages go to a new directory")
        out.mkdir(parents=True, exist_ok=True)
        (out / "output").mkdir(exist_ok=True)
        files = {pkg.behavior_file: pkg.behavior, **pkg.runs}
        files["manifest.json"] = json.dumps(pkg.manifest, ensure_ascii=False, indent=2) + "\n"
        written = []
        for rel, text in files.items():
            path = out / rel
            path.write_text(text, encoding="utf-8")
            written.append(path)
        return written

    # ------------------------------------------------------------------ roles
    def platform_type(self, rid: str) -> str:
        return f"IR_{self.mid}_{ident(rid)}".upper()

    def _role(self, rid: str, role: dict[str, Any]) -> str:
        mission = role["mission"]
        proc = f"{self.platform_type(rid)}_SM"
        self.processors[proc] = rid
        req = subst(self.b.instance.request, model=self.mid, role=ident(rid))
        owner = subst(self.b.instance.owner, model=self.mid, role=ident(rid))
        self.req, self.owner, self.rid = lit(req), lit(owner), rid
        states: dict[str, _State] = {s: _State() for s in mission["states"]}
        trees: dict[str, list[str]] = {}
        for s in mission["states"]:
            info = role["states"].get(s, {})
            if info.get("tree") is not None and s not in mission["terminal"]:
                self.state = states[s]
                self.sname = s
                root = self._node(info["tree"], "         ")
                if self._needs_tree_status(rid, s):
                    root = self._status_wrapper(root, s)
                trees[s] = root
        init = subst(
            self.b.instance.on_initialize,
            model=self.mid,
            role=ident(rid),
            request=lit(req),
            owner=lit(owner),
        )
        lines = [
            f"processor {proc} WSF_STATE_MACHINE",
            f"   update_interval {num(self.b.meta.update_interval_s)} sec",
            "   script_variables",
            '      string currentState = "IR_BOOT";',
            "   end_script_variables",
            "   on_initialize",
            *indent(init, "      "),
            "   end_on_initialize",
            "   state IR_BOOT",
            f"      next_state {mission['initial_state']} return true; end_next_state",
            "   end_state",
        ]
        for s in mission["states"]:
            lines += self._state(rid, role, s, states[s], trees.get(s))
        lines += [
            # transitions chain within one update; a cycle of transitions that are all true
            # would never return, so at most MAX_HOPS state changes happen per update
            '   on_update PLATFORM.SetAuxData("ir_hops", 0); '
            "currentState = STATE_MACHINE.Evaluate(currentState); end_on_update",
            "end_processor",
            "",
            f"platform_type {self.platform_type(rid)} {self.b.meta.base_platform_type}",
            f"   processor ir_behavior {proc} end_processor",
            *indent(self.b.meta.platform_body.strip(), "   "),
            "end_platform_type",
            "",
        ]
        return "\n".join(lines)

    def _state(self, rid, role, s, st: _State, tree: list[str] | None) -> list[str]:
        mission = role["mission"]
        entry = [
            'PLATFORM.SetAuxData("ir_entered_s", TIME_NOW);',
            'PLATFORM.SetAuxData("ir_hops", PLATFORM.AuxDataInt("ir_hops") + 1);',
            *[f'PLATFORM.SetAuxData("{g}", false);' for g in st.guards],
            *(
                [f'PLATFORM.SetAuxData("{self.P}_tree", "running");']
                if self._needs_tree_status(rid, s)
                else []
            ),
            f'writeln("IR_STATE|", TIME_NOW, "|", PLATFORM.Name(), "|{s}");',
            *subst(self.b.instance.state_entry, state=s).strip().splitlines(),
        ]
        # the evaluator's action starts (IR_ACT) are reported once per state entry
        entry += [f'PLATFORM.SetAuxData("{leaf.name}_act", false);' for leaf in st.leaves]
        entry += [
            f'PLATFORM.SetAuxData("{leaf.name}_{flag}", false);'
            for leaf in st.leaves
            if leaf.kind == "script"
            for flag in ("fail", "done")
        ]
        seen: list[str] = []
        for leaf in st.leaves:
            if leaf.enter.strip() and leaf.enter not in seen:
                seen.append(leaf.enter)
                entry += leaf.enter.strip().splitlines()
        out = [
            f"   state {s}",
            "      on_entry",
            *[f"         {x}" for x in entry if x],
            "      end_on_entry",
        ]
        exit_lines = self._exit(role["states"].get(s, {}), st)
        if exit_lines:
            out += ["      on_exit", *exit_lines, "      end_on_exit"]
        if s not in mission["terminal"]:
            # a state "transitions to itself" by staying (the last next_state below); AFSIM
            # refuses a second next_state to the same state
            local = [t for t in mission["transitions"] if t["from"] == s and t["to"] != s]
            globals_ = [g for g in mission["global"] if g["to"] != s]
            ordered = sorted(local + globals_, key=lambda t: -int(t.get("priority", 0)))
            # AFSIM allows one next_state per target: transitions to the same state are
            # merged in priority order, placed where the first of them stands
            targets: dict[str, list[dict[str, Any]]] = {}
            for t in ordered:
                targets.setdefault(t["to"], []).append(t)
            for to, group in targets.items():
                out += [
                    f"      next_state {to}",
                    f'         if (PLATFORM.AuxDataInt("ir_hops") >= {MAX_HOPS})',
                    f'         {{ writeln("IR_LOOP|", TIME_NOW, "|", PLATFORM.Name(), "|{s}"); '
                    "return false; }",
                ]
                for t in group:
                    cond, consume = self._trigger(t["on"])
                    out += [
                        f"         if ({cond})",
                        "         {",
                        *[f"            {x}" for x in consume],
                        "            return true;",
                        "         }",
                    ]
                out += ["         return false;", "      end_next_state"]
        out.append(f"      next_state {s} return true; end_next_state")
        if tree is not None:
            out += [
                "      advanced_behavior_tree",
                "         btt on",
                *tree,
                "      end_advanced_behavior_tree",
            ]
        out.append("   end_state")
        return out

    def _exit(self, info: dict[str, Any], st: _State) -> list[str]:
        lines: list[str] = []
        for leaf in st.leaves:
            if not leaf.cancel.strip():
                if leaf.kind == "native_leaf":
                    lines.append(f'         PLATFORM.SetAuxData("{leaf.name}_req", "");')
                continue
            if leaf.kind == "script":
                lines += [f'         if (PLATFORM.AuxDataBool("{leaf.name}_run"))', "         {"]
                lines += indent(leaf.cancel, "            ")
                lines += [
                    f'            PLATFORM.SetAuxData("{leaf.name}_run", false);',
                    "         }",
                ]
            else:
                lines += [
                    f'         if (PLATFORM.AuxDataString("{leaf.name}_req") != "")',
                    "         {",
                    f'            string req = PLATFORM.AuxDataString("{leaf.name}_req");',
                    *indent(leaf.cancel, "            "),
                    f'            PLATFORM.SetAuxData("{leaf.name}_req", "");',
                    "         }",
                ]
        exits: list[str] = []
        for leaf in st.leaves:
            if leaf.exit.strip() and leaf.exit not in exits:
                exits.append(leaf.exit)
        for text in exits:
            lines += indent(text, "         ")
        released: set[str] = set()
        for lease in info.get("leases", []):
            released.update(lease.get("release_on_exit", []))
        for res in sorted(released):
            rb = self.b.resource.get(res)
            if rb is None or not rb.release:
                continue
            lines += indent(self._fmt(rb.release, platform="PLATFORM", resource=res), "         ")
        return lines

    # ------------------------------------------------------------------ triggers
    def _event(self, name: str) -> dict[str, Any] | None:
        return self.m.get("events", {}).get(name)

    def _needs_tree_status(self, rid: str, state: str) -> bool:
        mission = self.m["roles"][rid]["mission"]
        names = [t["on"] for t in mission["transitions"] if t["from"] == state]
        names += [g["on"] for g in mission["global"]]
        return any("tree" in (self._semantics(n) or {}) for n in names)

    def _semantics(self, name: str) -> dict[str, Any] | None:
        ev = self._event(name)
        if ev is not None and ev.get("source") == "tree":
            return {"tree": ev.get("result")}
        if ev is None or ev.get("source") != "tactic":
            return None
        return self.c.semantics.event(ev.get("tactic"), ev.get("text"))

    def _trigger(self, name: str) -> tuple[str, list[str]]:
        if name in self.m["conditions"]:
            return self._expr(self.m["conditions"][name]), []
        ev = self._event(name)
        if ev is None:
            raise CompileError(f"trigger '{name}' is neither a condition nor an event")
        flag = f"ir_ev_{ident(name)}"
        consume = [f'PLATFORM.SetAuxData("{flag}", false);']
        source = ev.get("source")
        if source == "external":
            return self._external(name, flag), consume
        if source == "timer":
            return (
                f'(TIME_NOW - PLATFORM.AuxDataDouble("ir_entered_s")) >= {num(ev["after_s"])}',
                [],
            )
        if source == "tree":
            return f'PLATFORM.AuxDataString("{self.P}_tree") == "{ev.get("result")}"', []
        if source == "tactic":
            sem = self._semantics(name)
            if sem is None:
                raise CompileError(
                    f"tactic event '{ev.get('text')}' of {ev.get('tactic')} has no semantics"
                )
            if sem.get("external"):
                return self._external(name, flag), consume
            if "tree" in sem:
                return f'PLATFORM.AuxDataString("{self.P}_tree") == "{sem["tree"]}"', []
            if "after_s" in sem:
                return (
                    f'(TIME_NOW - PLATFORM.AuxDataDouble("ir_entered_s")) >= {num(sem["after_s"])}',
                    [],
                )
            if "expression" in sem:
                return self._expr(sem["expression"]), []
            if "event" in sem:
                other = f"ir_ev_{ident(str(sem['event']))}"
                return f'PLATFORM.AuxDataBool("{other}")', [
                    f'PLATFORM.SetAuxData("{other}", false);'
                ]
        raise CompileError(f"{source} event '{name}' cannot be compiled yet")

    def _external(self, name: str, flag: str) -> str:
        extra = self.b.external.get(name)
        cond = f'PLATFORM.AuxDataBool("{flag}")'
        return f"({cond} || ({extra.strip()}))" if extra else cond

    # ------------------------------------------------------------------ expressions
    def _expr(self, e: Any) -> str:
        if not isinstance(e, dict) or len(e) != 1:
            raise CompileError(f"bad expression {e!r}")
        key, value = next(iter(e.items()))
        if key == "all":
            return "(" + " && ".join(self._expr(v) for v in value) + ")"
        if key == "any":
            return "(" + " || ".join(self._expr(v) for v in value) + ")"
        if key == "not":
            return f"!({self._expr(value)})"
        if key == "condition_ref":
            return self._expr(self.m["conditions"][value])
        if key == "compare":
            left, right = self._operand(value["left"]), self._operand(value["right"])
            return f"({left} {OPS[value['operator']]} {right})"
        return self._operand(e)

    def _operand(self, op: Any) -> str:
        if not isinstance(op, dict):
            return lit(op)
        if set(op) == {"value", "unit"}:
            dim = infer_dimension(str(op["unit"]))
            return num(
                to_canonical(float(op["value"]), str(op["unit"]), dim) if dim else op["value"]
            )
        key, value = next(iter(op.items()))
        if key == "literal":
            return lit(value)
        if key == "parameter_ref":
            return lit(self.params.get(value))
        if key == "bb":
            return self._bb(value)
        if key == "function_call":
            args = value.get("arguments") or {}
            if value["name"] == "time_in_state_gte":
                d = self._operand(args["duration"])
                return f'((TIME_NOW - PLATFORM.AuxDataDouble("ir_entered_s")) >= {d})'
            if value["name"] == "elapsed_since_gte":
                f = self._bb(args["field"])
                d = self._operand(args["duration"])
                # the platform writes 0 for "not set"
                return f"(({f} > 0.0) && ((TIME_NOW - {f}) >= {d}))"
        raise CompileError(f"operand {op!r} cannot be compiled")

    def _bb(self, path: str) -> str:
        spec = self.m["blackboard"].get(path) or {}
        kind = spec.get("type")
        if path.startswith("mission."):
            aux = f"ir_bb_{ident(path[len('mission.') :])}"
        else:
            aux = self.b.aux_key(path, spec.get("slice"))
        if aux is None:
            self.missing.add(f"blackboard field {path}")
            return "false"
        getter = AUX_GET.get(str(kind))
        if getter is None:
            raise CompileError(f"blackboard field {path} of type {kind} cannot be read")
        return f'PLATFORM.{getter}("{aux}")'

    # ------------------------------------------------------------------ tree
    def _node(self, n: dict[str, Any], pad: str, weight: Any = None) -> list[str]:
        kind = n["type"]
        kids = n.get("children") or []
        if kind in ("tactic", "subtree"):
            inner = (
                self._node(kids[0], pad + ("   " if n.get("failure_as_running") else ""), weight)
                if kids
                else []
            )
            if not n.get("failure_as_running"):
                return inner
            # keep running instead of failing: the subtree, or a leaf that reports running
            return [
                f"{pad}selector",
                *inner,
                f"{pad}   behavior_node {self._running_leaf()}",
                f"{pad}end_selector",
            ]
        if kind in ("action", "condition"):
            names = self._leaf(n, weight)
            if len(names) == 1:
                return [f"{pad}behavior_node {names[0]}"]
            # a project leaf with its inputs leaf: one sequence, as one node of the tree
            return [
                f"{pad}sequence",
                *[f"{pad}   behavior_node {x}" for x in names],
                f"{pad}end_sequence",
            ]
        if weight is not None:
            raise CompileError("a weight can only be given to a leaf in AFSIM")
        if kind in COMPOSITE_TAGS:
            tag = COMPOSITE_TAGS[kind]
            out = [f"{pad}{tag}"]
            if kind == "parallel":
                p = n.get("policy") or {"type": "one"}
                policy = {"one": "succeed_on_one", "all": "succeed_on_all"}.get(p["type"])
                out.append(f"{pad}   success_policy {policy or 'threshold ' + str(p.get('count'))}")
            for kid in kids:
                w = kid.get("weight") if kind in ("priority_selector", "weighted_random") else None
                out += self._node(kid, pad + "   ", w)
            return [*out, f"{pad}end_{tag}"]
        if kind == "failure_as_running":
            return [
                f"{pad}selector",
                *self._node(kids[0], pad + "   "),
                f"{pad}   behavior_node {self._running_leaf()}",
                f"{pad}end_selector",
            ]
        if kind in ("inverter", "succeeder", "negator"):
            return [
                f"{pad}decorator {kind}",
                *self._node(kids[0], pad + "   "),
                f"{pad}end_decorator",
            ]
        if kind == "repeat":
            rep = n["repeat"]
            if "count" in rep:
                how = f"repeat {int(rep['count'])}"
            elif "duration_s" in rep:
                how = f"for {num(rep['duration_s'])} sec"
            else:
                how = "until_done"
            return [
                f"{pad}decorator repeater {how}",
                *self._node(kids[0], pad + "   "),
                f"{pad}end_decorator",
            ]
        if kind == "timeout":
            # tactic-set timeout: the child, or wait until the time is up and fail
            guard = self._guard(n)
            return [
                f"{pad}selector",
                *(self._node(kids[0], pad + "   ") if kids else []),
                f"{pad}   behavior_node {guard}",
                f"{pad}end_selector",
            ]
        raise CompileError(f"node type {kind} cannot be compiled")

    def _status_wrapper(self, root: list[str], state: str) -> list[str]:
        key = f"{self.P}_tree"
        base = f"{self.P}_{ident(self.rid)}_{ident(state)}_status"
        for suffix, value, ret in (
            ("reset", "running", "Success()"),
            ("ok", "success", "Success()"),
            ("fail", "failure", 'Failure("tree failed")'),
        ):
            self.behaviors += [
                f"advanced_behavior {base}_{suffix}",
                "   precondition return Success(); end_precondition",
                f'   execute PLATFORM.SetAuxData("{key}", "{value}"); return {ret}; end_execute',
                "end_advanced_behavior",
                "",
            ]
        pad = "         "
        body = [("   " + line) if line.strip() else line for line in root]
        return [
            f"{pad}selector",
            f"{pad}   sequence",
            f"{pad}      behavior_node {base}_reset",
            *[("   " + line) for line in body],
            f"{pad}      behavior_node {base}_ok",
            f"{pad}   end_sequence",
            f"{pad}   behavior_node {base}_fail",
            f"{pad}end_selector",
        ]

    def _running_leaf(self) -> str:
        name = f"{self.P}_running"
        if not getattr(self, "_has_running", False):
            self._has_running = True
            self.behaviors += [
                f"advanced_behavior {name}",
                "   precondition return Success(); end_precondition",
                '   execute return Running("running"); end_execute',
                "end_advanced_behavior",
                "",
            ]
        return name

    def _new_name(self, suffix: str = "") -> str:
        count = getattr(self, "_count", 0) + 1
        self._count = count
        name = f"{self.P}_{ident(self.rid)}_{ident(self.sname)}_{count}{suffix}"
        self.nodes[name] = {"role": self.rid, "state": self.sname}
        return name

    def _guard(self, n: dict[str, Any]) -> str:
        name = self._new_name("_timeout")
        key, flag = f"{name}_t", f"{name}_set"
        self.state.guards.append(flag)
        self.behaviors += [
            f"advanced_behavior {name}",
            "   precondition return Success(); end_precondition",
            "   execute",
            f'      if (!PLATFORM.AuxDataBool("{flag}"))',
            f'      {{ PLATFORM.SetAuxData("{flag}", true); '
            f'PLATFORM.SetAuxData("{key}", TIME_NOW); }}',
            f'      if ((TIME_NOW - PLATFORM.AuxDataDouble("{key}")) >= '
            f"{num(n.get('duration_s') or 0.0)})",
            '      { return Failure("timeout"); }',
            '      return Running("waiting");',
            "   end_execute",
            "end_advanced_behavior",
            "",
        ]
        return name

    def _precondition(self, weight: Any) -> str:
        if weight is None:
            return "return Success();"
        value = weight if isinstance(weight, int | float) else self._operand(weight)
        return f"return {num(value) if isinstance(value, int | float) else value};"

    def _leaf(self, n: dict[str, Any], weight: Any) -> list[str]:
        pre = self._precondition(weight)
        if n["type"] == "condition":
            if "tactic_condition" in n:
                sem = self.c.semantics.tactic_conditions.get(n["tactic_condition"])
                if sem is None:
                    self.missing.add(f"tactic condition {n['tactic_condition']}")
                    expr = "false"
                elif "event" in sem:
                    expr = f'PLATFORM.AuxDataBool("ir_ev_{ident(str(sem["event"]))}")'
                else:
                    expr = self._expr(sem["expression"])
            else:
                expr = self._expr(self.m["conditions"][n["condition"]])
            name = self._new_name("_cond")
            body = f"return {expr};" if weight is None else f"if ({expr}) {{ {pre} }} return 0.0;"
            self.behaviors += [
                f"advanced_behavior {name}",
                f"   precondition {body} end_precondition",
                # a node with only a precondition never lets a sequence continue (probe w01)
                "   execute return Success(); end_execute",
                "end_advanced_behavior",
                "",
            ]
            return [name]
        action = str(n.get("action"))
        self.role_actions.setdefault(self.rid, set()).add(action)
        self.state_actions.setdefault(self.rid, {}).setdefault(self.sname, set()).add(action)
        ab = self.b.action.get(action)
        if ab is None:
            self.missing.add(f"action {action}")
            return []
        name = self._new_name()
        self.nodes[name]["action"] = action
        params = n.get("params") or {}
        if n.get("operation") == "cancel":
            return [self._cancel_leaf(name, action, ab, pre)]
        if ab.kind == "script":
            func = name.upper()
            body = self._fmt(ab.body, platform="aPlatform", action=action, params=params, leaf=n)
            self.scripts += [
                f"script string {func}(WsfPlatform aPlatform)",
                *indent(body, "   "),
                "end_script",
                "",
            ]
            self.behaviors += [
                f"advanced_behavior {name}",
                f"   precondition {pre} end_precondition",
                "   execute",
                *_activation(name, action),
                f"      string st = {func}(PLATFORM);",
                f'      PLATFORM.SetAuxData("{name}_run", st == "RUNNING");',
                # the first failure after the state was entered, with its code (IR_FAIL)
                '      if (st != "SUCCESS" && st != "RUNNING" && '
                f'!PLATFORM.AuxDataBool("{name}_fail"))',
                f'      {{ PLATFORM.SetAuxData("{name}_fail", true); '
                f'writeln("IR_FAIL|", TIME_NOW, "|", PLATFORM.Name(), "|{action}|", st); }}',
                # the first success after the state was entered (IR_DONE)
                f'      if (st == "SUCCESS" && !PLATFORM.AuxDataBool("{name}_done"))',
                f'      {{ PLATFORM.SetAuxData("{name}_done", true); '
                f'writeln("IR_DONE|", TIME_NOW, "|", PLATFORM.Name(), "|{action}"); }}',
                '      if (st == "SUCCESS") { return Success(); }',
                f'      if (st == "RUNNING") {{ return Running("{action}"); }}',
                "      return Failure(st);",
                "   end_execute",
                "end_advanced_behavior",
                "",
            ]
            self.state.leaves.append(
                _Leaf(
                    name,
                    action,
                    "script",
                    self.req,
                    self._fmt(ab.cancel, platform="PLATFORM", action=action, params=params, leaf=n),
                    self._fmt(ab.exit, platform="PLATFORM", action=action, params=params, leaf=n),
                    self._fmt(ab.enter, platform="PLATFORM", action=action, params=params, leaf=n),
                )
            )
            return [name]
        # native leaf of the project, fed by a generated inputs leaf
        inputs = self._fmt(
            ab.inputs, platform="PLATFORM", action=action, params=params, leaf=n, request="req"
        )
        done = self._fmt(
            ab.done, platform="PLATFORM", action=action, params=params, leaf=n, request="req"
        )
        self.behaviors += [
            f"advanced_behavior {name}_in",
            f"   precondition {pre} end_precondition",
            "   execute",
            *_activation(name, action),
            f'      string req = PLATFORM.AuxDataString("{name}_req");',
            '      bool fresh = (req == "");',
            f"      if (!fresh) {{ fresh = ({done.strip() or 'false'}); }}",
            "      if (fresh)",
            "      {",
            f'         int c = PLATFORM.AuxDataInt("{name}_n") + 1;',
            f'         PLATFORM.SetAuxData("{name}_n", c);',
            f'         req = "{name}_" + (string)c;',
            f'         PLATFORM.SetAuxData("{name}_req", req);',
            "      }",
            *indent(inputs, "      "),
            "      return Success();",
            "   end_execute",
            "end_advanced_behavior",
            "",
        ]
        self.state.leaves.append(
            _Leaf(
                name,
                action,
                "native_leaf",
                "req",
                self._fmt(
                    ab.cancel,
                    platform="PLATFORM",
                    action=action,
                    params=params,
                    leaf=n,
                    request="req",
                ),
                self._fmt(ab.exit, platform="PLATFORM", action=action, params=params, leaf=n),
                self._fmt(ab.enter, platform="PLATFORM", action=action, params=params, leaf=n),
            )
        )
        return [f"{name}_in", ab.leaf]

    def _cancel_leaf(self, name: str, action: str, ab, pre: str) -> str:
        cancel = self._fmt(ab.cancel, platform="PLATFORM", action=action, params={}, leaf={})
        self.behaviors += [
            f"advanced_behavior {name}_cancel",
            f"   precondition {pre} end_precondition",
            "   execute",
            *indent(cancel, "      "),
            "      return Success();",
            "   end_execute",
            "end_advanced_behavior",
            "",
        ]
        return f"{name}_cancel"

    def _fmt(
        self,
        template: str,
        *,
        platform: str,
        action: str = "",
        params=None,
        leaf=None,
        request: str | None = None,
        resource: str = "",
    ) -> str:
        values = {
            "platform": platform,
            "request": request if request is not None else self.req,
            "owner": self.owner,
            "action": lit(action),
            "resource": resource,
        }
        resolved = self._params(leaf or {}, params or {})

        def sub(match: re.Match[str]) -> str:
            key = match.group(1)
            if key.startswith("p?:"):
                name = key[3:]
                if name in resolved:
                    return lit(resolved[name])
                spec = self._param_spec(leaf or {}, name)
                getter = {
                    "number": "AuxDataDouble",
                    "integer": "AuxDataInt",
                    "boolean": "AuxDataBool",
                }
                return (
                    f'{platform}.{getter.get(spec.get("type"), "AuxDataString")}("ir_param_{name}")'
                )
            if key.startswith("p:"):
                name = key[2:]
                if name not in resolved:
                    raise CompileError(f"action {action} has no parameter '{name}' to write")
                return lit(resolved[name])
            if key in values:
                return str(values[key])
            raise CompileError(f"unknown placeholder {{{key}}} in binding")

        return re.sub(r"\{([a-z_]+\??(?::[A-Za-z0-9_]+)?)\}", sub, template)

    def _param_spec(self, leaf: dict[str, Any], name: str) -> dict[str, Any]:
        contract = self.c.actions.get(str(leaf.get("action")))
        spec = contract.parameters.get(name) if contract else None
        return spec if isinstance(spec, dict) else {}

    def _params(self, leaf: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
        contract = self.c.actions.get(str(leaf.get("action")))
        out: dict[str, Any] = {}
        if contract is not None:
            for name, spec in contract.parameters.items():
                if isinstance(spec, dict) and "default" in spec:
                    out[name] = spec["default"]
        for name, value in params.items():
            if isinstance(value, dict) and set(value) == {"parameter_ref"}:
                value = self.params.get(value["parameter_ref"])
            out[name] = value
        return out

    def _scenario_parameters(self, runs: list[RunConfig]) -> None:
        """Values the scenario supplies (a route name, …) replace the model's placeholders.
        They live in the local run configuration and never in the model or a prompt."""
        given: dict[str, Any] = {}
        for run in runs:
            for name, value in run.parameters.items():
                if name not in self.params:
                    raise CompileError(f"run {run.id}: the model has no parameter '{name}'")
                if name in given and given[name] != value:
                    raise CompileError(f"runs disagree on the value of parameter '{name}'")
                given[name] = value
        self.params.update(given)

    # ------------------------------------------------------------------ run file
    def _run(self, run: RunConfig, behavior_file: str) -> str:
        roles = self.m["roles"]
        lines = [
            f"# Generated run '{run.id}' of IR 2.0 model '{self.m['model']['id']}' - do not edit.",
            f"define_path_variable CASE {ident(run.id)}",
            "file_path .",
            f"file_path {self.b.meta.model_root}",
            "log_file output/$(CASE).log",
            run.preamble.strip(),
            *[f"include_once {inc}" for inc in run.includes],
            f"include_once {behavior_file}",
            "",
        ]
        for p in run.platforms:
            if p.role not in roles:
                raise CompileError(f"run {run.id}: platform {p.name} has unknown role {p.role}")
            lines += [
                f"platform {p.name} {self.platform_type(p.role)}",
                *indent(p.body, "   "),
                "end_platform",
                "",
            ]
        lines.append(run.extra.strip())
        for ev in sorted(run.events, key=lambda e: e.at_s):
            name = self._event_name(ev.event)
            targets = [ev.platform] if ev.platform else [p.name for p in run.platforms]
            lines += [f"execute at_time {num(ev.at_s)} sec absolute"]
            lines += [
                f'   WsfSimulation.FindPlatform("{t}").SetAuxData("ir_ev_{ident(name)}", true);'
                for t in targets
            ]
            lines += ["end_execute"]
        if run.sample_s > 0:
            lines += _sampler(run.sample_s)
        if run.record and "event_pipe" not in f"{run.preamble}\n{run.extra}":
            # recording for replay in mystic, next to the console and the event log
            lines += ["event_pipe", "   file output/$(CASE).aer", "end_event_pipe"]
        if run.record_events and "event_output" not in f"{run.preamble}\n{run.extra}":
            # AFSIM's standard event log: the judge counts shots, hits and kills from it
            lines += [
                "event_output",
                "   file output/$(CASE).evt",
                *[f"   enable {e}" for e in EVENT_OUTPUT],
                "end_event_output",
            ]
        lines += [
            f"random_seed {run.random_seed}",
            f"start_time {run.start_time}",
            f"end_time {num(run.end_time_s)} sec",
            "",
        ]
        return "\n".join(x for x in lines if x is not None)

    def _event_name(self, name: str) -> str:
        """A run names an event by id or by its tactic-set text."""
        return name if name in self.m.get("events", {}) else event_id(name)


def _sampler(every_s: float) -> list[str]:
    """Position and speed of every platform each ``every_s`` (IR_POS, see the module doc).
    North and east offsets are differences of degrees times 111320 m, the east one not yet
    scaled by cos(lat0): the judge does that (no trigonometry in the script)."""
    return [
        f"execute at_interval_of {num(every_s)} sec",
        "   WsfPlatform r = WsfSimulation.PlatformEntry(0);",
        '   if (!r.AuxDataExists("ir_pos_lat0"))',
        "   {",
        '      r.SetAuxData("ir_pos_lat0", r.Latitude());',
        '      r.SetAuxData("ir_pos_lon0", r.Longitude());',
        '      writeln("IR_POS_ORIGIN|", r.Latitude(), "|", r.Longitude());',
        "   }",
        '   double lat0 = r.AuxDataDouble("ir_pos_lat0");',
        '   double lon0 = r.AuxDataDouble("ir_pos_lon0");',
        "   for (int i = 0; i < WsfSimulation.PlatformCount(); i = i + 1)",
        "   {",
        "      WsfPlatform p = WsfSimulation.PlatformEntry(i);",
        '      writeln("IR_POS|", TIME_NOW, "|", p.Name(), "|", p.Side(), "|", '
        '(p.Latitude() - lat0) * 111320.0, "|", (p.Longitude() - lon0) * 111320.0, "|", '
        'p.Speed(), "|", p.Heading());',
        "   }",
        "end_execute",
    ]


def _activation(name: str, action: str) -> list[str]:
    """Report the first execution of an action leaf after its state was entered."""
    return [
        f'      if (!PLATFORM.AuxDataBool("{name}_act"))',
        "      {",
        f'         PLATFORM.SetAuxData("{name}_act", true);',
        f'         writeln("IR_ACT|", TIME_NOW, "|", PLATFORM.Name(), "|{action}");',
        "      }",
    ]
