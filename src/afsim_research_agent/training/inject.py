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
from afsim_research_agent.falcon.v3.fixer import SheetFixer, _clip, _prompt, _rebuild
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


# ------------------------------------------------- generic injectors (any v3 sheet)
_TRANS = re.compile(r"^(\s*)(\w+) --> (\w+) : (\w+)\s*$", re.M)


def g_e2_interface(blocks: dict[str, str]) -> Iterator[Injection]:
    for key, t in blocks.items():
        if not key.startswith("pytree:"):
            continue
        for m in re.finditer(r"\b(route|point)=(\w+)", t):
            if t.count(m.group(0)) == 1:
                kind = "路线" if m.group(1) == "route" else "点"
                yield Injection(
                    "E2", key, m.group(0), f"{m.group(1)}={m.group(2)}_x",
                    f"{kind}名写成想定接口里没有的 {m.group(2)}_x",
                    f"引用的{kind} {m.group(2)}_x 不在想定接口中",
                    f"改回想定接口的 {m.group(0)}",
                )  # fmt: skip
                return


def g_e4_entry(blocks: dict[str, str]) -> Iterator[Injection]:
    t = blocks.get("conditions", "")
    for line in t.splitlines():
        m = re.search(r" and time_in_state\(\) >= [\d.]+ \* s", line)
        if m and t.count(line) == 1:
            name = line.split("=")[0].strip()
            yield Injection(
                "E4", "conditions", line, line.replace(m.group(0), ""),
                f"{name} 去掉时间保护",
                f"进入状态的同一拍 {name} 就成立，状态一进入就离开",
                f"{name} 加回{m.group(0)}",
            )  # fmt: skip
            return


def g_e5_exit(blocks: dict[str, str]) -> Iterator[Injection]:
    for key, t in blocks.items():
        if not key.startswith("mermaid:"):
            continue
        for m in _TRANS.finditer(t):
            src, dst, trig = m.group(2), m.group(3), m.group(4)
            line = m.group(0) + "\n"
            if t.count(line) != 1 or len(re.findall(rf"^\s*{src} --> ", t, re.M)) != 1:
                continue  # the only exit of its state: the state is then stuck
            yield Injection(
                "E5", key, line, "", f"{key} 删掉 {src} 的出口 {trig}",
                f"{src} 没有出口转换（{trig}），进入后停在这个状态",
                f"加回转换 {src} --> {dst} : {trig}",
            )  # fmt: skip
            break


def g_e8_reference(blocks: dict[str, str]) -> Iterator[Injection]:
    for key, t in blocks.items():
        if not key.startswith("mermaid:"):
            continue
        m = next((m for m in _TRANS.finditer(t) if len(m.group(3)) > 4), None)
        if m and t.count(m.group(0)) == 1:
            dst = m.group(3)
            bad = dst[:-2] + dst[-1]
            yield Injection(
                "E8", key, m.group(0), m.group(0).replace(f"--> {dst} :", f"--> {bad} :"),
                "转换的目标状态名拼错", f"转换指向不存在的状态 {bad}",
                f"目标状态改回 {dst}",
            )  # fmt: skip
            break
    for key, t in blocks.items():
        if not key.startswith("pytree:"):
            continue
        m = re.search(r'tactic\("([\w.]+)"', t)
        if m and t.count(m.group(0)) == 1 and len(m.group(1)) > 8:
            tid = m.group(1)
            bad = tid[:-3] + tid[-2:]
            yield Injection(
                "E8", key, m.group(0), m.group(0).replace(tid, bad), "子树名拼错",
                f"战术集里没有 {bad}", f"改回 {tid}",
            )  # fmt: skip
            break


def g_e9_value(blocks: dict[str, str]) -> Iterator[Injection]:
    t = blocks.get("model", "")
    for m in re.finditer(r"  (\w+):\n(?:    .*\n)*?    after: ['\"]?(\d+) s['\"]?", t):
        v = int(m.group(2))
        yield Injection(
            "E9", "model", m.group(0), m.group(0).replace(f"{v} s", f"{v * 10} s"),
            f"{m.group(1)} 的时限 {v} s 写成 {v * 10} s",
            f"{m.group(1)} 的时限与需求不符（需求为 {v} 秒），想定里到时没有转换",
            f"{m.group(1)} 的 after 改回 {v} s",
        )  # fmt: skip
        return


def g_e10_cancel(blocks: dict[str, str]) -> Iterator[Injection]:
    for key, t in blocks.items():
        if not key.startswith("mermaid:"):
            continue
        for line in t.splitlines(keepends=True):
            m = re.match(r"\s*\* --> (\w+) : (\w+)\s*$", line)
            if m and t.count(line) == 1:
                yield Injection(
                    "E10", key, line, "", f"{key} 删掉全局转换 {m.group(2)}",
                    f"{key.split(':')[1]} 没有 {m.group(2)} 的全局转换，任何状态下都不响应",
                    f"加回 * --> {m.group(1)} : {m.group(2)}",
                )  # fmt: skip
                return


GENERIC = [g_e2_interface, g_e4_entry, g_e5_exit, g_e8_reference, g_e9_value, g_e10_cancel]


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


def execute(
    fixer: SheetFixer,
    records: list[dict[str, Any]],
    action: str,
    args: dict[str, Any],
    thought: str,
) -> str:
    """One loop step with the loop's own tools and bookkeeping (``fix_sheet``)."""
    fixer.step = len(records) + 1
    if action in ("edit", "diagnose"):
        fixer.reads = 0
    if action == "show":
        result = fixer.show(str(args.get("block", "")), args.get("start"), args.get("end"))
        fixer.reads += 1
        fixer.looks += 1
    elif action == "diagnose":
        result = fixer.diagnose(args)
    elif action == "edit":
        result = fixer.edit(args.get("changes"), str(args.get("reason") or thought))
    else:
        result = f"（评测不执行 {action}）"
    records.append(
        {"step": fixer.step, "action": action, "args": args, "thought": thought,
         "result": _clip(result)}
    )  # fmt: skip
    return result


def injected(gold_sheet: str, inj: Injection, d: Any) -> tuple[SheetFixer, tuple[str, str]] | None:
    """Apply the injection to a ready drafter: its fixer and the edit (old, new) that
    undoes it, or None when the snippet is not unique."""
    texts = {b.key: b.text for b in d.blocks}
    text = texts.get(inj.block, "")
    if text.count(inj.before) != 1:
        return None
    start = text.index(inj.before)
    texts[inj.block] = text.replace(inj.before, inj.after)
    d.blocks = _rebuild(d.blocks, texts)
    window = _unique_window(texts[inj.block], start, start + len(inj.after), inj.before)
    return (SheetFixer(d), window) if window else None


def _ready(request: dict[str, Any], requirement: str, facts: list[str], gold: list[Block]):
    d = drafter(request, requirement, facts)
    d.blocks = [Block(b.kind, b.role, b.text, b.line, b.info) for b in gold]
    d.slices = d._slices(d.compile(pending=True))
    asyncio.run(d.obligations_stage())
    return d


def _look_args(d: Any, inj: Injection) -> dict[str, Any]:
    """``show`` of the injected block: whole when what the loop returns (120 lines, 5000
    characters) reaches the injected place, else a window of lines around it."""
    block = next(b for b in d.blocks if b.key == inj.block)
    at = block.text.find(inj.after) if inj.after else -1
    if at < 0:
        at = max(block.text.find(inj.before[:40]), 0)
    idx = block.text[:at].count("\n")
    if idx < 100 and len(block.text) <= 4500:
        return {"block": inj.block}
    lo = max(idx - 20, 0)
    return {"block": inj.block, "start": block.line + lo, "end": block.line + idx + 40}


def make(
    gold_sheet: str,
    request: dict[str, Any] | None,
    requirement: str,
    facts: list[str],
    inj: Injection,
    look: bool = False,
    base: Callable[[], Any] | None = None,
) -> RepairSample:
    """One injection → the loop's steps that fix it. ``look`` puts a ``show`` of the block
    first (repair v2: the edit is written after seeing the text, not from memory);
    ``base`` builds the drafter for a sheet that is not a pilot one (``collect.load``)."""
    out = RepairSample(inj.rule, "")
    gold = split_sheet(gold_sheet)[0]
    d = base() if base is not None else _ready(request or {}, requirement, facts, gold)
    got = injected(gold_sheet, inj, d)
    if got is None:
        out.problems.append("原文片段不唯一或找不到唯一的修改片段")
        return out
    fixer, (old, new) = got
    check = fixer.last
    if check.verified:
        out.problems.append("注入后检查仍全部通过")
        return out
    if fixer.outside_left():
        out.problems.append("注入后的失败按规则不属于稿件")
        return out
    out.failed = _failed(check)
    out.stage = "l1" if not check.l1_ok else "behaviour"
    changes = [{"block": inj.block, "old": old, "new": new}]
    records: list[dict[str, Any]] = []

    def step(action: str, args: dict[str, Any], thought: str) -> str:
        out.steps.append(
            {
                "prompt": _prompt(fixer, records, MAX_STEPS - len(records)),
                "target": _json(thought, action, args),
            }
        )
        return execute(fixer, records, action, args, thought)

    if look:
        step("show", _look_args(d, inj), f"先看 {inj.block} 的内容，再按检查结果定位")
    if fixer.needs_diagnosis:
        found = [f for f in fixer.findings() if f.sheet]
        issue = found[0].render() if found else (out.failed[0] if out.failed else inj.what)
        args = {"issue": issue[:400], "where": [inj.block], "cause": inj.cause, "plan": inj.plan}
        step("diagnose", args, "按检查结果和块的内容写诊断" if look else "先按检查结果写诊断")
    result = step("edit", {"changes": changes, "reason": inj.plan}, inj.plan)
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


def injections(gold_sheet: str, generic: bool | str = False) -> list[Injection]:
    """The pilot injectors (the generated sheets' names); with ``generic`` the ones that
    work on any v3 sheet (historical drafts); ``"both"``: pilot then generic, the same
    change only once. A sample's ``meta.injset`` says which list its index refers to."""
    blocks = {b.key: b.text for b in split_sheet(gold_sheet)[0]}
    sets = {False: INJECTORS, True: GENERIC, "both": INJECTORS + GENERIC}[generic]
    out: list[Injection] = []
    seen: set[tuple[str, str, str]] = set()
    for f in sets:
        for inj in f(blocks):
            key = (inj.block, inj.before, inj.after)
            if key not in seen:
                seen.add(key)
                out.append(inj)
    return out
