"""Requirement properties: checks written from the requirement, not by the sheet's author.

``falcon fix`` passed py9 in four steps, with two tactical mistakes left in: the checks
it had to satisfy were the sheet's own tests (which the loop may rewrite when the
behavior changes) and a few generic probes. A loop optimises for what it is checked
against, so the checks that decide "the requirement is met" must come from somewhere
the loop cannot write.

Here a separate step (its own model call, which sees the requirement, the clauses and
the selected tactic but no state diagram, tree or test of the sheet) states the
requirement as a few situation → response properties over observable behavior:

* **respond** — held in a situation, the role issues one of these actions / sends one
  of these kinds of message (or takes this formation) within N ticks;
* **settle** — … and is still doing so once it has settled (no bouncing away);
* **avoid** — never does any of these within N ticks.

Each property is checked from a class of start states (``moving`` — states whose own
tree drives along a route; ``any``; ``initial``) by ticking the role with the L2
runtime under the constant situation (messages and elapsed time only on the first tick,
as in the oscillation probe). Properties are stored in the package
(``requirement_properties.yaml``), are validated against the closed vocabulary, are
listed in the review for a person to confirm or disable, and cannot be edited by the
fix loop: a property the model thinks is wrong is a question for a person.
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from afsim_research_agent.falcon.sheet.probes import MOVE_COMMANDS, _tree_commands
from afsim_research_agent.falcon.sheet.values import quantity

PROPERTIES_FILE = "requirement_properties.yaml"
PROPERTIES_VERSION = "falcon-properties-2026.10.20"
SECTIONS = ("tracks", "terrain", "platform", "components", "elapsed", "messages", "team")
COMPONENT_KINDS = ("comms", "sensors", "weapons", "mover")
MAX_TICKS = 8


class PropertyDraft(BaseModel):
    id: str
    clauses: list[str] = Field(default_factory=list)
    text: str = ""
    role: str = "any"
    start: Literal["moving", "any", "initial"] = "any"
    situation: dict[str, Any] = Field(default_factory=dict)
    within: int = 3
    step: str = "10 s"  # time one tick stands for (the time in a state grows with it)
    repeat_messages: bool = False  # the situation's messages arrive every tick, not only the first
    kind: Literal["respond", "settle", "avoid"] = "respond"
    actions: list[str] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)
    formation: str | None = None


class SkippedClause(BaseModel):
    clause: str
    reason: str = ""


class PropertySet(BaseModel):
    properties: list[PropertyDraft] = Field(default_factory=list)
    skipped: list[SkippedClause] = Field(default_factory=list)


# ---------------------------------------------------------------------- writing
def property_prompt(
    requirement: str,
    clauses: list[Any],
    tactic: str,
    roles: dict[str, dict[str, Any]],
    actions: list[str],
    message_kinds: list[str],
) -> str:
    role_lines = "\n".join(
        f"- {rid}：{info.get('platform', '')}，{info.get('description', '')}"
        for rid, info in roles.items()
    )
    clause_lines = "\n".join(f"- {c.id}（{c.kind}）：{c.text}" for c in clauses)
    return f"""你是独立的需求核查员。根据需求和选定战术，把可以在单个角色上检验的要求写成"情况 → 反应"属性，用来检查别人写的战术行为模型。
你看不到模型的状态图、行为树和测试，也不需要看：属性只描述从外部能观察到的行为（动作、发出的消息种类、队形）。

需求：{requirement}
需求条款：
{clause_lines}
选定战术：
{tactic[:2500]}

角色：
{role_lines}
可观察的动作（只能用这些名字）：{", ".join(actions)}
消息种类（协同消息名的后缀，只能用这些）：{", ".join(message_kinds) or "无"}

每条属性：
- id：p01、p02…；clauses：依据的条款 ID；text：一句话说明要求。
- role：角色 ID，或 any（每个有状态机的角色都要满足）。
- start：从哪些状态出发检验。moving = 沿路线行进的状态；any = 所有非终止状态；initial = 初始状态。
- situation：保持不变的情况，写法同单步测试的 given，按部分嵌套，例如 {{"tracks": {{"hostile_count": 1, "nearest_hostile_range": "800 m"}}, "components": {{"comms.available": false}}, "messages": ["ack"]}}。可用：
  - tracks（hostile_count、nearest_hostile_range、current_target_valid、current_target_range）；
  - terrain（class、distance_to_class.<类别>）；
  - platform（damaged、alive）；
  - components（comms.available、sensors.available、weapons.available、mover.available，写 true/false）；
  - elapsed（state：在当前状态已停留的时间）；
  - messages（收到的消息种类列表，如 [ack]）。
  数值写成"数 单位"（如 "800 m"）。
- kind：
  - respond = within 拍内至少一拍出现 actions/messages/formation 中的任一项；
  - settle = 稳定后仍在做；
  - avoid = within 拍内都不出现。
- within：拍数（1–{MAX_TICKS}）；step：每拍代表的时间（默认 "10 s"），在某状态停留的时间按拍累加，用来检验超时类要求（例如 step "1 min"）。
- 消息默认只在第一拍送达；repeat_messages: true 表示每拍都会收到（例如上级的确认在报告之后才到）。
- actions / messages / formation：期望（或禁止）的动作名、消息种类、队形名。

要求：
- 只写能用上面词汇检验的要求，通常 3–8 条。
- 不能这样检验的条款（例如车距数值、与敌距离、需要多角色配合的过程）放进 skipped 并说明原因。
- 宁缺毋滥：写出的每条属性都要是需求或战术明确要求的。

输出 JSON：{{"properties": [...], "skipped": [{{"clause": "rq_..", "reason": "..."}}]}}"""


def message_kinds(ir_or_messages: Any) -> list[str]:
    msgs = ir_or_messages.get("messages") if isinstance(ir_or_messages, dict) else ir_or_messages
    kinds = []
    for m in msgs or []:
        name = m["id"] if isinstance(m, dict) else str(m)
        kind = name.split("__", 1)[1] if "__" in name else name
        if kind not in kinds:
            kinds.append(kind)
    return kinds


def normalize_situation(raw: Any) -> dict[str, Any]:
    """Accept the flat spellings models write: {"terrain.class": "urban",
    "components.comms.available": false} → {"terrain": {"class": "urban"},
    "components": {"comms.available": false}} (qwen wrote all seven py9 properties so)."""
    out: dict[str, Any] = {}
    for key, value in (raw or {}).items():
        key = str(key).removeprefix("given.")
        if key == "messages":
            out["messages"] = value if isinstance(value, list) else [value]
            continue
        if "." in key and key.split(".", 1)[0] in SECTIONS:
            section, rest = key.split(".", 1)
            out.setdefault(section, {})[rest] = value
            continue
        if isinstance(value, dict):
            out.setdefault(key, {}).update(value)
        else:
            out[key] = value
    messages = out.get("messages")
    if isinstance(messages, list):
        kinds = []
        for m in messages:
            if isinstance(m, dict):  # [{contact_report.ack: {}}] or [{"kind": "ack"}]
                m = m.get("kind") or next(iter(m), "")
            kinds.append(str(m).rsplit(".", 1)[-1].rsplit("__", 1)[-1])
        out["messages"] = kinds
    return out


def validate_drafts(
    drafts: PropertySet, roles: list[str], actions: set[str], kinds: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Drafts → stored properties (checked against the vocabulary) and rejections."""
    kept: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in drafts.properties:
        p = p.model_copy(update={"situation": normalize_situation(p.situation)})
        why = []
        if p.role != "any" and p.role not in roles:
            why.append(f"未知角色 {p.role}")
        unknown = [a for a in p.actions if a not in actions]
        if unknown:
            why.append(f"未知动作 {', '.join(unknown)}")
        bad_kinds = [k for k in p.messages if k not in kinds]
        if bad_kinds:
            why.append(f"未知消息种类 {', '.join(bad_kinds)}")
        bad_sections = [s for s in p.situation if s not in SECTIONS]
        if bad_sections:
            why.append(f"情况中未知的部分 {', '.join(bad_sections)}")
        if not (p.actions or p.messages or p.formation):
            why.append("没有写期望的动作、消息或队形")
        if p.id in seen:
            why.append("ID 重复")
        if why:
            rejected.append(
                {"id": p.id, "why": "；".join(why), "draft": p.model_dump(exclude_none=True)}
            )
            continue
        seen.add(p.id)
        item = p.model_dump(exclude_none=True)
        item["within"] = max(1, min(MAX_TICKS, int(p.within)))
        item["enabled"] = True
        item["status"] = "unconfirmed"
        kept.append(item)
    return kept, rejected


def properties_document(
    properties: list[dict[str, Any]],
    skipped: list[dict[str, str]],
    rejected: list[dict[str, Any]],
    written_by: str,
) -> str:
    doc = {
        "format": "RequirementProperties",
        "version": PROPERTIES_VERSION,
        "written_by": written_by,
        "note": (
            "由独立的核查步骤根据需求条款与选定战术写成（不看状态图、行为树和测试），"
            "用来检查战术稿是否满足需求。falcon fix 不能修改本文件。请人工核对："
            "确认的把 status 改为 confirmed；写错的改正或把 enabled 改为 false"
        ),
        "properties": properties,
        "skipped": skipped,
        "rejected": rejected,
    }
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=100)


def load_properties(package_dir: Path) -> list[dict[str, Any]] | None:
    path = Path(package_dir) / PROPERTIES_FILE
    if not path.is_file():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [p for p in data.get("properties") or [] if isinstance(p, dict)]


def properties_hash(package_dir: Path) -> str | None:
    path = Path(package_dir) / PROPERTIES_FILE
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


# ---------------------------------------------------------------------- checking
def _situation(ir: dict[str, Any], role: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    """A property's situation → an L2 given (units, generic components, message kinds)."""
    given: dict[str, Any] = {}
    deps = role.get("dependencies") or {}
    for section, values in (raw or {}).items():
        if section == "messages":
            kinds = values if isinstance(values, list) else [values]
            wanted = {str(k) for k in kinds}
            given["messages"] = [
                {"message": m["id"], "fields": {}}
                for m in ir.get("messages") or []
                if m["id"].split("__", 1)[-1] in wanted
                and role["id"] in (m.get("to") or [role["id"]])
            ]
            continue
        if not isinstance(values, dict):
            continue
        out: dict[str, Any] = {}
        for key, value in values.items():
            q = quantity(value) if isinstance(value, str) else None
            value = q if q is not None else value
            if section == "components":
                kind, _, prop = str(key).partition(".")
                if kind == "mover":
                    out[f"mover.{prop}"] = value
                elif kind in COMPONENT_KINDS:
                    for comp in deps.get(kind) or []:
                        out[f"{comp['id']}.{prop}"] = value
                else:
                    out[key] = value
            else:
                out[key] = value
        given[section] = out
    return given


def _starts(ir: dict[str, Any], role: dict[str, Any], start: str) -> list[str]:
    machine = role.get("state_machine") or {}
    states = [s for s in machine.get("states") or [] if not s.get("terminal")]
    if start == "initial":
        return [machine["initial_state"]] if machine.get("initial_state") else []
    if start == "moving":
        return [s["id"] for s in states if _tree_commands(ir, s.get("tree")) & MOVE_COMMANDS]
    return [s["id"] for s in states]


def _tick(
    ir: dict[str, Any], role: str, state: str, given: dict[str, Any]
) -> dict[str, Any] | None:
    from afsim_research_agent.ir.l2 import run_test

    test = {
        "id": "property",
        "given": {"role": role, "instance": 0, "state": state, **given},
        "expect": {},
    }
    try:
        return run_test(ir, test).result
    except (KeyError, TypeError, ValueError):
        return None


def _observed(result: dict[str, Any]) -> tuple[set[str], set[str], set[str]]:
    actions = set(result.get("issued_actions") or [])
    kinds = {m.split("__", 1)[-1] for m in result.get("sent_messages") or []}
    formations = {
        str(v)
        for k, v in (result.get("blackboard") or {}).items()
        if k.endswith("current_formation")
    }
    for m in result.get("sent_message_details") or []:
        f = (m.get("fields") or {}).get("formation")
        if f:
            formations.add(str(f))
    return actions, kinds, formations


def _hit(p: dict[str, Any], seen: tuple[set[str], set[str], set[str]]) -> list[str]:
    actions, kinds, formations = seen
    out = [a for a in p.get("actions") or [] if a in actions]
    out += [f"消息 {k}" for k in p.get("messages") or [] if k in kinds]
    if p.get("formation") and p["formation"] in formations:
        out.append(f"队形 {p['formation']}")
    return out


def _want(p: dict[str, Any]) -> str:
    parts = list(p.get("actions") or [])
    parts += [f"{k} 类消息" for k in p.get("messages") or []]
    if p.get("formation"):
        parts.append(f"队形 {p['formation']}")
    return " / ".join(parts)


def check_property(
    ir: dict[str, Any], p: dict[str, Any]
) -> tuple[list[tuple[str, str]], str | None]:
    """(failures as (role, message), note when the property could not be checked)."""
    roles = [
        r
        for r in ir["roles"]
        if r.get("state_machine") and (p.get("role", "any") in ("any", r["id"]))
    ]
    if not roles:
        return [], f"属性 {p['id']}：没有可检验的角色 {p.get('role')}"
    failures: list[tuple[str, str]] = []
    checked = 0
    for role in roles:
        given0 = _situation(ir, role, p.get("situation") or {})
        for start in _starts(ir, role, p.get("start", "any")):
            checked += 1
            state, trace, hits, last, settled = start, [], [], None, False
            step = quantity(str(p.get("step") or "10 s")) or {"value": 10, "unit": "s"}
            in_state = 0  # ticks spent in the current state before this one
            for tick in range(int(p.get("within", 3))):
                given = copy.deepcopy(given0)
                if tick and not p.get("repeat_messages"):
                    given.pop("messages", None)
                if tick:
                    given["elapsed"] = {
                        "state": {"value": step["value"] * in_state, "unit": step["unit"]}
                    }
                result = _tick(ir, role["id"], state, given)
                if result is None:
                    break
                seen = _observed(result)
                found = _hit(p, seen)
                hits += found
                nxt = result.get("next_state") or state
                last = (nxt, found)
                trace.append(
                    f"{nxt}（动作 {sorted(seen[0]) or '无'}，消息 {sorted(seen[1]) or '无'}）"
                )
                # settled: the last tick kept the state (a bounce never settles)
                settled = nxt == state
                in_state = in_state + 1 if nxt == state else 0
                state = nxt
            kind = p.get("kind", "respond")
            ok = (
                bool(hits)
                if kind == "respond"
                else (settled and bool(last and last[1]) if kind == "settle" else not hits)
            )
            if ok:
                continue
            path = " → ".join([start, *trace])
            if kind == "avoid":
                why = f"出现了不该有的 {', '.join(sorted(set(hits)))}"
            elif kind == "settle" and not settled:
                why = f"{p.get('within', 3)} 拍内没有稳定下来（状态还在变化）"
            elif kind == "settle":
                why = f"稳定后没有在做 {_want(p)}"
            else:
                why = f"{p.get('within', 3)} 拍内没有出现 {_want(p)}"
            failures.append(
                (
                    role["id"],
                    f"需求属性 {p['id']}（{', '.join(p.get('clauses') or []) or '无条款'}：{p.get('text', '')}）"
                    f"不满足：角色 {role['id']} 从 {start} 出发，情况 "
                    f"{yaml.safe_dump(p.get('situation') or {}, allow_unicode=True, default_flow_style=True).strip()}"
                    f"，{why}。逐拍：{path}",
                )
            )
    if not checked:
        return [], f"属性 {p['id']}：没有符合 start={p.get('start')} 的起始状态，未检验"
    return failures, None


def check_properties(
    ir: dict[str, Any], properties: list[dict[str, Any]]
) -> tuple[list[tuple[str, str]], list[str]]:
    """(failures as (IR path, message), notes) for the enabled properties."""
    failures: list[tuple[str, str]] = []
    notes: list[str] = []
    for p in properties:
        if p.get("enabled") is False or not p.get("id"):
            continue
        found, note = check_property(ir, p)
        if note:
            notes.append(note)
        failures += [(f"properties.{p['id']}", message) for _role, message in found]
    return failures, notes


__all__ = [
    "PROPERTIES_FILE",
    "PropertyDraft",
    "PropertySet",
    "check_properties",
    "load_properties",
    "message_kinds",
    "properties_document",
    "properties_hash",
    "property_prompt",
    "validate_drafts",
]
