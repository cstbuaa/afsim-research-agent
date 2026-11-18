"""``falcon draft3``: requirement → tactic sheet v3 → IR 2.0 model package.

Stages (design falcon-v3-composition-and-gaps §6, §9):

1. requirement clauses (the shared F2 step);
2. structure: ``yaml model`` + one ``mermaid`` per role, from the capability set's slice index;
3. control: conditions, trees and gaps, from the slices the structure named;
4. L1 (IR 2.0 normalization) → block repairs, at most ``max_repair`` rounds;
5. verification: L2 tests and L3 scenarios, run on the internal model; failures go back to
   the verification block once;
6. the package: ``behavior_spec.yaml`` (IR 2.0), ``gaps.yaml``, the sheet, a source map,
   the validation report, a review page and the call trace.

Everything is written to a staging directory and renamed into place at the end; an
existing package directory is never overwritten.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from afsim_research_agent.falcon.errors import FalconError
from afsim_research_agent.falcon.llm import FalconModel, ModelRunner
from afsim_research_agent.falcon.models import FalconRequest, PlatformHint, TextAnswer
from afsim_research_agent.falcon.sheet.blocks import Block, SheetProblem, join_sheet
from afsim_research_agent.falcon.sheet.generate import merge
from afsim_research_agent.falcon.v3 import catalog as K
from afsim_research_agent.falcon.v3 import prompts as P
from afsim_research_agent.falcon.v3 import triage
from afsim_research_agent.falcon.v3.afsim_loop import (
    AfsimConfig,
    AfsimHistory,
    AfsimNote,
    AfsimRound,
    feedback,
    note_entry,
    run_once,
    sheet_hash,
)
from afsim_research_agent.falcon.v3.compare import compare as compare_tactics
from afsim_research_agent.falcon.v3.compare import render as render_comparison
from afsim_research_agent.falcon.v3.compiler import Compiled, compile_sheet
from afsim_research_agent.falcon.v3.fixer import fix_sheet, render_fix
from afsim_research_agent.ir.diagnostics import Diagnostic
from afsim_research_agent.ir.v2 import Contracts, normalize
from afsim_research_agent.ir.v2.obligations import Obligation, event_actions, state_actions
from afsim_research_agent.ir.v2.obligations import check as check_obligations
from afsim_research_agent.ir.v2.obligations import render as render_obligations
from afsim_research_agent.ir.v2.scenario_interface import scenario_tags
from afsim_research_agent.ir.v2.sim import run_l2, simulate
from afsim_research_agent.progress import report

FALCON_V3_VERSION = "falcon-3.0.0"
SHEET_FILE = "tactic_sheet.md"
STRUCTURE_KINDS = ("model", "mermaid")
CONTROL_KINDS = ("conditions", "pytree", "gaps")
GUIDES = {
    "model": P.MODEL_GUIDE,
    "mermaid": P.MODEL_GUIDE,
    "conditions": P.CONTROL_GUIDE,
    "pytree": P.CONTROL_GUIDE,
    "gaps": P.CONTROL_GUIDE,
    "verification": P.VERIFICATION_GUIDE,
}


class RoleHint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capability_class: str = "ground_vehicle"
    count: int = 1
    description: str = ""


class ScenarioParameter(BaseModel):
    """A parameter whose value the scenario supplies (e.g. a route name). Only the name,
    type and description reach the model; the value is set in the local run configuration."""

    model_config = ConfigDict(extra="forbid")
    type: str = "string"
    description: str = ""


class V3Request(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirement: str = ""  # may be empty when tactic_selection names the input
    model_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    display_name: str | None = None
    roles: dict[str, RoleHint] = Field(default_factory=dict)
    scenario_parameters: dict[str, ScenarioParameter] = Field(default_factory=dict)
    allow_mission_ref: bool = False
    # "subtrees": tactic-set subtrees may be referenced; state trees and missions are shown as
    # examples the model learns from and writes its own; "all": everything may be referenced
    reference_policy: Literal["subtrees", "all"] = "subtrees"
    # state names to use per role (e.g. to line up with an evaluation scenario)
    state_names: dict[str, list[str]] = Field(default_factory=dict)
    # "coarse": one state per phase (wait / work / decide / hold / shut down), phase exits by
    # tree events; "fine": the drafter may split a phase into a state per step
    granularity: Literal["fine", "coarse"] = "fine"
    # skeleton: choose a verified skeleton (falcon.v3.skeleton) and let the program write the
    # sheet; free: the model writes roles, state diagrams, events and parameters itself
    drafting: Literal["free", "skeleton"] = "free"
    # external events the scenario raises: id → description (names line up with the run)
    scenario_events: dict[str, str] = Field(default_factory=dict)
    slices: list[str] | None = None  # override the slices the structure names
    capability_set: Path | None = None  # default: the configured capability set
    max_repair: int = Field(default=3, ge=0, le=6)
    fix_steps: int = Field(default=20, ge=0, le=60)  # self-checking repair loop budget
    # requirement obligations checked on every run (None: extracted from the requirement)
    obligations: list[dict[str, Any]] | None = None
    extract_obligations: bool = True
    # drafts made independently; the one that passes most checks is kept
    candidates: int = Field(default=1, ge=1, le=5)
    # Crow's tactic_selection.yaml (or the tactics directory holding it), relative to the
    # request file: the selected tactic becomes the input (falcon.v3.crow_bridge)
    tactic_selection: Path | None = None
    role_map: dict[str, str] = Field(default_factory=dict)  # Crow role text -> role id
    # tactic elements whose capability the set lacks (Crow's annotation): "skip" leaves them
    # out of this model (deferred to capability development); "wait" keeps them as gaps,
    # and the model waits for the capability-development agent (no AFSIM run until then)
    # (default "block"): a core element (step, formation) the capability set cannot carry
    # stops the draft (status needs_capability) instead of being imitated
    missing_capabilities: Literal["block", "skip", "wait"] = "block"
    # a Crow tactic is first reduced to an executable version built from the existing
    # actions and subtrees ("simple"); the full tactic is kept for later. "full": draft the
    # tactic as Crow wrote it
    tactic_level: Literal["simple", "full"] = "simple"
    # the scenario interface (ir.v2.scenario_interface): the points, routes and events the
    # scenario supplies; derived from the requirement when not given
    scenario_interface: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _input(self) -> V3Request:
        if not self.requirement.strip() and self.tactic_selection is None:
            raise ValueError("requirement is required (or give tactic_selection)")
        return self

    @classmethod
    def load(cls, path: Path) -> V3Request:
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


class ObligationItem(BaseModel):
    id: str
    kind: str
    description: str = ""
    clause: str | None = None
    action: str | None = None
    after: str | None = None
    min_s: float | None = None
    first: str | None = None
    then: str | None = None
    on_event: str | None = None
    within_s: float | None = None
    roles: list[str] | None = None
    min_m: float | None = None
    tol_m: float | None = None
    duration_s: float | None = None
    speed_mps: float | None = None
    min_bounds: int | None = None


class ClauseMapItem(BaseModel):
    clause: str
    actions: list[str] = Field(default_factory=list)
    how: str = ""
    missing: str = ""


class MapAnswer(BaseModel):
    items: list[ClauseMapItem] = Field(default_factory=list)


class ObligationAnswer(BaseModel):
    obligations: list[ObligationItem] = Field(default_factory=list)


@dataclass
class Check:
    compiled: Compiled
    model: dict[str, Any] | None
    diagnostics: list[Diagnostic]
    problems: list[SheetProblem]  # errors to repair, with block and line
    l2: list[dict[str, Any]] = field(default_factory=list)
    l3: list[dict[str, Any]] = field(default_factory=list)
    # what the scenario binding cannot give the model (not the drafter's to repair)
    binding: list[str] = field(default_factory=list)
    # route / point parameters left to the platform kit's defaults (actions inside tactic-set
    # subtrees): not a binding problem, the 2D simulation cannot judge them, AFSIM decides
    kit_defaults: list[str] = field(default_factory=list)

    @property
    def l1_ok(self) -> bool:
        return self.model is not None and not self.problems

    @property
    def verified(self) -> bool:
        return (
            self.l1_ok
            and all(t["passed"] for t in self.l2)
            and all(s["passed"] for s in self.l3)
            and bool(self.l2 or self.l3)
        )


UNVERIFIED = "draft_unverified"


def coverage(drafter: Drafter, check: Check, history: Any = None) -> dict[str, dict[str, Any]]:
    """For each tactic clause (from the Crow selection): the obligations that check it and
    which of them a run judged (the 2D scenarios, the last AFSIM run). A clause no run
    judged is not verified, however the checks went: they only show that the model ran."""
    judged: dict[str, bool] = {}
    for s in check.l3:
        for v in s.get("obligations") or []:
            if v.get("judged", True):
                judged[str(v["id"])] = judged.get(str(v["id"]), True) and bool(v["passed"])
    if history is not None and history.rounds:
        for oid, ok in history.rounds[-1].judged.items():
            judged[oid] = judged.get(oid, True) and ok
    out: dict[str, dict[str, Any]] = {}
    for c in drafter.clauses:
        if c.get("origin") != "tactic":
            continue
        obs = [o for o in drafter.obligations if c["id"] in o.clauses()]
        hit = [o for o in obs if o.id in judged]
        out[c["id"]] = {
            "text": c.get("text", ""),
            "obligations": [o.id for o in obs],
            "judged": [o.id for o in hit],
            "effect": [o.id for o in hit if o.spatial],
            "verified": bool(hit) and all(judged[o.id] for o in hit),
        }
    return out


@dataclass
class V3Result:
    package_dir: Path
    status: str
    check: Check
    rounds: list[dict[str, Any]]

    @property
    def exit_code(self) -> int:
        return {"draft_valid": 0, UNVERIFIED: 4, "needs_capability": 5}.get(self.status, 3)


def _uncovered(
    o: Any,
    after: dict[str, dict[str, set[str]]],
    contracts: Any = None,
    model: Any = None,
    capable: dict[str, set[str]] | None = None,
) -> str:
    """The coverage note of an obligation no scenario judged. A response whose action no
    state after the event runs cannot be covered by a scenario: the response states must
    run it (or the obligation does not apply); the note names the states and the subtrees
    that contain the action, as a ready attach_subtree call (r172 / r179 only looked)."""
    head = f"  [FAIL] 需求约束 {o.id}（{o.description or o.kind}）"
    if o.kind == "response" and o.on_event and o.action and after:
        if not any(o.action in per.get(str(o.on_event), set()) for per in after.values()):
            text = (
                head
                + f"无法被想定覆盖：外部事件 {o.on_event} 之后进入的状态（及其后的状态）都不运行 "
                f"{o.action}；在响应这个事件的状态树里加入它（或含有它的战术）"
            )
            subs = sorted(
                (
                    tid
                    for tid, t in (getattr(contracts, "tactics", None) or {}).items()
                    if t.section == "subtrees" and f'"{o.action}"' in json.dumps(t.raw)
                ),
                # a shutdown / cease-fire subtree first, then the smallest (r172: the first
                # by name was a re-attack subtree)
                key=lambda tid: (
                    not any(w in tid for w in ("shutdown", "cease", "safe", "stop", "hold")),
                    len(json.dumps(contracts.tactics[tid].raw)),
                    tid,
                ),
            )
            family = str(o.action).split(".", 1)[0] + "."
            targets = [
                (role, str(t.get("to")))
                for role, r in ((model or {}).get("roles") or {}).items()
                for t in ((r.get("mission") or {}).get("transitions") or [])
                + ((r.get("mission") or {}).get("global") or [])
                if str(t.get("on")) == str(o.on_event)
            ]
            # the role that runs the action's slice (the members fire, not the leader)
            runs = {n.rsplit("_", 1)[0]: acts for n, acts in (capable or {}).items()}
            targets.sort(key=lambda rt: not any(a.startswith(family) for a in runs.get(rt[0], ())))
            if subs and targets:
                role, state = targets[0]
                text += (
                    f'，例如 attach_subtree {{"role": "{role}", "state": "{state}", '
                    f'"subtree": "{subs[0]}", "position": "start"}}'
                    + (f"（含 {o.action} 的子树：{', '.join(subs[:4])}）" if len(subs) > 1 else "")
                )
            return text
    return (
        head
        + "没有被任何想定覆盖：在 verification 中加一个会出现它的前提的想定"
        + (f"（外部事件 {o.on_event}）" if o.on_event else "")
    )


class Drafter:
    def __init__(self, request: V3Request, contracts: Contracts, run: ModelRunner) -> None:
        self.r = request
        self.c = contracts
        self.run = run
        self.blocks: list[Block] = []
        self.rounds: list[dict[str, Any]] = []
        self.clauses: list[dict[str, Any]] = []
        self.slices: list[str] = []
        self.notes: list[str] = []
        self.obligations: list[Obligation] = []
        self.afsim_note: AfsimNote | None = None  # the last AFSIM run's verdict and feedback
        # set while AFSIM is in the loop: run AFSIM on a model -> (passed, feedback, stop)
        self.afsim_probe: Any = None
        self.afsim_budget = 0  # AFSIM runs the repair loop may still start
        self.afsim_verdicts: dict[str, AfsimNote] = {}  # sheet hash -> AFSIM verdict
        self.tactic: Any = None  # the Crow selection (falcon.tactic.SelectedTactic)
        self.simplified: Any = None  # simplify.Simplified: the executable version's record
        self.afsim_history: Any = None  # AfsimHistory of the chosen draft (coverage)
        # tactic clause → {"actions", "how", "missing"} (map_stage) and the clauses the
        # capability set cannot carry (clause → missing capability)
        self.clause_map: dict[str, dict[str, Any]] = {}
        self.blocked: dict[str, str] = {}
        self.crow_caps: list[dict[str, Any]] = []  # Crow's capability map of that selection
        self.kit: dict[str, Any] | None = None  # the scenario kit (binding values), if any

    # ------------------------------------------------------------------ helpers
    @property
    def iface(self) -> Any:
        from afsim_research_agent.ir.v2.scenario_interface import ScenarioInterface

        if not self.r.scenario_interface:
            return None
        return ScenarioInterface.model_validate(self.r.scenario_interface)

    def binding(self, model: dict[str, Any]) -> Any:
        """The scenario binding of a normalized model (kit values, defaults otherwise)."""
        from afsim_research_agent.ir.v2.scenario_interface import binding_for

        return binding_for(model, self.kit, self.iface)

    def scenario_text(self, model: dict[str, Any] | None = None) -> str:
        iface = self.iface
        if iface is None:
            return ""
        text = iface.prompt_text()
        if model is not None:
            from afsim_research_agent.ir.v2.scenario_interface import summary

            text += "\n想定绑定（二维仿真的基准环境）：\n" + summary(self.binding(model))
        return text

    @property
    def sheet(self) -> str:
        return join_sheet(self.blocks, self.r.display_name or self.r.model_id)

    def compile(self, *, pending: bool = False) -> Compiled:
        return compile_sheet(
            self.sheet,
            self.c,
            model_id=self.r.model_id,
            roles_hint={k: v.model_dump() for k, v in self.r.roles.items()},
            allow_mission_ref=self.r.allow_mission_ref,
            reference_policy=self.r.reference_policy,
            scenario_parameters={k: v.model_dump() for k, v in self.r.scenario_parameters.items()},
            scenario_events=dict(self.r.scenario_events),
            scenario_interface=self.r.scenario_interface,
            pending_triggers=pending,
            slices=self.r.slices,
        )

    def check(self, *, pending: bool = False, verify: bool = False) -> Check:
        compiled = self.compile(pending=pending)
        problems = list(compiled.errors)
        model, diags = None, []
        if compiled.spec is not None and not pending:
            result = normalize(compiled.spec, self.c)
            diags = result.diagnostics
            model = result.model if result.ok else None
            problems += [compiled.problem(d) for d in diags if d.severity.value == "error"]
        if self.crow_caps and compiled.spec is not None:
            from afsim_research_agent.falcon.v3.crow_bridge import gap_problems

            gaps = compiled.spec.get("gaps") or {}
            for gid, message in gap_problems(
                gaps, self.crow_caps, mode=self.r.missing_capabilities
            ):
                block, line = compiled.locate(f"gaps.{gid}")
                problems.append(SheetProblem(block, line, message, code="CROW_GAP"))
        if compiled.spec is not None and not pending and not compiled.errors:
            from afsim_research_agent.falcon.v3 import semantics

            tactic = [c for c in self.clauses if c.get("origin") == "tactic"]
            for path, message, code in semantics.problems(
                compiled.spec,
                compiled.implements,
                tactic,
                self.clause_map,
                self.c.tactics,
                set(self.r.scenario_parameters) | set(scenario_tags(compiled.spec)),
            ):
                if code == "UNUSED_PARAM" and not tactic:
                    continue  # checked for drafts of a selected tactic (Crow) only
                block, line = compiled.locate(path)
                problems.append(SheetProblem(block, line, message, code=code))
            for path, message, code in semantics.role_problems(
                compiled.spec, self.c, self.c.tactics
            ):
                block, line = compiled.locate(path)
                problems.append(SheetProblem(block, line, message, code=code))
            for path, message, code in semantics.role_total_problems(
                self.r.requirement, compiled.spec
            ):
                block, line = compiled.locate(path)
                problems.append(SheetProblem(block, line, message, code=code))
            if self.iface is not None:
                for path, message, code in semantics.scenario_literals(compiled.spec):
                    block, line = compiled.locate(path)
                    problems.append(SheetProblem(block, line, message, code=code))
        out = Check(compiled, model, diags, problems)
        if model is not None:
            self._align_roles(model)
        if model is not None and self.iface is not None:
            from afsim_research_agent.ir.v2.scenario_interface import (
                ROUTING,
                check_binding,
                interface_param_issues,
            )

            out.binding = check_binding(model, self.binding(model))
            for issue in interface_param_issues(model):
                if issue.literal:
                    continue  # a written literal: SCN_LITERAL above, with its autofix
                if ROUTING[issue.route] == "model":
                    # the drafter wrote it: a model error, sent to the repair loop
                    block, line = compiled.locate(f"state_trees.{issue.role}.{issue.state}")
                    code = "SCN_MISSING" if issue.kind == "missing" else "SCN_NOT_INTERFACE"
                    out.problems.append(SheetProblem(block, line, issue.message(), code=code))
                else:
                    out.kit_defaults.append(issue.message())
        if verify and out.l1_ok and model is not None:
            spec = compiled.spec or {}
            out.l2 = [
                {"id": c.id, "passed": c.passed, "detail": c.detail}
                for c in run_l2(model, self.c, spec.get("tests") or [])
            ]
            covered: set[str] = set()
            unmodelled: set[str] = set()
            per_state = state_actions(spec, {k: t.raw for k, t in self.c.tactics.items()})
            per_event = event_actions(spec.get("missions") or {}, per_state)
            instances = [
                (f"{rid}_{i}", rid)
                for rid, role in model["roles"].items()
                for i in range(int(role["role"].get("count", 1)))
            ]
            capable = {
                name: set().union(*per_state.get(rid, {}).values()) if per_state.get(rid) else set()
                for name, rid in instances
            }
            after = {name: per_event.get(rid, {}) for name, rid in instances}
            # "advances" is judged on the 2D positions too (the binding gives both runs the
            # same geometry): met when some scenario shows it
            advances = [o for o in self.obligations if o.kind == "advances"]
            advanced: dict[str, str] = {}
            for raw in [*(spec.get("scenarios") or []), *self._auto_scenarios(spec, model)]:
                res = simulate(model, self.c, raw, self.binding(model))
                if advances and res.tracks and self.iface is not None:
                    from afsim_research_agent.ir.v2.effects import judge

                    own = dict(instances)
                    for v in judge(advances, res.tracks, own, res.activations, res.events):
                        if v.judged and (v.passed or v.id not in advanced):
                            advanced[v.id] = ("ok " if v.passed else "") + v.detail
                verdicts = check_obligations(
                    self.obligations,
                    res.activations,
                    res.events,
                    step_s=res.step_s,
                    end_s=res.duration_s,
                    capable=capable,
                    after_event=after,
                    completions=res.completions,
                    completions_only=True,
                )
                covered |= {v.id for v in verdicts if v.judged}
                unmodelled |= set(res.idealized)
                broken = [v for v in verdicts if v.judged and not v.passed]
                render = res.render()
                # actions the 2D simulation has no model for succeed at once and write
                # nothing: a scenario that fails with them says nothing about the model
                # (the AFSIM run checks it); requirement obligations are still judged
                blind = self._blind_fields(spec, res.idealized)
                inconclusive = not res.passed and bool(blind)
                if inconclusive:
                    head, _, rest = render.partition("\n")
                    render = (
                        f"{head}\n  （未判定：条件读取的 "
                        + ", ".join(sorted(blind)[:6])
                        + " 只由二维仿真没有模型的动作写入（"
                        + ", ".join(sorted(res.idealized)[:6])
                        + "，立即成功、不写字段）；本想定由 AFSIM 运行检验）\n"
                        + rest
                    )
                lines = render_obligations(verdicts, self.obligations)
                if lines:
                    render += "\n" + "\n".join(lines)
                out.l3.append(
                    {
                        "id": res.scenario,
                        "passed": (res.passed or inconclusive) and not broken,
                        "inconclusive": inconclusive,
                        "render": render,
                        "obligations": [v.__dict__ for v in verdicts],
                        "idealized": sorted(res.idealized),
                        "placeholders": sorted(res.placeholders),
                        "unresolved": sorted(res.unresolved),
                        "states": {k: [list(x) for x in v] for k, v in res.states.items()},
                        "transitions": res.transitions,
                        "failures": {k: [list(x) for x in v] for k, v in res.failures.items()},
                        "code_notes": dict(res.code_notes),
                        "duration_s": float(
                            ((raw.get("environment") or {}).get("duration_s")) or 0.0
                        ),
                    }
                )
            short = [(k, d) for k, d in advanced.items() if not d.startswith("ok ")]
            if short:
                out.l3.append(
                    {
                        "id": "effect_advances",
                        "passed": False,
                        "render": "effect_advances: FAIL\n"
                        + "\n".join(
                            f"  [FAIL] 需求约束 {k}：没有任何想定中车辆实际前出够远（{d}）；"
                            "检查前出的状态是否走完路线后才转出（不要在边走边搜时一发现目标就停下）"
                            for k, d in short
                        ),
                        "obligations": [],
                        "states": {},
                        "transitions": {},
                        "duration_s": 0.0,
                    }
                )
            # an obligation on actions the 2D simulation has no model for cannot be covered
            # there (they start but do nothing, so the anchor rarely happens): AFSIM judges it
            runs_any = set().union(*capable.values()) if capable else set()
            unused = [
                o
                for o in self.obligations
                if o.id not in covered
                and not o.spatial
                and any(a not in runs_any for a in o.actions())
            ]
            for o in unused:
                # the model reaches the requirement another way (follow_route ends stopped,
                # so there is no separate stop): the obligation names an action it never runs
                note = f"需求约束 {o.id} 不适用于本模型：模型没有用到 " + "、".join(
                    a for a in o.actions() if a not in runs_any
                )
                if note not in self.notes:
                    self.notes.append(note)
            missing = [
                o
                for o in self.obligations
                if o.id not in covered
                and o not in unused
                and not o.spatial  # effect obligations: judged on AFSIM positions only
                and not any(a in unmodelled or not self._modelled(a) for a in o.actions())
            ]
            if missing and spec.get("scenarios"):
                out.l3.append(
                    {
                        "id": "obligation_coverage",
                        "passed": False,
                        "render": "obligation_coverage: FAIL\n"
                        + "\n".join(_uncovered(o, after, self.c, model, capable) for o in missing),
                        "obligations": [],
                        "states": {},
                        "transitions": {},
                        "duration_s": 0.0,
                    }
                )
        here = self.afsim_verdicts.get(sheet_hash(self.sheet))
        note = here or self.afsim_note
        if verify and out.l1_ok and note is not None:
            current = here is not None
            if not (note.passed and current):
                # failed until AFSIM passes on this very sheet: a change alone does not count
                out.l3.insert(0, note_entry(note, current))  # shown first to the repair loop
        return out

    async def ask(
        self, stage: str, prompt: str, kinds: tuple[str, ...], single: str | None = None
    ) -> None:
        answer = await self.run(stage, prompt, TextAnswer)
        before = {b.key for b in self.blocks if b.kind in kinds}
        if single is not None:
            # one expected block: take it even when the fence label is off
            self.notes += merge(self.blocks, answer.text, kinds, target=Block(single, None, "", 0))
        else:
            self.notes += merge(self.blocks, answer.text, kinds)
        if not any(b.kind in kinds for b in self.blocks) and not before:
            self.notes.append(f"{stage} 的回答中没有 {'/'.join(kinds)} 代码块")

    async def repair(self, check: Check, kinds: tuple[str, ...], stage: str) -> bool:
        """One round: every block with problems is rewritten once. False: nothing to do."""
        by_block: dict[str, list[str]] = {}
        for p in check.problems:
            key = p.block
            if key.split(":")[0] not in kinds:
                continue
            by_block.setdefault(key, []).append(p.render())
        if not by_block:
            return False
        record = {"stage": stage, "blocks": {}}
        context = self._context(check)
        for key, problems in by_block.items():
            block = next((b for b in self.blocks if b.key == key), None)
            kind, _, role = key.partition(":")
            if block is None:
                block = Block(kind, role or None, "", 0)
            label = block.label
            prompt = P.repair_prompt(
                label, block.numbered() or "（空）", problems[:20], GUIDES.get(kind, ""), context
            )
            answer = await self.run(f"v3:repair:{kind}", prompt, TextAnswer)
            self.notes += merge(self.blocks, answer.text, (kind,), target=block)
            record["blocks"][key] = len(problems)
        self.rounds.append(record)
        return True

    def _modelled(self, action: str) -> bool:
        """Whether the 2D simulation has a model for the action."""
        from afsim_research_agent.ir.v2.sim.team import sim_spec

        return sim_spec(self.c, action) is not None

    def _blind_fields(self, spec: dict[str, Any], idealized: set[str]) -> set[str]:
        """Fields the model's conditions read that only actions without a 2D model write
        (``idealized``: such actions seen in the run; any action of the capability set
        without a 2D model counts too, whether it ran or not)."""
        from afsim_research_agent.falcon.v3.compiler import _bb_reads

        def unmodelled(action: str) -> bool:
            return action in idealized or not self._modelled(action)

        out: set[str] = set()
        if not idealized:
            return out
        for c in spec.get("conditions") or []:
            for path in _bb_reads(c.get("expression")):
                f = self.c.blackboard.get(path)
                if f is not None and f.written_by and all(unmodelled(a) for a in f.written_by):
                    out.add(path)
        return out

    @property
    def obligations_text(self) -> str:
        return "\n".join(
            f"- {o.id}（{o.clause or '—'}，{o.kind}）：{o.description}"
            + "；"
            + json.dumps(
                o.model_dump(exclude_none=True, exclude={"id", "description", "clause", "kind"}),
                ensure_ascii=False,
            )
            for o in self.obligations
        )

    def _context(self, check: Check) -> str:
        parts = [f"当前战术稿：\n{self.sheet}"]
        if self.obligations:
            parts.append("需求约束（程序检查）：\n" + self.obligations_text)
        if self.slices:
            parts.append(
                "能力集详细内容：\n" + K.details(self.c, self.slices, self.r.reference_policy)
            )
        return "\n\n".join(parts)

    # ------------------------------------------------------------------ stages
    async def clauses_stage(self) -> None:
        from afsim_research_agent.falcon.requirements import decompose

        request = FalconRequest.model_construct(
            requirement=self.r.requirement,
            platforms=[
                PlatformHint.model_construct(
                    role_hint=k, capability_class=v.capability_class, count=v.count
                )
                for k, v in self.r.roles.items()
            ],
            user_parameters={},
        )
        clauses, _values, notes = await decompose(request, self.run)
        self.clauses = [{"id": c.id, "text": c.text, "kind": c.kind} for c in clauses]
        self.notes += notes
        if self.tactic is not None:
            from afsim_research_agent.falcon.v3.crow_bridge import (
                deferred_elements,
                tactic_clauses,
            )

            # the selected tactic's steps, formations and exceptions must be implemented
            skip = (
                deferred_elements(self.crow_caps)
                if self.r.missing_capabilities != "wait"
                else set()
            )
            self.clauses += tactic_clauses(self.tactic, skip=skip)

    def autofix(self, check: Check) -> Check:
        """Mechanical repairs (``autofix``), each kept only when the sheet checks no worse:
        invented hold coordinates dropped; firing taken by time alone put on an external
        fire order; broken "arrived / done" conditions of a state whose tree ends by itself
        made tree events; other ENTRY_TRUE triggers wait until the state's actions have run."""
        from afsim_research_agent.falcon.v3 import autofix, semantics

        def fixes(spec: dict[str, Any]) -> list[Any]:
            items = semantics.completion_items(spec, self.c, self.c.tactics)
            done = [i[:4] for i in items if i[4]]
            entry = set(semantics.entry_true(spec, self.c, self.c.tactics))
            guard = [i[:4] for i in items if not i[4] and i[:4] in entry]
            fire = semantics.fire_by_time(spec, self.c, self.c.tactics)
            iface = self.iface
            out: list[Any] = [lambda bl: autofix.scenario_refs(bl, iface), autofix.held_here]
            order = next(
                (
                    (k, v)
                    for k, v in self.r.scenario_events.items()
                    if "fire" in k or "开火" in str(v) or "射击" in str(v)
                ),
                None,
            )
            if fire and order:
                out.append(lambda bl: autofix.fire_orders(bl, fire, *order))
            if done:
                out.append(lambda bl: autofix.completion_events(bl, done))
            if guard:
                out.append(lambda bl: autofix.entry_guards(bl, guard))
            return out

        if check.compiled.spec is None:
            return check
        for fix in fixes(check.compiled.spec):
            before = self.blocks
            self.blocks, notes = fix(self.blocks)
            if not notes:
                self.blocks = before
                continue
            after = self.check()
            if (
                after.compiled.spec is None
                or len(after.problems) > len(check.problems)
                or (check.l1_ok and not after.l1_ok)
            ):
                self.blocks = before
                self.notes.append("自动修正后检查变差，已撤回：" + "；".join(notes))
                continue
            self.notes += notes
            check = after
        return check

    def adopt_map(self, simplified: Any) -> dict[str, str]:
        """Clause → actions from the executable version (see ``simplify``): nothing in it
        is missing, so nothing is blocked."""
        kinds = {"steps": "s", "exceptions": "x", "geometry": "g"}
        for part in simplified.tactic.parts:
            for need in part.card.capability_needs:
                m = re.match(r"(steps|exceptions|geometry)\[(\d+)\]", need.source)
                if not m:
                    continue
                cid = f"{part.card.id}_{kinds[m.group(1)]}{int(m.group(2)) + 1}"
                # steps must use what they were reduced to; exceptions and formations are
                # guidance only (an exception often reuses the tree of another state)
                acts = list(need.maps_to) if m.group(1) == "steps" else []
                self.clause_map[cid] = {"actions": acts, "how": "", "missing": ""}
        for c in self.clauses:
            if c.get("origin") == "tactic":
                self.clause_map.setdefault(c["id"], {"actions": [], "how": "", "missing": ""})
        self.blocked = {}
        return self.blocked

    async def map_stage(self) -> dict[str, str]:
        """Map each tactic clause onto the capability set before drafting (Falcon's own
        check, next to Crow's annotation). Returns the blocked clauses: id → missing."""
        from afsim_research_agent.falcon.v3.crow_bridge import blocked_elements

        tactic = [c for c in self.clauses if c.get("origin") == "tactic"]
        if not tactic:
            return {}
        classes = {v.capability_class for v in self.r.roles.values()} or None
        prompt = P.capability_map_prompt(
            tactic,
            K.action_list(self.c, classes, None),
            {k: v.description or k for k, v in self.r.roles.items()},
        )
        answer = await self.run("v3:capability_map", prompt, MapAnswer)
        known = set(self.c.actions) | set(self.c.tactics)
        for item in answer.items:
            if item.clause not in {c["id"] for c in tactic}:
                continue
            acts = [a for a in item.actions if a in known]
            dropped = [a for a in item.actions if a not in known]
            self.clause_map[item.clause] = {
                "actions": acts,
                "how": item.how,
                "missing": item.missing.strip(),
                "unknown": dropped,
            }
        for c in tactic:
            m = self.clause_map.setdefault(c["id"], {"actions": [], "how": "", "missing": ""})
            if not m["actions"] and not m["missing"]:
                m["missing"] = "没有能实现它的动作"
        self.blocked = blocked_elements(tactic, self.crow_caps, self.clause_map)
        return self.blocked

    async def obligations_stage(self) -> None:
        """Requirement obligations: given by the request, or written by the model from the
        requirement clauses (independent of the draft, so a missed clause shows up)."""
        known = set(self.c.actions)
        if self.slices:
            allowed = set(K.closure(self.c, self.slices))
            known = {a for a, c in self.c.actions.items() if c.slice in allowed}
        events = set(self.r.scenario_events)
        raw: list[dict[str, Any]] = []
        if self.r.obligations is not None:
            raw = list(self.r.obligations)
        elif self.r.extract_obligations:
            classes = {v.capability_class for v in self.r.roles.values()} or None
            # only the slices the structure chose (with what their tactic-set entries use):
            # an obligation naming another slice's action would pull the draft there
            slices = K.closure(self.c, self.slices) if self.slices else None
            prompt = P.obligations_prompt(
                self.r.requirement,
                self.clauses,
                K.action_list(self.c, classes, slices),
                self.r.scenario_events,
                {k: v.description or k for k, v in self.r.roles.items()},
            )
            answer = await self.run("v3:obligations", prompt, ObligationAnswer)
            raw = [self._grounded(dict(o)) for o in answer.obligations]
            for item in raw:
                # one sentence: a model's reasoning does not belong in the description
                text = str(item.get("description") or "")
                if len(text) > 80:
                    item["description"] = text[:80].rstrip() + "…"
        seen: set[str] = set()
        for item in raw:
            try:
                ob = Obligation.model_validate({k: v for k, v in item.items() if v is not None})
            except Exception as exc:  # noqa: BLE001 - a malformed item is dropped and noted
                self.notes.append(f"需求约束 {item.get('id')} 格式不对，已忽略：{str(exc)[:120]}")
                continue
            problems = ob.problems(known, events)
            unknown = [r for r in ob.roles if self.r.roles and r not in self.r.roles]
            if unknown:
                problems.append(f"{ob.id}: 角色 {'、'.join(unknown)} 不存在")
            if problems or ob.id in seen:
                self.notes.append(f"需求约束 {ob.id} 已忽略：{'；'.join(problems) or '重复'}")
                continue
            seen.add(ob.id)
            self.obligations.append(ob)
        self._interface_obligations(known, events)

    def _interface_obligations(self, known: set[str], events: set[str]) -> None:
        """Effect obligations the scenario interface implies, whatever the extraction wrote:
        a route forward means the vehicles must actually advance along it (at least half of
        its bound length) — otherwise a draft that never moves passes every check."""
        from afsim_research_agent.ir.v2.scenario_interface import bind

        iface = self.iface
        if iface is None or not iface.routes:
            return
        if any(o.kind == "advances" for o in self.obligations):
            return
        bound = bind(iface, self.kit)
        length = 0.0
        for r in bound.routes.values():
            (x0, y0), (x1, y1) = r.plane[0], r.plane[-1]
            length = max(length, ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5)
        if length <= 0:
            return
        tactic = [c["id"] for c in self.clauses if c.get("origin") == "tactic"]
        ob = Obligation.model_validate(
            {
                "id": "scn_advance_along_route",
                "kind": "advances",
                "description": "沿想定接口的前出路线实际前出（由想定接口推导）",
                "clause": tactic[0] if tactic else None,
                "min_m": round(length / 2),
            }
        )
        if not ob.problems(known, events):
            self.obligations.append(ob)
            self.notes.append(
                f"需求约束 {ob.id}：想定接口有前出路线，要求实际前出至少 {ob.min_m:g} m"
            )

    def _auto_scenarios(self, spec: dict[str, Any], model: dict[str, Any]) -> list[dict[str, Any]]:
        """2D scenarios the binding adds so every "respond to an order" obligation has its
        order raised once (the drafter's scenarios often leave an order out)."""
        if self.iface is None:
            return []
        externals = {
            k
            for k, v in (model.get("events") or {}).items()
            if isinstance(v, dict) and v.get("source") == "external"
        }
        raised = {
            str(e.get("event"))
            for sc in spec.get("scenarios") or []
            for e in (sc.get("events") or [])
            if isinstance(e, dict)
        }
        wanted = sorted({o.on_event for o in self.obligations if o.on_event} & externals - raised)
        end = self.binding(model).end_s + 40.0
        return [
            {
                "id": f"auto_{ev}",
                "description": f"想定绑定自动补充：运行中发出 {ev}",
                "environment": {"duration_s": end},
                "events": [{"at_s": round(end * 0.6), "event": ev}],
            }
            for ev in wanted
        ]

    def _align_roles(self, model: dict[str, Any]) -> None:
        """Obligations name roles of the request; a draft may name its only role otherwise
        (``role_1`` → ``observer_vehicle``): the obligation follows it instead of judging no
        vehicle at all."""
        roles = list(model.get("roles") or {})
        if len(roles) != 1:
            return
        for ob in self.obligations:
            if ob.roles and not set(ob.roles) <= set(roles):
                ob.roles = list(roles)

    def _grounded(self, item: dict[str, Any]) -> dict[str, Any]:
        """An extracted ``not_before`` whose delay the requirement and its clauses do not
        state is only an order: it becomes ``precedes`` (a made-up "at least 5 s after"
        fails a correct model that acts at once)."""
        if item.get("kind") != "not_before" or item.get("min_s") is None:
            return item
        text = self.r.requirement + "\n" + "\n".join(str(c.get("text", "")) for c in self.clauses)
        value = float(item["min_s"])
        numbers = {float(n) for n in re.findall(r"\d+(?:\.\d+)?", text)}
        if value in numbers or value <= 0:
            return item
        self.notes.append(
            f"需求约束 {item.get('id')}：需求没有给出 {value:g} s 的间隔，按先后顺序（precedes）判定"
        )
        out = {k: v for k, v in item.items() if k not in ("action", "after", "min_s", "kind")}
        return {**out, "kind": "precedes", "first": item.get("after"), "then": item.get("action")}

    async def skeleton_stage(self) -> bool:
        """Draft from a verified skeleton (``falcon.v3.skeleton``): the model only chooses;
        the program writes the sheet. False when no skeleton fits (draft from scratch)."""
        from afsim_research_agent.falcon.v3 import skeleton as SK

        events = {k: str(v) for k, v in self.r.scenario_events.items()}
        hint = {k: v.model_dump() for k, v in self.r.roles.items()}
        choice = await self.run(
            "v3:skeleton",
            SK.prompt(self.r.requirement, self.clauses, hint, events),
            SK.SkeletonChoice,
        )
        self.skeleton_choice = choice
        if not choice.applicable:
            self.notes.append(f"没有适用的骨架，从零起草：{choice.reason}")
            return False
        roles = self.r.roles
        if roles:
            single = [k for k, v in roles.items() if v.count == 1]
            many = [k for k, v in roles.items() if v.count > 1]
            if choice.leader_role not in roles and single:
                choice.leader_role = single[0]
            if choice.formation and choice.member_role not in roles and many:
                choice.member_role = many[0]
        if choice.wait_start and "start_order" not in events:
            choice.wait_start = False
            self.notes.append("想定不会发出开始命令 start_order：骨架不等开始命令")
        if choice.formation and choice.family == "formation":
            # every formation skeleton advances along the interface route ``approach``
            iface = dict(self.r.scenario_interface or {})
            routes = dict(iface.get("routes") or {})
            if "approach" not in routes:
                points = dict(iface.get("points") or {})
                points.setdefault("post", {"anchor": "own_start", "description": "车辆出发位置"})
                points.setdefault(
                    "ahead", {"anchor": "ahead", "description": "前出到达的位置（向敌方方向）"}
                )
                routes["approach"] = {
                    "start": "post",
                    "end": "ahead",
                    "description": "从出发位置前出到 ahead 的路线（骨架沿路线搜索前进）",
                }
                iface |= {"points": points, "routes": routes}
                self.r = self.r.model_copy(update={"scenario_interface": iface})
                self.notes.append("想定接口补上前出路线 approach（编队骨架沿它搜索前进）")
        text, notes, _ = SK.build(
            choice,
            events=events,
            display_name=self.r.display_name or self.r.model_id,
            clauses=[c["id"] for c in self.clauses],
        )
        self.notes.append(f"按已验证骨架起草：{choice.reason}")
        self.notes += notes
        self.blocks = []
        self.notes += merge(self.blocks, text, (*STRUCTURE_KINDS, *CONTROL_KINDS, "verification"))
        check = self.check(pending=True)
        self.slices = self._slices(check.compiled)
        return True

    async def structure_stage(self) -> Check:
        classes = {v.capability_class for v in self.r.roles.values()} or None
        prompt = P.structure_prompt(
            self.r.requirement,
            self.clauses,
            K.index(self.c, classes),
            {k: v.model_dump() for k, v in self.r.roles.items()},
            {k: v.model_dump() for k, v in self.r.scenario_parameters.items()},
            self.r.allow_mission_ref,
            state_names=self.r.state_names,
            scenario_events=self.r.scenario_events,
            policy=self.r.reference_policy,
            obligations=self.obligations_text,
            interface=self.scenario_text(),
            coarse=self.r.granularity == "coarse",
        )
        await self.ask("v3:structure", prompt, STRUCTURE_KINDS)
        check = self.check(pending=True)
        for _ in range(2):
            if not await self.repair(check, STRUCTURE_KINDS, "structure"):
                break
            check = self.check(pending=True)
        self.slices = self._slices(check.compiled)
        return check

    def _slices(self, compiled: Compiled) -> list[str]:
        if not compiled.slices:
            return K.slices(self.c)
        block = compiled.blocks.get("model")
        if block is not None and "slices:" not in block.text and not self.r.slices:
            self.notes.append("结构稿没有给出 slices，控制稿展示全部切片")
        return compiled.slices

    def _pending(self, compiled: Compiled) -> list[str]:
        spec = compiled.spec or {}
        known = {e["id"] for e in spec.get("events") or []} | {
            c["id"] for c in spec.get("conditions") or []
        }
        out: list[str] = []
        for m in (spec.get("missions") or {}).values():
            if "tactic" in m:
                continue
            for t in m["transitions"] + m["global"]:
                if t["on"] not in known and t["on"] not in out:
                    out.append(t["on"])
        return out

    def _states(self, compiled: Compiled) -> dict[str, list[str]]:
        spec = compiled.spec or {}
        out: dict[str, list[str]] = {}
        for rid, mission in (spec.get("missions") or {}).items():
            if "tactic" in mission:
                continue
            out[rid] = [s for s in mission["states"] if s not in mission["terminal"]]
        return out

    async def control_stage(self, structure: Check) -> Check:
        states = self._states(structure.compiled)
        roles = [r["id"] for r in (structure.compiled.spec or {}).get("roles", [])]
        prompt = P.control_prompt(
            self.r.requirement,
            self.clauses,
            self.sheet,
            K.details(self.c, self.slices, self.r.reference_policy),
            roles,
            states,
            pending=self._pending(structure.compiled),
            policy=self.r.reference_policy,
            obligations=self.obligations_text,
            interface=self.scenario_text(),
            mission_refs=[
                r
                for r, m in ((structure.compiled.spec or {}).get("missions") or {}).items()
                if "tactic" in m
            ],
            coarse=self.r.granularity == "coarse",
        )
        await self.ask("v3:control", prompt, CONTROL_KINDS)
        check = self.check()
        for _ in range(self.r.max_repair):
            if check.l1_ok:
                break
            if not await self.repair(check, STRUCTURE_KINDS + CONTROL_KINDS, "control"):
                break
            check = self.check()
        return check

    async def verification_stage(self, control: Check) -> Check:
        prompt = P.verification_prompt(
            self.r.requirement,
            self.sheet,
            self._summary(control),
            self.obligations_text,
            interface=self.scenario_text(control.model),
        )
        await self.ask("v3:verification", prompt, ("verification",), single="verification")
        check = self.check(verify=True)
        if not check.l1_ok:
            if await self.repair(check, ("verification",), "verification"):
                check = self.check(verify=True)
        stuck = self._stuck(check)
        if stuck:
            # the model, not the test: a state is never left in the 2D simulation
            problems = [SheetProblem(key, None, line) for key, line in stuck]
            fake = Check(check.compiled, check.model, check.diagnostics, problems)
            saved = self._snapshot()
            if await self.repair(fake, STRUCTURE_KINDS + CONTROL_KINDS, "behavior"):
                again = self.check(verify=True)
                if again.l1_ok:
                    check = again
                else:  # the repair broke L1: keep the sheet that passed
                    self.blocks = saved
                    self.notes.append("行为修复后 L1 不通过，已恢复修复前的战术稿")
        failed = [t for t in check.l2 if not t["passed"]] + [s for s in check.l3 if not s["passed"]]
        if failed and check.l1_ok:
            lines = [
                f"- [verification] {f['id']} 未通过：{f.get('detail') or f.get('render')}"
                for f in failed
            ]
            lines.append(
                "- 如果期望与需求一致、是模型行为不对，保持期望不变（问题会记入审阅页）；"
                "如果期望本身写错了（与需求和状态图不符），修改期望。不要为了通过而改成与需求不符的期望。"
            )
            block = next((b for b in self.blocks if b.kind == "verification"), None)
            if block is not None:
                answer = await self.run(
                    "v3:repair:verification",
                    P.repair_prompt(
                        block.label,
                        block.numbered(),
                        lines,
                        P.VERIFICATION_GUIDE,
                        self._context(check),
                    ),
                    TextAnswer,
                )
                saved = self._snapshot()
                self.notes += merge(self.blocks, answer.text, ("verification",), target=block)
                self.rounds.append({"stage": "verification", "blocks": {block.key: len(failed)}})
                again = self.check(verify=True)
                if again.l1_ok:
                    check = again
                else:
                    self.blocks = saved
                    self.notes.append("验证稿修复后 L1 不通过，已恢复修复前的验证稿")
        return check

    def _snapshot(self) -> list[Block]:
        return [Block(b.kind, b.role, b.text, b.line, b.info) for b in self.blocks]

    def _stuck(self, check: Check) -> list[tuple[str, str]]:
        """Failed scenarios where a role stays in a non-terminal state for the last half of
        the run: the triggers leaving it never hold. Returned as (block key, problem)."""
        if check.model is None:
            return []
        out: list[tuple[str, str]] = []
        for sc in check.l3:
            if sc["passed"]:
                continue
            for name, seq in sc.get("states", {}).items():
                if not seq:
                    continue
                t, state = seq[-1]
                role = name.rsplit("_", 1)[0]
                rm = check.model["roles"].get(role)
                if rm is None or state in rm["mission"]["terminal"]:
                    continue
                if sc["duration_s"] and t > sc["duration_s"] / 2:
                    continue
                exits = [
                    f"{x['from']} → {x['to']} 当 {x['on']}"
                    for x in rm["mission"]["transitions"]
                    if x["from"] == state
                ]
                spec_m = (check.compiled.spec or {}).get("missions", {}).get(role, {})
                if "tactic" in spec_m:
                    continue  # a referenced mission: its triggers are not the model's to fix
                key = f"mermaid:{role}"
                out.append(
                    (
                        key,
                        f"- [L3 {sc['id']}] {name} 从 {t:g} s 起一直停在 {state}，直到想定结束；"
                        f"离开它的转换：{'; '.join(exits) or '无'}。这些触发在运行中从未成立："
                        "检查条件读的字段是否会被本状态的动作或战术写入、是否猜测了字段取值；"
                        "能用计时事件或战术集任务的触发含义表达的，照它写",
                    )
                )
                out.append(("conditions", out[-1][1]))
        return out

    def _summary(self, check: Check) -> str:
        if check.model is None:
            return "（L1 未通过，模型未生成）"
        lines = []
        for rid, role in check.model["roles"].items():
            m = role["mission"]
            lines.append(
                f"角色 {rid}：初始 {m['initial_state']}；终止 {m['terminal']}；安全 {m['safe']}"
            )
            for t in m["transitions"]:
                lines.append(f"  {t['from']} → {t['to']} 当 {t['on']}")
            for g in m["global"]:
                lines.append(f"  * → {g['to']} 当 {g['on']}")
        events = check.model.get("events") or {}
        if events:
            lines.append("事件：" + ", ".join(f"{k}({v.get('source')})" for k, v in events.items()))
        lines.append(
            "参数："
            + ", ".join(f"{k}={v.get('value')}" for k, v in check.model["parameters"].items())
        )
        fields = sorted(
            f
            for f in check.model["blackboard"]
            if ".request." not in f and not f.startswith("execution.")
        )
        lines.append("可在测试中设置的黑板字段：" + ", ".join(fields[:80]))
        used: set[str] = set()

        def walk(node: dict[str, Any] | None) -> None:
            if not node:
                return
            if node.get("type") == "action" and "gap" not in node:
                used.add(str(node.get("action")))
            for kid in node.get("children") or []:
                walk(kid)

        for role in check.model["roles"].values():
            for body in role["states"].values():
                walk(body.get("tree"))
        bare = sorted(a for a in used if not self._modelled(a))
        if bare:
            lines.append(
                "二维仿真中没有模型的动作（按立即成功处理，它们本应写入的黑板字段保持默认值，"
                "依赖这些字段的条件会立即按默认值判断；L2 单步测试与想定期望要据此写）："
                + ", ".join(bare)
            )
        lines.append(
            "注意：一次更新中，转换后的新状态会立即执行并检查自己的转换，条件已满足时会连续转换。"
        )
        return "\n".join(lines)


# ---------------------------------------------------------------------- package
def _review(request: V3Request, drafter: Drafter, check: Check, status: str) -> str:
    spec = check.compiled.spec or {}
    lines = [f"# Falcon v3 审阅：{request.display_name or request.model_id}", ""]
    lines.append(f"- 状态：{status}；IR 2.0；能力集切片：{', '.join(drafter.slices)}")
    policy = (
        "可引用战术集子树；任务状态机与状态树自己写（战术集任务和状态树只作范例）"
        if request.reference_policy == "subtrees"
        else ("允许整体引用战术集任务" if request.allow_mission_ref else "不允许整体引用战术集任务")
    )
    lines.append(f"- 引用方式：{policy}")
    if request.scenario_interface:
        iface = drafter.iface
        lines += ["", "## 想定接口与绑定", "", iface.prompt_text()]
        if check.model is not None:
            from afsim_research_agent.ir.v2.scenario_interface import summary

            bound = drafter.binding(check.model)
            lines += ["", "默认绑定：", summary(bound)]
            lines += [f"- 默认取值：{d}" for d in bound.defaults]
        if check.binding:
            lines += ["", "绑定问题（模型无误，想定提供不了；不交给起草修复）："]
            lines += [f"- {b}" for b in check.binding]
        if check.kit_defaults:
            lines += ["", "由装备包默认值提供的参数（二维判定不了，看 AFSIM 结果）："]
            lines += [f"- {b}" for b in check.kit_defaults]
    cover = coverage(drafter, check, drafter.afsim_history)
    if cover:
        lines += ["", "## 战术条款的验证", ""]
        lines.append(
            "每个条款要有运行判定过的需求约束才算验证；只证明动作被调用、状态被进入的不算。"
        )
        for cid, c in cover.items():
            if c["verified"]:
                mark = "已验证" + ("（含效果约束）" if c["effect"] else "（只有时序约束）")
            elif c["judged"]:
                mark = "未通过"
            elif c["obligations"]:
                mark = "未验证：约束 " + "、".join(c["obligations"]) + " 没有被任何运行判定"
            else:
                mark = "未验证：没有约束检查这个条款"
            lines.append(f"- {cid} {c['text'][:60]}：{mark}")
    lines.append("")
    lines.append("## 引用、新建与缺口")
    missions = {r: m for r, m in (spec.get("missions") or {}).items()}
    for rid, m in missions.items():
        if "tactic" in m:
            lines.append(f"- {rid}：整体引用战术集任务 `{m['tactic']}`")
    tactics: set[str] = set()
    actions: set[str] = set()

    def walk(n: dict[str, Any]) -> None:
        if n.get("type") == "tactic":
            tactics.add(n["tactic"])
        if n.get("type") == "action" and not str(n.get("action", "")).startswith("gap."):
            actions.add(n["action"])
        for k in n.get("children") or []:
            walk(k)

    for sub in spec.get("subtrees") or []:
        walk(sub["root"])
    for trees in (spec.get("state_trees") or {}).values():
        tactics |= {t for t in trees.values() if t in drafter.c.tactics}
    lines.append(f"- 引用战术集：{', '.join(sorted(tactics)) or '无'}")
    lines.append(f"- 直接使用的动作：{', '.join(sorted(actions)) or '无'}")
    lines.append(f"- 新建子树：{', '.join(s['id'] for s in spec.get('subtrees') or []) or '无'}")
    gaps = (check.model or {}).get("gaps") or spec.get("gaps") or {}
    if gaps:
        for gid, g in gaps.items():
            lines.append(
                f"- 缺口 `{gid}`（{g.get('kind')}）：{g.get('description')}；理由：{g.get('reason') or '未写'}"
            )
    else:
        lines.append("- 缺口：无")
    lines.append("")
    lines.append("## 校验")
    errors = [d for d in check.diagnostics if d.severity.value == "error"]
    warnings = [d for d in check.diagnostics if d.severity.value == "warning"]
    lines.append(
        f"- L1：{'通过' if check.l1_ok else '未通过'}（错误 {len(errors)}，警告 {len(warnings)}）"
    )
    for p in check.problems[:30]:
        lines.append(f"  {p.render()}")
    for w in warnings[:20]:
        lines.append(f"  - 警告 {w.code} {w.path}: {w.message}")
    passed = sum(t["passed"] for t in check.l2)
    lines.append(f"- L2：{passed}/{len(check.l2)} 通过")
    for t in check.l2:
        if not t["passed"]:
            lines.append(f"  - {t['id']}: {t['detail']}")
    for s in check.l3:
        word = "未判定" if s.get("inconclusive") else ("通过" if s["passed"] else "未通过")
        lines.append(f"- L3 {s['id']}：{word}")
        lines += ["  " + x for x in s["render"].splitlines()[1:]]
    lines.append("")
    if check.model is not None:
        rows = compare_tactics(check.model, drafter.c)
        if rows:
            lines += [*render_comparison(rows), ""]
    if drafter.obligations:
        lines.append("## 需求约束（程序检查）")
        judged: dict[str, list[str]] = {}
        for s in check.l3:
            for v in s.get("obligations") or []:
                if v.get("judged"):
                    judged.setdefault(v["id"], []).append(
                        f"{s['id']} {'通过' if v['passed'] else '未通过'}"
                    )
        for o in drafter.obligations:
            lines.append(
                f"- {o.id}（{o.clause or '—'}）{o.description}："
                + ("；".join(judged.get(o.id, [])) or "没有想定覆盖")
            )
        lines.append("")
    lines.append("## 需求条款")
    implements = check.compiled.implements
    for c in drafter.clauses:
        lines.append(
            f"- {c['id']}：{c['text']} → {', '.join(implements.get(c['id'], [])) or '未标出'}"
        )
    assumptions = spec.get("assumptions") or []
    if assumptions:
        lines.append("")
        lines.append("## 假设（需确认）")
        lines += [f"- {a['id']}：{a['statement']}" for a in assumptions]
    if request.scenario_parameters:
        lines.append("")
        lines.append("## 想定提供的参数")
        lines.append(
            "以下参数的取值在本地运行配置的 `[parameters]` 中给出："
            + ", ".join(request.scenario_parameters)
        )
    if drafter.notes:
        lines.append("")
        lines.append("## 生成过程记录")
        lines += [f"- {n}" for n in drafter.notes[:40]]
    return "\n".join(lines) + "\n"


def write_package(
    staging: Path,
    request: V3Request,
    drafter: Drafter,
    check: Check,
    status: str,
    fixes: list[Any] | None = None,
) -> None:
    spec = check.compiled.spec or {}
    (staging / SHEET_FILE).write_text(drafter.sheet, encoding="utf-8")
    if drafter.tactic is not None:
        from afsim_research_agent.falcon.v3.crow_bridge import write_crow_files

        write_crow_files(
            staging,
            drafter.tactic,
            drafter.crow_caps,
            request.missing_capabilities,
            drafter.blocked,
            drafter.simplified,
        )
    if spec:
        (staging / "behavior_spec.yaml").write_text(
            yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
    if drafter.obligations:
        (staging / "obligations.yaml").write_text(
            yaml.safe_dump(
                {"obligations": [o.model_dump(exclude_none=True) for o in drafter.obligations]},
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
    if request.scenario_interface:
        # the two layers: the model names the scenario's points, routes and events; the
        # binding (here: the default one, laid out from the kit) gives them values
        doc: dict[str, Any] = {"scenario_interface": request.scenario_interface}
        if check.model is not None:
            bound = drafter.binding(check.model)
            doc["binding"] = bound.model_dump(mode="json")
            doc["binding_problems"] = check.binding
            if check.kit_defaults:
                doc["kit_defaults"] = check.kit_defaults
        (staging / "scenario_interface.yaml").write_text(
            yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
    gaps = (check.model or {}).get("gaps") or {}
    if gaps:
        (staging / "gaps.yaml").write_text(
            yaml.safe_dump({"gaps": gaps}, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
    (staging / "source_map.json").write_text(
        json.dumps(
            {k: list(v) for k, v in check.compiled.source.items()}, ensure_ascii=False, indent=1
        )
        + "\n",
        encoding="utf-8",
    )
    report = {
        "stage": "falcon_draft3",
        "status": status,
        "ir_version": "2.0",
        "capability": (check.model or {}).get("capability"),
        "diagnostics": [d.model_dump(mode="json") for d in check.diagnostics],
        "problems": [p.render() for p in check.problems],
        "binding_problems": check.binding,
        "kit_defaults": check.kit_defaults,
        "l2": check.l2,
        "l3": [
            {k: v for k, v in s.items() if k != "render"} | {"summary": s["render"]}
            for s in check.l3
        ],
        "gaps": sorted(gaps),
        # tactic clauses and the obligations a run judged for them (see coverage)
        "clause_coverage": coverage(drafter, check, getattr(drafter, "afsim_history", None)),
        "tactic_comparison": compare_tactics(check.model, drafter.c) if check.model else [],
    }
    (staging / "validation_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    review = _review(request, drafter, check, status)
    if drafter.tactic is not None:
        from afsim_research_agent.falcon.v3.crow_bridge import review_section

        section = review_section(
            drafter.tactic,
            drafter.crow_caps,
            request.missing_capabilities,
            drafter.blocked,
            drafter.simplified,
        )
        review += "\n" + "\n".join(section) + "\n"
    for outcome in fixes or []:
        review += "\n" + "\n".join(render_fix(outcome)) + "\n"
    (staging / "falcon_review.md").write_text(review, encoding="utf-8")
    if fixes:
        (staging / "fix_log.json").write_text(
            json.dumps(
                [
                    {
                        "solved": o.solved,
                        "steps": o.steps,
                        "score_start": o.score_start,
                        "score_end": o.score_end,
                        "question": o.question,
                        "summary": o.summary,
                        "log": o.log,
                        "diagnoses": o.diagnoses,
                        "afsim_runs": o.afsim_runs,
                        "classified": o.classified,
                        "findings": o.findings,
                        "diagnosed_unfixed": o.diagnosed_unfixed,
                        "edits": [e.__dict__ for e in o.edits],
                    }
                    for o in fixes
                ],
                ensure_ascii=False,
                indent=1,
            )
            + "\n",
            encoding="utf-8",
        )
    trace = {
        "model_id": request.model_id,
        "falcon_version": FALCON_V3_VERSION,
        "prompt_version": P.PROMPT_VERSION,
        "status": status,
        "slices": drafter.slices,
        "allow_mission_ref": request.allow_mission_ref,
        "clauses": drafter.clauses,
        "repairs": drafter.rounds,
        "calls": [r.model_dump(mode="json") for r in drafter.run.records],
    }
    (staging / "falcon_trace.json").write_text(
        json.dumps(trace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (staging / "falcon_request.yaml").write_text(
        yaml.safe_dump(
            request.model_dump(mode="json", exclude_none=True), allow_unicode=True, sort_keys=False
        ),
        encoding="utf-8",
    )


def _target(request: V3Request, request_path: Path, out_dir: Path | None) -> Path:
    target = (out_dir or request_path.parent) / request.model_id
    if target.exists():
        raise FileExistsError(f"{target} exists; model packages go to a new directory")
    return target


def compile_package(sheet_path: Path, request_path: Path, out: Path) -> V3Result:
    """Offline: an edited or hand-written sheet → IR 2.0 package with L1–L3 (no model)."""
    from afsim_research_agent.falcon.sheet.blocks import split_sheet

    request = V3Request.load(request_path)
    if out.exists():
        raise FileExistsError(f"{out} exists; model packages go to a new directory")
    contracts = Contracts.load(request.capability_set)
    if request.obligations is None:
        saved = sheet_path.parent / "obligations.yaml"
        data = yaml.safe_load(saved.read_text(encoding="utf-8")) if saved.exists() else {}
        request = request.model_copy(
            update={
                "obligations": (data or {}).get("obligations") or [],
                "extract_obligations": False,
            }
        )
    drafter = Drafter(request, contracts, ModelRunner(_NoModel()))
    import asyncio

    asyncio.run(drafter.obligations_stage())
    drafter.blocks = split_sheet(sheet_path.read_text(encoding="utf-8"))[0]
    drafter.slices = drafter._slices(drafter.compile(pending=True))
    check = drafter.check(verify=True)
    status = "draft_valid" if check.verified else "draft_invalid"
    if check.verified and any(not c["verified"] for c in coverage(drafter, check).values()):
        status = UNVERIFIED
    staging = out.with_name(out.name + ".partial")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    write_package(staging, request, drafter, check, status)
    staging.rename(out)
    return V3Result(out, status, check, [])


def command_events(requirement: str) -> dict[str, str]:
    """External events for the orders a requirement says the vehicles wait for (a fire
    order, a deploy order, …): the command events of the derived scenario interface."""
    from afsim_research_agent.ir.v2.scenario_interface import derive

    return {k: v.description for k, v in derive(requirement).events.items() if v.kind == "command"}


class _NoModel:
    name = "none"

    async def generate(self, stage: str, prompt: str, schema: Any) -> Any:  # pragma: no cover
        raise RuntimeError("compile3 does not call a model")


def _score(check: Check) -> tuple[int, int, int]:
    """Smaller is better: L1 failed, L1 problems, failed L2 tests / L3 scenarios (including
    broken requirement obligations and obligations no scenario covers)."""
    failed = sum(not t["passed"] for t in check.l2) + sum(not s["passed"] for s in check.l3)
    return (0 if check.l1_ok else 1, len(check.problems), failed)


async def _draft(
    drafter: Drafter, run: ModelRunner, structure: Check | None = None
) -> tuple[Check, list[Any]]:
    request = drafter.r
    if structure is None:
        report("起草：结构稿（角色、状态、参数）")
        structure = await drafter.structure_stage()
    report("起草：控制稿（状态图、条件、行为树）")
    control = drafter.autofix(await drafter.control_stage(structure))
    budget = request.fix_steps
    fixes: list[Any] = []
    if not control.l1_ok and budget:
        fixes.append(await fix_sheet(drafter, run, max(1, budget // 2), l1_only=True))
        budget -= fixes[-1].steps
        control = drafter.autofix(drafter.check())
    report(f"检查 L1：{'通过' if control.l1_ok else '未通过，进入修复'}")
    check = await drafter.verification_stage(control) if control.l1_ok else control
    if not check.verified and budget > 0:
        fixes.append(await fix_sheet(drafter, run, budget))
        budget -= fixes[-1].steps
        check = drafter.check(verify=True)
    if check.l1_ok and not (check.l2 or check.l3):
        # L1 was repaired only after the verification stage was skipped: write the tests and
        # scenarios now, then use what is left of the repair budget on them
        check = await drafter.verification_stage(check)
        if not check.verified and budget > 0:
            fixes.append(await fix_sheet(drafter, run, budget))
            check = drafter.check(verify=True)
    return check, fixes


async def _skeleton_draft(drafter: Drafter, run: ModelRunner) -> tuple[Check, list[Any]]:
    """A sheet written from a skeleton: checked as it is, then the repair budget on what
    the skeleton did not cover."""
    report("起草：按骨架生成，检查 L1–L3")
    check = drafter.autofix(drafter.check(verify=True))
    fixes: list[Any] = []
    if not check.verified and drafter.r.fix_steps > 0:
        fixes.append(await fix_sheet(drafter, run, drafter.r.fix_steps))
        check = drafter.check(verify=True)
    return check, fixes


NEEDS_CAPABILITY = "needs_capability"
NEEDS_BINDING = "needs_binding"


def with_interface(request: V3Request) -> tuple[V3Request, list[str]]:
    """The request with its scenario interface (given, or derived from the requirement);
    the request's scenario events join it as optional requests."""
    from afsim_research_agent.ir.v2.scenario_interface import (
        InterfaceEvent,
        ScenarioInterface,
        derive,
    )

    if request.scenario_interface:
        iface = ScenarioInterface.model_validate(request.scenario_interface)
        notes = ["想定接口由请求给出"]
    else:
        iface = derive(request.requirement)
        notes = ["想定接口由需求推导：" + "；".join(iface.sources)]
    for eid, text in request.scenario_events.items():
        iface.events.setdefault(eid, InterfaceEvent(kind="request", description=text))
    for problem in iface.check():
        notes.append("想定接口问题：" + problem)
    request = request.model_copy(
        update={"scenario_interface": iface.model_dump(), "scenario_events": iface.event_texts()}
    )
    return request, notes


def _blocked_package(staging: Path, target: Path, request: V3Request, lead: Drafter) -> V3Result:
    """The selected tactic needs capabilities the set does not have: no draft, a package
    that says which (and the selection, for the capability-development queue)."""
    from afsim_research_agent.falcon.v3.crow_bridge import review_section, write_crow_files

    write_crow_files(
        staging,
        lead.tactic,
        lead.crow_caps,
        request.missing_capabilities,
        lead.blocked,
        lead.simplified,
    )
    report = {
        "stage": "falcon_draft3",
        "status": NEEDS_CAPABILITY,
        "blocked_clauses": lead.blocked,
        "clause_map": lead.clause_map,
        "notes": lead.notes,
    }
    (staging / "validation_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        f"# Falcon v3：{request.display_name or request.model_id}",
        "",
        f"- 状态：{NEEDS_CAPABILITY}（选定战术的核心要素能力集实现不了，没有起草）",
        "- 处理：能力开发后重新起草；或在请求中设 missing_capabilities: skip（不实现这些要素，"
        "并在模型包中声明）；或另选战术。",
        "",
        "## 各条款的能力核对",
        "",
    ]
    for cid, m in lead.clause_map.items():
        mark = f"暂缺：{lead.blocked[cid]}" if cid in lead.blocked else "可实现"
        acts = "、".join(m.get("actions") or []) or "—"
        lines.append(f"- {cid}：{mark}（{acts}）")
    lines += [
        "",
        *review_section(
            lead.tactic,
            lead.crow_caps,
            request.missing_capabilities,
            lead.blocked,
            lead.simplified,
        ),
    ]
    (staging / "falcon_review.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if target.exists():
        shutil.rmtree(target)
    staging.rename(target)
    return V3Result(target, NEEDS_CAPABILITY, lead.check(pending=True), [])


async def run_draft3(
    request_path: Path,
    model: FalconModel,
    *,
    out_dir: Path | None = None,
    allow_mission_ref: bool | None = None,
    fix_steps: int | None = None,
    candidates: int | None = None,
    afsim: AfsimConfig | None = None,
    granularity: str | None = None,
    drafting: str | None = None,
) -> V3Result:
    import asyncio

    request = V3Request.load(request_path)
    if fix_steps is not None:
        request = request.model_copy(update={"fix_steps": fix_steps})
    if allow_mission_ref is not None:
        request = request.model_copy(update={"allow_mission_ref": allow_mission_ref})
    if candidates is not None:
        request = request.model_copy(update={"candidates": candidates})
    if granularity is not None:
        request = request.model_copy(update={"granularity": granularity})
    if drafting is not None:
        request = request.model_copy(update={"drafting": drafting})
    contracts = Contracts.load(request.capability_set)
    run = ModelRunner(model)
    tactic, bridge_notes, simplified = None, [], None
    if request.tactic_selection is not None:
        from afsim_research_agent.falcon.tactic import load_selection
        from afsim_research_agent.falcon.v3.crow_bridge import apply_selection, resolve_selection

        loaded = load_selection(resolve_selection(request, request_path))
        if request.tactic_level == "simple":
            from afsim_research_agent.falcon.v3.simplify import simplify

            try:
                simplified = await simplify(
                    run,
                    loaded,
                    contracts,
                    loaded.requirement(request.requirement),
                    K.action_list(contracts, None, None),
                )
                loaded = simplified.tactic
            except FalconError as exc:
                bridge_notes.append(f"化简为可执行版失败，按完整战术起草：{exc}")
        request, tactic, notes = apply_selection(request, request_path, contracts, loaded)
        bridge_notes = notes + (simplified.notes if simplified else []) + bridge_notes
    request, iface_notes = with_interface(request)
    bridge_notes += iface_notes
    target = _target(request, request_path, out_dir)
    staging = target.with_name(target.name + ".partial")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    kit = None
    if afsim is not None and afsim.kit is not None:
        from afsim_research_agent.ir.v2.afsim.scenario import load_kit

        kit = load_kit(afsim.kit)
    lead = Drafter(request, contracts, run)
    lead.kit = kit
    lead.tactic, lead.notes = tactic, list(bridge_notes)
    lead.simplified = simplified
    caps: list[dict[str, Any]] = []
    if tactic is not None:
        from afsim_research_agent.falcon.v3.crow_bridge import capability_map

        caps = capability_map(tactic, contracts)
    lead.crow_caps = caps
    drafters = [lead]
    try:
        # shared by every candidate: the clauses and the obligations the drafts are judged on
        await lead.clauses_stage()
        if tactic is not None:
            from afsim_research_agent.falcon.v3.crow_bridge import is_core, map_text

            if simplified is not None:
                # the executable version names, per element, the actions it uses (checked
                # against the capability set when it was made): no second judgment of its text
                blocked = lead.adopt_map(simplified)
            else:
                blocked = await lead.map_stage()
            mode = request.missing_capabilities
            core = {cid: why for cid, why in blocked.items() if is_core(cid)}
            if core and mode == "block":
                lead.notes.append(
                    "能力集实现不了选定战术的核心要素，未起草："
                    + "；".join(f"{cid} {why}" for cid, why in core.items())
                )
                return _blocked_package(staging, target, request, lead)
            if mode != "wait":
                # left out, declared (deferred_capabilities.yaml, review), never imitated
                lead.clauses = [c for c in lead.clauses if c["id"] not in blocked]
            text = map_text(lead.clause_map, blocked if mode != "wait" else {})
            if text:
                request = request.model_copy(
                    update={"requirement": request.requirement + "\n\n" + text}
                )
                lead.r = request
        skeleton = request.drafting == "skeleton" and await lead.skeleton_stage()
        if skeleton:
            request = lead.r  # the skeleton may have added the advance route to the interface
        # the obligations are written once the structure has chosen the slices
        structure = None if skeleton else await lead.structure_stage()
        await lead.obligations_stage()
        for _ in range(0 if skeleton else request.candidates - 1):
            d = Drafter(request, contracts, run)
            d.clauses, d.obligations, d.notes = list(lead.clauses), list(lead.obligations), []
            d.tactic, d.crow_caps, d.simplified = tactic, caps, simplified
            d.clause_map, d.blocked = lead.clause_map, lead.blocked
            d.kit = kit
            drafters.append(d)
        outcomes = await asyncio.gather(
            _skeleton_draft(lead, run) if skeleton else _draft(lead, run, structure),
            *(_draft(d, run) for d in drafters[1:]),
        )
        ranked = sorted(range(len(drafters)), key=lambda i: _score(outcomes[i][0]))
        best = ranked[0]
        drafter, (check, fixes) = drafters[best], outcomes[best]
        if drafter is not lead:
            drafter.notes[:0] = bridge_notes
        if len(drafters) > 1:
            drafter.notes.append(
                f"候选 {len(drafters)} 份，选第 {best + 1} 份；得分（L1 未过、L1 问题数、"
                "L2/L3 未通过数，越小越好）："
                + "，".join(f"第 {i + 1} 份 {_score(o[0])}" for i, o in enumerate(outcomes))
            )
            for i, d in enumerate(drafters):
                if i != best:
                    (staging / f"candidate_{i + 1}.md").write_text(d.sheet, encoding="utf-8")
        history = None
        if afsim is not None and check.l1_ok and check.model is not None and not check.binding:
            check, history = await _afsim_rounds(drafter, run, check, fixes, afsim, staging)
            drafter.afsim_history = history
        ok = check.verified and (history is None or history.passed)
        status = "draft_valid" if ok else "draft_invalid"
        cover = coverage(drafter, check, history)
        if ok and any(not c["verified"] for c in cover.values()):
            # every check passed, but some tactic clause was never judged by any run
            status = UNVERIFIED
        if check.l1_ok and check.binding:
            # the model is fine, the scenario cannot give it what it names
            status = NEEDS_BINDING
        write_package(staging, request, drafter, check, status, fixes)
        choice = getattr(lead, "skeleton_choice", None)
        if choice is not None:
            from afsim_research_agent.falcon.v3.skeleton import describe

            (staging / "skeleton_choice.yaml").write_text(describe(choice), encoding="utf-8")
        if history is not None:
            (staging / "afsim_rounds.md").write_text(_afsim_report(history), encoding="utf-8")
    except BaseException:
        (staging / SHEET_FILE).write_text(lead.sheet, encoding="utf-8")
        failed = target.with_name(target.name + ".failed")
        if failed.exists():
            shutil.rmtree(failed)
        staging.rename(failed)
        raise
    staging.rename(target)
    return V3Result(target, status, check, drafter.rounds)


async def _afsim_rounds(
    drafter: Drafter,
    run: ModelRunner,
    check: Check,
    fixes: list[Any],
    cfg: AfsimConfig,
    staging: Path,
) -> tuple[Check, AfsimHistory]:
    """Run the chosen draft in AFSIM. On failure the repair loop gets the (sanitized)
    evidence and re-runs AFSIM itself (``run_afsim``) after its edits, at most ``cfg.rounds``
    times; the draft counts as repaired only when AFSIM passes on the final sheet."""
    import asyncio

    history = AfsimHistory()
    meanings = {
        aid: {"failure": a.result.get("failure"), "codes": a.result.get("codes") or {}}
        for aid, a in drafter.c.actions.items()
        if isinstance(a.result, dict) and (a.result.get("failure") or a.result.get("codes"))
    }

    async def probe(model: dict[str, Any]) -> tuple[bool, str, bool]:
        """One AFSIM run of the current sheet: (passed, feedback, infrastructure failure)."""
        index = len(history.rounds)
        report(f"AFSIM 第 {index + 1} 次运行：编译并运行……")
        out = staging / "afsim" / f"round_{index}"
        obligations = [o.model_dump(exclude_none=True) for o in drafter.obligations]
        results, runs, error = await asyncio.to_thread(
            run_once, model, drafter.c, obligations, cfg, out
        )
        passed = error is None and bool(results) and all(r.passed for r in results or [])
        missions = {rid: role.get("mission") for rid, role in (model.get("roles") or {}).items()}
        findings = (
            []
            if passed or error is not None
            else triage.from_afsim(
                results,
                runs,
                model,
                drafter.iface.optional_events() if drafter.iface is not None else None,
            )
        )
        text = "" if passed else feedback(results, runs, error, missions, meanings, findings)
        local = error or "\n".join(r.render() for r in results or [])
        judged = {
            str(v["id"]): bool(v["passed"])
            for r in results or []
            for v in r.obligations
            if v.get("judged", True)
        }
        history.rounds.append(
            AfsimRound(index, passed, text, local, out, judged, [f.to_dict() for f in findings])
        )
        drafter.notes.append(f"AFSIM 第 {index + 1} 次运行：{'通过' if passed else '未通过'}")
        report(f"AFSIM 第 {index + 1} 次运行：{'通过' if passed else '未通过'}")
        drafter.afsim_note = AfsimNote(sheet_hash(drafter.sheet), text, passed, findings)
        drafter.afsim_verdicts[drafter.afsim_note.sheet_hash] = drafter.afsim_note
        infra = any(getattr(r, "infrastructure", False) for r in results or [])
        # every finding is the scenario's, the judge's or the capability set's: editing the
        # sheet cannot repair it either
        outside = not passed and triage.only_outside_sheet(findings)
        return passed, text, infra or outside

    passed, _text, infra = await probe(check.model or {})
    if passed or cfg.rounds <= 0:
        if passed:
            drafter.afsim_note = None
            drafter.afsim_verdicts.clear()
        return drafter.check(verify=True), history
    if infra:
        # the binding, the compiler or the lab files failed: editing the sheet cannot
        # repair it, so no repair round is spent on it
        drafter.notes.append(
            "AFSIM 运行失败不是战术稿的问题（基础设施，或规则判定为想定/判定误报/能力缺失），"
            "不交给战术稿修复；见 afsim_rounds.md"
        )
        return drafter.check(verify=True), history
    drafter.afsim_probe = probe
    drafter.afsim_budget = cfg.rounds
    fixes.append(await fix_sheet(drafter, run, cfg.steps * cfg.rounds))
    check = drafter.check(verify=True)
    judged = sheet_hash(drafter.sheet) in drafter.afsim_verdicts
    if not judged and drafter.afsim_budget > 0 and check.l1_ok and check.model is not None:
        # the loop ended on a sheet AFSIM has not judged (e.g. the best version was
        # restored): one last run decides
        await probe(check.model)
        drafter.afsim_budget -= 1
        check = drafter.check(verify=True)
    note = drafter.afsim_verdicts.get(sheet_hash(drafter.sheet))
    if note is None or not note.passed:
        drafter.notes.append(
            f"AFSIM 纠错未修好：循环内复验 {len(history.rounds) - 1} 次，最终稿未通过 AFSIM"
        )
    drafter.afsim_probe = None
    return check, history


def _afsim_report(history: AfsimHistory) -> str:
    """Local record of the AFSIM rounds (full judgements; stays in the package)."""
    lines = ["# AFSIM 运行记录", ""]
    for r in history.rounds:
        lines += [
            f"## 第 {r.index + 1} 次：{'通过' if r.passed else '未通过'}",
            "",
            "```text",
            r.local,
            "```",
            "",
        ]
        if r.feedback:
            lines += ["交给修复循环的反馈：", "", "```text", r.feedback, "```", ""]
        if r.findings:
            lines += ["规则判定：", ""]
            lines += [f"- {f.get('text')}" for f in r.findings]
            lines.append("")
    return "\n".join(lines)
