"""Reaction probes: situations every state of a draft should answer.

Single-step tests check what the model wrote against what the model's own tests expect;
they cannot notice a state the model forgot to give an exit. Three online runs in a row
(py7–py9) left movement states that ignore a detected enemy, and py9 left a reporting
state that waits for an acknowledgement forever. These probes put each state into a
fixed situation with the L2 runtime and report the states that do not react:

* **hostile** — an enemy is detected (count 1, 800 m, target valid). A state reacts when
  the enemy changes its next state compared with the same state left alone. Checked for
  the states whose tree moves along a route, and only when some other state of the same
  role does react (so a task without contact handling is not flagged).
* **no acknowledgement** — a state whose tree waits for a report_and_wait acknowledgement
  is held for 10 minutes with no message arriving. ``ack_timeout`` only makes the wait
  action fail; the state still needs an exit that does not depend on the message.
"""

from __future__ import annotations

from typing import Any

from afsim_research_agent.ir.l2 import run_test

MOVE_COMMANDS = {"follow_route", "move_to_position", "move_to_waypoint"}
HOSTILE = {
    "nearest_hostile_range": {"value": 800, "unit": "m"},
    "hostile_count": 1,
    "current_target_valid": True,
    "current_target_range": {"value": 800, "unit": "m"},
}


def _tree_commands(ir: dict[str, Any], tree_id: str | None) -> set[str]:
    """Action commands and generated behaviors a state's own tree uses (no subtrees)."""
    tree = next((t for t in ir["trees"] if t["id"] == tree_id), None)
    if tree is None:
        return set()
    behaviors = {b["id"]: b for b in ir["behaviors"]}
    found: set[str] = set()
    stack = [tree["root"]]
    while stack:
        node = stack.pop()
        stack += node.get("children") or []
        if node.get("type") == "behavior":
            found.add(node["behavior"])
            for action in behaviors.get(node["behavior"], {}).get("actions") or []:
                found.add(action.get("command", ""))
    return found


def _tick_result(
    ir: dict[str, Any], role: str, state: str, given: dict[str, Any]
) -> dict[str, Any] | None:
    test = {"id": "probe", "given": {"role": role, "instance": 0, "state": state, **given}}
    test["expect"] = {}
    try:
        return run_test(ir, test).result
    except (KeyError, TypeError, ValueError):
        return None


def _next_state(ir: dict[str, Any], role: str, state: str, given: dict[str, Any]) -> str | None:
    test = {"id": "probe", "given": {"role": role, "instance": 0, "state": state, **given}}
    test["expect"] = {}
    try:
        outcome = run_test(ir, test)
    except (KeyError, TypeError, ValueError):
        return None
    return outcome.result.get("next_state")


def _state_index(ir: dict[str, Any], role_id: str, state_id: str) -> str:
    for r_index, role in enumerate(ir["roles"]):
        if role["id"] != role_id:
            continue
        states = (role.get("state_machine") or {}).get("states") or []
        for s_index, state in enumerate(states):
            if state["id"] == state_id:
                return f"roles[{r_index}].state_machine.states[{s_index}]"
    return f"roles.{role_id}"


def oscillation_probes(ir: dict[str, Any], max_ticks: int = 6) -> list[tuple[str, str]]:
    """States that bounce back while nothing changes.

    Each L2 test's situation is held constant (messages only on the first tick, the time
    in state restarting at every entry) and the role is ticked from the test's state
    until it settles. Coming back to a state already visited means an exit condition of
    a later state still holds in the situation that led into it: py9's fix loop wrote
    ``* --> reporting : enemy_detected``, which also expanded to bounding overwatch, so a
    platoon that had reported, got the acknowledgement and started bounding went straight
    back to reporting while the enemy was still there."""
    problems: list[tuple[str, str]] = []
    seen: set[tuple[str, frozenset[str]]] = set()
    whens = {
        tr["transition_id"]: tr.get("when") or {}
        for role in ir["roles"]
        for st in (role.get("state_machine") or {}).get("states") or []
        for tr in st.get("transitions") or []
    }

    def describe(tid: str | None) -> str:
        when = whens.get(tid or "", {})
        name = None
        if isinstance(when, dict):
            name = when.get("condition_ref") or (
                when.get("id") if when.get("op") == "condition_ref" else None
            )
        return f"条件 {name}" if name else f"转换 {tid}"

    for test in ir.get("tests") or []:
        given = dict(test.get("given") or {})
        role, state = given.pop("role", None), given.pop("state", None)
        if not role or not state:
            continue
        given.pop("instance", None)
        path = [state]
        fired: list[str | None] = []
        current = state
        for tick in range(max_ticks):
            situation = dict(given)
            if tick:
                situation.pop("messages", None)
                situation.pop("elapsed", None)
            result = _tick_result(ir, role, current, situation)
            nxt = result.get("next_state") if result else None
            if nxt is None or nxt == current:
                break
            fired.append(result.get("fired_transition"))
            if nxt in path:
                start = path.index(nxt)
                cycle = path[start:] + [nxt]
                steps = "；".join(
                    f"{a} → {b}（{describe(t)}）"
                    for a, b, t in zip(cycle, cycle[1:], fired[start:])
                )
                key = (role, frozenset(cycle))
                if key not in seen:
                    seen.add(key)
                    problems.append(
                        (
                            _state_index(ir, role, current),
                            f"{role} 在情况不变时来回切换：{' → '.join(cycle)}"
                            f"（从测试 {test['id']} 的情况出发，消息只在第一拍送达）。"
                            f"各步：{steps}。这些条件在同一情况下同时成立。"
                            "改法：让它们互斥（给其中一条加上区分条件），或者调整优先级；"
                            "如果需要决定这种情况下该处于哪个状态（例如受损时是继续交替跃进还是撤出），"
                            "这是战术意图问题，用 ask_human 提问，不要靠删改测试绕开。"
                            f"注意 `* -->` 全局例外也会展开到 {current}",
                        )
                    )
                break
            path.append(nxt)
            current = nxt
    return problems


def reaction_probes(ir: dict[str, Any]) -> list[tuple[str, str]]:
    """(IR path of the state, message) for states that do not react to a probe."""
    problems: list[tuple[str, str]] = []
    waits = {
        f"{c['id']}__await_ack": c
        for c in ir.get("coordination") or []
        if c.get("pattern") == "report_and_wait"
    }
    for r_index, role in enumerate(ir["roles"]):
        machine = role.get("state_machine")
        if not machine:
            continue
        states = machine["states"]
        reacting: dict[str, str] = {}
        moving: list[tuple[int, str]] = []
        for s_index, state in enumerate(states):
            if state.get("terminal"):
                continue
            commands = _tree_commands(ir, state.get("tree"))
            # a reaction is what the enemy changes: compare with the same state left alone
            baseline = _next_state(ir, role["id"], state["id"], {})
            nxt = _next_state(ir, role["id"], state["id"], {"tracks": dict(HOSTILE)})
            if nxt and nxt != baseline and nxt != state["id"]:
                reacting[state["id"]] = nxt
            elif commands & MOVE_COMMANDS and any(
                not t.get("fallback") for t in state["transitions"]
            ):
                # a state with no exit of its own (e.g. return home) is the end of the task
                moving.append((s_index, state["id"]))
            waiting = [waits[w] for w in commands if w in waits]
            if waiting:
                quiet = _next_state(
                    ir,
                    role["id"],
                    state["id"],
                    {"elapsed": {"state": {"value": 10, "unit": "min"}}},
                )
                if quiet == state["id"]:
                    coord = waiting[0]
                    problems.append(
                        (
                            f"roles[{r_index}].state_machine.states[{s_index}]",
                            f"{role['id']} 在 {state['id']} 等待 {coord['id']} 的确认，但离开它的条件都依赖收到消息："
                            f"通信中断时 10 分钟后仍停在这里。ack_timeout 只让等待动作失败，不会让状态离开；"
                            f"加一条不依赖消息的出口，例如把确认条件写成 "
                            f"`{coord['id']}.ack_received or time_in_state() >= <超时参数>`",
                        )
                    )
        if reacting and moving:
            example = next(iter(reacting.items()))
            for s_index, state_id in moving:
                problems.append(
                    (
                        f"roles[{r_index}].state_machine.states[{s_index}]",
                        f"{role['id']} 在 {state_id} 沿路线行进时发现敌人（1 个目标、800 m、目标有效）没有任何转换，"
                        f"而在 {example[0]} 会转入 {example[1]}。任何行进阶段都要能应对接敌："
                        f"给它加一条 `{state_id} --> {example[1]} : <发现敌情的条件>`，"
                        "条件只写发现敌情本身，不要再与受损、距离等其他条件相与。"
                        f"不要轻易写成 `* --> {example[1]}`：它会展开到所有非终止状态，"
                        "包括正在处理接敌的状态（如交替跃进），使它们在敌人仍在时跳回去",
                    )
                )
    return problems
