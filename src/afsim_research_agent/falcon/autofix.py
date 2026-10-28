"""Deterministic fixes applied to the assembled draft before IR validation.

Only slips whose correct form is unambiguous are fixed here, so the model is
not asked to regenerate a whole section for them. Every change is reported
(as an ``EnforcementNote`` with rule ``auto_fix``) and shown in the review.
Anything that needs a judgement is left for the validator and the repair loop.
"""

from __future__ import annotations

import re
from typing import Any

from afsim_research_agent.falcon.models import EnforcementNote
from afsim_research_agent.ir.catalog import Catalog

LOGIC_KEYS = (
    "all",
    "any",
    "not",
    "compare",
    "literal",
    "condition_ref",
    "parameter_ref",
    "blackboard_value",
    "function_call",
)
COMPONENT_KINDS = {"sensor": "sensors", "weapon": "weapons", "comm": "comms"}
# keys models use instead of the component kind ("component: comm_0", "id: comm_0")
COMPONENT_KEY_ALIASES = ("component", "id", "name", "component_id")
_BB_KEY = re.compile(
    r"^(?:(?P<role>[a-z][a-z0-9_]*)\.)?(?P<board>mission)\.(?P<var>[A-Za-z_][A-Za-z0-9_]*)$"
)


def _note(notes: list[EnforcementNote], path: str, detail: str) -> None:
    notes.append(EnforcementNote(path=path, rule="auto_fix", detail=detail))


def _component_ids(data: dict[str, Any], kind: str) -> set[str]:
    plural = COMPONENT_KINDS[kind]
    return {
        c["id"]
        for role in data.get("roles", [])
        for c in (role.get("dependencies") or {}).get(plural, [])
        if isinstance(c, dict) and "id" in c
    }


class _ExpressionFixer:
    def __init__(self, data: dict[str, Any], catalog: Catalog, notes: list[EnforcementNote]):
        self.catalog = catalog
        self.notes = notes
        self.known = set(LOGIC_KEYS) | set(catalog.operands) | set(catalog.operand_aliases)
        self.component_ids = {k: _component_ids(data, k) for k in COMPONENT_KINDS}

    def fix(self, node: Any, path: str) -> Any:
        if isinstance(node, list):
            return [self.fix(item, f"{path}[{i}]") for i, item in enumerate(node)]
        if not isinstance(node, dict):
            return node
        out: dict[str, Any] = {}
        for key, value in node.items():
            new_key = key
            if isinstance(key, str) and key not in self.known and key.lower() in self.known:
                new_key = key.lower()
                _note(self.notes, path, f"运算符 '{key}' → '{new_key}'")
            value = self.fix(value, f"{path}.{new_key}")
            operand = self.catalog.operand(new_key) if isinstance(new_key, str) else None
            if operand is not None and operand.component in COMPONENT_KINDS:
                value = self._component(operand.component, value, f"{path}.{new_key}")
            out[new_key] = value
        return out

    def _component(self, kind: str, value: Any, path: str) -> Any:
        if isinstance(value, str):
            value = {"property": value}
        if not isinstance(value, dict):
            return value
        if kind not in value:
            for alias in COMPONENT_KEY_ALIASES:
                if alias in value and alias != "property":
                    value = {**value}
                    value[kind] = value.pop(alias)
                    _note(self.notes, path, f"字段 '{alias}' → '{kind}'")
                    break
        if kind not in value:
            ids = self.component_ids[kind]
            if len(ids) == 1:
                value = {**value, kind: next(iter(ids))}
                _note(self.notes, path, f"补全唯一的 {kind} 组件 '{next(iter(ids))}'")
        return value


def _expression_slots(data: dict[str, Any]) -> list[tuple[dict[str, Any], str, str]]:
    """(container, key, path) for every expression-bearing field of the draft."""
    slots: list[tuple[dict[str, Any], str, str]] = []
    for i, cond in enumerate(data.get("conditions", [])):
        if "expression" in cond:
            slots.append((cond, "expression", f"conditions[{i}].expression"))
    for i, beh in enumerate(data.get("behaviors", [])):
        for key in ("precondition",):
            if key in beh:
                slots.append((beh, key, f"behaviors[{i}].{key}"))
        for key in ("success", "failure"):
            outcome = beh.get(key)
            if isinstance(outcome, dict) and "expression" in outcome:
                slots.append((outcome, "expression", f"behaviors[{i}].{key}.expression"))
    for r, role in enumerate(data.get("roles", [])):
        for s, state in enumerate((role.get("state_machine") or {}).get("states", [])):
            for t, tr in enumerate(state.get("transitions", [])):
                if "when" in tr:
                    path = f"roles[{r}].state_machine.states[{s}].transitions[{t}].when"
                    slots.append((tr, "when", path))
    return slots


def _fix_actions(data: dict[str, Any], catalog: Catalog, notes: list[EnforcementNote]) -> None:
    for i, beh in enumerate(data.get("behaviors", [])):
        for j, action in enumerate(beh.get("actions", [])):
            definition = catalog.action(str(action.get("command", "")))
            if definition is None:
                continue
            if (
                action.get("issue_policy") == "every_tick"
                and not definition.every_tick
                and not action.get("allow_every_tick")
            ):
                action["issue_policy"] = "once_per_activation"
                _note(
                    notes,
                    f"behaviors[{i}].actions[{j}].issue_policy",
                    f"'{definition.name}' 不允许每 tick 下达，改为 once_per_activation",
                )


def _blackboard_keys(node: Any, found: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "blackboard_value" and isinstance(value, str):
                found.add(value)
            elif key == "key" and isinstance(value, str) and _BB_KEY.match(value):
                found.add(value)
            else:
                _blackboard_keys(value, found)
    elif isinstance(node, list):
        for item in node:
            _blackboard_keys(item, found)


def _fix_blackboards(data: dict[str, Any], notes: list[EnforcementNote]) -> None:
    """Conditions and behaviors are shared by roles; a variable one role declares and
    another role's logic reads is declared for that role too (same type and default)."""
    roles = data.get("roles", [])
    declared: dict[str, dict[str, dict[str, Any]]] = {}
    for role in roles:
        for board in role.get("blackboards", []):
            for var in board.get("variables", []):
                declared.setdefault(role["id"], {})[f"{board['id']}.{var['name']}"] = var
    used: set[str] = set()
    for key in ("conditions", "behaviors"):
        _blackboard_keys(data.get(key, []), used)
    for r, role in enumerate(roles):
        _blackboard_keys((role.get("state_machine") or {}).get("states", []), used)
    unqualified = set()
    for ref in used:
        match = _BB_KEY.match(ref)
        if match and match.group("role") is None:
            unqualified.add(f"{match.group('board')}.{match.group('var')}")
    for key in sorted(unqualified):
        owners = [r for r in roles if key in declared.get(r["id"], {})]
        if not owners:
            continue
        template = owners[0]
        var = declared[template["id"]][key]
        board_id, name = key.split(".", 1)
        for r, role in enumerate(roles):
            if key in declared.get(role["id"], {}):
                continue
            boards = role.setdefault("blackboards", [])
            board = next((b for b in boards if b.get("id") == board_id), None)
            if board is None:
                board = {"id": board_id, "scope": "platform", "variables": []}
                boards.append(board)
            board["variables"].append(dict(var))
            declared.setdefault(role["id"], {})[key] = var
            _note(
                notes,
                f"roles[{r}].blackboards",
                f"共享逻辑读取 '{key}'，按 '{template['id']}' 的定义为 '{role['id']}' 补充声明",
            )


def _fix_tests(data: dict[str, Any], catalog: Catalog, notes: list[EnforcementNote]) -> None:
    """``issued_actions`` lists action names; models often write behavior IDs instead."""
    behaviors = {b["id"]: b for b in data.get("behaviors", []) if "id" in b}
    for i, test in enumerate(data.get("tests", [])):
        expect = test.get("expect") or {}
        actions = expect.get("issued_actions")
        if not isinstance(actions, list):
            continue
        fixed: list[str] = []
        changed = False
        for name in actions:
            if not isinstance(name, str) or catalog.action(name) is not None:
                fixed.append(name)
                continue
            commands: list[str] = []
            if name in behaviors:
                commands = [a.get("command") for a in behaviors[name].get("actions", [])]
            elif "__" in name and catalog.action(name.rsplit("__", 1)[1]) is not None:
                commands = [name.rsplit("__", 1)[1]]  # generated '<coord>__<action>' behavior
            commands = [c for c in commands if isinstance(c, str)]
            if commands:
                fixed += commands
                changed = True
                _note(
                    notes, f"tests[{i}].expect.issued_actions", f"行为 '{name}' → 动作 {commands}"
                )
            else:
                fixed.append(name)
        if changed:
            expect["issued_actions"] = list(dict.fromkeys(fixed))


TEXT_KEYS = ("description", "statement", "summary", "reason", "display_name")
_TEMPLATE = re.compile(r"\$\{|\$\(|\{\{|\}\}|\{%|%\}|`")


def _fix_text(node: Any, path: str, notes: list[EnforcementNote]) -> None:
    """Free-text fields: drop template/code marks the IR loader rejects (models like
    backticks around identifiers). Rationales are handled by the basis enforcer."""
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            if key in TEXT_KEYS and isinstance(value, str) and _TEMPLATE.search(value):
                node[key] = _TEMPLATE.sub("", value)
                _note(notes, child, "文本中的模板/代码标记已去掉")
            else:
                _fix_text(value, child, notes)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _fix_text(value, f"{path}[{index}]", notes)


# Coarse drafts: a component the model uses but did not declare gets a default (it is a
# supporting capability; the user principle is to simplify it rather than fail the draft).
DEFAULT_COMPONENTS: dict[str, dict[str, tuple[str, str]]] = {
    "ground_vehicle": {
        "comms": ("platoon_net", "voice_data"),
        "sensors": ("vehicle_sight", "visual_search"),
        "weapons": ("main_gun", "direct_fire"),
    },
    "air_vehicle": {
        "comms": ("data_net", "data_link"),
        "sensors": ("radar", "radar_search"),
        "weapons": ("missile", "guided_missile"),
    },
    "surface_vessel": {
        "comms": ("data_net", "data_link"),
        "sensors": ("radar", "radar_search"),
        "weapons": ("main_gun", "direct_fire"),
    },
}
_NEED_TO_DEP = {"comms": "comms", "sensor": "sensors", "weapon": "weapons", "comm": "comms"}


def _role_names(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value.split("[", 1)[0]}
    if isinstance(value, list):
        return {n for v in value for n in _role_names(v)}
    if isinstance(value, dict):
        return {n for v in value.values() for n in _role_names(v)}
    return set()


def _operand_kinds(node: Any, catalog: Catalog, found: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            operand = catalog.operand(key) if isinstance(key, str) else None
            if operand is not None and operand.component in COMPONENT_KINDS:
                found.add(COMPONENT_KINDS[operand.component])
            _operand_kinds(value, catalog, found)
    elif isinstance(node, list):
        for value in node:
            _operand_kinds(value, catalog, found)


def _fix_dependencies(data: dict[str, Any], catalog: Catalog, notes: list[EnforcementNote]) -> None:
    roles = {r.get("id"): r for r in data.get("roles", []) if isinstance(r, dict)}
    needs: dict[str, set[str]] = {rid: set() for rid in roles}
    # every coordination pattern exchanges messages between its members
    for coord in data.get("coordination", []):
        members = _role_names(
            {k: v for k, v in coord.items() if k not in ("id", "pattern", "basis")}
        )
        for rid in members & set(roles):
            needs[rid].add("comms")
    used: set[str] = set()
    weapon_ids: list[str] = []
    for beh in data.get("behaviors", []):
        for action in beh.get("actions", []):
            definition = catalog.action(str(action.get("command", "")))
            if definition is None:
                continue
            used |= {_NEED_TO_DEP[n] for n in definition.requires if n in _NEED_TO_DEP}
            weapon = (action.get("arguments") or {}).get("weapon")
            if isinstance(weapon, str):
                weapon_ids.append(weapon)
    _operand_kinds(
        [data.get("conditions", []), data.get("behaviors", []), data.get("roles", [])],
        catalog,
        used,
    )
    for rid, role in roles.items():
        defaults = DEFAULT_COMPONENTS.get(str(role.get("capability_class")))
        if not defaults:
            continue
        deps = role.setdefault("dependencies", {})
        for dep in sorted(needs[rid] | used):
            if deps.get(dep):
                continue
            cid, capability = defaults[dep]
            if dep == "weapons" and weapon_ids:
                cid = weapon_ids[0]
            deps[dep] = [{"id": cid, "capability": capability, "required": False}]
            _note(
                notes,
                f"roles[{rid}].dependencies.{dep}",
                f"模型使用了{dep}但角色未声明，按默认补充 {cid}（{capability}，简化处理）",
            )


def _drop_generated_copies(data: dict[str, Any], notes: list[EnforcementNote]) -> None:
    """IDs '<coordination>__*' belong to the pattern expansion; a copy the model wrote
    itself would duplicate the generated element."""
    prefixes = tuple(f"{c.get('id')}__" for c in data.get("coordination", []) if c.get("id"))
    if not prefixes:
        return
    for key in ("conditions", "behaviors", "messages", "trees"):
        items = data.get(key) or []
        kept = [i for i in items if not str(i.get("id", "")).startswith(prefixes)]
        if len(kept) != len(items):
            dropped = [i.get("id") for i in items if i not in kept]
            data[key] = kept
            _note(notes, key, f"删除模型自行定义的协同生成元素 {dropped}（由协同模式展开）")


def _fix_formations_and_traces(data: dict[str, Any], notes: list[EnforcementNote]) -> None:
    """Drop formations without slots that nothing references (a "formation" the model
    invented for a coordination, e.g. two bounding groups), and scenario ``traces_to``
    entries naming nothing in the draft (often IDs copied from the prompt example)."""
    import json

    formations = data.get("formations") or []
    for form in list(formations):
        if form.get("slots"):
            continue
        fid = form.get("id")
        others = {k: v for k, v in data.items() if k not in ("formations", "scenarios")}
        if f'"{fid}"' in json.dumps(others, ensure_ascii=False):
            continue
        formations.remove(form)
        _note(notes, f"formations.{fid}", "编队没有站位且未被引用，已删除（协同模式负责相对位置）")
    known = {f.get("id") for f in formations} | {c.get("id") for c in data.get("coordination", [])}
    known |= {
        tr.get("transition_id")
        for role in data.get("roles", [])
        for st in (role.get("state_machine") or {}).get("states", [])
        for tr in st.get("transitions", [])
    }
    for index, scenario in enumerate(data.get("scenarios", [])):
        traces = scenario.get("traces_to")
        if not isinstance(traces, list):
            continue
        kept = [t for t in traces if t in known or "__" in str(t)]
        if len(kept) != len(traces):
            scenario["traces_to"] = kept
            dropped = [t for t in traces if t not in kept]
            _note(notes, f"scenarios[{index}].traces_to", f"删除不存在的追溯对象 {dropped}")


def autofix(data: dict[str, Any], catalog: Catalog) -> list[EnforcementNote]:
    notes: list[EnforcementNote] = []
    _fix_text(data, "", notes)
    _drop_generated_copies(data, notes)
    _fix_dependencies(data, catalog, notes)
    _fix_formations_and_traces(data, notes)
    fixer = _ExpressionFixer(data, catalog, notes)
    for container, key, path in _expression_slots(data):
        container[key] = fixer.fix(container[key], path)
    _fix_actions(data, catalog, notes)
    _fix_blackboards(data, notes)
    _fix_tests(data, catalog, notes)
    return notes
