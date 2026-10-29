"""Validation diagnostics shared by every IR stage."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


CODES: dict[str, str] = {
    "IR001": "Schema 版本缺失或不支持",
    "IR002": "未知字段、字段类型错误或 YAML 格式违规",
    "ID001": "标识符非法或重复",
    "REF001": "引用无法解析",
    "UNIT001": "单位缺失、未知或量纲不匹配",
    "TYPE001": "表达式或黑板类型不匹配",
    "BEH001": "行为缺少成功、失败、运行或超时语义",
    "ACT001": "动作不在白名单或参数非法（IR 2.0：动作 ID 不在能力集中，或不适用于该平台类别）",
    "ACT002": "动作参数不符合契约：缺必填、未知参数、类型、单位或范围（IR 2.0）",
    "TAC001": "引用的战术集子树或任务状态机不存在，或覆盖了未公开的参数（IR 2.0）",
    "BB001": "条件读取了未导入、也未声明的黑板字段，或自定义字段不在 mission.* 下（IR 2.0）",
    "ARB001": "并行分支争用同一独占资源，或保持了未知资源（IR 2.0）",
    "RES001": "资源冲突或资源声明遗漏",
    "TREE001": "行为树结构非法或存在递归",
    "FSM001": "初始状态、转换或兜底分支非法；IR 2.0 另含非终止状态无法到达安全状态",
    "FSM002": "状态没有对应的状态树，或状态树映射了不存在的状态（IR 2.0）",
    "DEP001": "必需能力未绑定",
    "ASM001": "未确认的关键假设阻塞生成",
    "EVD001": "缺少 basis 或证据引用",
    "EVD002": "basis 未确认或已被拒绝，阻塞规范化",
    "PAR001": "参数未知、未确认或为空，阻塞编译",
    "ROLE001": "角色未定义、指挥关系成环或实例越界",
    "FORM001": "编队槽位重复、偏移无单位或容差缺失",
    "COORD001": "协同模式不在目录中或引用无法解析",
    "BIND001": "后端无法为逻辑能力提供绑定",
    "SCN001": "测试/想定引用非法或覆盖率不足",
    "TEST001": "L2 测试未通过",
    "L3001": "L3 想定期望未满足",
}


class Diagnostic(BaseModel):
    """One validation finding (design §22)."""

    model_config = ConfigDict(frozen=True)

    severity: Severity
    code: str
    path: str
    message: str
    source_id: str | None = None
    suggestion: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)

    def render(self) -> str:
        return f"[{self.severity.value}] {self.code} {self.path}: {self.message}"


class Collector:
    """Accumulates diagnostics with small helpers."""

    def __init__(self) -> None:
        self.items: list[Diagnostic] = []

    def add(
        self,
        severity: Severity,
        code: str,
        path: str,
        message: str,
        *,
        suggestion: str | None = None,
        source_id: str | None = None,
        evidence_ids: Iterable[str] = (),
    ) -> None:
        self.items.append(
            Diagnostic(
                severity=severity,
                code=code,
                path=path,
                message=message,
                suggestion=suggestion,
                source_id=source_id,
                evidence_ids=list(evidence_ids),
            )
        )

    def error(self, code: str, path: str, message: str, **kw: object) -> None:
        self.add(Severity.ERROR, code, path, message, **kw)  # type: ignore[arg-type]

    def warning(self, code: str, path: str, message: str, **kw: object) -> None:
        self.add(Severity.WARNING, code, path, message, **kw)  # type: ignore[arg-type]

    def info(self, code: str, path: str, message: str, **kw: object) -> None:
        self.add(Severity.INFO, code, path, message, **kw)  # type: ignore[arg-type]

    def extend(self, items: Iterable[Diagnostic]) -> None:
        self.items.extend(items)

    @property
    def has_errors(self) -> bool:
        return has_errors(self.items)


def has_errors(items: Iterable[Diagnostic]) -> bool:
    return any(item.severity is Severity.ERROR for item in items)


def sort_diagnostics(items: Iterable[Diagnostic]) -> list[Diagnostic]:
    order = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}
    return sorted(items, key=lambda d: (order[d.severity], d.code, d.path, d.message))
