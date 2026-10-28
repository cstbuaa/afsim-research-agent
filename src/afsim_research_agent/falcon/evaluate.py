"""Online-evaluation metrics for one Falcon model package (design §8).

Reads only files inside the package (and optionally a reference package) and
writes ``falcon_eval.json``. Works on a fresh draft and again after
``falcon confirm`` + ``ir build`` (then build and L3 results are included).
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from afsim_research_agent.falcon.errors import FalconError
from afsim_research_agent.ir.loader import load_spec


def _json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _bases(data: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for name, param in (data.get("parameters") or {}).items():
        out.append((f"parameters.{name}", param.get("basis") or {}))
    for key in ("formations", "coordination", "behaviors"):
        for item in data.get(key) or []:
            if "basis" in item:
                out.append((f"{key}.{item.get('id')}", item["basis"]))
    for role in data.get("roles") or []:
        for state in (role.get("state_machine") or {}).get("states", []):
            for tr in state.get("transitions", []):
                if "basis" in tr:
                    out.append((f"transitions.{tr.get('transition_id')}", tr["basis"]))
    return out


def _structure(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "roles": sorted(r["id"] for r in data.get("roles") or []),
        "states": sorted(
            f"{r['id']}.{s['id']}"
            for r in data.get("roles") or []
            for s in (r.get("state_machine") or {}).get("states", [])
        ),
        "formations": sorted(f["id"] for f in data.get("formations") or []),
        "patterns": sorted(c.get("pattern", "") for c in data.get("coordination") or []),
        "parameters": sorted((data.get("parameters") or {}).keys()),
        "tests": len(data.get("tests") or []),
        "scenarios": len(data.get("scenarios") or []),
    }


def _overlap(mine: list[str], ref: list[str]) -> dict[str, Any]:
    a, b = set(mine), set(ref)
    return {"common": sorted(a & b), "only_draft": sorted(a - b), "only_reference": sorted(b - a)}


def evaluate_package(package: Path, reference: Path | None = None) -> dict[str, Any]:
    trace = _json(package / "falcon_trace.json")
    evidence = _json(package / "evidence_package.json")
    if trace is None or evidence is None:
        raise FalconError(
            "FAL002", f"{package.name} is not a Falcon package (trace/evidence missing)"
        )
    loaded = load_spec(package / "behavior_spec.yaml")
    data = loaded.data or {}
    report = _json(package / "validation_report.json") or {}

    clauses = trace.get("clauses", [])
    modelled = [c for c in clauses if c.get("kind") != "out_of_scope"]
    excused = {o["clause_id"] for o in trace.get("out_of_scope", [])}
    covered_ids = {ref for refs in trace.get("implements", {}).values() for ref in refs}
    covered = [c for c in modelled if c["id"] in covered_ids or c["id"] in excused]

    bases = _bases(data)
    statuses = Counter(b.get("status", "missing") for _, b in bases)
    with_evidence = sum(1 for _, b in bases if b.get("evidence"))
    rules = Counter(n["rule"] for n in trace.get("enforcement", []))
    dropped = evidence.get("dropped", [])
    claimed = len(evidence.get("evidence", [])) + sum(1 for d in dropped if d.get("statement"))
    calls = trace.get("calls", [])

    l2 = report.get("l2") or {}
    l2_tests = l2.get("tests", [])
    build_status = report.get("status") if report.get("stage") != "falcon_draft" else None
    l3 = report.get("l3")

    metrics: dict[str, Any] = {
        "package": package.name,
        "draft_status": trace.get("status"),
        "repair_rounds": len(trace.get("repairs", [])),
        "l1_l2_passed_within_3_rounds": trace.get("status") == "draft_valid"
        and len(trace.get("repairs", [])) <= 3,
        "clauses": len(clauses),
        "clause_coverage": round(len(covered) / len(modelled), 3) if modelled else 1.0,
        "uncovered_clauses": trace.get("uncovered_clauses", []),
        "out_of_scope": [o["clause_id"] for o in trace.get("out_of_scope", [])],
        "basis_total": len(bases),
        "basis_with_evidence_ratio": round(with_evidence / len(bases), 3) if bases else 0.0,
        "basis_status": dict(statuses),
        "evidence_items": len(evidence.get("evidence", [])),
        "propositions": len(evidence.get("claims", [])),
        "conflicting_propositions": sum(
            1 for c in evidence.get("claims", []) if c.get("conflicts")
        ),
        "dropped_claims": len(dropped),
        "dropped_claim_ratio": round(sum(1 for d in dropped if d.get("statement")) / claimed, 3)
        if claimed
        else 0.0,
        "dropped_reasons": dict(Counter(d["reason"] for d in dropped)),
        "user_specified_downgrades": rules.get("user_specified_unbacked", 0),
        "enforcement_rules": dict(rules),
        "l2_passed": sum(1 for t in l2_tests if t.get("passed")),
        "l2_total": len(l2_tests),
        "model_calls": len(calls),
        "model_failed_calls": sum(1 for c in calls if not c.get("ok")),
        "model_seconds": round(sum(c.get("seconds", 0) for c in calls), 1),
        "models": sorted({c.get("model") for c in calls}),
        "structure": _structure(data),
    }
    if build_status is not None:
        metrics["build_status"] = build_status
        if l3:
            metrics["l3"] = {
                s["id"]: {
                    "passed": s.get("passed"),
                    "failed_expectations": [
                        e["id"] for e in s.get("expectations", []) if not e.get("passed")
                    ],
                }
                for s in l3
            }
    if reference is not None:
        ref = load_spec(reference / "behavior_spec.yaml").data or {}
        mine, theirs = metrics["structure"], _structure(ref)
        metrics["vs_reference"] = {
            key: _overlap(mine[key], theirs[key])
            for key in ("roles", "states", "formations", "patterns", "parameters")
        }
    (package / "falcon_eval.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return metrics


def render_metrics(m: dict[str, Any]) -> str:
    lines = [
        f"模型包：{m['package']}",
        f"草稿状态：{m['draft_status']}；修复轮数 {m['repair_rounds']}；"
        f"≤3 轮内通过 L1+L2：{'是' if m['l1_l2_passed_within_3_rounds'] else '否'}",
        f"条款覆盖率：{m['clause_coverage']:.0%}（{m['clauses']} 条；未覆盖 {m['uncovered_clauses'] or '无'}；"
        f"超出范围 {m['out_of_scope'] or '无'}）",
        f"basis：{m['basis_total']} 个，有证据 {m['basis_with_evidence_ratio']:.0%}；状态分布 {m['basis_status']}",
        f"证据：{m['evidence_items']} 条主张，{m['propositions']} 个命题（{m['conflicting_propositions']} 个冲突）；"
        f"丢弃 {m['dropped_claims']}（主张丢弃率 {m['dropped_claim_ratio']:.0%}）{m['dropped_reasons']}",
        f"user_specified 被降级：{m['user_specified_downgrades']}；代码强制调整 {m['enforcement_rules']}",
        f"L2：{m['l2_passed']}/{m['l2_total']}",
        f"模型调用：{m['model_calls']} 次（失败 {m['model_failed_calls']}），耗时 {m['model_seconds']} s，模型 {m['models']}",
    ]
    if "build_status" in m:
        lines.append(f"正式构建：{m['build_status']}")
        for sid, s in (m.get("l3") or {}).items():
            lines.append(
                f"  L3 {sid}：{'通过' if s['passed'] else '未通过 ' + str(s['failed_expectations'])}"
            )
    if "vs_reference" in m:
        lines.append("与参考包对比（共有 / 仅草稿 / 仅参考）：")
        for key, o in m["vs_reference"].items():
            lines.append(f"  {key}：{len(o['common'])} / {o['only_draft']} / {o['only_reference']}")
    lines.append("已写入 falcon_eval.json")
    return "\n".join(lines)
