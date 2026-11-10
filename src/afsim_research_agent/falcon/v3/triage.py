"""Triage of failed runs: whose fault, what it most likely is, how to change it.

The repair loop used to get symptoms ("left MOVE at 0 s", "did not move within 15 s") and had
to work back to the cause itself; most L2/L3/AFSIM repairs ended without a single edit, and
several tried to "repair" failures the sheet could not cause (a judge that flagged a stop at
the own position as a move that did not move; an enemy that never came into radar range).

Here the deterministic side does that step. Every finding names

* a **category** — ``model`` / ``test`` (the sheet: state diagram, conditions, trees; the
  verification block) or ``scenario`` / ``judge`` / ``capability`` (not the sheet's to fix:
  the scenario or its binding, a false alarm of the run checks, a capability the set lacks);
* the **symptom** from the manual below (one entry per failure pattern seen in the runs),
  with its likely cause and the change that fixes it;
* the role, state, trigger and blocks concerned, so the change can be made without
  searching.

Findings use only what the sheet itself names (role ids, state names, triggers, action ids),
never platform names, positions or native output: they go to the model service with the
feedback.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

MODEL, TEST, SCENARIO, JUDGE, CAPABILITY = "model", "test", "scenario", "judge", "capability"
SHEET = frozenset({MODEL, TEST})  # what the repair loop can and must fix
CATEGORY_TEXT = {
    MODEL: "稿件·模型",
    TEST: "稿件·验证块",
    SCENARIO: "想定/绑定",
    JUDGE: "判定误报",
    CAPABILITY: "能力缺失",
}
SENSOR_RANGE_M = 6000.0  # the lab search radar's maximum range (2D sensors: 5000 m)
TARGET_FIELDS = ("perception.", "engagement.", "threat.", "sharing.")
MOVE_ACTIONS = ("movement.move_to_point", "movement.follow_route", "movement.move_in_direction")
HOLDS = ("movement.stop", "movement.hold_position")


@dataclass(frozen=True)
class Symptom:
    id: str
    category: str
    title: str
    cause: str
    fix: str


MANUAL: dict[str, Symptom] = {
    s.id: s
    for s in (
        Symptom(
            "ENTER_LEAVE",
            MODEL,
            "进入状态同一拍（或 1 s 内）就离开",
            "离开它的条件在进入时已经成立：读的是字段初值或上一状态留下的值，"
            "而不是本状态动作做完以后才会变化的值",
            "在 model 块 events 中加 `{trigger}_tree: {{source: tree, result: success}}`，"
            "状态图里 {state} 的这条转换改用 {trigger}_tree（mermaid 与 model 同一步改），"
            "再删掉 conditions 里不再使用的 {trigger}；或改成读本状态动作写入的字段",
        ),
        Symptom(
            "STUCK",
            MODEL,
            "停在非终止状态直到结束",
            "离开它的触发在运行中从未成立：条件读的字段没有被本角色的动作写入，或猜错了字段取值",
            "让 {state} 的出口改用本状态树完成事件（events 加 `{state_l}_done: {{source: tree,"
            " result: success}}`），或改读本状态动作写入的字段；出口条件：{exits}",
        ),
        Symptom(
            "STUCK_NO_EXIT",
            MODEL,
            "停在非终止状态直到结束，这个状态在状态图里没有自己的出口转换",
            "状态图里没有从 {state} 出去的转换（只有“任意状态”的全局转换），漏写了或被删掉了",
            "对照需求，在 mermaid:{role} 里补上 {state} 的出口转换（何时离开、去哪个状态），"
            "触发名与 conditions / events 中的名字一致；不要改用树完成事件",
        ),
        Symptom(
            "STUCK_EVENT_TEST",
            TEST,
            "等待的外部事件在这个想定里没有发出",
            "想定没有安排事件 {trigger}，这一支走不到",
            "在 verification 这个想定的 events 中加 `{{event: {trigger}, at_s: …}}`；不要改模型",
        ),
        Symptom(
            "STUCK_EVENT_UNSENT",
            SCENARIO,
            "等待的可选外部事件在想定中没有发出",
            "{trigger} 是可选事件，生成的想定不发送它，这一支在本次运行里不会走到",
            "不是稿件问题，不要修改；用 classify 记录（category=scenario）",
        ),
        Symptom(
            "TARGET_OUT_OF_RANGE",
            SCENARIO,
            "一直没有目标",
            "运行中敌方离本车始终超过 {range_km:g} km（雷达作用距离之外），选不到目标",
            "不是稿件问题，不要修改；用 classify 记录（category=scenario）",
        ),
        Symptom(
            "NO_MOTION_AT_START",
            JUDGE,
            "开始移动但车辆没有移动",
            "{action} 的目标点就是出发点（{point}，接口锚点 own_start），车辆本来就不需要移动",
            "不是稿件问题，不要修改；用 classify 记录（category=judge）",
        ),
        Symptom(
            "NO_MOTION_UNBOUND",
            SCENARIO,
            "开始移动但车辆没有移动",
            "{action} 用的想定参数 {params} 没有由想定给出",
            "不是稿件问题，不要修改；用 classify 记录（category=scenario）",
        ),
        Symptom(
            "NO_MOTION_HELD",
            MODEL,
            "开始移动但车辆没有移动",
            "{state} 的树里同时有 {holds}，抵消了 {action}",
            "把 {holds} 从 {state} 的并行分支里去掉，或放到移动完成之后（sequence 中排在后面）",
        ),
        Symptom(
            "NO_MOTION",
            MODEL,
            "开始移动但车辆没有移动",
            "{action} 没有生效：参数（速度、路线、目标点）不对，或被别的动作覆盖",
            "用 lookup {action} 核对参数；位置和路线只用 point=/route= 引用想定接口，不要写坐标",
        ),
        Symptom(
            "STOP_DRIFT",
            MODEL,
            "停车后车辆又移动",
            "停车没有撤销其他运动控制（编队槽位、路线等仍在驱动车辆）",
            "停车所在状态不要再引用会驱动运动的子树；先结束编队/路线，再停车",
        ),
        Symptom(
            "NO_FIRE_STATE",
            MODEL,
            "没有开火",
            "开火所在状态 {states} 从未进入",
            "检查进入 {states} 的转换条件是否会成立（命令类条件用外部事件，不要等槽位/到位字段）",
        ),
        Symptom(
            "NO_FIRE_TARGET",
            SCENARIO,
            "进入了开火状态但没有开火",
            "开火状态里没有可打的目标（敌方不在射程或没有选到目标）",
            "不是稿件问题时用 classify 记录（category=scenario）；若开火前没有选目标，在开火前加选目标",
        ),
        Symptom(
            "OBLIGATION",
            MODEL,
            "需求约束未满足",
            "{detail}",
            "对照约束找到对应的状态或动作；先 diagnose 写出是哪条转换或哪个动作导致的",
        ),
        Symptom(
            "UNCOVERED",
            TEST,
            "需求约束没有被任何想定覆盖",
            "verification 里没有出现约束 {obligation} 前提的想定",
            "在 verification 中加一个会出现这个前提的想定（外部事件、敌方出现/消失、故障）；不要改模型",
        ),
        Symptom(
            "INFRA",
            JUDGE,
            "运行没有完成",
            "绑定、编译器或实验室文件出错",
            "不是稿件问题，不要修改；用 classify 记录（category=judge）",
        ),
    )
}


@dataclass
class Finding:
    symptom: str
    check: str  # the failed check: a 2D scenario id or "afsim"
    role: str = ""
    state: str = ""
    detail: str = ""  # what was observed, specific
    blocks: list[str] = field(default_factory=list)
    values: dict[str, Any] = field(default_factory=dict)  # fills the manual's templates

    @property
    def category(self) -> str:
        return MANUAL[self.symptom].category

    @property
    def sheet(self) -> bool:
        return self.category in SHEET

    def _fill(self, text: str) -> str:
        values = {"state": self.state, "state_l": self.state.lower(), **self.values}
        try:
            return text.format(**values)
        except (KeyError, IndexError, ValueError):
            return text

    def render(self) -> str:
        m = MANUAL[self.symptom]
        who = " ".join(x for x in (self.role, self.state) if x)
        head = f"[{CATEGORY_TEXT[m.category]}] {self.check}" + (f" {who}" if who else "")
        text = f"{head}：{m.title}" + (f"（{self.detail}）" if self.detail else "")
        text += f"。原因：{self._fill(m.cause)}。改法：{self._fill(m.fix)}"
        if self.sheet and self.blocks:
            text += f"（块：{', '.join(self.blocks)}）"
        return text

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"category": self.category, "text": self.render()}


def _dedupe(findings: list[Finding]) -> list[Finding]:
    seen: set[tuple[str, str, str, str]] = set()
    out = []
    for f in findings:
        key = (f.symptom, f.check, f.role, f.state)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


# ---------------------------------------------------------------- model helpers
def _mission(model: dict[str, Any] | None, role: str) -> dict[str, Any]:
    return (((model or {}).get("roles") or {}).get(role) or {}).get("mission") or {}


def _exits(model: dict[str, Any] | None, role: str, state: str) -> list[dict[str, Any]]:
    m = _mission(model, role)
    return [t for t in m.get("transitions") or [] if t.get("from") == state]


def _is_event(model: dict[str, Any] | None, trigger: str) -> bool:
    return trigger in ((model or {}).get("events") or {})


def _external(model: dict[str, Any] | None, trigger: str) -> bool:
    ev = ((model or {}).get("events") or {}).get(trigger) or {}
    return ev.get("source") == "external"


def _reads(expr: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(expr, dict):
        if isinstance(expr.get("bb"), str):
            out.add(expr["bb"])
        for v in expr.values():
            out |= _reads(v)
    elif isinstance(expr, list):
        for v in expr:
            out |= _reads(v)
    return out


def _actions(node: Any) -> list[dict[str, Any]]:
    if not isinstance(node, dict):
        return []
    out = [node] if node.get("type") == "action" else []
    for k in node.get("children") or []:
        out += _actions(k)
    if node.get("child") is not None:
        out += _actions(node["child"])
    return out


def _tree(model: dict[str, Any] | None, role: str, state: str) -> Any:
    states = (((model or {}).get("roles") or {}).get(role) or {}).get("states") or {}
    return (states.get(state) or {}).get("tree")


def _role_of(name: str) -> tuple[str, str]:
    """``role_1_0`` → (``role_1``, ``0``)."""
    role, _, inst = name.rpartition("_")
    return (role, inst) if inst.isdigit() else (name, "")


def _condition_text(model: dict[str, Any] | None, trigger: str) -> str:
    expr = ((model or {}).get("conditions") or {}).get(trigger)
    if expr is None:
        return trigger
    reads = sorted(_reads(expr))
    return f"{trigger}（读 {', '.join(reads)}）" if reads else trigger


def _stuck(
    check: str,
    role: str,
    state: str,
    t: float,
    model: dict[str, Any] | None,
    sent: set[str],
    optional_unsent: bool,
) -> Finding:
    """A role that stays in a non-terminal state: which kind of exit never held."""
    exits = _exits(model, role, state)
    triggers = [str(x.get("on")) for x in exits]
    for trig in triggers:
        if _is_event(model, trig) and _external(model, trig) and trig not in sent:
            if optional_unsent:
                return Finding(
                    "STUCK_EVENT_UNSENT", check, role, state, f"{t:g} s 起", [],
                    {"trigger": trig},
                )  # fmt: skip
            return Finding(
                "STUCK_EVENT_TEST", check, role, state, f"{t:g} s 起", ["verification"],
                {"trigger": trig},
            )  # fmt: skip
    text = "；".join(
        f"{x.get('from')} → {x.get('to')} 当 {_condition_text(model, str(x.get('on')))}"
        for x in exits
    )
    if not exits:
        # the state has no transition of its own: a missing exit, not a wrong condition
        # (S5 injection E5: the old advice to switch to a tree event led away from the fix)
        return Finding(
            "STUCK_NO_EXIT", check, role, state, f"{t:g} s 起一直停在 {state}",
            [f"mermaid:{role}"], {"role": role},
        )  # fmt: skip
    return Finding(
        "STUCK",
        check,
        role,
        state,
        f"{t:g} s 起一直停在 {state}",
        [f"mermaid:{role}", "conditions", "model"],
        {"exits": text or "无"},
    )


# ---------------------------------------------------------------- 2D scenarios
def from_scenario(entry: dict[str, Any], model: dict[str, Any] | None, spec: dict[str, Any] | None) -> list[Finding]:  # fmt: skip
    """Findings for one failed L3 entry of the 2D simulation."""
    if entry.get("passed") or entry.get("afsim") or entry.get("inconclusive"):
        return []
    sid = str(entry.get("id"))
    out: list[Finding] = []
    if sid == "obligation_coverage":
        for line in str(entry.get("render") or "").splitlines()[1:]:
            ob = line.split("需求约束", 1)[-1].split("（", 1)[0].strip()
            out.append(Finding("UNCOVERED", sid, detail=ob, blocks=["verification"],
                               values={"obligation": ob}))  # fmt: skip
        return out
    if sid == "effect_advances":
        return [
            Finding(
                "OBLIGATION", sid, detail="车辆前出不够远", blocks=["conditions", "model"],
                values={"detail": "前出状态没有走完路线就转出（边走边搜时一发现目标就停下）"},
            )
        ]  # fmt: skip
    scenario = next(
        (s for s in (spec or {}).get("scenarios") or [] if isinstance(s, dict) and s.get("id") == sid),
        {},
    )  # fmt: skip
    sent = {str(e.get("event")) for e in scenario.get("events") or [] if isinstance(e, dict)}
    for name, moves in (entry.get("transitions") or {}).items():
        role, _ = _role_of(name)
        for a, b in zip(moves, moves[1:], strict=False):
            if a.get("t") == b.get("t") and a.get("to") == b.get("from"):
                out.append(
                    Finding(
                        "ENTER_LEAVE", sid, role, str(a["to"]),
                        f"{a['t']:g} s 进入后同一拍因 {_condition_text(model, str(b['on']))} 离开",
                        [f"mermaid:{role}", "model", "conditions"], {"trigger": b["on"]},
                    )
                )  # fmt: skip
    duration = float(entry.get("duration_s") or 0.0)
    for name, seq in (entry.get("states") or {}).items():
        if not seq:
            continue
        t, state = seq[-1]
        role, _ = _role_of(name)
        if state in (_mission(model, role).get("terminal") or []):
            continue
        if duration and t > duration / 2:
            continue
        out.append(_stuck(sid, role, state, float(t), model, sent, optional_unsent=False))
    for v in entry.get("obligations") or []:
        if v.get("judged", True) and not v.get("passed", True):
            out.append(
                Finding("OBLIGATION", sid, detail=str(v.get("id")), blocks=["conditions", "model"],
                        values={"detail": str(v.get("detail") or "")[:200]})
            )  # fmt: skip
    return _dedupe(out)


def from_check(check: Any) -> list[Finding]:
    """Findings for every failed 2D scenario of a check (AFSIM findings ride on the note)."""
    model = getattr(check, "model", None)
    compiled = getattr(check, "compiled", None)
    spec = getattr(compiled, "spec", None) if compiled is not None else None
    out: list[Finding] = []
    for entry in getattr(check, "l3", None) or []:
        out += from_scenario(entry, model, spec)
    return _dedupe(out)


# ---------------------------------------------------------------- AFSIM runs
def _state_at(seq: list[Any], t: float) -> tuple[str, float, float | None]:
    """(state at t, entered, left) from a [(t, state)] sequence."""
    cur, entered, left = "", 0.0, None
    for i, (ts, st) in enumerate(seq):
        if ts <= t + 1e-6:
            cur, entered = st, ts
            left = seq[i + 1][0] if i + 1 < len(seq) else None
    return cur, entered, left


def _no_motion(res: Any, run: Any, issue: Any, role: str, model: dict[str, Any] | None) -> Finding:
    d = issue.data or {}
    action, t = str(d.get("action") or ""), float(d.get("t") or 0.0)
    state, _, left = _state_at((res.states or {}).get(issue.platform, []), t)
    acts = _actions(_tree(model, role, state))
    node = next((a for a in acts if a.get("action") == action), {})
    refs = [
        str(v["parameter_ref"])
        for v in (node.get("params") or {}).values()
        if isinstance(v, dict) and v.get("parameter_ref")
    ]
    params = (model or {}).get("parameters") or {}
    for ref in refs:
        tag = str((params.get(ref) or {}).get("scenario") or "")
        if tag.startswith("point:") and tag.endswith(":own_start"):
            return Finding(
                "NO_MOTION_AT_START", "afsim", role, state, f"{t:g} s 开始 {action}",
                values={"action": action, "point": tag.split(":")[1]},
            )  # fmt: skip
    bound = set((getattr(run, "parameters", None) or {}).keys())
    unbound = [r for r in refs if (params.get(r) or {}).get("scenario") and r not in bound]
    if unbound:
        return Finding(
            "NO_MOTION_UNBOUND", "afsim", role, state, f"{t:g} s 开始 {action}",
            values={"action": action, "params": ", ".join(unbound)},
        )  # fmt: skip
    if left is not None and left - t <= 1.0:
        exit_on = next(
            (
                str(x.get("on"))
                for x in _exits(model, role, state)
                if x.get("to") == _state_at(res.states.get(issue.platform, []), left)[0]
            ),
            "",
        )
        return Finding(
            "ENTER_LEAVE", "afsim", role, state, f"{t:g} s 开始 {action}，{left:g} s 就离开 {state}",
            [f"mermaid:{role}", "model", "conditions"], {"trigger": exit_on or "离开条件"},
        )  # fmt: skip
    holds = sorted({str(a.get("action")) for a in acts if a.get("action") in HOLDS})
    if holds:
        return Finding(
            "NO_MOTION_HELD", "afsim", role, state, f"{t:g} s 开始 {action}", [f"pytree:{role}"],
            {"action": action, "holds": "、".join(holds)},
        )  # fmt: skip
    return Finding(
        "NO_MOTION", "afsim", role, state, f"{t:g} s 开始 {action}", [f"pytree:{role}"],
        {"action": action},
    )  # fmt: skip


def _min_enemy_distance(res: Any, platform: str, own: set[str]) -> float | None:
    motion = getattr(res, "motion", None) or {}
    me = motion.get(platform)
    if not me:
        return None
    others = [m for p, m in motion.items() if p not in own]
    if not others:
        return None
    best = math.inf
    for o in others:
        for a in (me.get("start"), me.get("end")):
            for b in (o.get("start"), o.get("end")):
                if a and b:
                    best = min(best, math.hypot(a[0] - b[0], a[1] - b[1]))
    return None if best is math.inf else best


def from_afsim(
    results: list[Any] | None,
    runs: list[Any],
    model: dict[str, Any] | None,
    optional_events: set[str] | None = None,
) -> list[Finding]:
    """Findings for failed AFSIM runs (``RunJudgement`` with the ``RunConfig`` it ran)."""
    out: list[Finding] = []
    for res, run in zip(results or [], runs, strict=False):
        if getattr(res, "passed", False):
            continue
        platforms = {p.name: p for p in getattr(run, "platforms", []) or []}
        own = set(platforms)
        sent = {str(e.event) for e in getattr(run, "events", []) or []}
        end = float(getattr(run, "end_time_s", 0.0) or 0.0)
        handled: set[str] = set()
        for issue in getattr(res, "issues", []) or []:
            if getattr(issue, "source", "") == "infrastructure":
                out.append(Finding("INFRA", "afsim"))
                continue
            p = platforms.get(issue.platform or "")
            role = p.role if p else ""
            if issue.code == "NO_MOTION" and p:
                out.append(_no_motion(res, run, issue, role, model))
                handled.add(issue.platform)
            elif issue.code == "STOP_DRIFT":
                out.append(
                    Finding("STOP_DRIFT", "afsim", role, blocks=[f"pytree:{role}"] if role else [])
                )
            elif issue.code == "IR_LOOP":
                chain = (issue.data or {}).get("chain") or []
                out.append(
                    Finding(
                        "ENTER_LEAVE", "afsim", role, str(chain[0]) if chain else "",
                        " → ".join(map(str, chain[:6])), [f"mermaid:{role}", "model", "conditions"],
                        {"trigger": "离开条件"},
                    )
                )  # fmt: skip
            elif issue.code == "EVENTS" and (issue.data or {}).get("kind") == "WEAPON_FIRED":
                out += _no_fire(res, run, model)
            elif issue.code == "OBLIGATION":
                detail = str((issue.data or {}).get("detail") or "")
                out.append(
                    Finding("OBLIGATION", "afsim", detail=str((issue.data or {}).get("id") or ""),
                            blocks=["conditions", "model"], values={"detail": detail[:200]})
                )  # fmt: skip
        # roles that stayed in a non-terminal state from early on
        for name, seq in (getattr(res, "states", None) or {}).items():
            p = platforms.get(name)
            if p is None or not seq or name in handled:
                continue
            t, state = seq[-1]
            if state in (_mission(model, p.role).get("terminal") or []) or state in (
                _mission(model, p.role).get("safe") or []
            ):
                continue
            if end and t > end / 2:
                continue
            exits = _exits(model, p.role, state)
            reads = set().union(
                *[_reads(((model or {}).get("conditions") or {}).get(str(x.get("on")))) for x in exits]
            ) if exits else set()  # fmt: skip
            far = _min_enemy_distance(res, name, own)
            if (
                any(r.startswith(TARGET_FIELDS) for r in reads)
                and far is not None
                and far > SENSOR_RANGE_M
            ):
                out.append(
                    Finding("TARGET_OUT_OF_RANGE", "afsim", p.role, state, f"最近约 {far / 1000:.1f} km",
                            values={"range_km": SENSOR_RANGE_M / 1000})
                )  # fmt: skip
                continue
            unsent = {
                str(x.get("on"))
                for x in exits
                if _external(model, str(x.get("on"))) and str(x.get("on")) not in sent
            }
            optional = bool(unsent) and (optional_events is None or unsent <= optional_events)
            out.append(
                _stuck("afsim", p.role, state, float(t), model, sent, optional_unsent=optional)
            )
    return _dedupe(out)


def _no_fire(res: Any, run: Any, model: dict[str, Any] | None) -> list[Finding]:
    out = []
    for p in getattr(run, "platforms", []) or []:
        roles = ((model or {}).get("roles") or {}).get(p.role) or {}
        fire_states = [
            s
            for s, st in (roles.get("states") or {}).items()
            if any(
                str(a.get("action", "")).startswith("engagement.") for a in _actions(st.get("tree"))
            )
        ]
        if not fire_states:
            continue
        visited = {s for _, s in (getattr(res, "states", {}) or {}).get(p.name, [])}
        if not visited & set(fire_states):
            out.append(
                Finding("NO_FIRE_STATE", "afsim", p.role, fire_states[0], blocks=[f"mermaid:{p.role}", "conditions", "model"],
                        values={"states": "、".join(fire_states)})
            )  # fmt: skip
        else:
            out.append(
                Finding("NO_FIRE_TARGET", "afsim", p.role, sorted(visited & set(fire_states))[0])
            )
    return out


# ---------------------------------------------------------------- summaries
def render(findings: list[Finding], limit: int = 8) -> list[str]:
    if not findings:
        return []
    lines = ["定位与改法（规则判定，先照此处理）："]
    for i, f in enumerate(findings[:limit], start=1):
        lines.append(f"  ({i}) {f.render()}")
    if any(not f.sheet for f in findings[:limit]):
        lines.append(
            "  标为想定/绑定、判定误报、能力缺失的不是战术稿的问题：不要为它们修改稿件，"
            "用 classify 记录后处理其余问题"
        )
    return lines


def only_outside_sheet(findings: list[Finding]) -> bool:
    """Every finding is someone else's to fix (and there is at least one)."""
    return bool(findings) and all(not f.sheet for f in findings)
