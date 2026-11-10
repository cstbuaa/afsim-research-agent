"""S5a (design ``training-data-construction-v1`` §3, training plan §3.2): repair samples by
error injection. A gold sheet gets one typical error (E1–E10); the sample is what the online
repair loop (``fixer.fix_sheet``) shows at its first step and the step that fixes it:

- L1 errors: prompt → ``edit``;
- L2 / L3 / obligation errors (the loop wants a diagnosis first): prompt → ``diagnose``, then
  the prompt after that diagnosis → ``edit``.

A sample is kept only when the checker reports a problem after the injection and the edit,
run through the loop's own tools (``SheetFixer.diagnose`` / ``edit``), brings the sheet back
to all checks passing. Which checker items failed is recorded, not assumed."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from afsim_research_agent.falcon.sheet.blocks import Block, split_sheet
from afsim_research_agent.falcon.v3.fixer import SheetFixer, _prompt, _rebuild
from afsim_research_agent.training.render import drafter

MAX_STEPS = 20  # the loop's step budget the prompt reports (fix_sheet default)


@dataclass
class Injection:
    rule: str  # E1 … E10
    block: str  # block key
    before: str  # original snippet (unique in the block)
    after: str  # injected snippet
    what: str  # the error, one line (meta)
    cause: str  # diagnosis cause (L2/L3 samples)
    plan: str  # diagnosis plan / edit reason


@dataclass
class RepairSample:
    rule: str
    stage: str  # l1 | behaviour
    steps: list[dict[str, str]] = field(default_factory=list)  # [{"prompt", "target"}]
    ok: bool = False
    problems: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)  # checker items that failed


# ---------------------------------------------------------------------- injectors
def _lines(text: str) -> list[str]:
    return text.splitlines()


def _state_body(text: str, state: str) -> str | None:
    """The ``def STATE():`` function of a pytree block, text included."""
    m = re.search(rf"^def {state}\(\):\n(?:    .*\n?)+", text, re.M)
    return m.group(0) if m else None


def e1_writer(blocks: dict[str, str]) -> Iterator[Injection]:
    """The search inside the advance is dropped: target_detected reads a field nothing in
    that state writes."""
    t = blocks.get("pytree:leader", "")
    body = _state_body(t, "SEARCH_ADVANCE")
    line = '        tactic("perception.search_for_target")\n'
    if body and line in body:
        yield Injection(
            "E1",
            "pytree:leader",
            body,
            body.replace(line, ""),
            "SEARCH_ADVANCE 里删掉搜索子树",
            "SEARCH_ADVANCE 的出口 target_detected 读 perception.selected_target.valid，"
            "但这个状态里没有搜索动作写它，目标永远不会被发现",
            '在 SEARCH_ADVANCE 的 parallel("all") 里加回 tactic("perception.search_for_target")',
        )


def e2_interface(blocks: dict[str, str]) -> Iterator[Injection]:
    t = blocks.get("pytree:leader", "")
    if "route=approach" in t and t.count("route=approach") == 1:
        yield Injection(
            "E2",
            "pytree:leader",
            "movement.follow_route(route=approach)",
            "movement.follow_route(route=forward)",
            "路线名写成想定接口里没有的 forward",
            "follow_route 引用的路线 forward 不在想定接口中",
            "路线改回想定接口的 route=approach",
        )
        yield Injection(
            "E2",
            "pytree:leader",
            "movement.follow_route(route=approach)",
            "movement.follow_route(latitude_deg=30.0, longitude_deg=120.0)",
            "路线写成经纬度参数",
            "follow_route 没有经纬度参数，位置和路线只能引用想定接口",
            "改回 route=approach",
        )


def e3_role(blocks: dict[str, str]) -> Iterator[Injection]:
    t = blocks.get("pytree:member", "")
    body = _state_body(t, "TRACK")
    line = '    tactic("perception.track_selected_target")\n'
    if body and line in body:
        new = (
            "def TRACK():\n"
            '    with parallel("all"):\n'
            '        tactic("perception.track_selected_target")\n'
            "        formation.command_standoff(desired_range_m=500)\n"
        )
        yield Injection(
            "E3",
            "pytree:member",
            body,
            body.replace(body, new),
            "僚车 TRACK 里加了长车专用的保持距离指挥",
            "formation.command_standoff 是长车指挥动作，僚车只负责本车搜索与跟踪",
            '僚车 TRACK 只保留 tactic("perception.track_selected_target")',
        )


def e4_entry(blocks: dict[str, str]) -> Iterator[Injection]:
    t = blocks.get("conditions", "")
    guard = " and time_in_state() >= 3 * s"
    if guard in t and t.count(guard) == 1:
        line = next(x for x in _lines(t) if guard in x)
        yield Injection(
            "E4",
            "conditions",
            line,
            line.replace(guard, ""),
            "target_lost 去掉时间保护",
            "进入跟踪状态的同一拍目标还没被选中，target_lost 立即成立，状态一进入就离开",
            "target_lost 加回 and time_in_state() >= 3 * s",
        )


def e5_exit(blocks: dict[str, str]) -> Iterator[Injection]:
    for key in ("mermaid:leader", "mermaid:member", "mermaid:observer"):
        t = blocks.get(key, "")
        for line in _lines(t):
            if re.match(r"\s+\w+ --> \w+ : target_lost$", line) and t.count(line) == 1:
                yield Injection(
                    "E5",
                    key,
                    line + "\n",
                    "",
                    f"{key} 删掉 target_lost 出口",
                    f"{line.strip().split(' -->')[0]} 没有 target_lost 的出口，"
                    "目标丢失后停在这个状态",
                    f"加回转换 {line.strip()}",
                )
                break


def e6_tree_event(blocks: dict[str, str]) -> Iterator[Injection]:
    for key in ("pytree:leader", "pytree:member", "pytree:observer"):
        t = blocks.get(key, "")
        body = _state_body(t, "SHUTDOWN")
        if not body:
            continue
        inner = body.split("\n", 1)[1]
        new = "def SHUTDOWN():\n    with repeat(count=100):\n" + "".join(
            "    " + x + "\n" for x in inner.splitlines() if x.strip()
        )
        yield Injection(
            "E6",
            key,
            body,
            new,
            f"{key} SHUTDOWN 的收尾子树套进 100 次重复",
            "SHUTDOWN 的出口 shutdown_done 等树执行成功，但收尾子树被重复 100 次，"
            "想定结束前树都不会完成",
            "去掉 repeat(count=100)，收尾子树执行一次",
        )
        break


def e7_safety(blocks: dict[str, str]) -> Iterator[Injection]:
    t = blocks.get("pytree:leader", "")
    old = 'tactic("formation_standoff.formation_safe_shutdown")'
    if old in t and t.count(old) == 1:
        yield Injection(
            "E7",
            "pytree:leader",
            old,
            'tactic("perception.safe_sensor_shutdown")',
            "长车 SHUTDOWN 只关雷达不停车",
            "长车收尾用了只关雷达的子树，没有先停车，违反“先停车后关雷达”",
            '长车 SHUTDOWN 改回 tactic("formation_standoff.formation_safe_shutdown")',
        )


def e8_reference(blocks: dict[str, str]) -> Iterator[Injection]:
    for key in ("mermaid:leader", "mermaid:observer"):
        t = blocks.get(key, "")
        if "--> SHUTDOWN : cancelled" in t:
            yield Injection(
                "E8",
                key,
                "--> SHUTDOWN : cancelled",
                "--> SHUTDWN : cancelled",
                "撤收转换的目标状态名拼错",
                "转换指向不存在的状态 SHUTDWN",
                "目标状态改回 SHUTDOWN",
            )
            break
    t = blocks.get("pytree:leader", "")
    old = "formation_standoff.formation_standoff_track"
    if t.count(old) == 1:
        yield Injection(
            "E8",
            "pytree:leader",
            old,
            "formation_standoff.formation_standof_track",
            "子树名拼错",
            "战术集里没有 formation_standof_track",
            "改回 formation_standoff.formation_standoff_track",
        )
    m = re.search(r"desired_range_m=([\d.]+)", t)
    if m and t.count(m.group(0)) == 1:
        yield Injection(
            "E8",
            "pytree:leader",
            m.group(0),
            m.group(0).replace("desired_range_m", "desired_range"),
            "参数名拼错",
            "子树没有参数 desired_range",
            "参数名改回 desired_range_m",
        )


def e9_value(blocks: dict[str, str]) -> Iterator[Injection]:
    t = blocks.get("model", "")
    for name in ("reacquire_time_up", "search_time_up"):
        m = re.search(rf"  {name}:\n(?:    .*\n)*?    after: ['\"]?(\d+) s['\"]?", t)
        if m:
            v = int(m.group(1))
            wrong = v * 10
            yield Injection(
                "E9",
                "model",
                m.group(0),
                m.group(0).replace(f"{v} s", f"{wrong} s"),
                f"{name} 的时限 {v} s 写成 {wrong} s",
                f"{name} 的时限与需求不符（需求为 {v} 秒），想定里到时没有转换",
                f"{name} 的 after 改回 {v} s",
            )


def e10_cancel(blocks: dict[str, str]) -> Iterator[Injection]:
    for key in ("mermaid:leader", "mermaid:member", "mermaid:observer"):
        t = blocks.get(key, "")
        line = "  * --> SHUTDOWN : cancelled\n"
        if line in t:
            yield Injection(
                "E10",
                key,
                line,
                "",
                f"{key} 删掉全局撤收转换",
                f"{key.split(':')[1]} 没有 cancelled 的全局转换，收到撤收命令不响应",
                "加回 * --> SHUTDOWN : cancelled",
            )


INJECTORS: list[Callable[[dict[str, str]], Iterator[Injection]]] = [
    e1_writer,
    e2_interface,
    e3_role,
    e4_entry,
    e5_exit,
    e6_tree_event,
    e7_safety,
    e8_reference,
    e9_value,
    e10_cancel,
]


# ---------------------------------------------------------------------- samples
def _unique_window(text: str, start: int, end: int, original: str) -> tuple[str, str] | None:
    """The smallest whole-line window around [start, end) of ``text`` that occurs once, and
    the same window with the original snippet back (the edit's old / new)."""
    lines = text.splitlines(keepends=True)
    offs = [0]
    for x in lines:
        offs.append(offs[-1] + len(x))
    lo = max(i for i in range(len(lines) + 1) if offs[i] <= start)
    hi = min(i for i in range(len(offs)) if offs[i] >= end) if end > start else lo + 1
    hi = max(hi, lo + 1)
    while True:
        a, b = offs[max(lo, 0)], offs[min(hi, len(lines))]
        window = text[a:b]
        if window.strip() and text.count(window) == 1:
            injected_part = text[start:end]
            fixed = window[: start - a] + original + window[start - a + len(injected_part) :]
            return window.rstrip("\n"), fixed.rstrip("\n")
        if lo <= 0 and hi >= len(lines):
            return None
        lo, hi = lo - 1, hi + 1


def _failed(check: Any) -> list[str]:
    out = [f"L1:{p.render()[2:][:120]}" for p in check.problems[:6]]
    if check.l1_ok:
        out += [f"L2:{t['id']}" for t in check.l2 if not t["passed"]]
        out += [f"L3:{s['id']}" for s in check.l3 if not s["passed"]]
    return out


def _json(thought: str, action: str, args: dict[str, Any]) -> str:
    return json.dumps({"thought": thought, "action": action, "args": args}, ensure_ascii=False)


def make(
    gold_sheet: str,
    request: dict[str, Any],
    requirement: str,
    facts: list[str],
    inj: Injection,
) -> RepairSample:
    out = RepairSample(inj.rule, "")
    d = drafter(request, requirement, facts)
    gold = split_sheet(gold_sheet)[0]
    d.blocks = [Block(b.kind, b.role, b.text, b.line, b.info) for b in gold]
    d.slices = d._slices(d.compile(pending=True))
    asyncio.run(d.obligations_stage())
    texts = {b.key: b.text for b in d.blocks}
    text = texts[inj.block]
    if text.count(inj.before) != 1:
        out.problems.append("原文片段不唯一")
        return out
    start = text.index(inj.before)
    texts[inj.block] = text.replace(inj.before, inj.after)
    d.blocks = _rebuild(d.blocks, texts)
    window = _unique_window(texts[inj.block], start, start + len(inj.after), inj.before)
    if window is None:
        out.problems.append("找不到唯一的修改片段")
        return out
    old, new = window
    fixer = SheetFixer(d)
    check = fixer.last
    if check.verified:
        out.problems.append("注入后检查仍全部通过")
        return out
    out.failed = _failed(check)
    out.stage = "l1" if not check.l1_ok else "behaviour"
    changes = [{"block": inj.block, "old": old, "new": new}]
    records: list[dict[str, Any]] = []
    if fixer.needs_diagnosis:
        found = [f for f in fixer.findings() if f.sheet]
        issue = found[0].render() if found else (out.failed[0] if out.failed else inj.what)
        where = [inj.block]
        args = {"issue": issue[:400], "where": where, "cause": inj.cause, "plan": inj.plan}
        thought = "先按检查结果写诊断"
        out.steps.append(
            {
                "prompt": _prompt(fixer, records, MAX_STEPS),
                "target": _json(thought, "diagnose", args),
            }
        )
        fixer.step = 1
        result = fixer.diagnose(args)
        records.append(
            {"step": 1, "action": "diagnose", "args": args, "thought": thought, "result": result}
        )
    thought = inj.plan
    prompt = _prompt(fixer, records, MAX_STEPS - len(records))
    out.steps.append(
        {
            "prompt": prompt,
            "target": _json(thought, "edit", {"changes": changes, "reason": inj.plan}),
        }
    )
    fixer.step = len(records) + 1
    result = fixer.edit(changes, inj.plan)
    if not fixer.solved:
        out.problems.append("按目标修改后检查仍未全部通过：" + result[:300])
        return out
    after = {b.key: b.text.strip() for b in d.blocks}
    want = {b.key: b.text.strip() for b in gold}
    if after != want:
        out.problems.append("修改后与原稿不一致")
        return out
    out.ok = True
    return out


def injections(gold_sheet: str) -> list[Injection]:
    blocks = {b.key: b.text for b in split_sheet(gold_sheet)[0]}
    return [inj for f in INJECTORS for inj in f(blocks)]
