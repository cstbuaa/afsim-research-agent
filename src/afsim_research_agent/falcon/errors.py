"""Falcon error codes and exit codes (design §6)."""

from __future__ import annotations

CODES: dict[str, str] = {
    "FAL001": "Crow 结果缺失、无法解析或未通过 Falcon 输入门槛",
    "FAL002": "输出目录已存在或模型包状态不允许此操作",
    "FAL003": "请求文件无效",
    "FAL004": "模型输出无法使用（多次重试后仍不满足约束）",
}

EXIT_REFUSED = 2
EXIT_INPUT = 3
EXIT_DRAFT_INVALID = 4


class FalconError(Exception):
    """A refusal or input problem; ``details`` are printed one per line."""

    def __init__(self, code: str, message: str, details: list[str] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details or []

    @property
    def exit_code(self) -> int:
        return EXIT_REFUSED if self.code == "FAL001" else EXIT_INPUT

    def render(self) -> str:
        lines = [f"{self.code} {CODES.get(self.code, '')}：{self.message}"]
        lines += [f"  - {item}" for item in self.details]
        return "\n".join(lines)
