"""v2 drafting: three model calls write the tactic sheet; the compiler, the shared
assembly/enforcement and the IR check run after each; problems go back to the model
one block at a time (design §3.4, §4)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from afsim_research_agent.capability.constraints import VocabularyConstraints
from afsim_research_agent.capability.ir_check import NEEDS_FILE
from afsim_research_agent.falcon.assemble import Assembled, assemble, generated_ids
from afsim_research_agent.falcon.errors import FalconError
from afsim_research_agent.falcon.fill import OBSERVABLES, DraftState
from afsim_research_agent.falcon.llm import ModelRunner
from afsim_research_agent.falcon.models import (
    EnforcementNote,
    EvidencePackage,
    FalconRequest,
    RepairRound,
    RequirementClause,
    SkeletonDraft,
    TextAnswer,
    UserValueRecord,
)
from afsim_research_agent.falcon.repair import CheckResult, check_package, write_draft
from afsim_research_agent.falcon.sheet import prompts as P
from afsim_research_agent.falcon.sheet.blocks import Block, join_sheet, split_sheet
from afsim_research_agent.falcon.sheet.compiler import CompileResult, compile_sheet
from afsim_research_agent.falcon.sheet.locate import locate
from afsim_research_agent.falcon.skeleton import clause_data, compact_evidence
from afsim_research_agent.ir.catalog import Catalog
from afsim_research_agent.ir.validate import _vocabulary_concepts

SHEET_FILE = "tactic_sheet.md"
SOURCE_MAP_FILE = "source_map.json"
OBSERVED_FILE = "observed_expectations.yaml"  # recorded L2 results, reviewed by a person
OBSERVED_KEYS = ("tree_status", "issued_actions", "sent_messages")
STAGE_KINDS = {
    "structure": ("model", "mermaid"),
    "control": ("conditions", "xml", "pytree", "basis", "needs"),
    "verification": ("verification",),
}
EARLY_REPAIRS = 2  # repair rounds after the structure and control drafts
CONTROL_KINDS = ("model", "mermaid", "conditions", "xml", "pytree", "basis", "needs")
BLOCK_KINDS = (*CONTROL_KINDS, "verification")

STRUCTURE_EXAMPLE = """```yaml model
model: {display_name: 双机巡逻, intent: patrol, concepts: []}
roles:
  lead: {count: 1, platform: air_vehicle, comms: {datalink: data_link}, sensors: {radar: radar_search}}
  wing: {count: 1, platform: air_vehicle, superior: lead, comms: {datalink: data_link}}
parameters:
  pair_spacing: {value: 1 km, range: [300 m, 5 km], basis: inferred, evidence: [ev_0000000000], rationale: 资料给出松散编队间隔}
  detect_range: {value: 40 km, basis: unknown, rationale: 资料未给出，按常见雷达距离取参考值}
formations:
  pair: {slots: {"wing[0]": {forward: -1 * pair_spacing, right: pair_spacing}}, tolerance: pair_spacing, basis: inferred, evidence: [ev_0000000000], rationale: 同上}
coordination:
  follow: {pattern: leader_follower, leader: lead, followers: wing, formations: {patrol: pair, intercept: pair}, basis: unknown}
implements:
  rq_01: [pair, follow]
  rq_02: ["lead: patrol -> intercept"]
```

```mermaid lead
stateDiagram-v2
  [*] --> patrol
  patrol --> intercept : target_detected [sync]
  intercept --> patrol : target_lost [sync]
```

```mermaid wing
stateDiagram-v2
  [*] --> patrol
  intercept
```"""


@dataclass
class SheetContext:
    request: FalconRequest
    clauses: list[RequirementClause]
    user_values: list[UserValueRecord]
    package: EvidencePackage
    catalog: Catalog
    constraints: VocabularyConstraints | None = None
    tactic_block: str = ""
    capabilities: set[str] | None = None
    known_gaps: list[dict[str, Any]] | None = None  # Crow gaps of the selected tactic
    # test id → recorded mechanical results (tree status, actions, messages)
    observed: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def tree_format(self) -> str:
        return self.request.sheet_tree_format


@dataclass
class SheetOutcome:
    blocks: list[Block]
    compiled: CompileResult
    assembled: Assembled
    check: CheckResult
    notes: list[EnforcementNote]
    rounds: list[RepairRound] = field(default_factory=list)
    text: str = ""
    problems: list[str] = field(default_factory=list)  # rendered with block and line

    @property
    def skeleton(self) -> SkeletonDraft:
        assert self.compiled.skeleton is not None
        return self.compiled.skeleton


# ---------------------------------------------------------------------- helpers
def merge(
    blocks: list[Block], answer: str, kinds: tuple[str, ...], target: Block | None = None
) -> list[str]:
    """Put the blocks of ``answer`` into ``blocks`` (same key replaces). Returns notes."""
    found, problems = split_sheet(answer)
    notes = [p.render() for p in problems if p.severity == "error"]
    if target is not None:
        match = [b for b in found if b.kind == target.kind and (b.role in (None, target.role))]
        if not match:
            fence = re.search(r"```[^\n]*\n(.*?)```", answer, re.S)
            if fence is None:
                return [f"修复回答中没有 {target.label} 代码块"]
            match = [Block(target.kind, target.role, fence.group(1).rstrip(), target.line)]
        new = match[0]
        new.role = target.role
        new.kind = target.kind
        blocks[:] = [b for b in blocks if b.key != target.key] + [new]
        return notes
    for block in found:
        if block.kind not in kinds:
            continue
        blocks[:] = [b for b in blocks if b.key != block.key] + [block]
    return notes


def compile_blocks(ctx: SheetContext, blocks: list[Block], stage: str) -> tuple[str, CompileResult]:
    text = join_sheet(blocks, ctx.request.display_name or ctx.request.model_id)
    return text, compile_sheet(
        text,
        request=ctx.request,
        clauses=ctx.clauses,
        catalog=ctx.catalog,
        stage=stage,
        constraints=ctx.constraints,
        known_gaps=ctx.known_gaps,
        observed=ctx.observed,
    )


def to_state(ctx: SheetContext, compiled: CompileResult) -> DraftState:
    if compiled.skeleton is None:
        raise FalconError(
            "FAL004", "tactic sheet defines no roles", [p.render() for p in compiled.errors[:10]]
        )
    return DraftState(
        request=ctx.request,
        clauses=ctx.clauses,
        user_values=ctx.user_values,
        package=ctx.package,
        skeleton=compiled.skeleton,
        catalog=ctx.catalog,
        capabilities=ctx.capabilities,
        constraints=ctx.constraints,
        sections=dict(compiled.sections),
    )


def usage(ctx: SheetContext, compiled: CompileResult) -> dict[str, str] | None:
    if compiled.skeleton is None:
        return None
    data = assemble(ctx.request, compiled.skeleton, compiled.sections, ctx.package).data
    try:
        generated = generated_ids(data, ctx.catalog) or {}
    except Exception:  # noqa: BLE001 - partial sheets may not expand; usage is a hint
        return None
    return generated.get("usage")


def guide_for(kind: str, ctx: SheetContext, symbols: dict[str, Any] | None = None) -> str:
    if kind in ("model", "mermaid"):
        return (
            P.STRUCTURE_GUIDE.format(role_fields=P.ROLE_FIELDS)
            + "\n协同模式目录：\n"
            + P.patterns(ctx.catalog)
        )
    if kind == "verification":
        return P.verification_guide(
            OBSERVABLES, P.used_patterns(symbols) if symbols else None
        ) + P.transition_checklist(symbols)
    # a repair keeps the block's own form; drafting uses the requested one
    tree_format = {"xml": "xml", "pytree": "python"}.get(kind, ctx.tree_format)
    text = P.control_guide(tree_format, P.used_patterns(symbols) if symbols else None).format(
        namespaces=P.namespaces(ctx.catalog, ctx.constraints),
        functions=P.functions(ctx.catalog, ctx.constraints),
    )
    return (
        text
        + "\n动作节点（完整动作表；表外节点会被编译器拒绝）：\n"
        + P.action_table(ctx.catalog, ctx.constraints, tree_format)
        + P.known_gaps_block(ctx.known_gaps, ctx.constraints)
    )


def _fired_hint(message: str, test_id: str, symbols: dict[str, Any]) -> str:
    """A test expected one transition and another fired: say what the fired one reads and
    which of the test's inputs no condition reads (the usual sign of a wrong test intent,
    or of a condition that forgot an input)."""
    match = re.search(r"got '([\w]+)'", message.split("fired_transition", 1)[-1])
    if not match:
        return ""
    fired = next(
        (t for t in symbols.get("transition_checklist") or [] if t["id"] == match.group(1)),
        None,
    )
    if fired is None:
        return ""
    reads = fired.get("inputs") or []
    unread = [
        g
        for g in (symbols.get("test_given") or {}).get(test_id, [])
        if not any(g.startswith(r) or r.startswith(g) for r in reads)
    ]
    text = f"（实际触发了 {fired['transition']}，其条件 {fired['when']}"
    text += f" 只读取 {', '.join(reads)}" if reads else ""
    if unread:
        text += (
            f"；测试设定的 {', '.join(unread)} 不被这个条件读取。若意图是这些输入阻止该转换，"
            "应在条件或行为树里读取它们；否则是测试意图写错，改 expect 或 given"
        )
    return text + "）"


def _problem_lines(
    compiled: CompileResult, check: CheckResult | None, data: dict[str, Any] | None, keys: list[str]
) -> dict[str, list[str]]:
    """Problems per block key, rendered with sheet lines."""
    out: dict[str, list[str]] = {}
    for p in compiled.errors:
        block = p.block if p.block in keys else _kind_key(p.block, keys)
        out.setdefault(block, []).append(p.render())
    if check is not None and data is not None:
        failed = set(compiled.symbols.get("failed_trees", []))
        real = [
            p
            for p in check.problems
            if not (p.path == "$" and len(check.problems) > 1)
            and not any(f"tree '{t}'" in p.message for t in failed)  # compile holes already say so
        ]
        inputs = compiled.symbols.get("test_inputs") or {}
        untested = set(compiled.symbols.get("untested") or [])
        # the compiler already lists transitions no test names; L2 says the same again
        real = [
            p
            for p in real
            if not (
                "not exercised by any L2 test" in p.message
                and p.path.rsplit(".", 1)[-1] in untested
            )
        ]
        for p in real:
            block, line = locate(p.path, p.section, data, compiled.source_map, keys, p.message)
            where = f"第 {line} 行" if line else "整块"
            hint = f"（建议：{p.suggestion}）" if p.suggestion else ""
            test_id = p.path.split(".", 1)[1] if p.path.startswith("tests.") else ""
            if p.code == "TEST001" and inputs.get(test_id):
                hint += (
                    f"（期望转换的条件读取 {', '.join(inputs[test_id])}，该测试没有设定，取默认值）"
                )
            if p.code == "TEST001":
                hint += _fired_hint(p.message, test_id, compiled.symbols)
            out.setdefault(block, []).append(
                f"- [{block} {where}] {p.code} {p.path}: {p.message}{hint}"
            )
    # the same failure reported twice (once with the actual result appended): keep the longer
    for block, lines in out.items():
        best: dict[str, str] = {}
        for line in lines:
            key = re.split(r"；实际结果|（期望转换|（实际触发", line)[0]
            if len(line) > len(best.get(key, "")):
                best[key] = line
        out[block] = list(best.values())
    return out


def _kind_key(name: str, keys: list[str]) -> str:
    """A problem filed under a block kind ("conditions", "xml") → an existing key."""
    if name in keys:
        return name
    match = [k for k in keys if k.split(":")[0] == name]
    if not match and name in ("xml", "pytree"):
        match = [k for k in keys if k.split(":")[0] in ("xml", "pytree")]
    return match[0] if match else name


def render_problems(
    compiled: CompileResult, check: CheckResult | None, data: dict[str, Any] | None
) -> list[str]:
    keys = [b.key for b in compiled.blocks]
    return [
        line for lines in _problem_lines(compiled, check, data, keys).values() for line in lines
    ]


def _stage(check: CheckResult | None) -> int:
    """How far the checks got: 0 = L2 ran, 1 = loaded but stopped at L1, 2 = not loaded.

    Later stages report problems the earlier ones hide, so a draft that gets further is
    better even when it shows more problems.
    """
    if check is None or not check.loaded:
        return 2
    return 0 if check.l2 is not None else 1


def _score(compiled: CompileResult, check: CheckResult | None) -> tuple[int, int]:
    count = len(compiled.errors) + (len(check.problems) if check is not None else 0)
    return (_stage(check), count)


# ---------------------------------------------------------------------- drafting
async def _repair_blocks(
    ctx: SheetContext,
    blocks: list[Block],
    problems: dict[str, list[str]],
    run: ModelRunner,
    stage: str,
    compiled: CompileResult,
    kinds: tuple[str, ...] | None = None,
    reverted: dict[str, list[str]] | None = None,
    stuck: set[str] | None = None,
) -> list[str]:
    notes: list[str] = []
    text = join_sheet(blocks, ctx.request.display_name or ctx.request.model_id)
    by_key = {b.key: b for b in blocks}
    hint = usage(ctx, compiled)
    for key, lines in problems.items():
        block = by_key.get(key)
        if block is None:
            kind, _, role = key.partition(":")
            if kind not in BLOCK_KINDS:
                continue
            if kind in ("xml", "pytree"):  # a missing tree block: write it in the asked form
                kind = "pytree" if ctx.tree_format == "python" else "xml"
            block = Block(kind, role or None, "", 0)
        if kinds is not None and block.kind not in kinds:
            continue
        prompt = P.repair_prompt(
            label=block.label,
            numbered=block.numbered() or "（这一块还没有内容，请按说明补写）",
            problems=lines,
            sheet=text,
            symbols=compiled.symbols,
            usage=hint,
            guide=guide_for(block.kind, ctx, compiled.symbols),
            reverted=(reverted or {}).get(key),
            stuck=key in (stuck or set()),
        )
        answer = await run(f"sheet:repair:{block.kind}", prompt, TextAnswer)
        notes += merge(blocks, answer.text, (block.kind,), target=block)
    return notes


async def _early(
    ctx: SheetContext, blocks: list[Block], run: ModelRunner, stage: str, kinds: tuple[str, ...]
) -> CompileResult:
    _, compiled = compile_blocks(ctx, blocks, stage)
    previous: dict[str, list[str]] = {}
    for _ in range(EARLY_REPAIRS):
        keys = [b.key for b in blocks]
        problems = _problem_lines(compiled, None, None, keys)
        problems = {k: v for k, v in problems.items() if k.split(":")[0] in kinds}
        if not problems:
            break
        stuck = {k for k, v in problems.items() if previous.get(k) == v}
        previous = problems
        await _repair_blocks(ctx, blocks, problems, run, stage, compiled, kinds, stuck=stuck)
        _, compiled = compile_blocks(ctx, blocks, stage)
    return compiled


async def draft_sheet(
    ctx: SheetContext,
    run: ModelRunner,
    package_dir: Path,
    *,
    max_repair: int = 3,
    with_l3: bool = False,
) -> SheetOutcome:
    request = ctx.request
    clauses = clause_data(ctx.clauses, {"id", "text", "kind", "roles"})
    claims = compact_evidence(ctx.package, limit=60)
    terrain = request.terrain_classes or list(ctx.catalog.vocabularies["terrain_classes"])
    structure = P.structure_prompt(
        requirement=request.requirement,
        clauses=clauses,
        tactic=ctx.tactic_block,
        claims=claims,
        platforms=[p.model_dump() for p in request.platforms],
        catalog=ctx.catalog,
        intents=list(ctx.catalog.vocabularies["intents"]),
        concepts=sorted(_vocabulary_concepts()),
        terrain=terrain,
        example=STRUCTURE_EXAMPLE,
    )
    blocks: list[Block] = []
    if request.sheet_calls == 1:
        prompt = P.one_call_prompt(
            structure=structure,
            control_guide=guide_for("trees", ctx),
            verification_guide=guide_for("verification", ctx),
        )
        answer = await run("sheet:full", prompt, TextAnswer)
        merge(blocks, answer.text, tuple(k for kinds in STAGE_KINDS.values() for k in kinds))
        compiled = await _early(ctx, blocks, run, "full", CONTROL_KINDS)
    else:
        answer = await run("sheet:structure", structure, TextAnswer)
        merge(blocks, answer.text, STAGE_KINDS["structure"])
        compiled = await _early(ctx, blocks, run, "structure", STAGE_KINDS["structure"])
        text = join_sheet(blocks, request.display_name or request.model_id)
        control = P.control_prompt(
            sheet=text,
            symbols=compiled.symbols,
            usage=usage(ctx, compiled),
            catalog=ctx.catalog,
            constraints=ctx.constraints,
            claims=claims,
            tactic=ctx.tactic_block,
            tree_format=ctx.tree_format,
            known_gaps=ctx.known_gaps,
        )
        answer = await run("sheet:control", control, TextAnswer)
        merge(blocks, answer.text, STAGE_KINDS["control"])
        compiled = await _early(ctx, blocks, run, "control", CONTROL_KINDS)
        text = join_sheet(blocks, request.display_name or request.model_id)
        verification = P.verification_prompt(
            sheet=text,
            symbols=compiled.symbols,
            usage=usage(ctx, compiled),
            observables=OBSERVABLES,
        )
        answer = await run("sheet:verification", verification, TextAnswer)
        merge(blocks, answer.text, STAGE_KINDS["verification"])

    # full compile → shared assembly / enforcement → IR check → block repair
    text, compiled = compile_blocks(ctx, blocks, "full")
    state = to_state(ctx, compiled)
    assembled, notes = write_draft(state, package_dir)
    check = check_package(package_dir, assembled.problems, with_l3=with_l3)
    rounds: list[RepairRound] = []
    best = (
        _score(compiled, check),
        0,
        [Block(b.kind, b.role, b.text, b.line, b.info) for b in blocks],
    )
    reverted: dict[str, list[str]] = {}  # block → problems its undone rewrite had
    previous: dict[str, list[str]] = {}  # block → its problems when last sent for repair
    for round_no in range(1, max_repair + 1):
        keys = [b.key for b in blocks]
        problems = _problem_lines(compiled, check, assembled.data, keys)
        if not problems:
            break
        last = round_no == max_repair
        if len(problems) > 1 and "verification" in problems and not last:
            del problems["verification"]  # fix the model before its tests (as in v1)
        codes = sorted({p.code for p in check.problems} | ({"SHEET"} if compiled.errors else set()))
        rounds.append(
            RepairRound(
                round=round_no,
                errors=_score(compiled, check)[1],
                sections=sorted(problems),
                codes=codes,
                loaded=check.loaded,
            )
        )
        before = {b.key: Block(b.kind, b.role, b.text, b.line, b.info) for b in blocks}
        stage_before = _stage(check)
        stuck = {k for k, v in problems.items() if previous.get(k) == v}
        previous = dict(problems)
        await _repair_blocks(
            ctx, blocks, problems, run, "full", compiled, reverted=reverted, stuck=stuck
        )
        reverted = {}
        text, compiled = compile_blocks(ctx, blocks, "full")
        state = to_state(ctx, compiled)
        assembled, notes = write_draft(state, package_dir)
        check = check_package(package_dir, assembled.problems, with_l3=with_l3)
        # a rewrite that made its own block worse is undone (the others are kept) — unless
        # the checks got further: then the new problems were hidden before, not caused
        after = _problem_lines(compiled, check, assembled.data, [b.key for b in blocks])
        worse = []
        if _stage(check) >= stage_before:
            worse = [
                k for k in problems if k in before and len(after.get(k, [])) > len(problems[k])
            ]
        if worse:
            reverted = {k: after.get(k, []) for k in worse}
            blocks[:] = [before[b.key] if b.key in worse else b for b in blocks]
            text, compiled = compile_blocks(ctx, blocks, "full")
            state = to_state(ctx, compiled)
            assembled, notes = write_draft(state, package_dir)
            check = check_package(package_dir, assembled.problems, with_l3=with_l3)
            rounds[-1].codes = [*rounds[-1].codes, f"reverted:{','.join(worse)}"]
        score = _score(compiled, check)
        if score < best[0]:
            best = (
                score,
                round_no,
                [Block(b.kind, b.role, b.text, b.line, b.info) for b in blocks],
            )
    if _score(compiled, check) > best[0]:
        errors_now = _score(compiled, check)[1]
        blocks = best[2]
        text, compiled = compile_blocks(ctx, blocks, "full")
        state = to_state(ctx, compiled)
        assembled, notes = write_draft(state, package_dir)
        check = check_package(package_dir, assembled.problems, with_l3=with_l3)
        label = "首稿" if best[1] == 0 else f"第 {best[1]} 轮修复后"
        notes.append(
            EnforcementNote(
                path="$",
                rule="repair_rollback",
                detail=f"最后一轮后有 {errors_now} 个问题，已回退到{label}的战术稿（{_score(compiled, check)[1]} 个问题）",
            )
        )
    if check.l2 is not None:
        recorded = record_observed(ctx.observed, check.l2)
        if recorded != ctx.observed:
            ctx.observed = recorded
            text, compiled = compile_blocks(ctx, blocks, "full")
            state = to_state(ctx, compiled)
            assembled, notes = write_draft(state, package_dir)
            check = check_package(package_dir, assembled.problems, with_l3=with_l3)
    (package_dir / SHEET_FILE).write_text(text, encoding="utf-8")
    write_sheet_files(package_dir, compiled, ctx.observed)
    return SheetOutcome(
        blocks=blocks,
        compiled=compiled,
        assembled=assembled,
        check=check,
        notes=notes,
        rounds=rounds,
        text=text,
        problems=render_problems(compiled, check, assembled.data),
    )


def needs_document(compiled: CompileResult) -> str | None:
    """``capability_needs.yaml``: the sheet's ``needs`` for the IR capability check."""
    if not compiled.needs:
        return None
    doc = {
        "format": "SheetCapabilityNeeds",
        "source": SHEET_FILE,
        "note": "战术稿 yaml needs 登记的词表外能力；ir build 把它们列为能力缺口",
        "needs": compiled.needs,
    }
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=100)


def record_observed(
    existing: dict[str, dict[str, Any]], l2: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Record the mechanical results of every test whose intent passed and that has no
    recorded entry yet (entries already there, possibly edited by a person, are kept)."""
    out = {k: dict(v) for k, v in existing.items()}
    for t in l2.get("tests") or []:
        if t["id"] in out or not t.get("passed"):
            continue
        result = t.get("result") or {}
        out[t["id"]] = {k: result.get(k) for k in OBSERVED_KEYS}
    return out


def load_observed(package_dir: Path) -> dict[str, dict[str, Any]]:
    path = package_dir / OBSERVED_FILE
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    tests = data.get("tests") or {}
    return {str(k): dict(v) for k, v in tests.items() if isinstance(v, dict)}


def observed_document(observed: dict[str, dict[str, Any]]) -> str:
    doc = {
        "format": "ObservedTestExpectations",
        "note": (
            "单步测试的机械结果（树状态、发出的动作与消息），由首次运行的实际结果记录；"
            "战术稿只写意图（下一状态、转换、黑板）。请人工核对，需要时直接修改，"
            "falcon compile 会按这里的值严格比较；删去某条则下次重新记录"
        ),
        "tests": observed,
    }
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=100)


def write_sheet_files(
    package_dir: Path, compiled: CompileResult, observed: dict[str, dict[str, Any]] | None = None
) -> None:
    (package_dir / SOURCE_MAP_FILE).write_text(
        json.dumps(compiled.source_map, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    if observed:
        (package_dir / OBSERVED_FILE).write_text(observed_document(observed), encoding="utf-8")
    needs = needs_document(compiled)
    path = package_dir / NEEDS_FILE
    if needs is not None:
        path.write_text(needs, encoding="utf-8")
    elif path.exists():
        path.unlink()


def sheet_summary(outcome: SheetOutcome) -> dict[str, Any]:
    return {
        "prompt_version": P.SHEET_PROMPT_VERSION,
        "blocks": [b.label for b in outcome.blocks],
        "holes": [p.render() for p in outcome.compiled.errors],
        "warnings": [p.render() for p in outcome.compiled.warnings],
        "needs": outcome.compiled.needs,
        "observed": observed_rows(outcome.compiled),
    }


def observed_rows(compiled: CompileResult) -> list[dict[str, Any]]:
    """Per test: the recorded results, and where the sheet's own guess differed."""
    written = compiled.symbols.get("written_mechanics") or {}
    rows = []
    for test in compiled.sections["verification"].tests:  # type: ignore[attr-defined]
        expect = test.expect.model_dump(exclude_none=True)
        if not any(k in expect for k in OBSERVED_KEYS):
            continue
        guess = written.get(test.id) or {}
        differs = [k for k in OBSERVED_KEYS if k in guess and guess[k] != expect.get(k)]
        rows.append(
            {
                "id": test.id,
                "next_state": expect.get("next_state"),
                **{k: expect.get(k) for k in OBSERVED_KEYS},
                "differs": differs,
            }
        )
    return rows


def render_sheet_section(info: dict[str, Any]) -> list[str]:
    lines = [
        "## 9. 战术稿（Falcon v2）",
        "",
        f"模型写的是 `{SHEET_FILE}`（{len(info['blocks'])} 个代码块：{'、'.join(info['blocks'])}），"
        f"由编译器生成 `behavior_spec.yaml`；`{SOURCE_MAP_FILE}` 记录每个 IR 元素来自哪一块哪一行。",
        "人工修改时改战术稿，然后运行 `python -m afsim_research_agent.falcon compile <模型包>` 重新生成 IR。",
        "",
    ]
    if info["holes"]:
        lines += ["未解决的空洞（编译时被跳过的内容）：", "", *info["holes"], ""]
    if info.get("needs"):
        lines += [
            f"词表外能力需求（`{NEEDS_FILE}`，在第 8 节列为能力缺口）：",
            "",
            "| 需求 | 缺口编号 | 使用位置 | 替代写法 |",
            "| --- | --- | --- | --- |",
        ]
        for n in info["needs"]:
            lines.append(
                f"| {n['need']} | {n['gap_id']} | {', '.join(n['used_by'])} | {n['substitute'] or '—'} |"
            )
        lines.append("")
    rows = info.get("observed") or []
    if rows:
        names = {"tree_status": "树状态", "issued_actions": "动作", "sent_messages": "消息"}
        lines += [
            f"单步测试的机械结果（`{OBSERVED_FILE}`，由首次运行记录，请核对；战术稿只写意图）：",
            "",
            "| 测试 | 下一状态 | 树状态 | 动作 | 消息 | 模型原先的预期不同 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for r in rows:
            lines.append(
                f"| {r['id']} | {r['next_state'] or '—'} | {r['tree_status'] or '—'} | "
                f"{', '.join(r['issued_actions'] or []) or '—'} | "
                f"{', '.join(m.replace('__', '.') for m in r['sent_messages'] or []) or '—'} | "
                f"{'、'.join(names[k] for k in r['differs']) or '—'} |"
            )
        lines.append("")
    if info["warnings"]:
        lines += ["编译提示：", "", *info["warnings"], ""]
    return lines
