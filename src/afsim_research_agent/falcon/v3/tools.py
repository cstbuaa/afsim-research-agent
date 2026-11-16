"""Repair tools v2 (design ``repair-tools-v2``): the tools that answer *why a check fails*
and the structured edits, used by :mod:`falcon.v3.fixer`.

Read tools (no change to the sheet):

- :func:`explain_check` — one failed check (an L1 problem number, an L2 test id, an L3
  scenario id): what it expects, what happened, the value of every exit condition at that
  moment term by term, and the edits of this loop that touched those conditions;
- :func:`eval_condition` — a condition / trigger evaluated term by term in a given state;
- :func:`who_writes` — the actions (contracts) that write a blackboard field, the states of
  this sheet whose trees run them, the conditions that read it;
- :func:`history` — the edits tried in this loop and what each did to the score.

Structured edits turn a named change into new block texts (no copying of the original
text): :func:`structured` (``set_condition``, ``add_transition``, ``remove_transition``,
``set_trigger``, ``declare_event``, ``set_state_tree``, ``attach_subtree``, ``rename``).
:func:`lint` looks at a change before it is applied (a time guard added to a condition
that holds on entry is warned about; a constant condition is refused)."""

from __future__ import annotations

import json
import re
from typing import Any

import yaml

from afsim_research_agent.falcon.v3 import catalog as K

RESULT = 4500
TRANSITION = re.compile(
    r"^(\s*)(\[\*\]|\*|[A-Za-z_][\w]*)\s*-->\s*(\[\*\]|[A-Za-z_][\w]*)\s*(?::\s*(.*?))?\s*$"
)
WRITE_TOOLS = (
    "set_condition",
    "add_transition",
    "remove_transition",
    "set_trigger",
    "declare_event",
    "set_state_tree",
    "attach_subtree",
    "rename",
    "use_tree_event",
    "set_expectation",
    "set_scenario",
    "set_test",
    "simplify_follower",
)
READ_TOOLS = ("explain_check", "eval_condition", "who_writes", "history")


def _clip(text: str, n: int = RESULT) -> str:
    return text if len(text) <= n else text[:n] + f"\n…（截断，共 {len(text)} 字）"


def _num(x: Any) -> str:
    if isinstance(x, float):
        return f"{x:g}"
    return json.dumps(x, ensure_ascii=False) if isinstance(x, str) else str(x)


# ---------------------------------------------------------------------- expressions
def _operand_text(inst: Any, op: Any) -> str:
    if not isinstance(op, dict):
        return _num(op)
    if set(op) == {"value", "unit"}:
        return f"{_num(op['value'])} {op['unit']}"
    key, value = next(iter(op.items()))
    if key == "literal":
        return _num(value)
    if key == "parameter_ref":
        return f"{value}(参数)={_num(inst.params.get(value))}"
    if key == "bb":
        return f"{value}={_num(inst.blackboard.get(value))}"
    if key == "function_call":
        args = value.get("arguments") or {}
        if value["name"] == "time_in_state_gte":
            d = inst._operand(args.get("duration"))
            return f"time_in_state()={inst.time_in_state:g}s >= {_num(d)}s"
        if value["name"] == "elapsed_since_gte":
            return f"elapsed_since({args.get('field')}) >= {_num(inst._operand(args.get('duration')))}s"
        return str(value.get("name"))
    return json.dumps(op, ensure_ascii=False)[:120]


OPS = {"eq": "==", "ne": "!=", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}


def explain_expr(inst: Any, expr: Any, depth: int = 0, seen: set[str] | None = None) -> list[str]:
    """Each node of a condition expression with its value; false terms of a true ``all``
    (or true terms of a false ``any``) are what decides the result."""
    seen = seen or set()
    pad = "  " * depth
    value = bool(inst.eval(expr))
    mark = "真" if value else "假"
    if not isinstance(expr, dict) or len(expr) != 1:
        return [f"{pad}{expr!r} = {mark}"]
    key, body = next(iter(expr.items()))
    if key in ("all", "any"):
        word = "且" if key == "all" else "或"
        lines = [f"{pad}（{word}）= {mark}"]
        for kid in body:
            sub = explain_expr(inst, kid, depth + 1, seen)
            decisive = (key == "all" and not inst.eval(kid) and not value) or (
                key == "any" and inst.eval(kid) and value
            )
            if decisive:
                sub[0] += "  ← 决定结果的项"
            lines += sub
        return lines
    if key == "not":
        return [f"{pad}not = {mark}", *explain_expr(inst, body, depth + 1, seen)]
    if key == "condition_ref":
        cond = inst.model["conditions"].get(body)
        if cond is None or body in seen or depth > 6:
            return [f"{pad}{body} = {mark}"]
        return [f"{pad}{body} = {mark}", *explain_expr(inst, cond, depth + 1, seen | {body})]
    if key == "compare":
        left = _operand_text(inst, body["left"])
        right = _operand_text(inst, body["right"])
        return [f"{pad}{left} {OPS.get(body['operator'], body['operator'])} {right} → {mark}"]
    return [f"{pad}{_operand_text(inst, expr)} → {mark}"]


def explain_trigger(inst: Any, name: str) -> list[str]:
    """A transition trigger: a condition term by term, or the event and what it waits for."""
    if name in inst.model["conditions"]:
        value = "真" if inst._trigger(name) else "假"
        return [f"{name}（条件）= {value}", *explain_expr(inst, inst.model["conditions"][name], 1)]
    event = inst.events.get(name)
    if event is None:
        return [f"{name}：既不是条件也不是事件 → 假"]
    source = event.get("source")
    value = "真" if inst._trigger(name) else "假"
    if source == "external":
        why = "外部事件，" + ("已收到" if name in inst.latched else "未收到")
    elif source == "timer":
        why = f"计时 {inst.time_in_state:g}s / {float(event.get('after_s') or 0):g}s"
    elif source == "tree":
        why = f"树的结果 {inst.tree_status}，等待 {event.get('result')}"
    else:
        why = f"{source} 事件"
    return [f"{name}（事件：{why}）= {value}"]


def _exits(inst: Any, state: str) -> list[dict[str, Any]]:
    local = [t for t in inst.mission["transitions"] if t["from"] == state]
    globals_ = [g | {"from": "*"} for g in inst.mission["global"] if g["to"] != state]
    return sorted(local + globals_, key=lambda t: -int(t.get("priority", 0)))


def _tick_once(inst: Any) -> None:
    """The tree part of one update (what ``Instance.step`` does before the transitions)."""
    tree = inst.role_model["states"].get(inst.state, {}).get("tree")
    if tree is not None and inst.state not in inst.mission["terminal"]:
        inst.tree_status = inst.tick(tree)
    if inst.force_tree is not None:
        inst.tree_status, inst.force_tree = inst.force_tree, None


def exits_text(inst: Any, state: str) -> list[str]:
    lines = []
    fired = False
    for t in _exits(inst, state):
        ok = inst._trigger(t["on"])
        tag = "  ← 本拍触发" if ok and not fired else ""
        fired = fired or ok
        lines.append(f"  {t['from']} → {t['to']} 当 {t['on']}{tag}")
        lines += ["    " + x for x in explain_trigger(inst, t["on"])]
    return lines or ["  （这个状态没有出口转换）"]


# ---------------------------------------------------------------------- explain_check
def _related_edits(fixer: Any, names: set[str]) -> list[str]:
    out = []
    for e in fixer.edits:
        changed = {
            ln.split("=", 1)[0].strip()
            for ln in set(e.after.splitlines()) ^ set(e.before.splitlines())
            if "=" in ln
        } | {
            m.group(4).strip()
            for ln in set(e.after.splitlines()) ^ set(e.before.splitlines())
            if (m := TRANSITION.match(ln)) and m.group(4)
        }
        hit = sorted(names & changed)
        if hit:
            out.append(f"第 {e.step} 步修改 {e.block} 改过 {', '.join(hit)}（{e.reason[:80]}）")
    return out


def _l2(fixer: Any, test_id: str) -> str:
    from afsim_research_agent.ir.v2.sim.run import prepare_l2

    check = fixer.last
    model = check.model
    spec = check.compiled.spec or {}
    tests = spec.get("tests") or []
    idx = next((i for i, t in enumerate(tests) if str(t.get("id")) == test_id), None)
    if idx is None:
        return f"没有 L2 测试 {test_id}；现有：{[t.get('id') for t in tests]}"
    raw = tests[idx]
    result = next((t for t in check.l2 if t["id"] == test_id), None)
    _block, line = check.compiled.locate(f"tests[{idx}]")
    ready = prepare_l2(model, fixer.d.c, raw)
    if isinstance(ready, str):
        return f"L2 测试 {test_id} 不能运行：{ready}"
    case, inst = ready
    g = case.given
    given = {k: v for k, v in (raw.get("given") or {}).items() if k not in ("role", "state")}
    lines = [
        f"L2 测试 {test_id}（verification"
        + (f" 第 {line} 行" if line else "")
        + "）"
        + (f"：{'通过' if result and result['passed'] else '未通过'}" if result else ""),
        f"  给定：角色 {g.role}，状态 {g.state}，"
        + _clip(json.dumps(given, ensure_ascii=False), 600),
        f"  期望：{json.dumps(raw.get('expect') or {}, ensure_ascii=False)}",
    ]
    if result and not result["passed"]:
        lines.append(f"  实际：{result['detail']}")
    _tick_once(inst)
    lines.append(f"  这一拍：树的结果 {inst.tree_status}；{g.state} 的出口（按优先级）：")
    lines += exits_text(inst, g.state)
    names = {t["on"] for t in _exits(inst, g.state)}
    rel = _related_edits(fixer, names)
    if rel:
        lines.append("  本轮相关的修改：")
        lines += ["    " + r for r in rel]
    expect = case.expect.next_state
    if expect and result and not result["passed"]:
        want = [t for t in _exits(inst, g.state) if t["to"] == expect]
        if not want:
            lines.append(
                f"  {g.state} 没有到 {expect} 的转换：要么在状态图里补上，要么期望写错了"
                "（期望与需求不符时可以改期望）"
            )
        else:
            lines.append(
                f"  要到 {expect}：上面通往 {expect} 的触发需要在这一拍为真（看标出的决定项），"
                "且优先级更高的出口为假"
            )
    return "\n".join(lines)


def _l3(fixer: Any, sid: str) -> str:
    from afsim_research_agent.falcon.v3.fixer import _never_entered, _skipped

    check = fixer.last
    entry = next((s for s in check.l3 if str(s["id"]) == sid), None)
    if entry is None:
        return f"没有 L3 想定 {sid}；现有：{[s['id'] for s in check.l3]}"
    spec = check.compiled.spec or {}
    lines = [f"L3 想定 {sid}：{'通过' if entry['passed'] else '未通过'}"]
    render = entry["render"].split("  定位与改法", 1)[0].splitlines()
    lines += ["  " + x for x in render[1:] if "FAIL" in x or "未判定" in x or " stayed in " in x][
        :12
    ]
    broken = [o for o in entry.get("obligations") or [] if o.get("judged") and not o.get("passed")]
    for o in broken[:6]:
        lines.append(f"  义务 {o.get('id')} 未满足：{str(o.get('detail'))[:300]}")
    for name, seq in (entry.get("states") or {}).items():
        path = " -> ".join(f"{s}@{t:g}" for t, s in seq)
        lines.append(f"  {name}: {_clip(path, 400)}")
    lines += ["  " + x for x in _never_entered(entry, spec)]
    lines += ["  " + x for x in _skipped(entry, fixer._conditions())]
    model = check.model
    if model is not None:
        from afsim_research_agent.ir.v2.runtime import Instance
        from afsim_research_agent.ir.v2.sim.run import _StubHost

        for name, seq in (entry.get("states") or {}).items():
            if not seq:
                continue
            role = name.rsplit("_", 1)[0]
            last = seq[-1][1]
            if role not in model["roles"]:
                continue
            inst = Instance(name, role, 0, model, fixer.d.c, _StubHost({}))
            mission = inst.mission
            if last in mission["terminal"]:
                continue
            lines.append(f"  {name} 最后停在 {last}，它的出口和触发的定义：")
            for t in _exits(inst, last):
                definition = fixer._conditions().get(t["on"])
                ev = inst.events.get(t["on"])
                lines.append(
                    f"    {t['from']} → {t['to']} 当 {t['on']}"
                    + (f" = {definition}" if definition else "")
                    + (f"（事件 {json.dumps(ev, ensure_ascii=False)}）" if ev else "")
                )
    lines.append(
        "  想看某一时刻的条件取值：eval_condition（给定状态和黑板）；想看完整轨迹：simulate"
    )
    return "\n".join(lines)


def _l1(fixer: Any, index: int) -> str:
    text = fixer.explain(index)
    if text.startswith("没有问题"):
        return text
    p = fixer.last.problems[index - 1]
    c = fixer.d.c
    fields = [f for f in re.findall(r"[a-z_]+(?:\.[a-z_0-9]+)+", p.message) if f in c.blackboard]
    actions = [a for a in re.findall(r"[a-z_]+(?:\.[a-z_0-9]+)+", p.message) if a in c.actions]
    extra = []
    for f in dict.fromkeys(fields):
        extra.append(who_writes(fixer, f, short=True))
    for a in dict.fromkeys(actions):
        if a not in "".join(extra):
            extra.append(f"动作 {a}：{K._writes_text(c, c.actions[a])}")
    if "ENTRY_TRUE" in p.message:
        extra.append(entry_true_hint(fixer, p.message))
    return text + ("\n" + "\n".join(extra) if extra else "")


def _transition_of(fixer: Any, message: str) -> tuple[str, str, str, str] | None:
    """(role, from, to, trigger) of the transition an ENTRY_TRUE problem is about."""
    m = re.search(r"状态 (\w+) → (\w+) 的条件 (\w+)", message)
    if not m:
        return None
    src, dst, cond = m.groups()
    for b in fixer.d.blocks:
        if b.kind == "mermaid" and any(
            t[1] == src and t[2] == dst and t[3] == cond for t in _transitions(b.text)
        ):
            return str(b.role), src, dst, cond
    return None


def entry_true_hint(fixer: Any, message: str) -> str:
    where = _transition_of(fixer, message)
    head = (
        "改法方向（二选一）：1) 让条件读本状态动作执行之后才会变化的字段（who_writes 查谁写它）；"
        "2) 用树事件："
    )
    # _decision_hint (both exits as tree success / failure events, failure tests stubbed) is
    # kept for set_test but no longer the ENTRY_TRUE advice: in round 5 (11-12) it turned
    # sheets that the single success event had solved (r145, r170, r178) into L2 / L3
    # failures, mostly through task expiry on re-attacks.
    if where:
        role, src, dst, cond = where
        result = _result_of(cond)
        name = f"{src.lower()}_tree_{'failed' if result == 'failure' else 'done'}"
        head += (
            f'一步完成：use_tree_event {{"role": "{role}", "from": "{src}", "to": "{dst}", '
            f'"result": "{result}", "old_trigger": "{cond}"}}（声明树事件 {name} 并改用它；'
            f"树事件在 {src} 的树以该结果结束时触发；等同于 declare_event 再 set_trigger）"
        )
        more = _other_entry_true(fixer, (role, src, dst))
        if more:
            head += (
                "；同类问题还有 "
                + str(len(more))
                + ' 处，可用 items 一次改完：use_tree_event {"items": ['
                + ", ".join(
                    json.dumps(
                        {"role": r, "from": f, "to": t, "result": _result_of(c), "old_trigger": c},
                        ensure_ascii=False,
                    )
                    for r, f, t, c in [(role, src, dst, cond), *more][:8]
                )
                + "]}"
            )
    else:
        head += "declare_event 声明 source: tree 的事件，再用 set_trigger 把这条转换的触发改成它"
    return head + "。只追加 time_in_state() 等待只是推迟出口，测试期望立即转出时会失败"


def _decision_hint(fixer: Any, role: str, src: str, dst: str, cond: str) -> str | None:
    """A judging state (task valid → go on, invalid → HOLDING): both exits read what the
    state's check decides, so the pair becomes the tree's success and failure events, and
    the L2 tests of the failure exit stub the check as failing. Switching only one exit to
    the success event made the invalid-task tests fail (r114, r139, r156, round 4)."""
    got = _mermaid(fixer, role)
    if isinstance(got, str):
        return None
    conds = fixer._conditions()
    exits = [t for t in _transitions(got[1]) if t[1] == src and t[3] in conds]
    neg = [t for t in exits if NEGATIVE.search(str(t[3]))]
    pos = [t for t in exits if not NEGATIVE.search(str(t[3]))]
    if len(neg) != 1 or len(pos) != 1:
        return None
    (_, _, fdst, fcond), (_, _, sdst, scond) = neg[0], pos[0]
    items = [
        {"role": role, "from": src, "to": sdst, "result": "success", "old_trigger": scond},
        {"role": role, "from": src, "to": fdst, "result": "failure", "old_trigger": fcond},
    ]
    text = (
        f"{src} 是判断状态（{scond} → {sdst}，{fcond} → {fdst}）：两个出口都取决于本状态检查的结果，"
        "进入时字段还是初值。改法：成功出口用树成功事件、失败出口用树失败事件，一步完成："
        f'use_tree_event {{"items": {json.dumps(items, ensure_ascii=False)}}}'
    )
    tests = _l2_tests_to(fixer, role, src, fdst)
    if tests:
        model = getattr(fixer.last, "model", None) or {}
        tree = (((model.get("roles") or {}).get(role) or {}).get("states") or {}).get(src, {})
        from afsim_research_agent.falcon.v3.triage import _actions

        acts = [str(x.get("action")) for x in _actions(tree.get("tree")) if x.get("action")]
        stub = {acts[0]: "failure"} if acts else {"<本状态的检查动作>": "failure"}
        text += (
            f"；之后 L2 测试 {', '.join(tests)} 要把本状态的检查动作模拟为失败（树失败才会走 {fdst}）："
            + "；".join(
                f'set_test {{"id": "{t}", "actions": {json.dumps(stub, ensure_ascii=False)}, '
                '"because": "无效时检查动作失败，树失败事件转出"}'
                for t in tests[:3]
            )
        )
    return text


def _vroot(data: Any) -> dict[str, Any]:
    """The verification mapping: the block may wrap it in a top-level ``verification:``."""
    if isinstance(data, dict) and isinstance(data.get("verification"), dict):
        return data["verification"]
    return data if isinstance(data, dict) else {}


def _l2_tests_to(fixer: Any, role: str, state: str, dst: str) -> list[str]:
    block = fixer._block("verification")
    try:
        data = yaml.safe_load(block.text) if block is not None else None
    except yaml.YAMLError:
        return []
    data = _vroot(data)
    out = []
    for t in data.get("tests") or []:
        g, e = t.get("given") or {}, t.get("expect") or {}
        if g.get("role") == role and g.get("state") == state and e.get("next_state") == dst:
            out.append(str(t.get("id")))
    return out


NEGATIVE = re.compile(
    r"fail|invalid|expire|lost|loss|abort|error|timeout|unsafe|blocked|(^|_)no_|(^|_)not_"
)


def _result_of(cond: str) -> str:
    """The tree result an ENTRY_TRUE exit is switched to: success unless the condition says
    failure. Negative names (task_invalid, no_targets) stay on success too: the L2 tests
    stub the state's actions as succeeding, and a failure event made them fail (r145 in
    round 4, 11-12, solved in round 3 with success)."""
    return "failure" if "fail" in cond else "success"


def _other_entry_true(fixer: Any, skip: tuple[str, str, str]) -> list[tuple[str, str, str, str]]:
    """The other ENTRY_TRUE problems of the last check, as (role, from, to, condition)."""
    out = []
    for p in getattr(fixer.last, "problems", None) or []:
        if "ENTRY_TRUE" not in p.message:
            continue
        where = _transition_of(fixer, p.message)
        if where and where[:3] != skip and where not in out:
            out.append(where)
    return out


def _role_of(fixer: Any, src: str, trig: str) -> str | None:
    for b in fixer.d.blocks:
        if b.kind == "mermaid" and any(t[1] == src and t[3] == trig for t in _transitions(b.text)):
            return str(b.role)
    return None


def problem_hint(fixer: Any, message: str) -> str | None:
    """A concrete way to fix an L1 problem, with the tool calls written out."""
    if "ENTRY_TRUE" in message:
        return entry_true_hint(fixer, message)
    m = re.search(r"STATE_FROZEN 角色 (\w+) 的状态 (\w+)", message)
    if m:
        role, state = m.groups()
        writers = re.findall(r"只由 ([\w.、]+) 写入", message)
        acts = {a for w in writers for a in w.split("、")}
        subs = sorted(
            tid
            for tid, e in fixer.d.c.tactics.items()
            if e.section == "subtrees" and any(f'"{a}"' in json.dumps(e.raw) for a in acts)
        )
        if subs:
            return (
                f'改法：attach_subtree {{"role": "{role}", "state": "{state}", "subtree": "{subs[0]}", '
                f'"position": "end"}}（含写入动作的子树：{", ".join(subs[:4])}）；或把出口改成读 {state} '
                "的动作会写的字段"
            )
        return f"改法：把 {'、'.join(sorted(acts)[:3])} 加到 {state} 的树里（set_state_tree），或改读 {state} 的动作会写的字段"
    m = re.search(r"树事件 (\w+) 要等 (\w+) 的 parallel", message)
    if m:
        event, state = m.groups()
        field = re.search(r"如 ([a-z_]+(?:\.[a-z_0-9]+)+)", message)
        role = _role_of(fixer, state, event) or "<角色>"
        name = f"{event}_now"
        to = next(
            (
                t[2]
                for b in fixer.d.blocks
                if b.kind == "mermaid"
                for t in _transitions(b.text)
                if t[1] == state and t[3] == event
            ),
            "<目标状态>",
        )
        return (
            f'改法：新建一个读字段的条件，再让转换改用它：set_condition {{"name": "{name}", "expr": '
            f'"{field.group(1) if field else "<进入后才会变化的字段>"}"}}，然后 set_trigger '
            f'{{"role": "{role}", "from": "{state}", "to": "{to}", "trigger": "{name}", '
            f'"old_trigger": "{event}"}}。新条件不要和事件 {event} 同名；事件不再使用时可以留着'
        )
    return None


def explain_check(fixer: Any, ref: Any) -> str:
    raw = str(ref if ref is not None else "").strip()
    raw = re.sub(r"^(L[123])\s*[:：]\s*", "", raw)
    check = fixer.last
    if raw.lstrip("[").rstrip("]").isdigit():
        return _clip(_l1(fixer, int(raw.lstrip("[").rstrip("]"))))
    if not check.l1_ok:
        return f"L1 未通过，L2/L3 还没有运行；用问题编号（1–{len(check.problems)}）查看 L1 问题"
    if any(t["id"] == raw for t in check.l2) or any(
        str(t.get("id")) == raw for t in (check.compiled.spec or {}).get("tests") or []
    ):
        return _clip(_l2(fixer, raw))
    if any(str(s["id"]) == raw for s in check.l3):
        return _clip(_l3(fixer, raw))
    failed = [t["id"] for t in check.l2 if not t["passed"]] + [
        s["id"] for s in check.l3 if not s["passed"]
    ]
    return f"没有检查项 {raw}；未通过的：{failed or '无'}（L1 问题用编号）"


def first_failure(fixer: Any) -> str | None:
    check = fixer.last
    if not check.l1_ok:
        return "1" if check.problems else None
    for t in check.l2:
        if not t["passed"]:
            return str(t["id"])
    for s in check.l3:
        if not s["passed"]:
            return str(s["id"])
    return None


# ---------------------------------------------------------------------- eval_condition
def eval_condition(fixer: Any, role: str, state: str, name: str, given: Any) -> str:
    from afsim_research_agent.ir.v2.sim.run import prepare_l2

    model = fixer.last.model
    if model is None:
        return "L1 未通过，还不能求值；先修 L1（可用 who_writes 查字段由谁写）"
    given = given if isinstance(given, dict) else {}
    raw = {"id": "eval", "given": {"role": role, "state": state, **given}, "expect": {}}
    ready = prepare_l2(model, fixer.d.c, raw)
    if isinstance(ready, str):
        return f"给定写法不对：{ready}"
    _case, inst = ready
    _tick_once(inst)
    if not name:
        return f"{role} 在 {state}（树的结果 {inst.tree_status}）的出口：\n" + "\n".join(
            exits_text(inst, state)
        )
    return f"{role} 在 {state}（树的结果 {inst.tree_status}）：\n" + "\n".join(
        explain_trigger(inst, name)
    )


# ---------------------------------------------------------------------- who_writes
def _walk(
    node: Any, spec_subs: dict[str, Any], tactics: dict[str, Any], seen: set[str]
) -> set[str]:
    out: set[str] = set()
    if isinstance(node, list):
        for x in node:
            out |= _walk(x, spec_subs, tactics, seen)
        return out
    if not isinstance(node, dict):
        return out
    if isinstance(node.get("action"), str):
        out.add(node["action"])
    for key in ("subtree", "tactic"):
        ref = node.get(key)
        if isinstance(ref, str) and ref not in seen:
            seen.add(ref)
            root = spec_subs.get(ref)
            if root is None and ref in tactics:
                root = tactics[ref].raw.get("root")
            out |= _walk(root, spec_subs, tactics, seen)
    for v in node.values():
        if isinstance(v, dict | list):
            out |= _walk(v, spec_subs, tactics, seen)
    return out


def state_actions(fixer: Any) -> dict[tuple[str, str], set[str]]:
    """(role, state) → the actions its tree may run, referenced subtrees included."""
    spec = fixer.last.compiled.spec if getattr(fixer.last, "compiled", None) else None
    spec = spec or {}
    subs = {s["id"]: s.get("root") for s in spec.get("subtrees") or [] if isinstance(s, dict)}
    out: dict[tuple[str, str], set[str]] = {}
    for role, states in (spec.get("state_trees") or {}).items():
        for state, ref in (states or {}).items():
            root = subs.get(ref) if isinstance(ref, str) else ref
            out[(role, state)] = _walk(root, subs, fixer.d.c.tactics, {str(ref)})
    return out


def who_writes(fixer: Any, path: str, short: bool = False) -> str:
    c = fixer.d.c
    path = str(path).strip().strip('"')
    if path not in c.blackboard:
        import difflib

        near = difflib.get_close_matches(path, sorted(c.blackboard), n=6, cutoff=0.5)
        return f"没有黑板字段 {path}" + (f"；相近的有：{', '.join(near)}" if near else "")
    lines = [K.field_text(c, path).lstrip("- ")]
    writers = {}
    for a in c.actions.values():
        for w in a.writes or ():
            if str(w.get("field")) == path:
                how = [
                    f"{word}={w[key]}"
                    for key, word in (
                        ("reset_on_start", "启动时置"),
                        ("on_success", "成功时"),
                        ("on_failure", "失败时"),
                        ("only", "只会置为"),
                    )
                    if key in w
                ]
                writers[a.id] = "，".join(how)
    if writers:
        lines.append(
            "写它的动作：" + "；".join(f"{a}（{h}）" if h else a for a, h in writers.items())
        )
    else:
        lines.append("能力集里没有动作写它")
    where = state_actions(fixer)
    used = [
        f"{r}.{s}（{', '.join(sorted(acts & set(writers)))}）"
        for (r, s), acts in where.items()
        if acts & set(writers)
    ]
    lines.append("本稿件中运行这些动作的状态：" + ("；".join(used) or "无"))
    if not short:
        readers = [
            name
            for name, expr in fixer._conditions().items()
            if f'"bb": "{path}"' in expr or f'"{path}"' in expr
        ]
        lines.append("读它的条件：" + (", ".join(readers) or "无"))
    return ("字段 " if short else "") + "\n".join(lines)


# ---------------------------------------------------------------------- history
def _change_gist(before: str, after: str) -> str:
    a, b = before.splitlines(), after.splitlines()
    gone = [x.strip() for x in a if x not in b][:2]
    new = [x.strip() for x in b if x not in a][:2]
    parts = [f"- {x[:140]}" for x in gone] + [f"+ {x[:140]}" for x in new]
    return " | ".join(parts) or "（无文本变化）"


def history(fixer: Any) -> str:
    lines = []
    for e in fixer.edits:
        lines.append(
            f"第 {e.step} 步 {e.block}：{_change_gist(e.before, e.after)}；"
            f"得分 {e.score_before} → {e.score_after}"
        )
    for a in fixer.attempts:
        lines.append(f"第 {a['step']} 步（未生效）{a['what'][:200]}：{a['why'][:160]}")
    for dg in fixer.diagnoses:
        lines.append(f"第 {dg['step']} 步诊断：{dg['cause'][:160]} → {dg['plan'][:160]}")
    if not lines:
        return "本轮还没有修改"
    return "本轮记录（按类别）：\n" + "\n".join(lines[-20:])


def tried_summary(fixer: Any, limit: int = 8) -> str:
    rows = [
        f"- 第 {e.step} 步 {e.block}：{_change_gist(e.before, e.after)[:220]}，得分 "
        f"{e.score_before} → {e.score_after}"
        for e in fixer.edits[-limit:]
    ]
    rows += [f"- 第 {a['step']} 步未生效：{a['why'][:120]}" for a in fixer.attempts[-3:]]
    return "\n".join(rows)


# ---------------------------------------------------------------------- structured edits
def _cond_span(lines: list[str], name: str) -> tuple[int, int] | None:
    pat = re.compile(rf"^{re.escape(name)}\s*=(?!=)")
    for i, ln in enumerate(lines):
        if pat.match(ln):
            j, depth = i, ln.count("(") - ln.count(")")
            while (depth > 0 or lines[j].rstrip().endswith("\\")) and j + 1 < len(lines):
                j += 1
                depth += lines[j].count("(") - lines[j].count(")")
            return i, j
    return None


SCENARIO_PARTS = ("description", "environment", "vehicles", "hostiles", "events", "faults")


def _verification_edit(
    fixer: Any, op: str, a: dict[str, Any], texts: dict[str, str]
) -> dict[str, str] | str:
    """set_expectation / set_scenario: change one expectation of an L3 scenario, or parts
    of a scenario (vehicles, hostiles, events, faults), by id instead of a text edit. Each
    change is validated with the 2D models' own schema; a change to what is expected needs
    a because that cites the requirement (tests are not bent to the sheet)."""
    from afsim_research_agent.ir.v2.sim.run import Expectation, Scenario

    cur = texts.get("verification")
    if cur is None:
        return "没有 verification 块"
    try:
        data = yaml.safe_load(cur) or {}
    except yaml.YAMLError as exc:
        return f"verification 块不是合法的 YAML：{exc}"
    if not str(a.get("because") or "").strip():
        return f"{op} 要带 because：引用需求原文，说明为什么原来的写法与需求不符"
    if op == "set_test":
        return _set_test(fixer, data, a, texts)
    scenarios = _vroot(data).get("scenarios")
    sid = str(a.get("scenario") or "").strip()
    sc = next((x for x in scenarios or [] if isinstance(x, dict) and x.get("id") == sid), None)
    if sc is None:
        ids = [x.get("id") for x in scenarios or [] if isinstance(x, dict)]
        return f"没有想定 {sid or '（未写 scenario）'}；现有：{ids}"
    changes = a.get("set") if isinstance(a.get("set"), dict) else {}
    if op == "set_expectation":
        eid = str(a.get("id") or "").strip()
        exp = next(
            (e for e in sc.get("expect") or [] if isinstance(e, dict) and e.get("id") == eid), None
        )
        if exp is None:
            ids = [e.get("id") for e in sc.get("expect") or [] if isinstance(e, dict)]
            return f"想定 {sid} 没有期望 {eid or '（未写 id）'}；现有：{ids}"
        new = dict(exp)
        kind = str(a.get("kind") or "").strip()
        if kind == "visits_in_order" and new.get("state_sequence") is not None:
            new["visits_in_order"] = new.pop("state_sequence")
        elif kind:
            return 'kind 目前只支持 "visits_in_order"（把完整序列改成按顺序经过）'
        for k, v in changes.items():
            if k in ("id", "role"):
                return f"不能改期望的 {k}"
            new[k] = v
        for k in a.get("unset") or []:
            new.pop(str(k), None)
        if new == exp:
            return "期望没有变化"
        if not any(
            new.get(k) is not None
            for k in ("state_sequence", "visits_in_order", "final_state", "reaches", "never")
        ):
            return "改完后这个期望不再检查任何内容；不能这样删掉期望"
        try:
            Expectation.model_validate(new)
        except Exception as exc:  # noqa: BLE001 - pydantic's message names the field
            return f"期望写法不对：{str(exc)[:400]}"
        exp.clear()
        exp.update(new)
        what = f"想定 {sid} 的期望 {eid} 已改为 {json.dumps(new, ensure_ascii=False)}"
    else:
        bad = [k for k in changes if k not in SCENARIO_PARTS]
        if bad or not changes:
            return f"set_scenario 的 set 只能改 {list(SCENARIO_PARTS)}（不能改 id 和 expect）"
        new = dict(sc) | changes
        try:
            Scenario.model_validate(new)
        except Exception as exc:  # noqa: BLE001
            hint = (
                "\nvehicles 的写法：{角色名: [{position_m: [x, y]}, ...]}，角色名是 roles 里的名字"
                "（如 leader / member），不是车辆实例名；不写 vehicles 时按想定绑定摆放"
                if "vehicles" in changes
                else ""
            )
            return f"想定写法不对：{str(exc)[:400]}{hint}"
        sc.clear()
        sc.update(new)
        what = f"想定 {sid} 的 {', '.join(changes)} 已修改"
    texts["verification"] = yaml.safe_dump(
        data, allow_unicode=True, sort_keys=False, default_flow_style=None, width=1000
    ).rstrip()
    texts["_note"] = what
    return texts


def _set_test(
    fixer: Any, data: Any, a: dict[str, Any], texts: dict[str, str]
) -> dict[str, str] | str:
    """set_test: the stubbed action results (``actions``), blackboard values (``blackboard``)
    or the expected next state (``next_state``) of one L2 test. A tactic-set subtree id in
    ``actions`` stands for its actions (the test stubs action leaves only)."""
    from afsim_research_agent.ir.v2.sim.run import TestCase

    if isinstance(a.get("actions"), dict):
        acts: dict[str, Any] = {}
        tactics = fixer.d.c.tactics
        for k, v in a["actions"].items():
            entry = tactics.get(str(k))
            inner = (
                ((entry.raw.get("uses") or {}).get("actions") or []) if entry is not None else []
            )
            if inner:
                acts.update({str(x): v for x in inner})
            else:
                acts[str(k)] = v
        a = a | {"actions": acts}
    tests = _vroot(data).get("tests")
    tid = str(a.get("id") or "").strip()
    t = next((x for x in tests or [] if isinstance(x, dict) and x.get("id") == tid), None)
    if t is None:
        return f"没有 L2 测试 {tid or '（未写 id）'}；现有：{[x.get('id') for x in tests or [] if isinstance(x, dict)]}"
    new = json.loads(json.dumps(t))
    given = new.setdefault("given", {})
    for key in ("actions", "blackboard"):
        if isinstance(a.get(key), dict):
            given[key] = dict(given.get(key) or {}) | a[key]
    if a.get("next_state"):
        new.setdefault("expect", {})["next_state"] = str(a["next_state"])
    if new == t:
        return 'set_test 要写 "actions"、"blackboard" 或 "next_state" 中至少一项（且与现在不同）'
    try:
        TestCase.model_validate(new)
    except Exception as exc:  # noqa: BLE001
        return f"测试写法不对：{str(exc)[:400]}"
    t.clear()
    t.update(new)
    texts["verification"] = yaml.safe_dump(
        data, allow_unicode=True, sort_keys=False, default_flow_style=None, width=1000
    ).rstrip()
    texts["_note"] = f"L2 测试 {tid} 已改为 {json.dumps(new, ensure_ascii=False)[:300]}"
    return texts


def _use_tree_event(fixer: Any, a: dict[str, Any]) -> dict[str, str] | str:
    """declare_event (a tree event of the source state) and set_trigger on its transition in
    one step, for one transition or several (``items``): the usual ENTRY_TRUE fix took two
    steps per state, and sheets with many such states ran out of steps (r116, 11-12).
    Only for L1's ENTRY_TRUE problems: once L1 passes, it was still the most used tool on 17
    of the sheets one L3 item short (6+ calls each) and broke judging-state tests."""
    from afsim_research_agent.falcon.v3.fixer import _rebuild

    last = fixer.last
    if getattr(last, "l1_ok", False) and not any(
        "ENTRY_TRUE" in p.message for p in getattr(last, "problems", None) or []
    ):
        first = first_failure(fixer)
        return (
            "use_tree_event 只用于 L1 的 ENTRY_TRUE 问题；L1 已经通过，现在的失败项要按原因改："
            "缺出口用 add_transition，缺动作用 attach_subtree / set_state_tree，期望或想定不对用 "
            "set_expectation / set_scenario / set_test。确实要让一条转换等树结束，用 declare_event 再 "
            "set_trigger。"
            + (f"\n第一个未通过项的解释：\n{explain_check(fixer, first)}" if first else "")
        )

    items = a.get("items") if isinstance(a.get("items"), list) else [a]
    saved = fixer.d.blocks
    done = []
    try:
        for it in items:
            if not isinstance(it, dict):
                return 'use_tree_event 的 items 每项写 {"role", "from", "to"}'
            role, src, dst = (str(it.get(k) or "").strip() for k in ("role", "from", "to"))
            if not (role and src and dst):
                return 'use_tree_event 要写 {"role", "from", "to"}（可选 "result": success|failure, "old_trigger"）'
            result = str(it.get("result") or "")
            if not result:  # from the name of the condition it replaces
                got = _mermaid(fixer, role)
                cur = (
                    [t[3] for t in _transitions(got[1]) if t[1] == src and t[2] == dst]
                    if isinstance(got, tuple)
                    else []
                )
                result = _result_of(str(it.get("old_trigger") or (cur[0] if len(cur) == 1 else "")))
            if result not in ("success", "failure"):
                return 'result 写 "success"（树成功时）或 "failure"（树失败时）'
            name = str(it.get("name") or "").strip() or (
                f"{src.lower()}_tree_{'done' if result == 'success' else 'failed'}"
            )
            texts = structured(
                fixer, "declare_event", {"name": name, "source": "tree", "result": result}
            )
            if isinstance(texts, str):
                return texts
            texts.pop("_note", None)
            fixer.d.blocks = _rebuild(fixer.d.blocks, texts)
            args = {"role": role, "from": src, "to": dst, "trigger": name}
            if it.get("old_trigger"):
                args["old_trigger"] = it["old_trigger"]
            texts = structured(fixer, "set_trigger", args)
            if isinstance(texts, str):
                return f"{src} → {dst}：{texts}"
            texts.pop("_note", None)
            fixer.d.blocks = _rebuild(fixer.d.blocks, texts)
            done.append(
                f"{role} {src} → {dst} 改由树事件 {name}（{src} 的树{'成功' if result == 'success' else '失败'}时）触发"
            )
        out = {b.key: b.text for b in fixer.d.blocks}
    finally:
        fixer.d.blocks = saved
    out["_note"] = "；".join(done)
    return out


def _mermaid(fixer: Any, role: str) -> tuple[str, str] | str:
    key = f"mermaid:{role}"
    b = fixer._block(key)
    if b is None:
        roles = [x.role for x in fixer.d.blocks if x.kind == "mermaid"]
        return f"没有角色 {role} 的状态图；现有角色：{roles}"
    return key, b.text


def _transitions(text: str) -> list[tuple[int, str, str, str | None]]:
    out = []
    for i, ln in enumerate(text.splitlines()):
        m = TRANSITION.match(ln)
        if m:
            out.append((i, m.group(2), m.group(3), (m.group(4) or None)))
    return out


def _states(text: str) -> set[str]:
    return {x for _, a, b, _ in _transitions(text) for x in (a, b) if x not in ("[*]", "*")}


def _fn_span(lines: list[str], name: str) -> tuple[int, int] | None:
    for i, ln in enumerate(lines):
        if re.match(rf"^def {re.escape(name)}\s*\(", ln):
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or lines[j].startswith((" ", "\t"))):
                j += 1
            while j > i + 1 and not lines[j - 1].strip():
                j -= 1
            return i, j
    return None


def structured(fixer: Any, op: str, a: dict[str, Any]) -> dict[str, str] | str:
    """New block texts for a structured edit, or why it cannot be made."""
    texts = {b.key: b.text for b in fixer.d.blocks}
    if op == "set_condition":
        name, expr = str(a.get("name") or "").strip(), str(a.get("expr") or "").strip()
        if not name or not expr:
            return 'set_condition 要写 {"name": 条件名, "expr": 表达式}'
        if expr.startswith(f"{name} ="):
            expr = expr.split("=", 1)[1].strip()
        events = _events(fixer) | _declared(texts.get("model") or "")
        used = [e for e in events if re.search(rf"(?<![\w.]){re.escape(e)}(?![\w])", expr)]
        if used or "events." in expr:
            return (
                f"条件里不能引用事件（{', '.join(used) or expr}）：事件只能作状态图转换的触发。"
                '把转换的触发改成事件：set_trigger {"role", "from", "to", "trigger": 事件名, '
                '"old_trigger": 原条件名}'
            )
        cur = texts.get("conditions")
        if cur is None:
            texts["conditions"] = f"{name} = {expr}"
            return texts
        lines = cur.splitlines()
        span = _cond_span(lines, name)
        if span is None:
            lines.append(f"{name} = {expr}")
        else:
            lines[span[0] : span[1] + 1] = [f"{name} = {expr}"]
        texts["conditions"] = "\n".join(lines)
        return texts
    if op in ("add_transition", "remove_transition", "set_trigger"):
        role = str(a.get("role") or "").strip()
        got = _mermaid(fixer, role)
        if isinstance(got, str):
            return got
        key, text = got
        src, dst = str(a.get("from") or "").strip(), str(a.get("to") or "").strip()
        trig = str(a.get("trigger") or "").strip() or None
        states = _states(text)
        for s in (src, dst):
            if s and s not in ("*", "[*]") and s not in states and op != "add_transition":
                return f"{role} 的状态图里没有状态 {s}；现有：{sorted(states)}"
        rows = text.splitlines()
        hits = [t for t in _transitions(text) if t[1] == src and t[2] == dst]
        if op == "add_transition":
            if not src or not dst or not trig:
                return 'add_transition 要写 {"role", "from", "to", "trigger"}（from 写 * 表示任意状态）'
            if any(t[3] == trig for t in hits):
                return f"已经有 {src} --> {dst} : {trig}"
            new = f"  {src} --> {dst} : {trig}"
            end = next(
                (i for i, ln in enumerate(rows) if re.match(r"^\s*\S+\s*-->\s*\[\*\]", ln)),
                len(rows),
            )
            rows.insert(end, new)
            texts[key] = "\n".join(rows)
            unknown = trig not in fixer._conditions() and trig not in _events(fixer)
            if unknown and not a.get("_quiet"):
                texts["_note"] = (
                    f"注意：触发 {trig} 在 conditions 和 events 里都没有定义；用 set_condition "
                    "或 declare_event 补上（同一步也可以用 edit 一起改）"
                )
            return texts
        if not hits:
            have = [
                f"{t[1]} --> {t[2]} : {t[3]}" for t in _transitions(text) if src in (t[1], t[2])
            ]
            return f"没有 {src} --> {dst} 的转换；与 {src} 相关的：{have or '无'}"
        if op == "remove_transition":
            pick = [t for t in hits if trig is None or t[3] == trig]
            if len(pick) != 1:
                return f"{src} --> {dst} 有 {len(hits)} 条，写上 trigger 指明删哪一条：{[t[3] for t in hits]}"
            del rows[pick[0][0]]
            texts[key] = "\n".join(rows)
            return texts
        old = str(a.get("old_trigger") or "").strip() or None
        pick = [t for t in hits if old is None or t[3] == old]
        if len(pick) != 1:
            return f"{src} --> {dst} 有 {len(hits)} 条，写上 old_trigger：{[t[3] for t in hits]}"
        if not trig:
            return "set_trigger 要写新的 trigger"
        i = pick[0][0]
        indent = TRANSITION.match(rows[i]).group(1)
        rows[i] = f"{indent}{src} --> {dst} : {trig}"
        texts[key] = "\n".join(rows)
        return texts
    if op == "declare_event":
        name = str(a.get("name") or "").strip()
        source = str(a.get("source") or "").strip()
        if not name or source not in ("tree", "timer", "external"):
            return 'declare_event 要写 {"name", "source": tree|timer|external, "result"（tree）或 "after"（timer，如 "5 s"）}'
        body: dict[str, Any] = {"source": source}
        if source == "tree":
            body["result"] = str(a.get("result") or "success")
        elif source == "timer":
            body = {"after": str(a.get("after") or a.get("after_s") or "")}
            if not body["after"]:
                return 'timer 事件要写 "after"，如 "5 s"'
        if a.get("description"):
            body["description"] = str(a["description"])
        cur = texts.get("model")
        if cur is None:
            return "没有 model 块"
        entry = (
            f"  {name}: "
            + yaml.safe_dump(body, allow_unicode=True, default_flow_style=True).strip()
        )
        rows = cur.splitlines()
        top = next((i for i, ln in enumerate(rows) if re.match(r"^events\s*:", ln)), None)
        if top is None:
            rows.append("events:")
            rows.append(entry)
        else:
            if rows[top].split(":", 1)[1].strip() not in ("", "{}"):
                data = yaml.safe_load(cur) or {}
                data.setdefault("events", {})[name] = body
                texts["model"] = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
                return texts
            rows[top] = "events:"
            j = top + 1
            same = None
            while j < len(rows) and (rows[j].startswith((" ", "\t")) or not rows[j].strip()):
                if re.match(rf"^\s+{re.escape(name)}\s*:", rows[j]):
                    same = j
                j += 1
            if same is not None:
                rows[same] = entry
            else:
                rows.insert(j, entry)
        texts["model"] = "\n".join(rows)
        texts["_note"] = (
            f"事件 {name} 只能作状态图转换的触发（不能写进 conditions）：用 set_trigger 把要用它的"
            f'转换改过来，如 set_trigger {{"role", "from", "to", "trigger": "{name}", "old_trigger": 原触发}}'
        )
        return texts
    if op == "use_tree_event":
        return _use_tree_event(fixer, a)
    if op == "simplify_follower":
        return _simplify_follower(fixer, a)
    if op in ("set_expectation", "set_scenario", "set_test"):
        return _verification_edit(fixer, op, a, texts)
    if op in ("set_state_tree", "attach_subtree"):
        role, state = str(a.get("role") or "").strip(), str(a.get("state") or "").strip()
        key = f"pytree:{role}"
        cur = texts.get(key)
        if cur is None:
            return f"没有块 {key}；现有：{[b.key for b in fixer.d.blocks if b.kind == 'pytree']}"
        rows = cur.splitlines()
        span = _fn_span(rows, state)
        if op == "set_state_tree":
            code = str(a.get("code") or "").strip("\n")
            if not code.startswith(f"def {state}("):
                return f'code 要是完整的函数：以 "def {state}():" 开头'
            if span is None:
                rows += ["", *code.splitlines()]
            else:
                rows[span[0] : span[1]] = code.splitlines()
            texts[key] = "\n".join(rows)
            return texts
        sub = str(a.get("subtree") or "").strip()
        if sub not in fixer.d.c.tactics or fixer.d.c.tactics[sub].section != "subtrees":
            import difflib

            subs = [k for k, t in fixer.d.c.tactics.items() if t.section == "subtrees"]
            near = difflib.get_close_matches(sub, subs, n=6, cutoff=0.4)
            return f"没有可引用的子树 {sub}" + (f"；相近的有：{', '.join(near)}" if near else "")
        if span is None:
            return f"{key} 里没有状态 {state} 的函数（def {state}():）"
        body = rows[span[0] + 1 : span[1]]
        line = f'    tactic("{sub}")'
        if a.get("position") == "start":
            rows.insert(span[0] + 1, line)
        else:
            rows.insert(span[0] + 1 + len(body), line)
        texts[key] = "\n".join(rows)
        return texts
    if op == "rename":
        kind = str(a.get("kind") or "").strip()
        old, new = str(a.get("old") or "").strip(), str(a.get("new") or "").strip()
        if kind not in ("trigger", "condition", "state", "event") or not old or not new:
            return 'rename 要写 {"kind": trigger|condition|event|state, "old", "new"}'
        pat = re.compile(
            rf"(?<![\w.]){re.escape(old)}(?![\w(])" if kind != "state" else rf"\b{re.escape(old)}\b"
        )
        kinds = {
            "trigger": ("mermaid", "conditions", "model"),
            "condition": ("mermaid", "conditions", "model"),
            "event": ("mermaid", "model", "verification"),
            "state": ("mermaid", "pytree", "verification", "model"),
        }[kind]
        n = 0
        for b in fixer.d.blocks:
            if b.kind in kinds:
                new_text, k = pat.subn(new, texts[b.key])
                texts[b.key] = new_text
                n += k
        if n == 0:
            return f"没有找到 {old}"
        return texts
    return f"不认识的修改 {op}"


def _declared(model_text: str) -> set[str]:
    try:
        data = yaml.safe_load(model_text) or {}
    except yaml.YAMLError:
        return set()
    ev = data.get("events") if isinstance(data, dict) else None
    return {str(k) for k in ev} if isinstance(ev, dict) else set()


def _events(fixer: Any) -> set[str]:
    spec = fixer.last.compiled.spec if getattr(fixer.last, "compiled", None) else None
    return {str(e.get("id")) for e in (spec or {}).get("events") or [] if isinstance(e, dict)}


# ---------------------------------------------------------------------- lint
TIME_GUARD = re.compile(r"time_in_state\(\)\s*>=?")


def lint(fixer: Any, before: dict[str, str], after: dict[str, str]) -> tuple[str | None, list[str]]:
    """(refusal, warnings) for a change before it is applied."""
    warnings: list[str] = []
    old = before.get("conditions") or ""
    new = after.get("conditions") or ""
    if old != new:

        def defs(text: str) -> dict[str, str]:
            rows = text.splitlines()
            out = {}
            for ln in rows:
                m = re.match(r"^(\w+)\s*=(?!=)\s*(.*)$", ln)
                if m:
                    span = _cond_span(rows, m.group(1))
                    out[m.group(1)] = (
                        " ".join(x.strip() for x in rows[span[0] : span[1] + 1]) if span else ln
                    )
            return out

        a, b = defs(old), defs(new)
        events = _declared(after.get("model") or before.get("model") or "")
        for name, text in b.items():
            if text == a.get(name):
                continue
            if name in events:
                return (
                    f"修改被拒绝：条件 {name} 和 model 里的事件 {name} 同名。给条件换一个名字，再用"
                    "set_trigger 让转换改用新条件（或 rename 事件）",
                    [],
                )
            expr = text.split("=", 1)[1] if "=" in text else text
            used = [e for e in events if re.search(rf"(?<![\w.]){re.escape(e)}(?![\w])", expr)]
            if used or "events." in expr:
                return (
                    f"修改被拒绝：条件 {name} 里引用了事件 {', '.join(used) or 'events.*'}。事件只能作"
                    '状态图转换的触发：set_trigger {"role", "from", "to", "trigger": 事件名, '
                    '"old_trigger": 原条件名}',
                    [],
                )
        for name, text in b.items():
            expr = text.split("=", 1)[1].strip() if "=" in text else text
            if expr.strip("() ") in ("True", "False", "true", "false", "0", "1"):
                return (f"修改被拒绝：条件 {name} 被写成常量 {expr}，这等于删掉了这个判断", [])
            gained = len(TIME_GUARD.findall(text)) > len(TIME_GUARD.findall(a.get(name, "")))
            if name in a and gained:
                entry = any(
                    "ENTRY_TRUE" in p.message and name in p.message for p in fixer.last.problems
                )
                warnings.append(
                    f"注意：给条件 {name} 加了 time_in_state() 等待。"
                    + ("L1 判它进入即成立：" if entry else "")
                    + "等待只会推迟出口，不会让条件读到本状态的工作结果；若测试期望立即转出会失败。"
                    "需求确实要求等待时可以保留，否则改为读本状态动作写入的字段或用树事件"
                )
    return None, warnings


FOLLOW_SUBTREES = ("perception.search_for_target", "perception.track_selected_target")
SHUTDOWN_SUBTREES = ("perception.safe_sensor_shutdown",)


def _simplify_follower(fixer: Any, a: dict[str, Any]) -> dict[str, str] | str:
    """A follower the leader's formation steps drive gets two states: FOLLOW (its own
    sensing only) and SHUTDOWN (safe sensor shutdown after the end request), then COMPLETE.
    Its L2 tests become one cancel test, its L3 expectations "reaches FOLLOW" (or SHUTDOWN
    after the end request), and the model's references to its old states point to FOLLOW.
    The tests are rewritten because the states they named no longer exist (FOLLOWER_FSM)."""
    from afsim_research_agent.falcon.v3.fixer import _rebuild

    role = str(a.get("role") or "").strip()
    got = _mermaid(fixer, role)
    if isinstance(got, str):
        return got
    mkey, mtext = got
    texts = {b.key: b.text for b in fixer.d.blocks}
    try:
        model = yaml.safe_load(texts.get("model") or "") or {}
    except yaml.YAMLError:
        model = {}
    model = model if isinstance(model, dict) else {}
    events = (model.get("events") or {}) if isinstance(model, dict) else {}
    externals = [
        k for k, v in events.items() if isinstance(v, dict) and v.get("source") == "external"
    ]
    cancel = "cancelled" if "cancelled" in externals else (externals[0] if externals else None)
    if cancel is None:
        return "model 的 events 里没有外部的结束请求（如 cancelled: {source: external}），先用 declare_event 声明"
    roles = model.get("roles") or {}
    if role not in roles:
        return f"model 的 roles 里没有 {role}；现有：{list(roles)}"
    tactics = fixer.d.c.tactics

    def pick(names: tuple[str, ...], word: str) -> str:
        subs = [k for k, t in tactics.items() if getattr(t, "section", "") == "subtrees"]
        return next((t for t in names if t in tactics), "") or next(
            (k for k in subs if word in k), ""
        )

    follow = str(a.get("follow") or pick(FOLLOW_SUBTREES, "search"))
    shutdown = str(a.get("shutdown") or pick(SHUTDOWN_SUBTREES, "shutdown"))
    if not follow or follow not in tactics:
        return f"战术集里没有可用的感知子树（{', '.join(FOLLOW_SUBTREES)}）；用 follow 指定"
    old_states = sorted(_states(mtext))
    old_tree = (fixer._block(f"pytree:{role}") or type("B", (), {"text": ""})).text.splitlines()
    shut_body: list[str] = []
    if not shutdown:  # no shutdown subtree: keep the role's own shutdown state's body
        for st in old_states:
            span = _fn_span(old_tree, st) if re.search(r"SHUT|STOP|SAFE", st) else None
            if span:
                shut_body = old_tree[span[0] + 1 : span[1]]
                break
        if not shut_body:
            return "战术集里没有安全关机子树，原状态里也没有关机状态；用 shutdown 指定一个子树"
    done = f"{role}_shutdown_done"
    saved = fixer.d.blocks
    try:
        texts = structured(
            fixer, "declare_event", {"name": done, "source": "tree", "result": "success"}
        )
        if isinstance(texts, str):
            return texts
        texts.pop("_note", None)
        texts[mkey] = "\n".join(
            [
                "stateDiagram-v2",
                "  [*] --> FOLLOW",
                f"  FOLLOW --> SHUTDOWN : {cancel}",
                f"  SHUTDOWN --> COMPLETE : {done}",
                "  COMPLETE --> [*]",
            ]
        )
        shut = [f'    tactic("{shutdown}")'] if shutdown else shut_body
        texts[f"pytree:{role}"] = "\n".join(
            [
                "def FOLLOW():",
                "    # 由长车的编队动作带动行驶；本车只做感知",
                f'    tactic("{follow}")',
                "",
                "def SHUTDOWN():",
                *shut,
            ]
        )
        # the model's clause / implements references to the old states
        if texts.get("model"):
            texts["model"] = re.sub(
                rf"\b{re.escape(role)}\.(?!FOLLOW\b|SHUTDOWN\b|COMPLETE\b)[A-Z][A-Z0-9_]*\b",
                f"{role}.FOLLOW",
                texts["model"],
            )
        vb = texts.get("verification")
        if vb:
            try:
                data = yaml.safe_load(vb) or {}
            except yaml.YAMLError:
                data = None
            root = _vroot(data)
            if root:
                tests = [
                    t for t in root.get("tests") or [] if (t.get("given") or {}).get("role") != role
                ]
                tests.append(
                    {
                        "id": f"{role}_cancel_shutdown",
                        "given": {"role": role, "state": "FOLLOW", "events": [cancel]},
                        "expect": {"next_state": "SHUTDOWN"},
                    }
                )
                root["tests"] = tests
                for sc in root.get("scenarios") or []:
                    raised = any(
                        isinstance(e, dict) and e.get("event") == cancel
                        for e in sc.get("events") or []
                    )
                    for e in sc.get("expect") or []:
                        if isinstance(e, dict) and e.get("role") == role:
                            keep = {k: e[k] for k in ("id", "role", "instance") if k in e}
                            e.clear()
                            e.update(keep)
                            e["reaches"] = "SHUTDOWN" if raised else "FOLLOW"
                texts["verification"] = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        fixer.d.blocks = _rebuild(fixer.d.blocks, texts)
        out = {b.key: b.text for b in fixer.d.blocks}
    finally:
        fixer.d.blocks = saved
    out["_note"] = (
        f"{role} 改为 FOLLOW（{follow}）→ SHUTDOWN（{shutdown or '原关机动作'}）→ "
        f"COMPLETE；原来的状态 {', '.join(old_states)} 已去掉，相关测试和期望已改写"
    )
    return out
