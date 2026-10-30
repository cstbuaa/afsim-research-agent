"""AFSIM in the drafting loop: compile the chosen draft, run it in AFSIM, and hand what the
run showed back to the repair loop as one more failed check.

The feedback is written for a model service, so it carries only what the draft itself
already names — its state names, the capability set's action ids, its requirement
obligations — and the role instance of each platform (``member_1``), never the platform
names, positions, native script or auxiliary-data names, file paths or raw AFSIM output of
the local run. A run expectation (marker) is described by its ``marker_notes`` entry in the
run configuration, or only counted when it has none.

One round: compile → run → if a run fails, the repair loop gets the feedback as a failed
check named ``afsim``; the check stays failed until the sheet changes, then the repair
loop may finish once its 2D checks pass, and the next round runs AFSIM again.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from afsim_research_agent.ir.v2.afsim import Binding, CompileError, Compiler, RunConfig
from afsim_research_agent.ir.v2.afsim.evaluate import RunJudgement, run_package

SAFE_ERRORS = (re.compile(r"Duplicate next_state (\w+) in state (\w+)"),)
IR_ACT_MARKER = re.compile(r"IR_ACT\\\|\[\^\|\]\+\\\|(\w+)\\\|(.+)$")


@dataclass
class AfsimConfig:
    binding: Path
    runs: list[Path]
    mission: str
    rounds: int = 2  # repair rounds after a failed run
    steps: int = 15  # repair loop steps per round
    timeout_s: float = 300.0


@dataclass
class AfsimRound:
    index: int
    passed: bool
    feedback: str  # sanitized, what the repair loop sees
    local: str  # full judgement, kept in the package only
    package: Path | None = None


@dataclass
class AfsimNote:
    sheet_hash: str
    feedback: str


@dataclass
class AfsimHistory:
    rounds: list[AfsimRound] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.rounds) and self.rounds[-1].passed


def sheet_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def run_once(
    model: dict[str, Any],
    contracts: Any,
    obligations: list[dict[str, Any]],
    cfg: AfsimConfig,
    out: Path,
) -> tuple[list[RunJudgement] | None, list[RunConfig], str | None]:
    """Compile and run; (judgements, runs, compile error)."""
    runs = [RunConfig.load(r) for r in cfg.runs]
    try:
        compiler = Compiler(model, contracts, Binding.load(cfg.binding))
        package = compiler.package(runs, obligations=obligations)
        compiler.write(package, out)
    except CompileError as exc:
        return None, runs, str(exc)
    return run_package(out, cfg.mission, cfg.timeout_s), runs, None


def feedback(
    results: list[RunJudgement] | None, runs: list[RunConfig], compile_error: str | None
) -> str:
    """What a failed AFSIM round tells the repair loop (sanitized, see the module doc)."""
    if compile_error is not None:
        # the binding's messages name native entries: say only which kind of entry is missing
        kind = "动作或字段" if "binding has no entry" in compile_error else "模型"
        return (
            f"afsim: FAIL\n  AFSIM 编译失败（{kind}在本地 AFSIM 绑定中无法生成，细节只在本地记录）"
        )
    lines = [
        "afsim: FAIL",
        "  （真实 AFSIM 运行的结果，二维仿真看不到这些问题；改动战术稿后此项待下一次 AFSIM"
        "运行判定，L1/L2/L3 仍须通过）",
    ]
    for res, run in zip(results or [], runs, strict=False):
        labels = {p.name: f"{p.role}_{p.instance}" for p in run.platforms}
        notes = dict(run.expect.marker_notes)

        def anon(text: str, labels: dict[str, str] = labels) -> str:
            for name in sorted(labels, key=len, reverse=True):
                text = re.sub(rf"\b{re.escape(name)}\b", labels[name], text)
            return text

        if not res.completed or res.errors:
            safe = []
            for e in res.errors:
                for rx in SAFE_ERRORS:
                    m = rx.search(e)
                    if m:
                        safe.append(m.group(0))
            lines.append(
                "  AFSIM 没有完成仿真"
                + (f"：{'; '.join(safe)}" if safe else "（输入错误，细节只在本地记录）")
            )
        for name, seq in res.states.items():
            if name not in labels:
                continue
            path = " -> ".join(f"{s}@{t:g}" for t, s in seq[:40])
            more = f" …（共 {len(seq)} 次进入）" if len(seq) > 40 else ""
            lines.append(f"  {labels[name]} 状态轨迹：{path}{more}")
        for p in res.problems:
            lines.append("  问题：" + _problem(p, anon, notes))
    return "\n".join(lines)


def _problem(p: str, anon: Any, notes: dict[str, str]) -> str:
    m = re.match(r"console has no line matching '(.*)'$", p)
    if m:
        pattern = m.group(1).replace("\\\\", "\\")
        act = IR_ACT_MARKER.search(pattern)
        if act:
            action = act.group(2).replace("\\.", ".")
            return f"{anon(act.group(1))} 从未开始动作 {action}"
        if pattern in notes:
            return f"运行检查未满足：{notes[pattern]}"
        return "一项运行检查未满足（检查内容只在本地记录）"
    m = re.match(r"(\S+): transitions kept firing from (\w+) within one update", p)
    if m:
        return (
            f"{anon(m.group(1))} 在 {m.group(2)} 一次更新内连续转换超过 8 次被截断："
            "进入后各转换条件立即成立，状态一路连跳（常见原因：条件读到上一轮留下的值，"
            "如开火计数仍大于 0、毁伤时刻仍有效；应改为本轮新产生的变化或用树事件/计时）"
        )
    m = re.match(r"(\S+) wrote no IR_STATE lines", p)
    if m:
        return f"{anon(m.group(1))} 的状态机没有运行"
    m = re.match(r"(\S+) visited (\[.*\]), expected (\[.*\])$", p)
    if m:
        return f"{anon(m.group(1))} 的状态序列是 {m.group(2)}，想定要求 {m.group(3)}"
    if "differs from the handwritten version" in p:
        return "状态序列与手写参考版本不同"
    if "stayed" in p and "handwritten version" in p:
        return anon(re.sub(r", the handwritten version .*$", "", p)) + "（与手写参考版本相差过大）"
    m = re.match(r"requirement obligation (\w+): (.*)$", p, re.S)
    if m:
        return f"需求约束 {m.group(1)} 未满足：{anon(m.group(2))}"
    if "handwritten reference console was not found" in p:
        return "（本地参考记录缺失，不影响本稿）"
    return "一项运行检查未满足（细节只在本地记录）"


def note_entry(note: AfsimNote) -> dict[str, Any]:
    """The failed check the repair loop sees while the sheet is unchanged."""
    return {
        "id": "afsim",
        "passed": False,
        "afsim": True,
        "render": note.feedback,
        "obligations": [],
        "idealized": [],
        "placeholders": [],
        "unresolved": [],
        "states": {},
        "transitions": {},
        "duration_s": 0.0,
    }
