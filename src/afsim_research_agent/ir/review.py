"""Human review report (``review.md``), ordered as design §27.4:
pending parameters -> inferred rules -> conflicting evidence -> assumptions,
followed by validation, L2 and L3 results."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from afsim_research_agent.ir.diagnostics import Diagnostic, Severity
from afsim_research_agent.ir.models import Basis, BehaviorSpec

if TYPE_CHECKING:
    from afsim_research_agent.capability.ir_check import CapabilityCheck

_STATUS = {
    "explicit": "明确",
    "inferred": "推断",
    "conflicting": "冲突",
    "unknown": "未知",
    "user_specified": "用户指定",
}
_CONFIRM = {"pending": "待确认", "confirmed": "已确认", "rejected": "已拒绝"}


def _basis_items(spec: BehaviorSpec) -> list[tuple[str, str, Basis, str]]:
    """(kind, id, basis, value text) for every element carrying a basis."""
    items: list[tuple[str, str, Basis, str]] = []
    for name, param in sorted(spec.parameters.items()):
        if param.basis is not None:
            value = "（空）" if param.value is None else f"{param.value} {param.unit or ''}".strip()
            items.append(("参数", name, param.basis, value))
    for form in spec.formations:
        if form.basis is not None:
            items.append(("编队", form.id, form.basis, f"{len(form.slots)} 个槽位"))
    for coord in spec.coordination:
        if coord.basis is not None:
            items.append(("协同", coord.id, coord.basis, coord.pattern))
    for role in spec.roles:
        if role.state_machine is None:
            continue
        for state in role.state_machine.states:
            for tr in state.transitions:
                if tr.basis is not None:
                    items.append(
                        ("转换", f"{role.id}.{tr.transition_id}", tr.basis, f"{state.id} → {tr.to}")
                    )
    for behavior in spec.behaviors:
        if behavior.basis is not None:
            items.append(("行为", behavior.id, behavior.basis, behavior.description))
    return items


def _evidence(basis: Basis) -> str:
    parts = list(basis.evidence) + [f"假设:{a}" for a in basis.assumptions]
    return ", ".join(parts) or "—"


def _table(rows: list[list[str]], head: list[str]) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "| " + " | ".join("---" for _ in head) + " |"]
    for row in rows:
        out.append(
            "| " + " | ".join(cell.replace("|", "\\|").replace("\n", " ") for cell in row) + " |"
        )
    return out


def render_review(
    spec: BehaviorSpec | None,
    diagnostics: list[Diagnostic],
    *,
    status: str,
    ir_hash: str | None = None,
    l2: dict[str, Any] | None = None,
    l3: list[dict[str, Any]] | None = None,
    capability: CapabilityCheck | None = None,
    l3_faithful: list[dict[str, Any]] | None = None,
    references: list[Any] | None = None,
) -> str:
    """``references``: reference values (``ir.references``); when given, unconfirmed
    parameters are listed there instead of among the items that block the build."""
    lines: list[str] = []
    title = spec.model.display_name if spec else "（规范无法解析）"
    lines += [f"# 审查报告：{title}", ""]
    status_text = {
        "passed": "✅ 已通过 L1/L2/L3，生成物已输出",
        "draft": "📝 草稿：存在待确认项，仅完成校验与 L2，不生成后端代码",
        "blocked": "⛔ 阻塞：存在未确认的参数、依据或假设",
        "failed": "❌ 失败：存在校验错误或测试未通过",
    }.get(status, status)
    lines += [f"**状态：** {status_text}", ""]
    if spec is not None:
        lines += [
            f"- 模型：`{spec.model.id}` {spec.model.version}（意图 `{spec.model.intent}`）",
            f"- 角色：{', '.join(f'{r.id}×{r.count}' for r in spec.roles)}",
        ]
    if ir_hash:
        lines.append(f"- IR 哈希：`{ir_hash}`")
    lines.append("")

    if spec is not None:
        items = _basis_items(spec)
        pending = [
            i
            for i in items
            if i[2].status != "user_specified"
            and i[2].confirmation != "confirmed"
            and not (references is not None and i[0] == "参数" and i[2].confirmation != "rejected")
        ]
        inferred = [i for i in items if i[2].status == "inferred"]
        conflicting = [i for i in items if i[2].status == "conflicting"]
        lines += ["## 1. 待确认参数与规则", ""]
        if pending:
            lines += _table(
                [
                    [k, f"`{i}`", v, _STATUS[b.status], _CONFIRM[b.confirmation], _evidence(b)]
                    for k, i, b, v in pending
                ],
                ["类别", "ID", "取值", "依据", "确认", "证据"],
            )
        else:
            lines.append("无。")
        if references:
            from afsim_research_agent.ir.references import render_section as reference_section

            lines += ["", *reference_section(references)]
        lines += ["", "## 2. 推断规则", ""]
        if inferred:
            lines += _table(
                [[k, f"`{i}`", v, b.rationale or "—", _evidence(b)] for k, i, b, v in inferred],
                ["类别", "ID", "取值", "推断理由", "证据"],
            )
        else:
            lines.append("无。")
        lines += ["", "## 3. 冲突证据", ""]
        if conflicting:
            lines += _table(
                [[k, f"`{i}`", v, b.rationale or "—", _evidence(b)] for k, i, b, v in conflicting],
                ["类别", "ID", "所选值", "选择理由", "冲突证据"],
            )
        else:
            lines.append("无。")
        lines += ["", "## 4. 假设", ""]
        if spec.assumptions:
            lines += _table(
                [
                    [
                        f"`{a.id}`",
                        a.statement,
                        ", ".join(a.affects) or "—",
                        "是" if a.requires_confirmation else "否",
                        _CONFIRM[a.confirmation],
                    ]
                    for a in spec.assumptions
                ],
                ["ID", "内容", "影响", "需确认", "状态"],
            )
        else:
            lines.append("无。")
        user_values = [i for i in items if i[2].status == "user_specified"]
        if user_values:
            lines += [
                "",
                f"另有 {len(user_values)} 项为用户指定值（无需证据，但请确认取值是否合适）。",
            ]
        lines.append("")

    errors = [d for d in diagnostics if d.severity is Severity.ERROR]
    warnings = [d for d in diagnostics if d.severity is Severity.WARNING]
    lines += ["## 5. 校验结果", "", f"错误 {len(errors)}，警告 {len(warnings)}。", ""]
    shown = errors + warnings
    if shown:
        lines += _table(
            [[d.severity.value, d.code, f"`{d.path}`", d.message] for d in shown[:200]],
            ["级别", "代码", "位置", "说明"],
        )
        if len(shown) > 200:
            lines.append(f"（其余 {len(shown) - 200} 条见 validation_report.json）")
    lines.append("")

    if l2 is not None:
        tests = l2["tests"]
        passed = sum(1 for t in tests if t["passed"])
        lines += ["## 6. L2 单步语义测试", "", f"通过 {passed}/{len(tests)}。", ""]
        lines += _table(
            [
                [
                    f"`{t['id']}`",
                    t["role"],
                    "✅" if t["passed"] else "❌",
                    "; ".join(t["failures"]) or "—",
                ]
                for t in tests
            ],
            ["测试", "角色", "结果", "失败原因"],
        )
        lines += ["", "转换覆盖："]
        for role, cov in l2["transition_coverage"].items():
            missing = ", ".join(cov["missing"]) or "无"
            lines.append(f"- {role}：已覆盖 {len(cov['covered'])}，未覆盖 {missing}")
        lines.append("")

    if l3 is not None:
        lines += ["## 7. L3 闭环想定（内置二维仿真）", ""]
        for scn in l3:
            mark = "✅" if scn["passed"] else "❌"
            lines += [f"### {mark} `{scn['id']}`", ""]
            lines.append(
                f"追溯：{', '.join(scn['traces_to']) or '—'}；事件时间：{scn['event_times'] or '—'}"
            )

            lines.append("")
            lines += _table(
                [
                    [
                        f"`{e['id']}`",
                        e["observable"],
                        "✅" if e["passed"] else "❌",
                        str(e["measured"]),
                        str(e["threshold"]),
                        e["detail"],
                    ]
                    for e in scn["expectations"]
                ],
                ["期望", "观测量", "结果", "实测", "阈值", "说明"],
            )
            lines.append("")
        lines += [
            "> 内置仿真为简化二维运动学，用于检查协同逻辑与时序，不代表车辆动力学或武器效果；",
            "> 战术效果仍需人工审查。",
            "",
        ]

    if l3_faithful is not None:
        lines += _faithful_section(l3_faithful)

    if capability is not None:
        from afsim_research_agent.capability.ir_check import render_section

        lines += render_section(capability, 8)
    return "\n".join(lines)


def _faithful_section(l3: list[dict[str, Any]]) -> list[str]:
    lines = [
        "## 7b. L3 忠实模式",
        "",
        "未处理的平台能力缺口在仿真中不提供：读取得到目录默认的“未知”值（如无敌情、默认地形），"
        "相应动作无效。未达成的期望说明战术依赖这些能力；只作提示，不影响构建状态。",
        "",
    ]
    rows = []
    for scn in l3:
        failed = [e["id"] for e in scn["expectations"] if not e["passed"]]
        withheld = "，".join(f"{k}×{v}" for k, v in (scn.get("withheld") or {}).items())
        rows.append(
            [
                f"`{scn['id']}`",
                "✅" if scn["passed"] else "❌",
                ", ".join(failed) or "—",
                withheld or "—",
            ]
        )
    lines += _table(rows, ["想定", "结果", "未达成的期望", "用到的未提供能力（次数）"])
    lines.append("")
    return lines
