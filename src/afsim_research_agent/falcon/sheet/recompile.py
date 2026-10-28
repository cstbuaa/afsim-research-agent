"""``falcon compile <package>``: rebuild ``behavior_spec.yaml`` from an edited tactic sheet.

No model is called. The requirement clauses and user values come from
``falcon_trace.json``, evidence from the package. ``confirmations.yaml`` is rewritten
for the new draft; decisions already filled in are kept for the same elements, so
``falcon confirm`` can be run again right after.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from afsim_research_agent.capability.catalog import CapabilityCatalog, CatalogError
from afsim_research_agent.capability.constraints import (
    VocabularyConstraints,
    vocabulary_constraints,
)
from afsim_research_agent.capability.granularity import CapabilityPolicy
from afsim_research_agent.capability.ir_check import CROW_GAPS_FILE, load_crow_gaps
from afsim_research_agent.falcon.errors import FalconError
from afsim_research_agent.falcon.models import (
    EvidencePackage,
    FalconRequest,
    FalconTrace,
)
from afsim_research_agent.falcon.repair import check_package, write_draft
from afsim_research_agent.falcon.review import confirmation_items, write_confirmations
from afsim_research_agent.falcon.sheet.compiler import compile_sheet
from afsim_research_agent.falcon.sheet.generate import (
    SHEET_FILE,
    SheetContext,
    load_observed,
    record_observed,
    render_problems,
    to_state,
    write_sheet_files,
)
from afsim_research_agent.ir.catalog import Catalog, load_catalog


@dataclass
class RecompileReport:
    status: str
    problems: list[str]
    pending: int

    def render(self) -> str:
        head = f"状态：{self.status}；待确认 {self.pending} 项"
        return "\n".join([head, *self.problems[:40]])


def _load(path: Path, what: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FalconError("FAL002", f"{what} not found: {path.name}") from exc


def _constraints(
    trace: FalconTrace, request: FalconRequest, catalog: Catalog
) -> VocabularyConstraints | None:
    """The closed vocabulary the draft was generated under (none if it had none)."""
    used = trace.capability_constraints
    if not used:
        return None
    try:
        capability = CapabilityCatalog.load()
    except CatalogError:
        return None
    policy = CapabilityPolicy.make(request.capability_granularity, request.capability_refine)
    classes = set(used.get("platform_classes") or []) or {
        p.capability_class for p in request.platforms
    }
    return vocabulary_constraints(catalog, capability, classes, policy=policy)


@dataclass
class Workspace:
    """A model package opened for rebuilding: the context the sheet compiles in."""

    package_dir: Path
    ctx: SheetContext
    trace: FalconTrace
    request: FalconRequest

    def build(self, sheet: str, *, with_l3: bool = False):
        """Compile ``sheet``, write the draft into the package and check it.

        Returns (compiled, assembled, check); the tactic sheet file itself is not
        written."""
        compiled = compile_sheet(
            sheet,
            request=self.request,
            clauses=self.trace.clauses,
            catalog=self.ctx.catalog,
            stage="full",
            constraints=self.ctx.constraints,
            known_gaps=self.ctx.known_gaps,
            observed=self.ctx.observed,
        )
        assembled, _notes = write_draft(to_state(self.ctx, compiled), self.package_dir)
        check = check_package(self.package_dir, assembled.problems, with_l3=with_l3)
        return compiled, assembled, check


def open_workspace(package_dir: Path) -> Workspace:
    trace = FalconTrace.model_validate_json(
        _load(package_dir / "falcon_trace.json", "Falcon trace")
    )
    raw = yaml.safe_load(_load(package_dir / "falcon_request.yaml", "request snapshot")) or {}
    raw.setdefault("crow_result", "research_result.json")
    raw["requirement"] = raw.get("requirement") or "（见 falcon_trace.json 的条款）"
    request = FalconRequest.model_validate(raw)
    package = EvidencePackage.model_validate_json(
        _load(package_dir / "evidence_package.json", "evidence package")
    )
    catalog = load_catalog()
    crow = package_dir / CROW_GAPS_FILE
    ctx = SheetContext(
        request=request,
        clauses=trace.clauses,
        user_values=trace.user_values,
        package=package,
        catalog=catalog,
        constraints=_constraints(trace, request, catalog),
        known_gaps=load_crow_gaps(crow) if crow.is_file() else None,
        observed=load_observed(package_dir),
    )
    return Workspace(package_dir, ctx, trace, request)


def recompile_package(package_dir: Path) -> RecompileReport:
    sheet = _load(package_dir / SHEET_FILE, "tactic sheet")
    workspace = open_workspace(package_dir)
    ctx, package = workspace.ctx, workspace.ctx.package

    def build():
        return workspace.build(sheet)

    compiled, assembled, check = build()
    if check.l2 is not None:
        # tests added or edited since the draft: record what they now produce
        recorded = record_observed(ctx.observed, check.l2)
        if recorded != ctx.observed:
            ctx.observed = recorded
            compiled, assembled, check = build()
    write_sheet_files(package_dir, compiled, ctx.observed)
    old: dict[tuple[str, str], dict] = {}
    path = package_dir / "confirmations.yaml"
    if path.is_file():
        for item in (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("items") or []:
            old[(item.get("kind"), item.get("id"))] = item
    items = confirmation_items(assembled.data, package)
    for item in items:
        before = old.get((item["kind"], item["id"]))
        if before:
            for key in ("decision", "override", "note"):
                if before.get(key) not in (None, "pending"):
                    item[key] = before[key]
    write_confirmations(path, items)
    problems = render_problems(compiled, check, assembled.data)
    status = "draft_valid" if not problems else "draft_invalid"
    report = {
        "stage": "falcon_compile",
        "status": status,
        "diagnostics": [d.model_dump(mode="json") for d in check.diagnostics],
        "problems": problems,
        "l2": check.l2,
    }
    (package_dir / "validation_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return RecompileReport(
        status, problems, sum(1 for i in items if i.get("decision") == "pending")
    )
