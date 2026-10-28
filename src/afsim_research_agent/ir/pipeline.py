"""End-to-end package build: L1 -> gate -> normalize -> L2 -> bind -> compile -> L3.

Writes into the model package directory only these derived files:
``normalized_ir.json``, ``resolved_context.json``, ``validation_report.json``,
``capability_gaps.yaml``, ``review.md`` and ``generated/``. ``behavior_spec.yaml`` and
``evidence_package.json`` are never modified. A pre-existing ``generated/``
directory without our ``manifest.json`` is treated as foreign and not touched.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from afsim_research_agent.capability.catalog import CapabilityCatalog, CatalogError
from afsim_research_agent.capability.ir_check import (
    CROW_GAPS_FILE,
    GAPS_FILE,
    CapabilityCheck,
    afsim_gate,
    annotate_btcpp_xml,
    check_ir,
    load_crow_gaps,
    load_decisions,
    load_policy,
    load_sheet_needs,
)
from afsim_research_agent.ir.backends import COMPILERS, CompileRefused, compile_backend
from afsim_research_agent.ir.backends.btcpp_xml import verify_role_xml
from afsim_research_agent.ir.binding import BACKENDS, BINDING_VERSION, bind, load_overrides
from afsim_research_agent.ir.diagnostics import Collector, Diagnostic, has_errors, sort_diagnostics
from afsim_research_agent.ir.l2 import run_tests
from afsim_research_agent.ir.normalize import NORMALIZER_VERSION, canonical_json, normalize
from afsim_research_agent.ir.references import reference_values
from afsim_research_agent.ir.review import render_review
from afsim_research_agent.ir.sim import run_scenarios
from afsim_research_agent.ir.validate import validate_package

MANIFEST = "manifest.json"


class ForeignOutputError(RuntimeError):
    pass


@dataclass
class BuildResult:
    status: str
    diagnostics: list[Diagnostic]
    ir: dict[str, Any] | None = None
    l2: dict[str, Any] | None = None
    l3: list[dict[str, Any]] | None = None
    written: list[str] = field(default_factory=list)
    capability: CapabilityCheck | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("passed", "draft")


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _write(root: Path, rel: str, text: str, written: list[str]) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    written.append(rel)


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def _prepare_generated(root: Path) -> dict[str, Any] | None:
    gen = root / "generated"
    if not gen.exists():
        return None
    manifest = gen / MANIFEST
    if not manifest.exists():
        if any(_user_files(gen)):
            raise ForeignOutputError(
                f"{gen} exists but has no {MANIFEST}; "
                "refusing to overwrite files we did not generate"
            )
        return None
    return json.loads(manifest.read_text(encoding="utf-8"))


def _user_files(gen: Path) -> list[Path]:
    """Files under generated/ other than bytecode caches left by running our code."""
    return [p for p in gen.rglob("*") if p.is_file() and "__pycache__" not in p.parts]


def _remove_stale(root: Path, old: dict[str, Any] | None) -> None:
    if not old:
        return
    gen = root / "generated"
    for backend, info in old.get("backends", {}).items():
        for rel in info.get("files", {}):
            path = (gen / backend / rel).resolve()
            if gen.resolve() in path.parents and path.is_file():
                path.unlink()
        backend_dir = gen / backend
        if backend_dir.is_dir():
            for cache in sorted(backend_dir.rglob("__pycache__"), reverse=True):
                for item in cache.glob("*.pyc"):
                    item.unlink()
            # prune directories left empty (deepest first)
            for directory in sorted(
                (d for d in backend_dir.rglob("*") if d.is_dir()),
                key=lambda d: len(d.parts),
                reverse=True,
            ):
                if not any(directory.iterdir()):
                    directory.rmdir()
            if not any(backend_dir.iterdir()):
                backend_dir.rmdir()


def build_package(
    package_dir: str | Path,
    *,
    backends: tuple[str, ...] = BACKENDS,
    draft: bool = False,
    run_l3: bool = True,
    write: bool = True,
    capability_catalog: CapabilityCatalog | Path | str | None = None,
    check_capabilities: bool = True,
    crow_gaps: Path | str | None = None,
    crow_candidate: str | None = None,
    granularity: str | None = None,
    refine: list[str] | None = None,
    faithful_sim: bool = False,
    strict_parameters: bool = False,
) -> BuildResult:
    """``crow_gaps``: Crow's ``capability_gaps.yaml`` for the tactic this model implements
    (``crow_candidate`` selects that tactic's gaps); the IR check then reports which
    estimated gaps it confirms, resolves or does not use."""
    root = Path(package_dir)
    written: list[str] = []
    diag = Collector()
    validation = validate_package(root)
    spec = validation.spec
    norm = normalize(validation, draft=draft, strict_parameters=strict_parameters)
    diag.extend(norm.diagnostics)
    ir = norm.ir
    l2_dict: dict[str, Any] | None = None
    l3_dicts: list[dict[str, Any]] | None = None
    l3_faithful: list[dict[str, Any]] | None = None
    contexts: dict[str, Any] = {}
    files_by_backend: dict[str, dict[str, str]] = {}

    capability: CapabilityCheck | None = None
    if ir is not None and check_capabilities:
        capability = _check_capabilities(
            root, ir, capability_catalog, crow_gaps, crow_candidate, diag, granularity, refine
        )

    if ir is None:
        gate_codes = {"PAR001", "ASM001", "EVD002"}
        blocked = validation.ok and all(
            d.code in gate_codes for d in norm.diagnostics if d.severity.value == "error"
        )
        status = "blocked" if blocked else "failed"
    else:
        l2 = run_tests(ir)
        l2_dict = l2.to_dict()
        diag.extend(l2.diagnostics)
        if not l2.passed:
            status = "failed"
        elif draft:
            status = "draft"
        else:
            status = "passed"
            for name in backends:
                stopped = afsim_gate(capability, name)
                if stopped:
                    diag.error(
                        "CAP001",
                        f"backends.{name}",
                        f"{len(stopped)} 项平台能力缺口未处理（{', '.join(stopped)}）；"
                        f"请在 capability_decisions.yaml 中确认、接受替代写法或标记待插件",
                    )
                    continue
                if name not in COMPILERS:
                    diag.error("BIND001", "backend", f"unknown backend '{name}'")
                    continue
                context, items = bind(ir, name, load_overrides(root, name))
                diag.extend(items)
                if context is None:
                    continue
                contexts[name] = context
                try:
                    files = compile_backend(name, ir, context)
                except CompileRefused as exc:
                    diag.error("IR002", f"backends.{name}", str(exc))
                    continue
                if name == "btcpp_xml" and capability is not None:
                    files = {
                        rel: annotate_btcpp_xml(text, capability) if rel.endswith(".xml") else text
                        for rel, text in files.items()
                    }
                files_by_backend[name] = files
                if name == "btcpp_xml":
                    for role in ir["roles"]:
                        for problem in verify_role_xml(ir, role, files[f"{role['id']}.xml"]):
                            diag.error("TREE001", f"generated/btcpp_xml/{role['id']}.xml", problem)
            if run_l3 and ir["scenarios"]:
                sim_context = contexts.get("python_sim")
                if sim_context is None:
                    sim_context, items = bind(ir, "python_sim", load_overrides(root, "python_sim"))
                    diag.extend(items)
                if sim_context is not None:
                    results = run_scenarios(ir, sim_context, package_dir=root)
                    l3_dicts = [r.to_dict() for r in results]
                    for r in results:
                        for e in r.expectations:
                            if not e.passed:
                                diag.error(
                                    "L3001",
                                    f"scenarios.{r.id}.expect.{e.id}",
                                    f"{e.observable}: measured {e.measured}, "
                                    f"threshold {e.threshold} ({e.detail})",
                                )
                    if faithful_sim:
                        l3_faithful = _faithful_l3(ir, sim_context, root, capability, diag)
            if has_errors(diag.items):
                status = "failed"

    diagnostics = sort_diagnostics({d.model_dump_json(): d for d in diag.items}.values())
    result = BuildResult(status, diagnostics, ir, l2_dict, l3_dicts, written, capability)
    if not write:
        return result

    references = reference_values(ir) if ir is not None else []
    old_manifest = _prepare_generated(root)
    report = {
        "status": status,
        "ir_hash": ir["ir_hash"] if ir else None,
        "counts": {
            sev: sum(1 for d in diagnostics if d.severity.value == sev)
            for sev in ("error", "warning", "info")
        },
        "diagnostics": [d.model_dump(mode="json") for d in diagnostics],
        "assumptions": [a.model_dump() for a in spec.assumptions] if spec else [],
        "l2": l2_dict,
        "l3": l3_dicts,
        "l3_faithful": l3_faithful,
        "reference_values": [r.to_dict() for r in references],
        "capability": capability.summary() if capability else None,
    }
    if ir is not None:
        _write(root, "normalized_ir.json", _json(ir), written)
    if contexts:
        _write(root, "resolved_context.json", _json({"backends": contexts}), written)
    if capability is not None:
        _write(root, GAPS_FILE, capability.to_yaml(), written)
    elif (root / GAPS_FILE).exists():
        (root / GAPS_FILE).unlink()  # stale: no IR, or the check was turned off
    if status == "passed" and files_by_backend:
        _remove_stale(root, old_manifest)
        manifest: dict[str, Any] = {
            "ir_version": ir["ir_version"] if ir else None,
            "ir_hash": ir["ir_hash"] if ir else None,
            "spec_sha256": _sha((root / "behavior_spec.yaml").read_bytes()),
            "evidence_package_sha256": (
                _sha((root / "evidence_package.json").read_bytes())
                if (root / "evidence_package.json").exists()
                else None
            ),
            "catalog_versions": ir["catalog_versions"] if ir else {},
            "normalizer": NORMALIZER_VERSION,
            "binding_version": BINDING_VERSION,
            "capability_catalog": capability.catalog_version if capability else None,
            "backends": {},
        }
        for name, files in sorted(files_by_backend.items()):
            manifest["backends"][name] = {
                "template_version": COMPILERS[name][1],
                "files": {rel: _sha(text.encode("utf-8")) for rel, text in sorted(files.items())},
            }
            for rel, text in sorted(files.items()):
                _write(root, f"generated/{name}/{rel}", text, written)
        _write(root, f"generated/{MANIFEST}", _json(manifest), written)
    if status != "passed":
        # Never leave generated code next to a spec that no longer builds.
        _remove_stale(root, old_manifest)
        for rel in (f"generated/{MANIFEST}", "resolved_context.json"):
            if (root / rel).exists() and rel not in written:
                (root / rel).unlink()
        if ir is None and (root / "normalized_ir.json").exists():
            (root / "normalized_ir.json").unlink()
    _write(root, "validation_report.json", _json(report), written)
    _write(
        root,
        "review.md",
        render_review(
            spec,
            diagnostics,
            status=status,
            ir_hash=ir["ir_hash"] if ir else None,
            l2=l2_dict,
            l3=l3_dicts,
            capability=capability,
            l3_faithful=l3_faithful,
            references=references,
        ),
        written,
    )
    return result


def _faithful_l3(
    ir: dict[str, Any],
    context: dict[str, Any],
    root: Path,
    capability: CapabilityCheck | None,
    diag: Collector,
) -> list[dict[str, Any]] | None:
    """Faithful mode (integration point ⑦): run L3 again without the capabilities that are
    still open gaps. Failures are warnings: they show what the tactic relies on, they do not
    fail the build."""
    if capability is None:
        diag.warning("CAP003", "capability", "没有能力核对结果，忠实模式未运行")
        return None
    withheld = capability.withheld()
    results = run_scenarios(ir, context, package_dir=root, withheld=withheld)
    for r in results:
        for e in r.expectations:
            if not e.passed:
                diag.warning(
                    "L3F01",
                    f"scenarios.{r.id}.expect.{e.id}",
                    f"忠实模式下未达成（不提供 {', '.join(sorted(r.withheld or {})) or '—'}）："
                    f"{e.observable}: measured {e.measured}, threshold {e.threshold}",
                )
    return [r.to_dict() for r in results]


def _check_capabilities(
    root: Path,
    ir: dict[str, Any],
    catalog: CapabilityCatalog | Path | str | None,
    crow_gaps: Path | str | None,
    crow_candidate: str | None,
    diag: Collector,
    granularity: str | None = None,
    refine: list[str] | None = None,
) -> CapabilityCheck | None:
    """Integration point ⑤; never fails the build (only AFSIM backends are gated)."""
    try:
        if not isinstance(catalog, CapabilityCatalog):
            catalog = CapabilityCatalog.load(catalog)
    except CatalogError as exc:
        diag.info("CAP000", "capability", f"能力目录不可用，跳过平台能力核对：{exc}")
        return None
    crow = None
    crow_source = None
    if crow_gaps is not None:
        crow = load_crow_gaps(Path(crow_gaps), crow_candidate)
        crow_source = f"{Path(crow_gaps).name}" + (f"#{crow_candidate}" if crow_candidate else "")
    elif (root / CROW_GAPS_FILE).is_file():
        # written by Falcon for the tactic selected in Crow C5 (already filtered)
        crow = load_crow_gaps(root / CROW_GAPS_FILE)
        data = yaml.safe_load((root / CROW_GAPS_FILE).read_text(encoding="utf-8")) or {}
        crow_source = str(data.get("source") or CROW_GAPS_FILE)
    try:
        policy = load_policy(root, granularity, refine)
    except ValueError as exc:
        diag.error("CAP003", "capability_decisions.yaml", str(exc))
        return None
    check = check_ir(
        ir,
        catalog,
        policy=policy,
        decisions=load_decisions(root),
        crow_gaps=crow,
        crow_source=crow_source,
        sheet_needs=load_sheet_needs(root),
    )
    if check.unresolved:
        diag.warning(
            "CAP002",
            "capability",
            f"{len(check.unresolved)} 项平台能力缺口未处理，生成 AFSIM 后端前需处理"
            f"（见 {GAPS_FILE}）",
        )
    return check


def manifest_digest(manifest: dict[str, Any]) -> str:
    return _sha(canonical_json(manifest).encode("utf-8"))
