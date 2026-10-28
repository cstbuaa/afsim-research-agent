"""Research dimensions and the abstract tactic pattern table.

Research questions are planned along six dimensions (situation → action →
exception → basis, extended with roles/geometry, coordination and measures of
effectiveness; design crow-tactic-proposal-v1 §3.1). The pattern table is the
controlled list of abstract tactic patterns the planner may hypothesise (§5.3).
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Literal, get_args

Dimension = Literal[
    "applicability",
    "roles_geometry",
    "sequence_termination",
    "exceptions_abort",
    "coordination_authority",
    "effectiveness",
]

DIMENSIONS: tuple[str, ...] = get_args(Dimension)

DIMENSION_LABELS: dict[str, str] = {
    "applicability": "适用情况",
    "roles_geometry": "角色与几何",
    "sequence_termination": "动作序列与结束条件",
    "exceptions_abort": "例外与中止",
    "coordination_authority": "协同与决策权",
    "effectiveness": "效果衡量",
}

DIMENSION_GUIDE: dict[str, str] = {
    "applicability": "Under what situation, terrain and force ratio is the tactic used?",
    "roles_geometry": "Which unit takes which role; formation, spacing, bearing and altitude?",
    "sequence_termination": "Which steps, what each step does, and when each step is complete?",
    "exceptions_abort": "When is the tactic not used, abandoned midway, or switched to another?",
    "coordination_authority": (
        "Who decides, what information is exchanged, may subordinates decline?"
    ),
    "effectiveness": "Which criteria show that the tactic succeeded?",
}

# Template questions used only when the model leaves a dimension uncovered.
# "{topic}" is the requirement summary.
_TEMPLATES: dict[str, str] = {
    "applicability": (
        "In which situations, terrain and force ratios is this approach used, and when "
        "is it not appropriate? Context: {topic}"
    ),
    "roles_geometry": (
        "What roles, formations, spacings and relative positions do the units take? "
        "Context: {topic}"
    ),
    "sequence_termination": (
        "What is the step-by-step sequence of actions, and what condition ends each step? "
        "Context: {topic}"
    ),
    "exceptions_abort": (
        "Under what conditions is the action abandoned, interrupted, or replaced by another "
        "tactic? Context: {topic}"
    ),
    "coordination_authority": (
        "Who makes the decisions, what reports and signals are exchanged, and can "
        "subordinates refuse or adjust an order? Context: {topic}"
    ),
    "effectiveness": (
        "Which measures of effectiveness or performance are used to judge success? "
        "Context: {topic}"
    ),
}

# Keyword fallback for questions the model did not tag. Lower-case substrings.
_KEYWORDS: dict[str, tuple[str, ...]] = {
    "applicability": (
        "when to", "under what", "which situation", "condition", "terrain", "force ratio",
        "situation", "适用", "情况", "条件", "地形", "态势", "何时采用",
    ),
    "roles_geometry": (
        "formation", "spacing", "interval", "distance between", "role", "position",
        "geometry", "bearing", "altitude", "队形", "编队", "间距", "车距", "角色", "阵型", "方位",
    ),
    "sequence_termination": (
        "sequence", "step", "phase", "procedure", "until", "complete", "transition",
        "trigger", "顺序", "步骤", "阶段", "程序", "结束", "完成", "转换", "触发",
    ),
    "exceptions_abort": (
        "abort", "abandon", "exception", "contingency", "fail", "withdraw", "break off",
        "not use", "中止", "放弃", "例外", "失败", "撤", "应变",
    ),
    "coordination_authority": (
        "coordinat", "communicat", "report", "command", "control", "decide", "decision",
        "signal", "message", "协同", "通信", "报告", "指挥", "决策", "命令", "信号",
    ),
    "effectiveness": (
        "measure", "effectiveness", "performance", "success", "metric", "evaluate",
        "outcome", "效果", "成功", "指标", "评估", "效能",
    ),
}


def dimension_label(dimension: str) -> str:
    return DIMENSION_LABELS.get(dimension, dimension)


def infer_dimensions(question: str) -> list[str]:
    """Keyword fallback when the model gave a question no dimension tags."""
    text = question.lower()
    return [name for name in DIMENSIONS if any(word in text for word in _KEYWORDS[name])]


def template_question(dimension: str, topic: str) -> str:
    topic = re.sub(r"\s+", " ", topic).strip().rstrip(".。")
    return _TEMPLATES[dimension].format(topic=topic)


_PATTERNS = Path(__file__).with_name("catalog") / "tactic_patterns.toml"


@dataclass(frozen=True)
class TacticPattern:
    id: str
    name: str
    en: str
    roles: tuple[str, ...]
    examples: str


@dataclass(frozen=True)
class PatternTable:
    version: str
    patterns: tuple[TacticPattern, ...]

    @classmethod
    @cache
    def default(cls) -> PatternTable:
        return cls.from_file(_PATTERNS)

    @classmethod
    def from_file(cls, path: Path) -> PatternTable:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return cls(
            version=data["meta"]["version"],
            patterns=tuple(
                TacticPattern(
                    id=item["id"],
                    name=item["name"],
                    en=item["en"],
                    roles=tuple(item.get("roles", ())),
                    examples=item.get("examples", ""),
                )
                for item in data["patterns"]
            ),
        )

    @property
    def ids(self) -> set[str]:
        return {pattern.id for pattern in self.patterns}

    def get(self, pattern_id: str) -> TacticPattern | None:
        return next((p for p in self.patterns if p.id == pattern_id), None)

    def prompt_listing(self) -> str:
        return "\n".join(
            f"- {p.id}: {p.en} (roles: {', '.join(p.roles)})" for p in self.patterns
        )
