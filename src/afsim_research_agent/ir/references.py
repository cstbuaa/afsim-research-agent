"""Reference values: parameter values the build uses without confirmation.

A parameter whose basis is not user-specified and not confirmed passes the confirmation
gate as a *reference value* (design decision 2026-10-17): the model builds and runs with
it, and it is listed for later review and tuning. Values without any evidence are marked
separately, since they are the ones most likely to need adjusting.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

_STATUS = {
    "explicit": "证据直接给出",
    "inferred": "由证据推断",
    "conflicting": "证据冲突，择一",
    "unknown": "无依据",
}


@dataclass(frozen=True)
class ReferenceValue:
    name: str
    value: Any
    unit: str | None
    description: str
    status: str
    evidence: list[str]
    rationale: str | None
    grounded: bool  # False: no evidence at all ("无依据")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def reference_values(ir: dict[str, Any]) -> list[ReferenceValue]:
    """Unconfirmed parameters of a normalized IR; ungrounded ones first."""
    out = []
    for name, param in (ir.get("parameters") or {}).items():
        basis = param.get("basis") or {}
        if not basis or basis.get("status") == "user_specified":
            continue
        if basis.get("confirmation") == "confirmed":
            continue
        evidence = list(basis.get("evidence") or [])
        original = param.get("original") or {}
        out.append(
            ReferenceValue(
                name=name,
                value=original.get("value", param.get("value")),
                unit=original.get("unit", param.get("unit")),
                description=param.get("description", ""),
                status=str(basis.get("status")),
                evidence=evidence,
                rationale=basis.get("rationale"),
                grounded=bool(evidence) and basis.get("status") != "unknown",
            )
        )
    return sorted(out, key=lambda r: (r.grounded, r.name))


def render_section(values: list[ReferenceValue], number: str = "1b") -> list[str]:
    if not values:
        return []
    ungrounded = sum(1 for v in values if not v.grounded)
    lines = [
        f"## {number}. 参考值（已用于构建，待人工复核调参）",
        "",
        f"共 {len(values)} 项，其中 ⚠ 无依据 {ungrounded} 项。这些数值未经确认，"
        "按参考值通过了检验；调参时修改 `behavior_spec.yaml` 中的取值，"
        "或用 `falcon confirm` 确认后不再列出。",
        "",
        "| 参数 | 取值 | 依据 | 证据 | 说明 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for v in values:
        mark = "⚠ 无依据" if not v.grounded else _STATUS.get(v.status, v.status)
        value = f"{v.value} {v.unit or ''}".strip()
        note = v.rationale or v.description or "—"
        lines.append(
            f"| `{v.name}` | {value} | {mark} | {', '.join(v.evidence) or '—'} | "
            f"{note.replace('|', '／')} |"
        )
    lines.append("")
    return lines
