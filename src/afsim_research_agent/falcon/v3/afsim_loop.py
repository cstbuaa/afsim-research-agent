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
    results: list[RunJudgement] | None,
    runs: list[RunConfig],
    compile_error: str | None,
    missions: dict[str, Any] | None = None,  # role → mission (to name looping transitions)
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
        others = sorted(
            {t for e in res.engagements.values() for t in e.get("targets", {})} - set(labels)
        )
        targets = {t: f"目标 {i + 1}" for i, t in enumerate(others)}

        def anon(text: str, labels: dict[str, str] = labels) -> str:
            for name in sorted(labels, key=len, reverse=True):
                text = re.sub(rf"\b{re.escape(name)}\b", labels[name], text)
            return text

        for name, seq in res.states.items():
            if name not in labels:
                continue
            path = " -> ".join(f"{s}@{t:g}" for t, s in seq[:40])
            more = f" …（共 {len(seq)} 次进入）" if len(seq) > 40 else ""
            lines.append(f"  {labels[name]} 状态轨迹：{path}{more}")
        for name, e in res.engagements.items():
            if name in labels:
                hit = "，".join(f"{targets.get(t, t)} {n} 次" for t, n in e["targets"].items())
                lines.append(
                    f"  {labels[name]} 交战：开火 {e['fired']} 次，命中 {e['hits']} 次"
                    + (f"（{hit}）" if hit else "")
                    + (f"，击毁 {e['kills']}" if e["kills"] else "")
                )
        for name, acts in (getattr(res, "failures", {}) or {}).items():
            if name not in labels:
                continue
            for action, f in acts.items():
                # a code the capability set states; anything else is a native string
                code = f["code"] if f.get("known") else "代码只在本地记录"
                lines.append(
                    f"  {labels[name]} 动作失败：{action}（{code}；{f['count']} 次，"
                    f"首次 {f['first_s']:g} s）"
                )
        issues = list(getattr(res, "issues", []) or [])
        roles = {p.name: p.role for p in run.platforms}
        for issue in issues:
            line = "  问题：" + _issue(issue, labels, targets, anon)
            if issue.code == "IR_LOOP" and missions:
                path = _loop_path(
                    issue.data.get("chain") or [], missions.get(roles.get(issue.platform or "", ""))
                )
                if path:
                    line += f"。这一拍的路径：{path}——这些转换条件在进入各状态时已经同时成立"
            lines.append(line)
        if not issues and (not res.completed or res.errors):
            lines.append("  问题：AFSIM 没有完成仿真（细节只在本地记录）")
    return "\n".join(lines)


def _loop_path(chain: list[str], mission: Any) -> str:
    """A -(condition)-> B -(condition)-> ... for the states entered in one update."""
    if not chain or not isinstance(mission, dict):
        return ""
    trans = list(mission.get("transitions") or []) + [
        {"from": "*", **g} for g in mission.get("global") or []
    ]
    out = [chain[0]]
    for a, b in zip(chain, chain[1:], strict=False):
        on = next((t["on"] for t in trans if t["from"] in (a, "*") and t["to"] == b), "?")
        out.append(f"-({on})-> {b}")
    return " ".join(out)


def _issue(issue: Any, labels: dict[str, str], targets: dict[str, str], anon: Any) -> str:
    """One finding in the words of the draft (see the module doc for what stays local)."""
    d, who = issue.data, labels.get(issue.platform or "", issue.platform or "")
    match issue.code:
        case "AFSIM_ERROR":
            if issue.source == "infrastructure":
                return (
                    "AFSIM 输入错误，出在绑定或编译器（不是战术稿的问题，已记在本地，"
                    "不需要改战术稿）"
                )
            where = f"{d.get('role')} 状态 {d.get('state')}"
            if d.get("action"):
                where += f" 的动作 {d['action']}"
            return f"AFSIM 运行中 {where} 处的脚本出错（检查给这个动作的参数和进入条件）"
        case "NOT_COMPLETED":
            return "AFSIM 没有完成仿真"
        case "IR_LOOP":
            return (
                f"{who} 在 {d['state']} 一次更新内连续转换超过 8 次被截断：进入后各转换条件"
                "立即成立，状态一路连跳（常见原因：条件读到上一轮留下的值，如开火计数仍大于 0、"
                "毁伤时刻仍有效；应改为本轮新产生的变化或用树事件/计时）"
            )
        case "NO_STATE_OUTPUT":
            return f"{who} 的状态机没有运行"
        case "NO_EVIDENCE":
            return "所有车辆的状态机都没有运行"
        case "INERT":
            return "仿真跑完了但什么都没发生：没有动作开始，也没有车辆离开初始状态"
        case "SEQUENCE":
            return f"{who} 的状态序列是 {d['got']}，想定要求 {d['expected']}"
        case "MARKER":
            act = IR_ACT_MARKER.search(str(d.get("pattern") or ""))
            if act:
                return (
                    f"{anon(act.group(1))} 从未开始动作 {act.group(2).replace(chr(92) + '.', '.')}"
                )
            if d.get("note"):
                return f"运行检查未满足：{d['note']}"
            return "一项运行检查未满足（检查内容只在本地记录）"
        case "EVENTS":
            what = d.get("note") or (
                {
                    "WEAPON_FIRED": "开火",
                    "WEAPON_HIT": "命中",
                    "WEAPON_MISSED": "脱靶",
                    "PLATFORM_BROKEN": "击毁",
                }.get(d["kind"], d["kind"])
                + (f"（{d['by']} 角色）" if d.get("by") else "")
                + (f"（{targets.get(d['target'], '指定目标')}）" if d.get("target") else "")
            )
            return f"AFSIM 事件记录未满足：{what}——出现 {d['count']} 次，要求至少 {d['min']} 次"
        case "REFERENCE_SEQUENCE":
            return f"{who} 的状态序列 {d['got']} 与手写参考版本不同"
        case "INTERVAL":
            return (
                f"{who} 在 {d['state']} 停留 {d['stay']:g} s（{d['entered']:g} s 进入），"
                f"与手写参考版本相差超过 {d['limit']:g} s"
            )
        case "REFERENCE_MISSING":
            return "（本地参考记录缺失，不影响本稿）"
        case "OBLIGATION":
            return f"需求约束 {d['id']} 未满足：{anon(str(d['detail']))}"
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
