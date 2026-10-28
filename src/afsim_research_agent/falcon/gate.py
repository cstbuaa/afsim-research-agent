"""F0: request and Crow-result checks. Refuses before any file is written."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from afsim_research_agent.capability.granularity import CapabilityPolicy
from afsim_research_agent.crow.models import CrowResearchResult
from afsim_research_agent.falcon.errors import FalconError
from afsim_research_agent.falcon.models import FalconRequest
from afsim_research_agent.ir.catalog import load_catalog
from afsim_research_agent.ir.units import PHYSICAL_TYPES, UnitError, infer_dimension, to_canonical


@dataclass
class GateResult:
    request: FalconRequest
    request_dir: Path
    crow: CrowResearchResult
    crow_path: Path
    engine_capabilities: set[str] | None
    tactic: Any = None  # falcon.tactic.SelectedTactic | None (P4)


def load_request(path: Path) -> FalconRequest:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise FalconError("FAL003", f"cannot read request file {path.name}", [str(exc)]) from exc
    if not isinstance(raw, dict):
        raise FalconError("FAL003", "request file must be a mapping")
    try:
        return FalconRequest.model_validate(raw)
    except ValidationError as exc:
        details = [f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()]
        raise FalconError("FAL003", "request file is invalid", details) from exc


def _resolve(base: Path, value: Path) -> Path:
    return value if value.is_absolute() else (base / value)


def check_request(request: FalconRequest, base: Path) -> set[str] | None:
    problems: list[str] = []
    catalog = load_catalog()
    terrain = set(catalog.vocabularies["terrain_classes"])
    for name in request.terrain_classes:
        if name not in terrain:
            problems.append(f"terrain class '{name}' is not one of {sorted(terrain)}")
    for name, item in request.user_parameters.items():
        if item.unit is None:
            continue
        dimension = infer_dimension(item.unit)
        if dimension is None or dimension not in PHYSICAL_TYPES:
            problems.append(f"user_parameters.{name}: unknown unit '{item.unit}'")
            continue
        if isinstance(item.value, bool) or not isinstance(item.value, int | float):
            problems.append(f"user_parameters.{name}: a value with a unit must be a number")
            continue
        try:
            to_canonical(float(item.value), item.unit, dimension)
        except UnitError as exc:
            problems.append(f"user_parameters.{name}: {exc}")
    try:
        CapabilityPolicy.make(request.capability_granularity, request.capability_refine)
    except ValueError as exc:
        problems.append(f"capability_refine: {exc}")
    unknown_backends = set(request.backends) - {"python_sim", "btcpp_xml", "mermaid"}
    if unknown_backends:
        problems.append(f"unknown backends: {sorted(unknown_backends)}")
    capabilities: set[str] | None = None
    if request.target_engine is not None:
        path = _resolve(base, request.target_engine)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            capabilities = set(data.get("capabilities", {}))
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"target_engine: cannot read bindings file ({exc.__class__.__name__})")
    if problems:
        raise FalconError("FAL003", "request file is invalid", problems)
    return capabilities


def load_crow(path: Path) -> CrowResearchResult:
    try:
        return CrowResearchResult.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FalconError("FAL001", f"Crow result not found: {path.name}") from exc
    except ValidationError as exc:
        raise FalconError(
            "FAL001", f"Crow result cannot be parsed: {path.name}", [str(exc.errors()[0])]
        ) from exc


def check_readiness(crow: CrowResearchResult) -> None:
    readiness = crow.readiness
    if readiness is None:
        raise FalconError(
            "FAL001",
            "Crow result has no readiness assessment; rerun Crow (it evaluates the gate)",
        )
    if readiness.ready:
        return
    details = [
        f"未通过：{check.name}（实际 {check.value:g}，门槛 {check.threshold:g}）{check.detail}"
        for check in readiness.checks
        if not check.passed
    ]
    weak = [
        c.question for c in readiness.coverage if c.insufficient_evidence or c.strong_evidence < 2
    ]
    details += [f"证据不足的研究问题：{q}" for q in weak]
    details.append(
        "请补充资料后重跑 Crow：增加追加检索轮次、加入本地资料库或调整研究问题；"
        "Falcon 不提供强制运行开关。"
    )
    raise FalconError(
        "FAL001", f"Crow evidence package did not pass {readiness.gate_version}", details
    )


def run_gate(request_path: Path, out_dir: Path | None = None) -> tuple[GateResult, Path]:
    request = load_request(request_path)
    base = request_path.parent
    capabilities = check_request(request, base)
    tactic = None
    if request.tactic_selection is not None:
        from afsim_research_agent.falcon.tactic import load_selection

        tactic = load_selection(_resolve(base, request.tactic_selection))
    if request.crow_result is not None:
        crow_path = _resolve(base, request.crow_result)
    else:
        found = tactic.research_result() if tactic is not None else None
        if found is None:
            raise FalconError(
                "FAL003",
                "crow_result is not given and no research_result.json is next to the tactics",
                ["在请求文件中写明 crow_result（Crow 运行目录中的 research_result.json）"],
            )
        crow_path = found
    crow = load_crow(crow_path)
    check_readiness(crow)
    target = out_dir if out_dir is not None else base / "models" / request.model_id
    if target.exists():
        raise FalconError(
            "FAL002",
            f"output directory already exists: {target.name}",
            ["Falcon never overwrites a model package; pass --out <new directory>"],
        )
    return GateResult(request, base, crow, crow_path, capabilities, tactic), target
