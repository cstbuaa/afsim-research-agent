"""Split a tactic sheet into its fenced blocks and put revised blocks back.

A block is a fenced code block whose info string names its kind:

    ```yaml model        ```mermaid platoon_leader      ```python conditions
    ```xml platoon_leader   ```yaml basis               ```yaml verification
    ```python platoon_leader  (behavior trees as Python functions, ``pytree.py``)
    ```yaml needs             (capabilities outside the vocabulary → capability gaps)

Mermaid, XML and Python tree blocks belong to one role; the role may also be given inside the
block (``%% role: x`` / ``<root role="x">``). Line numbers are 1-based lines of the
whole sheet, so problems point at what a person sees in ``tactic_sheet.md``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_BLOCK_CHARS = 60_000
KINDS = ("model", "mermaid", "conditions", "xml", "pytree", "basis", "needs", "verification")
TREE_KINDS = ("xml", "pytree")  # a role's behavior trees: either form, or both
_FENCE = re.compile(r"^(?P<indent>\s*)(?P<ticks>`{3,}|~{3,})\s*(?P<info>[^\n`]*)$")
_ROLE_MERMAID = re.compile(r"%%\s*role\s*[:：]\s*([A-Za-z][\w]*)")
_ROLE_XML = re.compile(r"""<root\b[^>]*\brole\s*=\s*["']([A-Za-z][\w]*)["']""")
_ROLE_XML_COMMENT = re.compile(r"<!--\s*role\s*[:：]\s*([A-Za-z][\w]*)\s*-->")


@dataclass
class Block:
    kind: str  # one of KINDS
    role: str | None
    text: str
    line: int  # sheet line of the first content line
    info: str = ""

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.role}" if self.role else self.kind

    @property
    def label(self) -> str:
        names = {
            "model": "yaml model",
            "mermaid": "mermaid",
            "conditions": "python conditions",
            "xml": "xml",
            "pytree": "python",
            "basis": "yaml basis",
            "needs": "yaml needs",
            "verification": "yaml verification",
        }
        return names[self.kind] + (f" {self.role}" if self.role else "")

    def fence(self) -> str:
        return f"```{self.label}\n{self.text.rstrip()}\n```"

    def numbered(self) -> str:
        return "\n".join(
            f"{self.line + i:4d} | {text}" for i, text in enumerate(self.text.splitlines())
        )


@dataclass
class SheetProblem:
    """A hole: something the compiler could not parse or link."""

    block: str  # Block.key
    line: int | None
    message: str
    candidates: list[str] = field(default_factory=list)
    code: str = "SHEET"
    severity: str = "error"  # "warning": compiled with a default, not sent for repair

    def render(self) -> str:
        where = f"第 {self.line} 行" if self.line else "整块"
        near = f"（可选：{', '.join(self.candidates[:5])}）" if self.candidates else ""
        return f"- [{self.block} {where}] {self.message}{near}"


def _kind(info: str) -> tuple[str | None, str | None]:
    words = info.replace(",", " ").split()
    if not words:
        return None, None
    lang = words[0].lower()
    rest = words[1:]
    if lang in ("yaml", "yml"):
        name = rest[0].lower() if rest else ""
        kind = {
            "model": "model",
            "basis": "basis",
            "needs": "needs",
            "verification": "verification",
        }.get(name)
        if kind is None and name in ("tests", "verify", "validation"):
            kind = "verification"
        return kind, None
    if lang == "mermaid":
        return "mermaid", rest[0] if rest else None
    if lang in ("python", "py"):
        if not rest or rest[0].lower() in ("conditions", "condition"):
            return "conditions", None
        return "pytree", rest[0]
    if lang == "xml":
        return "xml", rest[0] if rest else None
    return None, None


def split_sheet(text: str) -> tuple[list[Block], list[SheetProblem]]:
    blocks: list[Block] = []
    problems: list[SheetProblem] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        match = _FENCE.match(lines[i])
        if not match:
            i += 1
            continue
        ticks, info = match.group("ticks"), match.group("info").strip()
        start = i + 1
        j = start
        while j < len(lines) and not lines[j].strip().startswith(ticks):
            j += 1
        if j >= len(lines):
            problems.append(SheetProblem("sheet", i + 1, f"代码块 ```{info} 没有结束标记"))
        body = "\n".join(lines[start:j])
        kind, role = _kind(info)
        if kind is None:
            problems.append(
                SheetProblem(
                    "sheet", i + 1, f"无法识别的代码块类型 '{info}'（已忽略）", severity="warning"
                )
            )
        elif len(body) > MAX_BLOCK_CHARS:
            problems.append(SheetProblem(kind, i + 1, f"代码块过长（>{MAX_BLOCK_CHARS} 字符）"))
        else:
            if kind == "mermaid" and role is None:
                found = _ROLE_MERMAID.search(body)
                role = found.group(1) if found else None
            if kind == "xml" and role is None:
                found = _ROLE_XML.search(body) or _ROLE_XML_COMMENT.search(body)
                role = found.group(1) if found else None
            if kind in ("mermaid", "xml") and role is None:
                problems.append(
                    SheetProblem(kind, i + 1, f"{kind} 代码块没有写明角色（```{kind} <角色>）")
                )
            else:
                blocks.append(Block(kind, role, body, start + 1, info))
        i = j + 1
    seen: dict[str, Block] = {}
    for block in blocks:
        if block.key in seen:
            problems.append(
                SheetProblem(
                    block.key,
                    block.line,
                    f"重复的代码块 {block.label}（使用后一个）",
                    severity="warning",
                )
            )
        seen[block.key] = block
    return list(seen.values()), problems


def join_sheet(blocks: list[Block], title: str = "") -> str:
    """A sheet from blocks in the canonical order (model, mermaid…, conditions, xml…,
    python…, basis, needs, verification); ``blocks`` is updated with the new line numbers."""
    order = {k: i for i, k in enumerate(KINDS)}
    ordered = sorted(blocks, key=lambda b: (order[b.kind], b.role or ""))
    head = f"# 战术稿：{title}" if title else "# 战术稿"
    text = head + "\n\n" + "\n\n".join(b.fence() for b in ordered) + "\n"
    blocks[:] = split_sheet(text)[0]
    return text
