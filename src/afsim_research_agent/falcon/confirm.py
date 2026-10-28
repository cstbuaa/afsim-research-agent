"""File-based confirmation: apply ``confirmations.yaml`` decisions to the spec.

Only the targeted fields change; comments and formatting elsewhere in
``behavior_spec.yaml`` are preserved. An item is skipped when the spec no
longer matches what the confirmation list was generated from (the user has
edited it by hand), and the reason is reported.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from afsim_research_agent.falcon.errors import FalconError
from afsim_research_agent.falcon.yamlpatch import PatchError, parse_path, set_key
from afsim_research_agent.ir.diagnostics import Collector
from afsim_research_agent.ir.loader import load_spec_text
from afsim_research_agent.ir.normalize import confirmation_gate
from afsim_research_agent.ir.units import PHYSICAL_TYPES, UnitError, to_canonical
from afsim_research_agent.ir.validate import parse_spec

DECISIONS = {"pending", "confirm", "reject", "override"}


@dataclass
class ConfirmReport:
    applied: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    blocking: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"已写回 {len(self.applied)} 项，无需改动 {len(self.unchanged)} 项，"
            f"跳过 {len(self.skipped)} 项，仍待决定 {len(self.pending)} 项。"
        ]
        lines += [f"  跳过 {target}：{reason}" for target, reason in self.skipped]
        if self.blocking:
            lines.append(f"仍阻塞正式构建的项（{len(self.blocking)}）：")
            lines += [f"  - {b}" for b in self.blocking[:30]]
        else:
            lines.append(
                "确认闸门已通过，可以运行：python -m afsim_research_agent.ir build <package>"
            )
        return "\n".join(lines)


def _get(data: Any, path: str) -> Any:
    node = data
    for segment in parse_path(path):
        if isinstance(segment, int):
            if not isinstance(node, list) or segment >= len(node):
                return None
            node = node[segment]
        else:
            if not isinstance(node, dict) or segment not in node:
                return None
            node = node[segment]
    return node


def _same_value(a: Any, b: Any) -> bool:
    if isinstance(a, int | float) and isinstance(b, int | float) and not isinstance(a, bool):
        return abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(a)))
    return a == b


def _check_target(item: dict[str, Any], node: Any) -> str | None:
    if not isinstance(node, dict):
        return "目标路径在 behavior_spec.yaml 中已不存在"
    kind = item.get("kind")
    element_id = item.get("id")
    if kind == "parameter":
        current = item.get("current") or {}
        if not _same_value(node.get("value"), current.get("value")) or node.get(
            "unit"
        ) != current.get("unit"):
            return "参数值已被手工修改，与确认清单不一致"
        return None
    found = node.get("id", node.get("transition_id"))
    if found != element_id:
        return f"路径处的元素是 '{found}'，不是 '{element_id}'（顺序已变化）"
    return None


def apply_confirmations(package_dir: Path, reviewer: str) -> ConfirmReport:
    spec_path = package_dir / "behavior_spec.yaml"
    conf_path = package_dir / "confirmations.yaml"
    if not spec_path.exists() or not conf_path.exists():
        raise FalconError("FAL002", "package has no behavior_spec.yaml or confirmations.yaml")
    if not reviewer.strip():
        raise FalconError("FAL003", "--by needs a reviewer name or identifier")
    try:
        conf = yaml.safe_load(conf_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise FalconError("FAL003", "confirmations.yaml cannot be parsed", [str(exc)]) from exc
    text = spec_path.read_text(encoding="utf-8")
    loaded = load_spec_text(text)
    if loaded.data is None:
        raise FalconError(
            "FAL003",
            "behavior_spec.yaml cannot be parsed",
            [d.render() for d in loaded.diagnostics],
        )
    data = loaded.data
    report = ConfirmReport()

    for item in conf.get("items", []):
        target = str(item.get("target", ""))
        decision = str(item.get("decision", "pending")).strip().lower()
        if decision not in DECISIONS:
            report.skipped.append(
                (target, f"未知决定 '{decision}'（可选 confirm / reject / override）")
            )
            continue
        if decision == "pending":
            report.pending.append(target)
            continue
        node = _get(data, target)
        kind = item.get("kind")
        if decision == "override" and kind != "parameter":
            report.skipped.append((target, "只有参数可以 override；规则请 confirm 或 reject"))
            continue
        if decision == "override":
            override = item.get("override") or {}
            if not isinstance(override, dict) or "value" not in override:
                report.skipped.append((target, "override 需要填写 {value, unit}"))
                continue
            if (
                isinstance(node, dict)
                and _same_value(node.get("value"), override.get("value"))
                and (node.get("basis", {}).get("status") == "user_specified")
            ):
                report.unchanged.append(target)
                continue
        problem = _check_target(item, node)
        if problem is not None:
            report.skipped.append((target, problem))
            continue
        try:
            text, changed = _apply(text, item, node, decision, reviewer)
        except (PatchError, UnitError, ValueError) as exc:
            report.skipped.append((target, f"写回失败：{exc}"))
            continue
        (report.applied if changed else report.unchanged).append(target)
        reparsed = load_spec_text(text)
        if reparsed.data is None:
            raise FalconError("FAL004", f"edit of {target} produced invalid YAML; nothing written")
        data = reparsed.data

    spec_path.write_text(text, encoding="utf-8")
    report.blocking = blocking_items(data)
    return report


def _apply(
    text: str, item: dict[str, Any], node: dict[str, Any], decision: str, reviewer: str
) -> tuple[str, bool]:
    target = str(item["target"])
    kind = item.get("kind")
    if kind == "assumption":
        want = "confirmed" if decision == "confirm" else "rejected"
        if node.get("confirmation") == want and node.get("confirmed_by") == reviewer:
            return text, False
        text = set_key(text, target, "confirmation", want)
        return set_key(text, target, "confirmed_by", reviewer), True
    basis_path = f"{target}.basis"
    basis = node.get("basis") or {}
    if decision in ("confirm", "reject"):
        want = "confirmed" if decision == "confirm" else "rejected"
        if basis.get("confirmation") == want and basis.get("confirmed_by") == reviewer:
            return text, False
        text = set_key(text, basis_path, "confirmation", want)
        return set_key(text, basis_path, "confirmed_by", reviewer), True
    # override (parameters only)
    override = item["override"]
    value = override["value"]
    unit = override.get("unit", node.get("unit"))
    kind_name = node.get("type")
    if kind_name in PHYSICAL_TYPES:
        if unit is None:
            raise ValueError("override of a physical parameter needs a unit")
        to_canonical(float(value), str(unit), str(kind_name))
    old = f"{node.get('value')} {node.get('unit') or ''}".strip()
    evidence = basis.get("evidence") or []
    note = f"用户覆盖：原值 {old}（{basis.get('status')}，证据 {evidence or '无'}）"
    if item.get("note"):
        note += f"；{item['note']}"
    text = set_key(text, target, "value", value)
    if unit is not None:
        text = set_key(text, target, "unit", unit)
    text = set_key(text, basis_path, "status", "user_specified")
    text = set_key(text, basis_path, "rationale", note)
    text = set_key(text, basis_path, "confirmation", "confirmed")
    return set_key(text, basis_path, "confirmed_by", reviewer), True


def blocking_items(data: dict[str, Any]) -> list[str]:
    spec, _ = parse_spec(data)
    if spec is None:
        return ["behavior_spec.yaml 结构无效，请运行 ir validate 查看"]
    diag = Collector()
    confirmation_gate(spec, diag)
    # unconfirmed parameter values are reference values: they do not block (PAR002 info)
    return [f"{d.path}: {d.message}" for d in diag.items if d.severity.value == "error"]


def package_status(package_dir: Path) -> dict[str, Any]:
    spec_path = package_dir / "behavior_spec.yaml"
    if not spec_path.exists():
        raise FalconError("FAL002", "package has no behavior_spec.yaml")
    loaded = load_spec_text(spec_path.read_text(encoding="utf-8"))
    if loaded.data is None:
        raise FalconError("FAL003", "behavior_spec.yaml cannot be parsed")
    counts = {"pending": 0, "confirmed": 0, "rejected": 0}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            basis = node.get("basis")
            if isinstance(basis, dict) and basis.get("confirmation") in counts:
                counts[basis["confirmation"]] += 1
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(loaded.data)
    for assumption in loaded.data.get("assumptions", []) or []:
        if isinstance(assumption, dict) and assumption.get("confirmation") in counts:
            counts[assumption["confirmation"]] += 1
    blocking = blocking_items(loaded.data)
    return {"counts": counts, "blocking": blocking, "ready_to_build": not blocking}
