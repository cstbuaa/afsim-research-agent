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
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from afsim_research_agent.falcon.llm import FalconModel, ModelRunner
from afsim_research_agent.falcon.models import FalconRequest, PlatformHint, TextAnswer
from afsim_research_agent.falcon.sheet.blocks import Block, SheetProblem, join_sheet
from afsim_research_agent.falcon.sheet.generate import merge
from afsim_research_agent.falcon.v3 import catalog as K
from afsim_research_agent.falcon.v3 import prompts as P
from afsim_research_agent.falcon.v3.compare import compare as compare_tactics
from afsim_research_agent.falcon.v3.compare import render as render_comparison
from afsim_research_agent.falcon.v3.compiler import Compiled, compile_sheet
from afsim_research_agent.falcon.v3.fixer import fix_sheet, render_fix
from afsim_research_agent.ir.diagnostics import Diagnostic
from afsim_research_agent.ir.v2 import Contracts, normalize
from afsim_research_agent.ir.v2.sim import run_l2, simulate

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
    requirement: str
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
    # external events the scenario raises: id → description (names line up with the run)
    scenario_events: dict[str, str] = Field(default_factory=dict)
    slices: list[str] | None = None  # override the slices the structure names
    capability_set: Path | None = None  # default: the configured capability set
    max_repair: int = Field(default=3, ge=0, le=6)
    fix_steps: int = Field(default=20, ge=0, le=60)  # self-checking repair loop budget

    @classmethod
    def load(cls, path: Path) -> V3Request:
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


@dataclass
class Check:
    compiled: Compiled
    model: dict[str, Any] | None
    diagnostics: list[Diagnostic]
    problems: list[SheetProblem]  # errors to repair, with block and line
    l2: list[dict[str, Any]] = field(default_factory=list)
    l3: list[dict[str, Any]] = field(default_factory=list)

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


@dataclass
class V3Result:
    package_dir: Path
    status: str
    check: Check
    rounds: list[dict[str, Any]]

    @property
    def exit_code(self) -> int:
        return 0 if self.status == "draft_valid" else 3


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

    # ------------------------------------------------------------------ helpers
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
        out = Check(compiled, model, diags, problems)
        if verify and out.l1_ok and model is not None:
            spec = compiled.spec or {}
            out.l2 = [
                {"id": c.id, "passed": c.passed, "detail": c.detail}
                for c in run_l2(model, self.c, spec.get("tests") or [])
            ]
            for raw in spec.get("scenarios") or []:
                res = simulate(model, self.c, raw)
                out.l3.append(
                    {
                        "id": res.scenario,
                        "passed": res.passed,
                        "render": res.render(),
                        "idealized": sorted(res.idealized),
                        "placeholders": sorted(res.placeholders),
                        "unresolved": sorted(res.unresolved),
                        "states": {k: [list(x) for x in v] for k, v in res.states.items()},
                        "transitions": res.transitions,
                        "duration_s": float(
                            ((raw.get("environment") or {}).get("duration_s")) or 0.0
                        ),
                    }
                )
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

    def _context(self, check: Check) -> str:
        parts = [f"当前战术稿：\n{self.sheet}"]
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
            mission_refs=[
                r
                for r, m in ((structure.compiled.spec or {}).get("missions") or {}).items()
                if "tactic" in m
            ],
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
        prompt = P.verification_prompt(self.r.requirement, self.sheet, self._summary(control))
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
        modelled = set(self.c.semantics.actions)
        bare = sorted(a for a in used if a not in modelled)
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
        lines.append(f"- L3 {s['id']}：{'通过' if s['passed'] else '未通过'}")
        lines += ["  " + x for x in s["render"].splitlines()[1:]]
    lines.append("")
    if check.model is not None:
        rows = compare_tactics(check.model, drafter.c)
        if rows:
            lines += [*render_comparison(rows), ""]
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
    if spec:
        (staging / "behavior_spec.yaml").write_text(
            yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8"
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
        "l2": check.l2,
        "l3": [
            {k: v for k, v in s.items() if k != "render"} | {"summary": s["render"]}
            for s in check.l3
        ],
        "gaps": sorted(gaps),
        "tactic_comparison": compare_tactics(check.model, drafter.c) if check.model else [],
    }
    (staging / "validation_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    review = _review(request, drafter, check, status)
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
    drafter = Drafter(request, contracts, ModelRunner(_NoModel()))
    drafter.blocks = split_sheet(sheet_path.read_text(encoding="utf-8"))[0]
    drafter.slices = drafter._slices(drafter.compile(pending=True))
    check = drafter.check(verify=True)
    status = "draft_valid" if check.verified else "draft_invalid"
    staging = out.with_name(out.name + ".partial")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    write_package(staging, request, drafter, check, status)
    staging.rename(out)
    return V3Result(out, status, check, [])


class _NoModel:
    name = "none"

    async def generate(self, stage: str, prompt: str, schema: Any) -> Any:  # pragma: no cover
        raise RuntimeError("compile3 does not call a model")


async def run_draft3(
    request_path: Path,
    model: FalconModel,
    *,
    out_dir: Path | None = None,
    allow_mission_ref: bool | None = None,
    fix_steps: int | None = None,
) -> V3Result:
    request = V3Request.load(request_path)
    if fix_steps is not None:
        request = request.model_copy(update={"fix_steps": fix_steps})
    if allow_mission_ref is not None:
        request = request.model_copy(update={"allow_mission_ref": allow_mission_ref})
    contracts = Contracts.load(request.capability_set)
    target = _target(request, request_path, out_dir)
    staging = target.with_name(target.name + ".partial")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    run = ModelRunner(model)
    drafter = Drafter(request, contracts, run)
    try:
        await drafter.clauses_stage()
        structure = await drafter.structure_stage()
        control = await drafter.control_stage(structure)
        budget = request.fix_steps
        fixes: list[Any] = []
        if not control.l1_ok and budget:
            fixes.append(await fix_sheet(drafter, run, max(1, budget // 2)))
            budget -= fixes[-1].steps
            control = drafter.check()
        check = await drafter.verification_stage(control) if control.l1_ok else control
        if not check.verified and budget > 0:
            fixes.append(await fix_sheet(drafter, run, budget))
            check = drafter.check(verify=True)
        status = "draft_valid" if check.verified else "draft_invalid"
        write_package(staging, request, drafter, check, status, fixes)
    except BaseException:
        (staging / SHEET_FILE).write_text(drafter.sheet, encoding="utf-8")
        failed = target.with_name(target.name + ".failed")
        if failed.exists():
            shutil.rmtree(failed)
        staging.rename(failed)
        raise
    staging.rename(target)
    return V3Result(target, status, check, drafter.rounds)
