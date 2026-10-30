"""Tactic sheet v3 → IR 2.0 ``behavior_spec`` (design falcon-v3-composition-and-gaps §2, §8).

A v3 sheet has these fenced blocks::

    ```yaml model            model, roles (optionally a tactic-set mission), parameters,
                             new blackboard fields, events, assumptions, implements
    ```mermaid <role>        the role's mission state machine (unless it takes a
                             tactic-set mission)
    ```python conditions     name = expression
    ```python <role>         state trees and subtrees as Python functions (``pytree``)
    ```yaml gaps             gap contracts (IR 2.0 ``gaps``)
    ```yaml verification     L2 tests and L3 scenarios (IR 2.0 ``tests`` / ``scenarios``)

The compiler links names through the capability set, writes the IR, and keeps a source
map (IR path → block and line) so normalization diagnostics point back at the sheet.
Whatever cannot be parsed or linked is a problem filed under its block; the rest still
compiles.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from afsim_research_agent.falcon.sheet.blocks import Block, SheetProblem, split_sheet
from afsim_research_agent.falcon.sheet.mermaid import parse_diagram
from afsim_research_agent.falcon.v3 import catalog as K
from afsim_research_agent.falcon.v3.catalog import internal_field
from afsim_research_agent.falcon.v3.pyexpr import ExprCompiler, ExprError, Symbols, quantity
from afsim_research_agent.falcon.v3.pytree import TreeSymbols, parse_block
from afsim_research_agent.ir.diagnostics import Diagnostic
from afsim_research_agent.ir.units import infer_dimension
from afsim_research_agent.ir.v2.contracts import STATIC_UPDATES, Contracts

V3_KINDS = ("model", "mermaid", "conditions", "pytree", "gaps", "verification")
STATE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
IDENT = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
GLOBAL_PRIORITY = 100
DIM_TYPES = {
    "distance": "distance",
    "speed": "speed",
    "time": "time",
    "angle": "angle",
    "altitude": "altitude",
    "mass": "mass",
    "frequency": "frequency",
    "ratio": "ratio",
}
BASIS = {"explicit", "inferred", "conflicting", "unknown", "user_specified"}


@dataclass
class Compiled:
    spec: dict[str, Any] | None
    problems: list[SheetProblem] = field(default_factory=list)
    source: dict[str, tuple[str, int]] = field(default_factory=dict)  # IR path → (block, line)
    blocks: dict[str, Block] = field(default_factory=dict)
    implements: dict[str, list[str]] = field(default_factory=dict)
    slices: list[str] = field(default_factory=list)  # slices the sheet may use (closure)

    @property
    def errors(self) -> list[SheetProblem]:
        return [p for p in self.problems if p.severity == "error"]

    def locate(self, path: str) -> tuple[str, int | None]:
        """Block key and sheet line of an IR path (longest known prefix)."""
        best = ""
        for prefix in self.source:
            if (
                path == prefix or path.startswith(prefix + ".") or path.startswith(prefix + "[")
            ) and len(prefix) > len(best):
                best = prefix
        if best:
            return self.source[best]
        head = re.split(r"[.\[]", path, maxsplit=1)[0]
        fallback = {
            "missions": "mermaid",
            "state_trees": "pytree",
            "subtrees": "pytree",
            "conditions": "conditions",
            "gaps": "gaps",
            "tests": "verification",
            "scenarios": "verification",
        }.get(head, "model")
        for key in self.blocks:
            if key.split(":")[0] == fallback:
                return key, None
        return "model", None

    def problem(self, d: Diagnostic) -> SheetProblem:
        block, line = self.locate(d.path)
        message = f"{d.code} {d.path}: {d.message}"
        if d.suggestion:
            message += f"（{d.suggestion}）"
        return SheetProblem(block, line, message, code=d.code, severity=d.severity.value)


def _key_lines(block: Block) -> dict[str, int]:
    """Sheet line of each ``key:`` in a YAML block (first occurrence, any depth)."""
    out: dict[str, int] = {}
    for i, text in enumerate(block.text.splitlines()):
        m = re.match(r"^\s*-?\s*['\"]?([A-Za-z_][\w.\[\]]*)['\"]?\s*:", text)
        if m and m.group(1) not in out:
            out[m.group(1)] = block.line + i
    return out


class SheetCompiler:
    def __init__(
        self,
        text: str,
        contracts: Contracts,
        *,
        model_id: str,
        roles_hint: dict[str, dict[str, Any]] | None = None,
        allow_mission_ref: bool = True,
        reference_policy: str = "all",  # "subtrees": state trees and missions are examples
        scenario_parameters: dict[str, dict[str, Any]] | None = None,
        pending_triggers: bool = False,
        slices: list[str] | None = None,
        scenario_events: dict[str, str] | None = None,
    ) -> None:
        self.slices_override = slices
        self.scenario_events = scenario_events or {}
        self.c = contracts
        # structure stage: a bare name in a transition is a condition to be written later
        self.pending_triggers = pending_triggers
        self.model_id = model_id
        self.roles_hint = roles_hint or {}
        self.reference_policy = reference_policy
        self.allow_mission_ref = allow_mission_ref and reference_policy == "all"
        self.scenario_parameters = scenario_parameters or {}
        blocks, problems = split_sheet(text)
        self.out = Compiled(None, list(problems))
        for b in blocks:
            if b.kind not in V3_KINDS:
                self.out.problems.append(
                    SheetProblem(
                        b.key, b.line, f"v3 战术稿不使用 {b.label} 块，已忽略", severity="warning"
                    )
                )
                continue
            if b.key in self.out.blocks:
                self.out.problems.append(
                    SheetProblem(b.key, b.line, f"{b.label} 块出现了两次，只用第一个")
                )
                continue
            self.out.blocks[b.key] = b

    # ------------------------------------------------------------------ helpers
    def fail(self, block: str, line: int | None, message: str, candidates: list[str] | None = None):
        self.out.problems.append(SheetProblem(block, line, message, candidates or []))

    def warn(self, block: str, line: int | None, message: str) -> None:
        self.out.problems.append(SheetProblem(block, line, message, severity="warning"))

    def at(self, path: str, block: str, line: int | None) -> None:
        if line is not None:
            self.out.source.setdefault(path, (block, line))

    def yaml_block(self, key: str) -> dict[str, Any]:
        block = self.out.blocks.get(key)
        if block is None:
            return {}
        try:
            data = yaml.safe_load(block.text) or {}
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            line = block.line + mark.line if mark is not None else block.line
            self.fail(key, line, f"YAML 语法错误：{getattr(exc, 'problem', exc)}")
            return {}
        if not isinstance(data, dict):
            self.fail(key, block.line, "块内容应是一个映射")
            return {}
        return data

    # ------------------------------------------------------------------ driver
    def compile(self) -> Compiled:
        if "model" not in self.out.blocks:
            self.fail("model", None, "缺少 yaml model 块")
            return self.out
        model = self.yaml_block("model")
        mlines = _key_lines(self.out.blocks["model"])
        spec: dict[str, Any] = {
            "schema_version": "2.0",
            "model": self._meta(model),
            "roles": [],
            "parameters": {},
            "blackboard": {},
            "conditions": [],
            "events": [],
            "subtrees": [],
            "state_trees": {},
            "missions": {},
        }
        self.mlines = mlines
        spec["parameters"] = self._parameters(model.get("parameters") or {})
        spec["blackboard"] = self._blackboard(model.get("blackboard") or {})
        spec["events"] = self._events(model.get("events") or {})
        spec["assumptions"] = self._assumptions(model.get("assumptions") or [])
        self.out.implements = self._implements(model.get("implements") or {})
        gaps = self.yaml_block("gaps")
        gaps = gaps.get("gaps", gaps) if isinstance(gaps.get("gaps"), dict) else gaps
        if gaps:
            spec["gaps"] = gaps
            glines = _key_lines(self.out.blocks["gaps"])
            for gid in gaps:
                self.at(f"gaps.{gid}", "gaps", glines.get(str(gid)))
        self.slices = self._slices(model)
        self.out.slices = list(self.slices)
        allowed = set(self.slices)
        fields = {
            p for p, f in self.c.blackboard.items() if f.slice in allowed and not internal_field(p)
        } | set(spec["blackboard"])
        cond_names = self._condition_names()
        self.expr = ExprCompiler(
            Symbols(
                fields,
                set(spec["parameters"]),
                cond_names,
                enums={p: f.enum for p, f in self.c.blackboard.items() if f.enum},
                elsewhere={
                    p: str(f.slice)
                    for p, f in self.c.blackboard.items()
                    if f.slice not in allowed and not internal_field(p)
                },
                internal={p for p in self.c.blackboard if internal_field(p)},
                free_strings={
                    p for p, f in self.c.blackboard.items() if f.type == "string" and not f.enum
                },
                # a declared string initial value ("" = not set) may be compared against
                string_initials={
                    p: f.initial
                    for p, f in self.c.blackboard.items()
                    if f.has_initial and isinstance(f.initial, str)
                },
            )
        )
        spec["conditions"] = self._conditions()
        roles = self._roles(model.get("roles") or {}, spec)
        spec["roles"] = roles
        event_ids = {e["id"] for e in spec["events"]}
        tree_symbols = TreeSymbols(
            actions=set(self.c.actions),
            tactics={
                t.id
                for t in self.c.tactics.values()
                if t.section == "subtrees"
                or (t.section == "state_trees" and self.reference_policy == "all")
            },
            examples={
                t.id
                for t in self.c.tactics.values()
                if t.section == "state_trees" and self.reference_policy != "all"
            },
            gaps={str(k): str(v.get("kind")) for k, v in gaps.items() if isinstance(v, dict)},
            parameters=set(spec["parameters"]),
            expr=self.expr,
        )
        self._trees(spec, tree_symbols, event_ids)
        verification = self.yaml_block("verification")
        if isinstance(verification.get("verification"), dict):
            verification = verification["verification"]
        vlines = (
            _key_lines(self.out.blocks["verification"]) if "verification" in self.out.blocks else {}
        )
        for key in ("tests", "scenarios"):
            items = verification.get(key) or []
            if not isinstance(items, list):
                self.fail("verification", vlines.get(key), f"{key} 应是列表")
                items = []
            spec[key] = items
            for i, item in enumerate(items):
                if isinstance(item, dict) and item.get("id"):
                    self.at(
                        f"{key}[{i}]", "verification", vlines.get(str(item["id"]), vlines.get(key))
                    )
        if not spec["missions"] and not self.out.errors:
            self.fail("model", None, "没有任何角色的任务状态机")
        used = {
            t["on"]
            for m in spec["missions"].values()
            if "tactic" not in m
            for t in m["transitions"] + m["global"]
        }
        unused = [e["id"] for e in spec["events"] if e["id"] not in used]
        if unused and not self.pending_triggers:
            # left out with a warning: a scenario event the model must use is checked next
            spec["events"] = [e for e in spec["events"] if e["id"] not in unused]
            for eid in unused:
                self.warn(
                    "model",
                    self.mlines.get(eid),
                    f"事件 {eid} 没有被任何转换使用，已忽略",
                )
        self._mission_ref_names(spec)
        self._condition_usage(spec)
        if not self.pending_triggers:
            self._writer_checks(spec)
            self._tree_event_checks(spec)
            self._instant_cycle_checks(spec)
        if self.scenario_events and not self.pending_triggers:
            external = {e["id"] for e in spec["events"] if e["source"] == "external"}
            for eid in sorted(external - set(self.scenario_events)):
                self.fail(
                    "model",
                    self.mlines.get(eid),
                    f"想定不会发出外部事件 {eid}；外部事件只能是 {', '.join(self.scenario_events)}",
                    list(self.scenario_events),
                )
            own = [m for m in spec["missions"].values() if "tactic" not in m]
            for eid in sorted(set(self.scenario_events) - external):
                if own:
                    self.fail(
                        "model",
                        self.mlines.get("events"),
                        f"想定会发出外部事件 {eid}（{self.scenario_events[eid]}），模型没有声明和使用它",
                    )
        self.out.spec = spec
        return self.out

    def _mission_ref_names(self, spec: dict[str, Any]) -> None:
        """A condition or event named like a trigger of a referenced tactic-set mission would
        replace that trigger (e.g. ``cancelled = False`` disables the safe shutdown)."""
        from afsim_research_agent.ir.v2.from_tactic import event_id

        own = {c["id"]: "conditions" for c in spec["conditions"]} | {
            e["id"]: "model" for e in spec["events"]
        }
        for mission in spec["missions"].values():
            entry = (
                self.c.tactics.get(str(mission.get("tactic", ""))) if "tactic" in mission else None
            )
            if entry is None:
                continue
            for t in entry.raw.get("transitions") or []:
                name = event_id(str(t.get("when", "")))
                block = own.pop(name, None)
                if block is None:
                    continue
                line = (
                    self.out.source.get(f"conditions.{name}", (block, None))[1]
                    if block == "conditions"
                    else self.mlines.get(name)
                )
                message = (
                    f"'{name}' 与引用的战术集任务 {entry.id} 的触发条件同名，会替换它；"
                    "引用任务时不要为它的转换写条件或事件"
                )
                if name in self._used_names(spec):
                    self.fail(block, line, message)
                    continue
                # unused by the model's own transitions and trees: drop it, the mission's
                # own trigger applies
                spec["conditions"] = [c for c in spec["conditions"] if c["id"] != name]
                spec["events"] = [e for e in spec["events"] if e["id"] != name]
                self.warn(block, line, message + "（未被使用，已删去）")

    def _used_names(self, spec: dict[str, Any]) -> set[str]:
        """Conditions and events the model's transitions and trees use (with references)."""
        used: set[str] = set()
        for m in spec["missions"].values():
            if "tactic" not in m:
                used |= {t["on"] for t in m["transitions"] + m["global"]}

        def walk(node: dict[str, Any]) -> None:
            if node.get("type") == "condition":
                used.add(str(node.get("condition")))
            for kid in node.get("children") or []:
                walk(kid)

        for sub in spec["subtrees"]:
            walk(sub["root"])
        exprs = {c["id"]: c["expression"] for c in spec["conditions"]}
        todo = list(used)
        while todo:
            name = todo.pop()
            for ref in _condition_refs(exprs.get(name)):
                if ref not in used:
                    used.add(ref)
                    todo.append(ref)
        return used

    def _condition_usage(self, spec: dict[str, Any]) -> None:
        """Problems of conditions nothing uses are warnings (the condition is left out);
        those of used conditions are errors. A transition naming an undefined condition is
        also filed under the conditions block, so the control repair can define it."""
        used = self._used_names(spec)
        for name, line, message, candidates in getattr(self, "cond_issues", []):
            if name in used or self.pending_triggers:
                self.fail("conditions", line, message, candidates)
            else:
                self.warn("conditions", line, message + "（该条件未被使用，已忽略）")
        for name, line in getattr(self, "missing_triggers", []):
            self.fail(
                "conditions",
                line,
                f"状态图使用的触发 '{name}' 没有定义：在这里写 {name} = 表达式"
                "（计时写 time_in_state() >= 2 * s），或在 model 的 events 中声明它",
            )

    # ------------------------------------------------------------------ writers (set 1.1)
    def _closure(
        self, ref: str | None, subs: dict[str, Any], gaps: dict[str, Any]
    ) -> tuple[set[str], set[str]]:
        """Actions / tactic steps a state's tree can run, and the fields its gaps write."""
        actions: set[str] = set()
        gap_writes: set[str] = set()
        seen: set[str] = set()

        def gap(name: str) -> None:
            spec = gaps.get(name.removeprefix("gap.")) or {}
            gap_writes.update((spec.get("blackboard") or {}).get("writes") or [])

        def walk(node: Any) -> None:
            if not isinstance(node, dict):
                return
            kind = node.get("type")
            if kind == "action" and node.get("action"):
                name = str(node["action"])
                gap(name) if name.startswith("gap.") else actions.add(name)
            elif kind == "subtree" and node.get("subtree"):
                visit(str(node["subtree"]))
            elif kind == "tactic" and node.get("tactic"):
                visit(str(node["tactic"]))
            for kid in [node.get("child"), *(node.get("children") or [])]:
                walk(kid)

        def visit(name: str) -> None:
            if name in seen:
                return
            seen.add(name)
            if name.startswith("gap."):
                gap(name)
            elif name in subs:
                walk(subs[name])
            elif name in self.c.tactics:
                walk(self.c.tactics[name].raw.get("root"))

        if ref:
            visit(ref)
        return actions, gap_writes

    def _writer_checks(self, spec: dict[str, Any]) -> None:
        """A condition may only read a field that something keeps up to date: a field the
        capability set says nothing writes is refused, and a transition (or a tree
        condition) of a state may only read fields that the state's tree writes — otherwise
        the field is frozen in that state and the condition never changes."""
        exprs = {c["id"]: c["expression"] for c in spec["conditions"]}
        paths = {c["id"]: f"conditions[{i}]" for i, c in enumerate(spec["conditions"])}
        used = self._used_names(spec)
        memo: dict[str, set[str]] = {}

        def fields(cid: str, stack: tuple[str, ...] = ()) -> set[str]:
            if cid in memo:
                return memo[cid]
            out: set[str] = set()
            for ref in _bb_reads(exprs.get(cid)):
                out.add(ref)
            for ref in _condition_refs(exprs.get(cid)):
                if ref not in stack:
                    out |= fields(ref, (*stack, cid))
            memo[cid] = out
            return out

        def label(cid: str) -> str:
            return "转换条件" if re.match(r".+_t\d+$", cid) else f"条件 {cid}"

        for cid in sorted(c for c in exprs if c in used):
            for path in sorted(fields(cid)):
                f = self.c.blackboard.get(path)
                if f is not None and f.unwritten:
                    block, line = self.out.locate(paths[cid])
                    self.fail(
                        block,
                        line,
                        f"{label(cid)}读取字段 {path}：能力集中没有任何动作写入它，不能用作条件"
                        + (f"（{f.note}）" if f.note else ""),
                    )
        subs = {s["id"]: s["root"] for s in spec["subtrees"]}
        gaps = spec.get("gaps") or {}
        reported: set[tuple[str, str, str]] = set()
        for rid, mission in spec["missions"].items():
            if "tactic" in mission:
                continue
            trees = spec["state_trees"].get(rid, {})
            for state in mission["states"]:
                if state in mission["terminal"]:
                    continue
                actions, gap_writes = self._closure(trees.get(state), subs, gaps)
                checks: list[tuple[str, str]] = []  # (condition id, IR path for the line)
                outgoing = [tr["on"] for tr in mission["transitions"] if tr["from"] == state]
                decided = _complementary(outgoing, exprs)
                for i, tr in enumerate(mission["transitions"]):
                    # a state that leaves on X or on not X decides at once on the values it
                    # entered with: reading fields its tree does not update is fine there
                    if tr["from"] == state and tr["on"] in exprs and tr["on"] not in decided:
                        checks.append((tr["on"], f"missions.{rid}.transitions[{i}]"))
                for cid in sorted(self._tree_conditions(trees.get(state), subs)):
                    if cid in exprs:
                        checks.append((cid, paths[cid]))
                for cid, where in checks:
                    for path in sorted(fields(cid)):
                        f = self.c.blackboard.get(path)
                        if (
                            f is None
                            or not f.written_by
                            or f.updated in STATIC_UPDATES
                            or set(f.written_by) & actions
                            or path in gap_writes
                            or (state, cid, path) in reported
                        ):
                            continue
                        reported.add((state, cid, path))
                        block, line = self.out.locate(where)
                        self.fail(
                            block,
                            line,
                            f"状态 {state} 的{label(cid)}读取 {path}，它只由 "
                            f"{', '.join(f.written_by)} 写入；{state} 的树中没有这些动作"
                            "（或含有它们的子树），该字段在此状态中不会变化。在树中加入相应动作"
                            "或子树，或改用本状态会更新的字段、计时；只做判断的状态可成对写出"
                            "互补的转换（X 与 not X），立即按进入时的值转出；或用树事件表达"
                            '"本状态的树完成"（events 中声明 {source: tree, result: success}）',
                        )

    def _instant_cycle_checks(self, spec: dict[str, Any]) -> None:
        """Transitions are checked again right after a state is entered, in the same
        update. A ring of states joined only by field conditions (no event, no time in state) keeps
        turning within one update once those fields hold — e.g. "assignments.count > 0"
        stays true after the first allocation. Refuse such a ring: at least one of its
        transitions must wait (a tree event, time in state or an external event). A ring
        whose conditions cannot all hold together (X out, not X back) cannot turn."""
        exprs = {c["id"]: c["expression"] for c in spec["conditions"]}

        def waits(cid: str, stack: tuple[str, ...] = ()) -> bool:
            if cid not in exprs:
                return True  # an event
            expr = exprs[cid]
            if _calls(expr, "time_in_state_gte"):
                return True
            return any(waits(r, (*stack, cid)) for r in _condition_refs(expr) if r not in stack)

        for rid, mission in spec["missions"].items():
            if "tactic" in mission:
                continue
            graph: dict[str, list[tuple[str, int]]] = {}
            for i, tr in enumerate(mission["transitions"]):
                if tr["from"] != tr["to"] and not waits(tr["on"]):
                    graph.setdefault(tr["from"], []).append((tr["to"], i))
            for ring in _rings(graph):
                atoms = [
                    a
                    for _, i in ring
                    for a in _atoms(exprs.get(mission["transitions"][i]["on"]), exprs)
                ]
                if _contradict(atoms):
                    continue  # e.g. leave on "not seen", come back on "seen": cannot both hold
                states = " -> ".join([*(s for s, _ in ring), ring[0][0]])
                block, line = self.out.locate(f"missions.{rid}.transitions[{ring[0][1]}]")
                self.fail(
                    block,
                    line,
                    f"状态环 {states} 的转换全是字段条件：这些字段一旦同时成立（如分配完成后"
                    "一直为真的计数），状态会在同一次更新内不停打转。让环上至少一个转换等待："
                    "用树事件（本状态的树完成，events 中声明 {source: tree, result: success}）、"
                    "计时（time_in_state() >= …）或外部事件；或把整条流水线放在一个状态的树里",
                )

    # ------------------------------------------------------------------ termination
    def _outcomes(self, node: Any, subs: dict[str, Any], seen: frozenset[str] = frozenset()):
        """Results a tree can end with ({"success", "failure"}), from the contracts'
        result semantics (an action that runs until stopped never succeeds)."""
        both = {"success", "failure"}
        if not isinstance(node, dict):
            return both
        kind = node.get("type")
        kids = [k for k in [node.get("child"), *(node.get("children") or [])] if k]
        if kind == "action":
            name = str(node.get("action") or "")
            a = self.c.actions.get(name)
            if a is None:
                return both
            if a.persistent:
                return {"failure"}
            return both
        if kind in ("subtree", "tactic"):
            ref = str(node.get("subtree") or node.get("tactic") or "")
            if ref in seen:
                return both
            if ref in subs:
                return self._outcomes(subs[ref], subs, seen | {ref})
            entry = self.c.tactics.get(ref)
            if entry is None:
                return both
            if entry.raw.get("persistent") is True:
                return {"failure"}
            return self._outcomes(entry.raw.get("root"), subs, seen | {ref})
        outs = [self._outcomes(k, subs, seen) for k in kids]
        if kind in ("sequence", "sequence_with_memory"):
            res = set()
            if all("success" in o for o in outs):
                res.add("success")
            if any("failure" in o for o in outs):
                res.add("failure")
            return res
        if kind in ("selector", "selector_with_memory"):
            res = set()
            if any("success" in o for o in outs):
                res.add("success")
            if all("failure" in o for o in outs):
                res.add("failure")
            return res
        if kind == "parallel":
            policy = node.get("policy") or {"type": node.get("success_policy") or "one"}
            ptype = policy.get("type") if isinstance(policy, dict) else str(policy)
            need = {"one": 1, "all": len(outs)}.get(str(ptype), int(policy.get("count", 1) or 1))
            res = set()
            if sum("success" in o for o in outs) >= need:
                res.add("success")
            if any(o for o in outs):
                res.add("failure")
            return res
        if kind == "failure_as_running":
            return {"success"} & (outs[0] if outs else both)
        if kind == "inverter":
            o = outs[0] if outs else both
            return {{"success": "failure", "failure": "success"}[x] for x in o}
        if kind == "succeeder":
            return {"success"}
        if kind == "negator":
            return {"failure"}
        if kind == "repeat":
            o = outs[0] if outs else both
            rep = node.get("repeat") or {}
            if rep.get("duration_s") or node.get("duration_s"):
                return {"success"}
            return {"success"} if o else set()
        return both

    def _tree_event_checks(self, spec: dict[str, Any]) -> None:
        """A tree event fires when the state's tree ends with that result: refuse one whose
        tree can never end that way (e.g. a tree that keeps running, like holding position)."""
        tree_events = {
            e["id"]: e.get("result", "success") for e in spec["events"] if e["source"] == "tree"
        }
        if not tree_events:
            return
        subs = {s["id"]: s["root"] for s in spec["subtrees"]}
        for rid, mission in spec["missions"].items():
            if "tactic" in mission:
                continue
            trees = spec["state_trees"].get(rid, {})
            for i, tr in enumerate(mission["transitions"]):
                result = tree_events.get(tr["on"])
                if result is None:
                    continue
                ref = trees.get(tr["from"])
                root = subs.get(ref) if ref in subs else {"type": "tactic", "tactic": ref}
                if ref is None or result in self._outcomes(root, subs):
                    continue
                word = "成功" if result == "success" else "失败"
                block, line = self.out.locate(f"missions.{rid}.transitions[{i}]")
                self.fail(
                    block,
                    line,
                    f"树事件 {tr['on']}（本状态的树{word}时触发）永远不会在 {tr['from']} 触发："
                    f"该状态的树不会{word}（含一直运行、不会自己结束的动作或子树）。"
                    "改用会变化的字段条件或计时事件，或让树在工作完成时结束",
                )

    def _tree_conditions(self, ref: str | None, subs: dict[str, Any]) -> set[str]:
        """Condition ids used inside the model's own trees reachable from a state tree."""
        found: set[str] = set()
        seen: set[str] = set()

        def walk(node: Any) -> None:
            if not isinstance(node, dict):
                return
            if node.get("type") == "condition" and node.get("condition"):
                found.add(str(node["condition"]))
            if node.get("type") == "subtree" and node.get("subtree") in subs:
                visit(str(node["subtree"]))
            for kid in [node.get("child"), *(node.get("children") or [])]:
                walk(kid)

        def visit(name: str) -> None:
            if name not in seen:
                seen.add(name)
                walk(subs[name])

        if ref in subs:
            visit(ref)
        return found

    def _slices(self, model: dict[str, Any]) -> list[str]:
        known = K.slices(self.c)
        wanted = self.slices_override
        if wanted is None:
            raw = model.get("slices")
            wanted = [str(x) for x in raw] if isinstance(raw, list) else None
        if wanted is None:
            return known
        for sid in wanted:
            if sid not in known:
                self.fail("model", self.mlines.get("slices"), f"'{sid}' 不是能力集的切片", known)
        chosen = [sid for sid in wanted if sid in known]
        for role in (model.get("roles") or {}).values():
            mission = role.get("mission") if isinstance(role, dict) else None
            ref = mission.get("tactic") if isinstance(mission, dict) else mission
            entry = self.c.tactics.get(str(ref or ""))
            if entry is not None and entry.slice not in chosen:
                chosen.append(entry.slice)
        return K.closure(self.c, chosen)

    # ------------------------------------------------------------------ model block
    def _meta(self, model: dict[str, Any]) -> dict[str, Any]:
        meta = model.get("model") or {}
        if not isinstance(meta, dict):
            meta = {}
        return {
            "id": self.model_id,
            "display_name": str(meta.get("display_name") or self.model_id),
            "version": "0.1.0",
            "intent": str(meta.get("intent") or meta.get("display_name") or self.model_id),
            "description": str(meta.get("description") or ""),
        }

    def _parameters(self, raw: Any) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if not isinstance(raw, dict):
            self.fail(
                "model", self.mlines.get("parameters"), "parameters 应是 名称: {value: …} 的映射"
            )
            return out
        for name, spec in raw.items():
            name = str(name)
            line = self.mlines.get(name)
            self.at(f"parameters.{name}", "model", line)
            if not IDENT.match(name):
                self.fail("model", line, f"参数名 '{name}' 应是英文蛇形 ID")
                continue
            if not isinstance(spec, dict):
                spec = {"value": spec}
            value = spec.get("value")
            ptype = spec.get("type")
            unit = spec.get("unit")
            q = quantity(value) if isinstance(value, str) else None
            if q is not None:
                value, unit = q["value"], q["unit"]
                ptype = ptype or DIM_TYPES.get(infer_dimension(unit) or "", None)
            elif ptype is None:
                if isinstance(value, bool):
                    ptype = "boolean"
                elif isinstance(value, int):
                    ptype = "integer"
                elif isinstance(value, float):
                    ptype = "ratio"
                else:
                    ptype = "string"
            param: dict[str, Any] = {"type": ptype, "value": value}
            if unit:
                param["unit"] = unit
            if spec.get("description"):
                param["description"] = str(spec["description"])
            basis = spec.get("basis")
            if basis is not None:
                status = basis if isinstance(basis, str) else (basis or {}).get("status")
                if status not in BASIS:
                    self.fail("model", line, f"参数 {name} 的 basis 应是 {sorted(BASIS)} 之一")
                else:
                    param["basis"] = {
                        "status": status,
                        "evidence": list(spec.get("evidence") or []),
                        "rationale": spec.get("rationale"),
                    }
            out[name] = param
        for name, sp in self.scenario_parameters.items():
            if name not in out:
                self.warn(
                    "model",
                    self.mlines.get("parameters"),
                    f"想定参数 {name} 未在 parameters 中定义，已补上",
                )
                out[name] = {
                    "type": sp.get("type", "string"),
                    "value": sp.get("placeholder", name),
                    "description": sp.get("description", "由想定提供"),
                }
        return out

    def _blackboard(self, raw: Any) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if not isinstance(raw, dict):
            return out
        for path, spec in raw.items():
            path = str(path)
            self.at(f"blackboard.{path}", "model", self.mlines.get(path))
            if not isinstance(spec, dict):
                spec = {"type": spec}
            decl = {k: spec[k] for k in ("type", "default", "unit", "description") if k in spec}
            out[path] = decl
        return out

    def _events(self, raw: Any) -> list[dict[str, Any]]:
        out = []
        if isinstance(raw, list):
            raw = {str(e.get("id")): e for e in raw if isinstance(e, dict)}
        if not isinstance(raw, dict):
            return out
        for i, (eid, spec) in enumerate(raw.items()):
            eid = str(eid)
            spec = spec if isinstance(spec, dict) else {}
            line = self.mlines.get(eid)
            self.at(f"events[{i}]", "model", line)
            event: dict[str, Any] = {"id": eid}
            after = spec.get("after", spec.get("after_s"))
            source = spec.get("source") or ("timer" if after is not None else "external")
            event["source"] = source
            if spec.get("description"):
                event["description"] = str(spec["description"])
            if after is not None:
                q = quantity(after) if isinstance(after, str) else None
                if q is not None:
                    from afsim_research_agent.ir.units import to_canonical

                    after = to_canonical(float(q["value"]), q["unit"], "time")
                if not isinstance(after, int | float):
                    self.fail("model", line, f"事件 {eid} 的 after 写成时长，如 2 s")
                else:
                    event["after_s"] = float(after)
            if source == "tree":
                result = spec.get("result", "success")
                if result not in ("success", "failure"):
                    self.fail(
                        "model", line, f"事件 {eid}：tree 事件的 result 写 success 或 failure"
                    )
                    continue
                event["result"] = result
            elif source not in ("external", "timer"):
                self.fail(
                    "model",
                    line,
                    f"事件 {eid}：模型只写 external、timer 或 tree 事件（战术集任务的事件自动带入）",
                )
                continue
            out.append(event)
        return out

    def _assumptions(self, raw: Any) -> list[dict[str, Any]]:
        out = []
        for i, item in enumerate(raw if isinstance(raw, list) else []):
            if isinstance(item, str):
                item = {"statement": item}
            if not isinstance(item, dict) or not item.get("statement"):
                continue
            out.append(
                {
                    "id": str(item.get("id") or f"as_{i + 1:02d}"),
                    "statement": str(item["statement"]),
                    "requires_confirmation": bool(item.get("requires_confirmation", True)),
                }
            )
        return out

    def _implements(self, raw: Any) -> dict[str, list[str]]:
        if not isinstance(raw, dict):
            return {}
        return {
            str(k): [str(x) for x in (v if isinstance(v, list) else [v])] for k, v in raw.items()
        }

    # ------------------------------------------------------------------ conditions
    def _condition_names(self) -> set[str]:
        block = self.out.blocks.get("conditions")
        if block is None:
            return set()
        try:
            module = ast.parse(block.text)
        except SyntaxError:
            return set()
        return {
            t.id
            for s in module.body
            if isinstance(s, ast.Assign)
            for t in s.targets
            if isinstance(t, ast.Name)
        }

    def _conditions(self) -> list[dict[str, Any]]:
        block = self.out.blocks.get("conditions")
        if block is None:
            return []
        self.cond_issues: list[tuple[str, int, str, list[str]]] = []
        try:
            module = ast.parse(block.text)
        except SyntaxError as exc:
            self.fail(
                "conditions", block.line + (exc.lineno or 1) - 1, f"Python 语法错误：{exc.msg}"
            )
            return []
        out = []
        for stmt in module.body:
            line = block.line + stmt.lineno - 1
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
                continue
            if (
                not isinstance(stmt, ast.Assign)
                or len(stmt.targets) != 1
                or not isinstance(stmt.targets[0], ast.Name)
            ):
                self.fail("conditions", line, "每行写成 条件名 = 表达式")
                continue
            name = stmt.targets[0].id
            if not IDENT.match(name):
                self.fail("conditions", line, f"条件名 '{name}' 应是英文蛇形 ID")
                continue
            try:
                expr = self.expr.compile_node(stmt.value)
            except ExprError as exc:
                self.cond_issues.append(
                    (name, line + exc.line_offset, f"{name}: {exc}", exc.candidates)
                )
                continue
            if _refers(expr, name):
                self.cond_issues.append((name, line, f"条件 {name} 引用了它自己", []))
                continue
            if "literal" in expr:
                self.cond_issues.append(
                    (
                        name,
                        line,
                        f"条件 {name} 是常量；外部请求写成 external 事件，"
                        '"本状态的树完成"写成 tree 事件（{source: tree, result: success}），'
                        "等一段时间写成 timer 事件，不需要的条件删去",
                        [],
                    )
                )
                continue
            self.at(f"conditions[{len(out)}]", "conditions", line)
            self.at(f"conditions.{name}", "conditions", line)
            out.append({"id": name, "expression": expr})
        return out

    # ------------------------------------------------------------------ roles & missions
    def _roles(self, raw: Any, spec: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        if not isinstance(raw, dict) or not raw:
            self.fail(
                "model", self.mlines.get("roles"), "roles 应是 角色名: {capability_class: …} 的映射"
            )
            return out
        for rid, info in raw.items():
            rid = str(rid)
            line = self.mlines.get(rid)
            info = info if isinstance(info, dict) else {}
            hint = self.roles_hint.get(rid, {})
            role = {
                "id": rid,
                "capability_class": info.get("capability_class")
                or hint.get("capability_class")
                or "ground_vehicle",
                "count": int(info.get("count") or 1),
                "description": str(info.get("description") or hint.get("description") or ""),
            }
            if info.get("superior") or info.get("subordinates"):
                role["command"] = {
                    "superior": info.get("superior"),
                    "subordinates": list(info.get("subordinates") or []),
                }
            out.append(role)
            self.at(f"roles.{rid}", "model", line)
            mission = info.get("mission")
            if mission is not None:
                if not self.allow_mission_ref:
                    self.fail(
                        "model",
                        line,
                        f"本次生成不允许整体引用战术集任务（角色 {rid}）；请用 mermaid 写任务状态机，"
                        "状态树可以引用战术集的子树和状态树",
                    )
                    continue
                if isinstance(mission, str):
                    mission = {"tactic": mission}
                ref = {"tactic": str(mission.get("tactic") or "")}
                if mission.get("role"):
                    ref["role"] = str(mission["role"])
                if mission.get("params"):
                    ref["params"] = dict(mission["params"])
                spec["missions"][rid] = ref
                self.at(f"missions.{rid}", "model", line)
                if f"mermaid:{rid}" in self.out.blocks:
                    self.warn(f"mermaid:{rid}", None, f"角色 {rid} 引用了战术集任务，状态图被忽略")
                continue
            block = self.out.blocks.get(f"mermaid:{rid}")
            if block is None:
                self.fail(
                    f"mermaid:{rid}",
                    None,
                    f"角色 {rid} 缺少 mermaid 状态图（或在 model 中引用战术集任务）",
                )
                continue
            spec["missions"][rid] = self._mission(rid, block, spec)
        for key in self.out.blocks:
            if key.startswith("mermaid:") and key.split(":", 1)[1] not in raw:
                self.warn(key, None, f"状态图的角色 {key.split(':', 1)[1]} 不在 roles 中")
        return out

    def _mission(self, rid: str, block: Block, spec: dict[str, Any]) -> dict[str, Any]:
        key = block.key
        diagram, problems = parse_diagram(block.text, rid, key, block.line)
        self.out.problems.extend(problems)
        wild = {(w.target, w.label, w.line) for w in diagram.wildcards}
        own = [t for t in diagram.transitions if (t.target, t.label, t.line) not in wild]
        rename = {s: s.upper() for s in diagram.states}
        for s in diagram.states:
            if not STATE.match(rename[s]):
                self.fail(key, diagram.lines.get(s), f"状态名 '{s}' 应是大写英文 ID")
        self.state_lines = getattr(self, "state_lines", {})
        for s in diagram.states:
            self.state_lines[(rid, rename[s])] = (key, diagram.lines.get(s, block.line))
        mission: dict[str, Any] = {
            "initial_state": rename.get(diagram.initial or "", diagram.initial or ""),
            "states": [rename[s] for s in diagram.states],
            "terminal": sorted(rename[s] for s in diagram.terminal),
            "transitions": [],
            "global": [],
        }
        self.at(f"missions.{rid}", key, block.line)
        events = {e["id"] for e in spec["events"]}
        for t in own:
            index = len(mission["transitions"])
            on = self._trigger(rid, t.label, key, t.line, spec, events)
            if on is None:
                continue
            self.at(f"missions.{rid}.transitions[{index}]", key, t.line)
            mission["transitions"].append(
                {"from": rename[t.source], "to": rename[t.target], "on": on}
            )
        for w in diagram.wildcards:
            index = len(mission["global"])
            on = self._trigger(rid, w.label, key, w.line, spec, events)
            if on is None:
                continue
            self.at(f"missions.{rid}.global[{index}]", key, w.line)
            mission["global"].append(
                {"to": rename[w.target], "on": on, "priority": GLOBAL_PRIORITY}
            )
        return mission

    def _trigger(
        self, rid: str, label: str, key: str, line: int, spec: dict[str, Any], events: set[str]
    ) -> str | None:
        label = label.strip()
        if label in events or label in {c["id"] for c in spec["conditions"]}:
            return label
        if self.pending_triggers and IDENT.match(label):
            return label
        try:
            expr = self.expr.compile(label)
        except ExprError as exc:
            if self.pending_triggers:
                return label
            if IDENT.match(label) and label not in self.c.blackboard:
                self.missing_triggers = getattr(self, "missing_triggers", [])
                self.missing_triggers.append((label, line))
            self.fail(
                key,
                line,
                f"转换条件 '{label}' 不是事件、条件名或可解析的表达式：{exc}",
                exc.candidates or sorted(events | {c["id"] for c in spec["conditions"]})[:8],
            )
            return None
        cid = f"{rid}_t{sum(1 for c in spec['conditions'] if c['id'].startswith(rid + '_t')) + 1}"
        self.at(f"conditions[{len(spec['conditions'])}]", key, line)
        spec["conditions"].append({"id": cid, "expression": expr})
        return cid

    # ------------------------------------------------------------------ trees
    def _role_states(self, rid: str, spec: dict[str, Any]) -> tuple[list[str], list[str]]:
        mission = spec["missions"].get(rid)
        if mission is None:
            return [], []
        if "tactic" in mission:
            entry = self.c.tactics.get(mission["tactic"])
            if entry is None or entry.section != "missions":
                return [], []
            raw = entry.raw
            return [str(s) for s in raw.get("states") or []], [
                str(s) for s in raw.get("terminal_states") or []
            ]
        return mission["states"], mission["terminal"]

    def _trees(self, spec: dict[str, Any], symbols: TreeSymbols, events: set[str]) -> None:
        names: dict[str, str] = {}  # function name → role that defines it
        parsed: dict[str, Any] = {}
        for key, block in self.out.blocks.items():
            if block.kind != "pytree":
                continue
            rid = block.role or ""
            if rid not in spec["missions"] and rid not in {r["id"] for r in spec["roles"]}:
                self.fail(key, block.line, f"树块的角色 '{rid}' 不在 roles 中")
                continue
            pb = parse_block(block.text, symbols, rid)
            for p in pb.problems:
                self.fail(key, block.line + p.line, p.message, p.candidates)
            parsed[rid] = (block, pb)
            for fn in pb.functions:
                if not STATE.match(fn.name):
                    if fn.name in names:
                        names[fn.name] = "*"  # defined by several roles → role-prefixed ids
                    else:
                        names[fn.name] = rid
        for rid, (block, pb) in parsed.items():
            key = block.key
            states, terminal = self._role_states(rid, spec)
            upper = {s.upper(): s for s in states}
            trees = spec["state_trees"].setdefault(rid, {})

            def sid(name: str, rid: str = rid) -> str:
                return name if names.get(name) == rid else f"{rid}_{name}"

            for cid, (expr, line) in pb.conditions.items():
                self.at(f"conditions[{len(spec['conditions'])}]", key, block.line + line)
                spec["conditions"].append({"id": cid, "expression": expr})
            for fn in pb.functions:
                line = block.line + fn.line
                if fn.root is None:
                    continue
                _rename_calls(fn.root, sid)
                if STATE.match(fn.name):
                    state = upper.get(fn.name)
                    if state is None:
                        self.fail(
                            key,
                            line,
                            f"函数 {fn.name} 不是角色 {rid} 的状态；状态树函数名与状态名相同，"
                            "子树函数用小写名",
                            states,
                        )
                        continue
                    if state in terminal:
                        self.warn(key, line, f"{state} 是结束状态，不执行树")
                        continue
                    ref = _direct_ref(fn.root)
                    if ref is None:
                        ref = f"{rid}_{state.lower()}"
                        self._add_subtree(spec, ref, fn.root, key, block.line, line)
                    trees[state] = ref
                    self.at(f"state_trees.{rid}.{state}", key, line)
                    self._map_nodes(f"state_trees.{rid}.{state}", fn.root, key, block.line)
                else:
                    if not IDENT.match(fn.name):
                        self.fail(key, line, f"子树函数名 '{fn.name}' 应是小写英文 ID")
                        continue
                    self._add_subtree(spec, sid(fn.name), fn.root, key, block.line, line)
        for rid, mission in spec["missions"].items():
            if "tactic" in mission:
                continue
            for state in mission["states"]:
                if state in mission["terminal"] or state in spec["state_trees"].get(rid, {}):
                    continue
                where = getattr(self, "state_lines", {}).get((rid, state))
                self.at(f"state_trees.{rid}.{state}", *(where or (f"pytree:{rid}", None)))
        _strip_lines(spec)

    def _add_subtree(
        self, spec: dict[str, Any], sid: str, root: dict[str, Any], key: str, first: int, line: int
    ) -> None:
        index = len(spec["subtrees"])
        spec["subtrees"].append({"id": sid, "root": root})
        self.at(f"subtrees[{index}]", key, line)
        self.at(f"subtrees.{sid}", key, line)
        self._map_nodes(f"subtrees[{index}].root", root, key, first)
        self._map_nodes(f"subtrees.{sid}.root", root, key, first)

    def _map_nodes(self, path: str, node: dict[str, Any], key: str, first: int) -> None:
        if "_line" in node:
            self.at(path, key, first + node["_line"])
        for i, kid in enumerate(node.get("children") or []):
            self._map_nodes(f"{path}.children[{i}]", kid, key, first)


def _bb_reads(expr: Any) -> set[str]:
    """Blackboard fields an expression reads (``bb`` operands and function ``field`` args)."""
    out: set[str] = set()
    if isinstance(expr, dict):
        for k, v in expr.items():
            if k == "bb" and isinstance(v, str):
                out.add(v)
            elif k == "field" and isinstance(v, str):
                out.add(v)
            else:
                out |= _bb_reads(v)
    elif isinstance(expr, list):
        for v in expr:
            out |= _bb_reads(v)
    return out


def _condition_refs(expr: Any) -> list[str]:
    if isinstance(expr, dict):
        out = [str(expr["condition_ref"])] if "condition_ref" in expr else []
        for v in expr.values():
            out += _condition_refs(v)
        return out
    if isinstance(expr, list):
        return [r for v in expr for r in _condition_refs(v)]
    return []


def _refers(expr: Any, name: str) -> bool:
    if isinstance(expr, dict):
        if expr.get("condition_ref") == name:
            return True
        return any(_refers(v, name) for v in expr.values())
    if isinstance(expr, list):
        return any(_refers(v, name) for v in expr)
    return False


def _direct_ref(root: dict[str, Any]) -> str | None:
    """A state function that only calls one subtree or tactic-set tree maps to it directly."""
    if root.get("type") == "tactic" and not root.get("params"):
        return str(root["tactic"])
    if root.get("type") == "subtree" and not str(root.get("subtree", "")).startswith("gap."):
        return str(root["subtree"])
    return None


def _rename_calls(node: dict[str, Any], sid) -> None:
    if node.get("type") == "subtree" and not str(node.get("subtree", "")).startswith("gap."):
        node["subtree"] = sid(node["subtree"])
    for kid in node.get("children") or []:
        _rename_calls(kid, sid)


def _strip_lines(spec: dict[str, Any]) -> None:
    def strip(node: dict[str, Any]) -> None:
        node.pop("_line", None)
        if not node.get("params"):
            node.pop("params", None)
        for kid in node.get("children") or []:
            strip(kid)

    for sub in spec["subtrees"]:
        strip(sub["root"])


def compile_sheet(text: str, contracts: Contracts, **kw: Any) -> Compiled:
    return SheetCompiler(text, contracts, **kw).compile()


def _complementary(cids: list[str], exprs: dict[str, Any]) -> set[str]:
    """Transition conditions that come in X / not X pairs among one state's transitions."""
    out: set[str] = set()
    for a in cids:
        for b in cids:
            if a == b or a not in exprs or b not in exprs:
                continue
            if exprs[b] in ({"not": exprs[a]}, {"not": {"condition_ref": a}}):
                out |= {a, b}
    return out


def _calls(expr: Any, name: str) -> bool:
    """Whether the expression must call the named function (not under `not` or `any`)."""
    if isinstance(expr, dict):
        call = expr.get("function_call")
        if isinstance(call, dict) and call.get("name") == name:
            return True
        return any(_calls(v, name) for k, v in expr.items() if k not in ("not", "any"))
    if isinstance(expr, list):
        return any(_calls(v, name) for v in expr)
    return False


def _rings(graph: dict[str, list[tuple[str, int]]]) -> list[list[tuple[str, int]]]:
    """One cycle per strongly connected group of states: [(state, transition index), ...]
    where the transition leaves that state for the next one in the ring."""
    rings: list[list[tuple[str, int]]] = []
    done: set[str] = set()
    for start in sorted(graph):
        if start in done:
            continue
        # depth-first walk from start; the first back edge to a state on the path is a ring
        path: list[tuple[str, int]] = []
        on_path: dict[str, int] = {}
        seen: set[str] = set()

        def walk(state: str) -> list[tuple[str, int]] | None:
            seen.add(state)
            on_path[state] = len(path)
            for nxt, i in graph.get(state, []):
                path.append((state, i))
                if nxt in on_path:
                    return path[on_path[nxt] :]
                if nxt not in seen and nxt not in done:
                    found = walk(nxt)
                    if found:
                        return found
                path.pop()
            del on_path[state]
            return None

        ring = walk(start)
        done |= seen
        if ring:
            rings.append(list(ring))
    return rings


_FLIP = {"lt": "gte", "lte": "gt", "gt": "lte", "gte": "lt", "eq": "ne", "ne": "eq"}


def _atoms(
    expr: Any, exprs: dict[str, Any], neg: bool = False, stack: tuple[str, ...] = ()
) -> list[tuple[str, str, Any]]:
    """Facts an expression requires (conjunctive positions only): (field, operator, value)."""
    if not isinstance(expr, dict):
        return []
    if "condition_ref" in expr:
        ref = str(expr["condition_ref"])
        return [] if ref in stack else _atoms(exprs.get(ref), exprs, neg, (*stack, ref))
    if "not" in expr:
        return _atoms(expr["not"], exprs, not neg, stack)
    if ("all" in expr and not neg) or ("any" in expr and neg):
        parts = expr.get("all") if "all" in expr else expr.get("any")
        return [a for part in parts or [] for a in _atoms(part, exprs, neg, stack)]
    if "bb" in expr:
        return [(str(expr["bb"]), "eq", not neg)]
    cmp = expr.get("compare")
    if isinstance(cmp, dict) and isinstance(cmp.get("left"), dict) and "bb" in cmp["left"]:
        right = cmp.get("right")
        if isinstance(right, dict) and "literal" in right:
            value: Any = right["literal"]
        elif isinstance(right, dict) and "value" in right and "unit" in right:
            value = (right["value"], right["unit"])
        elif isinstance(right, dict) and "parameter_ref" in right:
            value = ("param", right["parameter_ref"])
        else:
            return []
        op = str(cmp.get("operator"))
        if op not in _FLIP:
            return []
        if neg:
            op = _FLIP[op]
        if isinstance(value, bool) and op in ("eq", "ne"):
            return [(str(cmp["left"]["bb"]), "eq", value if op == "eq" else not value)]
        return [(str(cmp["left"]["bb"]), op, value)]
    return []


def _contradict(atoms: list[tuple[str, str, Any]]) -> bool:
    """Whether some two of the facts cannot hold together."""
    for i, (fa, oa, va) in enumerate(atoms):
        for fb, ob, vb in atoms[i + 1 :]:
            if fa != fb:
                continue
            if va == vb and _FLIP.get(oa) == ob:
                return True
            if va == vb and {oa, ob} in ({"eq", "lt"}, {"eq", "gt"}, {"lt", "gt"}):
                return True
            if oa == ob == "eq" and va != vb:
                return True
            a, b = _number(va), _number(vb)
            if a is None or b is None or a[1] != b[1]:
                continue
            if not _overlap(oa, a[0], ob, b[0]):
                return True
    return False


def _number(v: Any) -> tuple[float, str] | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int | float):
        return float(v), ""
    if isinstance(v, tuple) and len(v) == 2 and isinstance(v[0], int | float) and v[0] != "param":
        return float(v[0]), str(v[1])
    return None


def _overlap(oa: str, a: float, ob: str, b: float) -> bool:
    """Whether x (oa a) and x (ob b) can both hold for some number x."""

    def bounds(op: str, v: float) -> tuple[float, bool, float, bool] | None:
        inf = float("inf")
        return {
            "lt": (-inf, False, v, False),
            "lte": (-inf, False, v, True),
            "gt": (v, False, inf, False),
            "gte": (v, True, inf, False),
            "eq": (v, True, v, True),
        }.get(op)

    x, y = bounds(oa, a), bounds(ob, b)
    if x is None or y is None:
        return True  # "ne" excludes a single point only
    lo, lo_in = (x[0], x[1]) if (x[0], not x[1]) > (y[0], not y[1]) else (y[0], y[1])
    hi, hi_in = (x[2], x[3]) if (x[2], x[3]) < (y[2], y[3]) else (y[2], y[3])
    return lo < hi or (lo == hi and lo_in and hi_in)
