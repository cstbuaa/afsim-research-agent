"""The uniform return value of every orchestrator stage and extension tool."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Status = Literal["ok", "failed", "blocked", "needs_human", "not_configured"]

# Keep tool results short: the model sees this text; full outputs live in files.
MAX_PROBLEMS = 12
MAX_LINE = 300


@dataclass
class StageResult:
    """What a stage reports back to the agent.

    ``status``:
      - ``ok``             the stage finished and its checks passed
      - ``failed``         the stage ran but its checks did not pass (see ``problems``)
      - ``blocked``        the stage could not run (bad input, missing file, refused write)
      - ``needs_human``    a person must decide before continuing (see ``question``)
      - ``not_configured`` an extension point has no implementation registered yet
    """

    stage: str
    status: Status
    summary: str
    artifacts: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    question: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "stage": self.stage,
            "status": self.status,
            "summary": self.summary,
        }
        if self.artifacts:
            out["artifacts"] = self.artifacts
        if self.problems:
            shown = [_clip(p) for p in self.problems[:MAX_PROBLEMS]]
            if len(self.problems) > MAX_PROBLEMS:
                shown.append(f"……另有 {len(self.problems) - MAX_PROBLEMS} 条，见产物文件")
            out["problems"] = shown
        if self.next_steps:
            out["next_steps"] = self.next_steps
        if self.question:
            out["question"] = self.question
        if self.data:
            out["data"] = self.data
        return out

    def render(self) -> str:
        """The text a tool returns to the model (compact JSON, Chinese kept readable)."""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=1, default=str)


def _clip(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= MAX_LINE else text[: MAX_LINE - 1] + "…"


def rel(path: Path | str, root: Path) -> str:
    """Show paths relative to the workspace root (never leak absolute local paths)."""
    p = Path(path)
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return p.name
