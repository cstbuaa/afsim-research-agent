"""``falcon draft``: F0 → F6 for one request file.

Everything is written to a staging directory next to the target and renamed
into place at the end, so a refusal or a crash never leaves a half-written
model package behind.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

from afsim_research_agent.capability.catalog import CapabilityCatalog, CatalogError
from afsim_research_agent.capability.constraints import (
    VocabularyConstraints,
    vocabulary_constraints,
)
from afsim_research_agent.capability.granularity import CapabilityPolicy
from afsim_research_agent.capability.ir_check import (
    DECISIONS_FILE,
    GAPS_FILE,
    check_ir,
    decisions_template,
    render_section,
)
from afsim_research_agent.falcon.errors import EXIT_DRAFT_INVALID
from afsim_research_agent.falcon.evidence import normalize_evidence, write_package
from afsim_research_agent.falcon.fill import DraftState, fill_all
from afsim_research_agent.falcon.gate import run_gate
from afsim_research_agent.falcon.llm import FalconModel, ModelRunner
from afsim_research_agent.falcon.models import EvidencePackage, FalconTrace
from afsim_research_agent.falcon.prompts import PROMPT_VERSION
from afsim_research_agent.falcon.repair import DraftOutcome, draft_with_repair
from afsim_research_agent.falcon.requirements import decompose
from afsim_research_agent.falcon.review import (
    confirmation_items,
    render_review,
    write_confirmations,
    write_trace,
)
from afsim_research_agent.falcon.sheet.generate import (
    SheetContext,
    draft_sheet,
    render_sheet_section,
    sheet_summary,
    to_state,
)
from afsim_research_agent.falcon.skeleton import build_skeleton
from afsim_research_agent.falcon.tactic import CROW_GAPS_FILE, SELECTED_FILE
from afsim_research_agent.falcon.tactic import render_section as render_tactic
from afsim_research_agent.ir.catalog import load_catalog

FALCON_VERSION = "falcon-1.0.0"


def failure_path(target: Path) -> Path:
    return target.with_name(target.name + ".failure.json")


def write_failure(target: Path, run: ModelRunner, exc: BaseException) -> None:
    """Keep a small diagnostic record (no model output) when a draft aborts."""
    record = {
        "model_id": target.name,
        "error": f"{type(exc).__name__}: {str(exc)[:500]}",
        "last_stage": run.records[-1].stage if run.records else None,
        "calls": [r.model_dump(mode="json") for r in run.records],
    }
    try:
        failure_path(target).write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        pass


def _load_capability_catalog(
    value: CapabilityCatalog | Path | str | None | bool,
) -> tuple[CapabilityCatalog | None, str | None]:
    if value is False:
        return None, None
    if isinstance(value, CapabilityCatalog):
        return value, None
    try:
        return CapabilityCatalog.load(None if value in (None, True) else value), None
    except CatalogError as exc:
        return None, f"能力目录不可用，未按平台能力约束生成：{exc}"


@dataclass
class DraftResult:
    package_dir: Path
    status: str
    pending: int
    problems: list[str]

    @property
    def exit_code(self) -> int:
        return 0 if self.status == "draft_valid" else EXIT_DRAFT_INVALID


async def run_draft(
    request_path: Path,
    model: FalconModel,
    *,
    out_dir: Path | None = None,
    max_repair: int = 3,
    with_l3: bool = False,
    capability_catalog: CapabilityCatalog | Path | str | None | bool = None,
    generator: str | None = None,
    sheet_calls: int | None = None,
    tree_format: str | None = None,
) -> DraftResult:
    """``capability_catalog``: the platform capability catalog that constrains generation
    (K4) and checks the draft (K3); None = the default catalog, False = off."""
    gate, target = run_gate(request_path, out_dir)  # raises before anything is written
    original = gate.request
    tactic = gate.tactic
    request = original
    overrides: dict = {}
    if generator:
        overrides["generator"] = generator
    if sheet_calls:
        overrides["sheet_calls"] = sheet_calls
    if tree_format:
        overrides["sheet_tree_format"] = tree_format
    if overrides:
        request = original = original.model_copy(update=overrides)
    if tactic is not None:
        update: dict = {"requirement": tactic.requirement(original.requirement)}
        if not original.platforms:
            update["platforms"] = tactic.platforms()
        if "capability_refine" not in original.model_fields_set and tactic.refine():
            update["capability_refine"] = tactic.refine()
        request = original.model_copy(update=update)
    catalog = load_catalog()
    capability, capability_note = _load_capability_catalog(capability_catalog)
    policy = CapabilityPolicy.make(request.capability_granularity, request.capability_refine)
    run = ModelRunner(model)
    staging = target.with_name(target.name + ".partial")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        snapshot = original.model_dump(mode="json", exclude_none=True)
        (staging / "falcon_request.yaml").write_text(
            yaml.safe_dump(snapshot, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        tactic_notes: list[str] = []
        crow_package = tactic.evidence_package(gate.request_dir) if tactic is not None else None
        if crow_package is not None:
            package = EvidencePackage.model_validate_json(crow_package.read_text(encoding="utf-8"))
            tactic_notes.append(
                f"复用 Crow 证据包 `{crow_package.name}`（{package.package_id}），"
                "战术卡引用的证据 ID 可直接作为依据"
            )
        else:
            package = await normalize_evidence(gate.crow, request.requirement, run)
            if tactic is not None:
                tactic_notes.append(
                    "未找到 Crow 证据包，已重新规范化证据；战术卡的证据引用无法沿用"
                )
        write_package(package, staging / "evidence_package.json")

        clauses, user_values, notes = await decompose(request, run)
        tactic_block = ""
        if tactic is not None:
            clauses += tactic.clauses({e.evidence_id for e in package.evidence})
            tactic_block = tactic.prompt_block()
            (staging / SELECTED_FILE).write_text(tactic.selected_document(), encoding="utf-8")
            gaps_doc = tactic.crow_gaps_document()
            if gaps_doc is not None:
                (staging / CROW_GAPS_FILE).write_text(gaps_doc, encoding="utf-8")
        notes = tactic_notes + notes
        if capability_note:
            notes.append(capability_note)

        def make_constraints(classes: set[str]) -> VocabularyConstraints | None:
            if capability is None:
                return None
            constraints = vocabulary_constraints(catalog, capability, classes, policy=policy)
            scope = "粗粒度" if policy.coarse else "细粒度"
            if policy.refine:
                scope += f"，细化 {', '.join(sorted(policy.refine))}"
            notes.append(
                f"按平台能力约束生成（{', '.join(constraints.platform_classes)}；{scope}）："
                f"需确认 {len(constraints.names('confirm'))} 项，"
                f"不具备未列出 {len(constraints.names('unavailable'))} 项，"
                f"细节项未列出 {len(constraints.names('hidden'))} 项"
            )
            return constraints

        sheet_info = None
        if request.generator == "v2":
            classes = {p.capability_class for p in request.platforms} or {"ground_vehicle"}
            constraints = make_constraints(classes)
            ctx = SheetContext(
                request=request,
                clauses=clauses,
                user_values=user_values,
                package=package,
                catalog=catalog,
                constraints=constraints,
                tactic_block=tactic_block,
                capabilities=gate.engine_capabilities,
                known_gaps=tactic.crow_gaps() if tactic is not None else None,
            )
            sheet = await draft_sheet(ctx, run, staging, max_repair=max_repair, with_l3=with_l3)
            skeleton = sheet.skeleton
            state = to_state(ctx, sheet.compiled)
            outcome = DraftOutcome(sheet.assembled, sheet.check, sheet.notes, sheet.rounds)
            covered = {c for refs in sheet.assembled.implements.values() for c in refs}
            covered |= {c for n in sheet.compiled.needs for c in n["clauses"]}
            excused = {o.clause_id for o in skeleton.out_of_scope if o.reason.strip()}
            uncovered = [
                c.id
                for c in clauses
                if c.kind != "out_of_scope" and c.id not in covered and c.id not in excused
            ]
            sheet_info = sheet_summary(sheet)
            notes += [f"战术稿未解决：{line[2:]}" for line in sheet_info["holes"][:20]]
        else:
            skeleton, uncovered, more = await build_skeleton(
                request, clauses, package, catalog, gate.engine_capabilities, run, tactic_block
            )
            notes += more
            classes = {p.capability_class for p in request.platforms} | {
                r.capability_class for r in skeleton.roles
            }
            constraints = make_constraints(classes)
            state = DraftState(
                request=request,
                clauses=clauses,
                user_values=user_values,
                package=package,
                skeleton=skeleton,
                catalog=catalog,
                capabilities=gate.engine_capabilities,
                constraints=constraints,
                tactic_block=tactic_block,
            )
            await fill_all(state, run)
            outcome = await draft_with_repair(
                state, staging, run, max_rounds=max_repair, with_l3=with_l3
            )
        ok = outcome.check.ok and not (sheet_info and sheet_info["holes"])
        status = "draft_valid" if ok else "draft_invalid"
        problems = (
            sheet.problems
            if sheet_info is not None
            else [p.render() for p in outcome.check.problems]
        )

        trace = FalconTrace(
            model_id=request.model_id,
            falcon_version=FALCON_VERSION,
            prompt_version=PROMPT_VERSION,
            catalog_versions=dict(catalog.versions),
            status=status,
            clauses=clauses,
            user_values=user_values,
            implements=outcome.assembled.implements,
            uncovered_clauses=uncovered,
            out_of_scope=skeleton.out_of_scope,
            enforcement=outcome.enforcement,
            repairs=outcome.rounds,
            calls=run.records,
            skeleton=skeleton.model_dump(mode="json"),
            sections={
                k: v.model_dump(mode="json", by_alias=True) for k, v in state.sections.items()
            },
            capability_constraints=constraints.summary() if constraints else None,
            tactic=tactic.summary() if tactic is not None else None,
            generator=request.generator,
            sheet=sheet_info,
        )
        capability_check = None
        if capability is not None and outcome.check.ir is not None:
            capability_check = check_ir(
                outcome.check.ir,
                capability,
                policy=policy,
                crow_gaps=tactic.crow_gaps() if tactic is not None else None,
                crow_source=tactic.source_label if tactic is not None else None,
                sheet_needs=sheet.compiled.needs if sheet_info is not None else None,
            )
            (staging / GAPS_FILE).write_text(capability_check.to_yaml(), encoding="utf-8")
        if capability is not None:
            (staging / DECISIONS_FILE).write_text(decisions_template(policy), encoding="utf-8")
        trace_notes = [*notes, *(tactic.notes if tactic is not None else [])]
        items = confirmation_items(outcome.assembled.data, package)
        write_confirmations(staging / "confirmations.yaml", items)
        review = render_review(
            trace,
            outcome.assembled.data,
            package,
            items,
            problems,
            outcome.check.l2,
            outcome.check.l3,
            tactic_lines=render_tactic(tactic) if tactic is not None else None,
        )
        if capability_check is not None:
            review += "\n" + "\n".join(render_section(capability_check, 8))
        if sheet_info is not None:
            review += "\n" + "\n".join(render_sheet_section(sheet_info))
        if trace_notes:
            review += "\n## 附：生成过程记录\n\n" + "\n".join(f"- {n}" for n in trace_notes) + "\n"
        (staging / "falcon_review.md").write_text(review, encoding="utf-8")
        write_trace(staging / "falcon_trace.json", trace)
        report = {
            "stage": "falcon_draft",
            "status": status,
            "diagnostics": [d.model_dump(mode="json") for d in outcome.check.diagnostics],
            "problems": problems,
            "l2": outcome.check.l2,
            "l3_reference": outcome.check.l3,
            "capability": capability_check.summary() if capability_check else None,
        }
        (staging / "validation_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except BaseException as exc:
        shutil.rmtree(staging, ignore_errors=True)
        write_failure(target, run, exc)
        raise
    staging.rename(target)
    return DraftResult(target, status, len(items), problems)
