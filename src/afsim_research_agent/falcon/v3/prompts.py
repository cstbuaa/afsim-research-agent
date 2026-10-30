"""Prompts of the v3 tactic sheet (Chinese): structure, control, verification and block
repair (design falcon-v3-composition-and-gaps §3, §8).

Examples use a made-up capability set (``nav`` / ``sense``), never a real one.
"""

from __future__ import annotations

import json
from typing import Any

PROMPT_VERSION = "falcon-v3-2026.10.30"

RULES = """你在为作战仿真编写战术行为模型。模型分七层，你只写上三层：任务状态机、状态树、子树；
动作、黑板、仲裁、能力层由程序从能力集引用、推导和校验。你写的是"战术稿"：若干带标签的 Markdown 代码块。

组合的优先顺序（取第一个可行的）：
1. 整个角色的任务能由某个战术集任务承担 → 在 model 中引用它（若本次允许）；
2. 某状态要做的事能由战术集的子树 / 状态树完成 → `tactic("id")`；
3. 用已有动作、条件和战术集子树组合（不超过约 12 个节点）→ 在本稿写子树函数；
4. 需要可复用、逻辑复杂的子树，但能力集没有 → 在 yaml gaps 登记 kind: subtree 缺口，树里写 `gap("id")`；
5. 至少需要一个能力集没有的动作 → 登记 kind: action 缺口，树里写 `gap("id", 参数=值)`。
不能用一串相近的动作去冒充缺失的能力；"看起来相近"但参数、黑板或资源对不上的条目不要引用。
只用目录中出现的动作 ID、战术集 ID 和黑板字段，不要编造。"""

RULES_SUBTREES = """你在为作战仿真编写战术行为模型。模型分七层，你只写上三层：任务状态机、状态树、子树；
动作、黑板、仲裁、能力层由程序从能力集引用、推导和校验。你写的是"战术稿"：若干带标签的 Markdown 代码块。

本次的写法：
1. 任务状态机和每个状态的树都由你自己写。能力集里的战术集任务和状态树只作范例：
   参照它们如何划分阶段、写触发、组合子树，但不能引用它们；
2. 树中要做的事能由战术集子树完成 → `tactic("id")` 引用子树；
3. 子树能力不够（缺少某项工作、参数或时序不合需求）→ 仿照已有子树的写法，用动作、条件和其他子树
   在本稿新建子树（小写函数）；
4. 需要能力集没有的动作 → 在 yaml gaps 登记 kind: action 缺口，树里写 `gap("id", 参数=值)`；
   逻辑复杂、值得复用但写不出来的子树 → 登记 kind: subtree 缺口。
不能用一串相近的动作去冒充缺失的能力；"看起来相近"但参数、黑板或资源对不上的条目不要引用。
只用目录中出现的动作 ID、战术集 ID 和黑板字段，不要编造。"""


def rules(policy: str) -> str:
    return RULES_SUBTREES if policy == "subtrees" else RULES


MODEL_GUIDE = """```yaml model``` 块：
- model: {display_name, intent}
- 条件不能是常量 True / False；外部请求一律写成 external 事件，不写成条件。
- slices: [本任务要用的能力集切片 ID]（从下面的切片索引中选；下一步只展示这些切片的详细内容，
  条件只能读这些切片的黑板字段：要判断目标、传感器等状态，就要选上对应的切片）
- roles: {角色ID: {capability_class, count, description}}；角色 ID 用小写英文。
  若整体引用战术集任务：在角色下写 mission: {tactic: <战术集任务 ID>, role: <可选>, params: {…}}，此时不写该角色的状态图，
  也不要为它的转换写条件或事件（任务自带）；只有需求与任务的某个状态明显不同时，才在下一步替换该状态的树。
- parameters: {名称: {value: "10 s" | 数 | 字符串, description, basis: explicit|inferred|unknown|user_specified, rationale}}
  数值写成"数 单位"（m、km、m/s、km/h、s、min、deg）。想定提供的参数照抄给出的名称和类型，value 写成名称本身。
- blackboard: {mission.<名称>: {type: bool|int|double|string, default}}（只声明能力集没有的新字段，一般不需要）
- events: {事件ID: {source: external, description}} 或 {事件ID: {after: "30 s", description}}（进入状态后计时）；
  每个事件都要在状态图中作为某条转换的触发（直接写事件 ID），外部请求不要再写成条件
- assumptions: [{statement}]（模型依赖、需要人确认的前提）
- implements: {条款ID: [角色.状态 或 子树名]}（每条需求条款都要出现）

```mermaid <角色ID>``` 块（每个不引用战术集任务的角色一张 stateDiagram-v2）：
- 状态名用大写英文（如 SEARCH、TRACK）；`[*] --> 初始状态`；`A --> B : 触发`，同一状态的多条转换先写的优先；
- 触发写事件 ID、条件名（下一步在 python conditions 中定义）或简单表达式（`not target_seen`）；
- 任何阶段都要响应的情况写 `* --> 目标 : 触发`（全局转换，优先于各状态自己的转换）；
- 必须有安全收尾：含 SHUTDOWN 状态（或在 model 中说明的安全状态），每个状态都能到达它；
  结束状态写 `X --> [*]`，结束状态不执行树。"""

CONTROL_GUIDE = """```python conditions``` 块：每行 `条件名 = 表达式`：
- 黑板字段直接写路径：`sense.target.valid`；`and` / `or` / `not`；比较 `==  !=  <  <=  >  >=`；
- 数量写 `500 * m` 或 `"500 m"`；参数和其他条件直接写名字；
- 计时：`time_in_state() >= 10 * s`；`elapsed_since(某时刻字段) >= 5 * s`。

```python <角色ID>``` 块（每个角色一个）：用 Python 函数写树，只会被解析、不会执行。
- 函数名与状态名相同（大写）的是该状态的树；小写函数名是子树，可被调用 `name()`；
- 动作：`切片.动作(参数=值)`，参数按契约填写（数量写 `30 * s` 或 `"30 s"`，参数名直接写）；
- 战术集子树 / 状态树：`tactic("id")`；缺口占位：`gap("id", 参数=值)`；
- 条件：`assert 条件名或表达式`（不成立时该分支失败）；
- 函数体里依次写多句 = 带记忆的顺序（逐个完成，运行中的子节点下次继续）；
- 控制节点：`with parallel("all"):`（"one" 一个成功即成功，或写数字），`with sequence():`、`with selector():`（带记忆），
  `with reactive_sequence():`、`with reactive_selector():`（每次从头检查），`with repeat(until_done=True):`（完成一次后保持成功，
  避免重复下达；也可 count=次数、duration=时长），`with inverter():`、`with succeeder():`，`with keep_running():`（下面的节点失败时报告"运行中"，
  如没有目标时继续搜索，而不是让整棵树失败）；
  `if 条件: …`、`else: …`；`try: …` `except: …`（前者失败时执行后者）；
- 同一时刻要持续做的几件事（边走边搜、边跟踪边保持队形）放进 parallel；并行分支不能同时占用同一独占资源；
- 一直运行的动作（如保持位置）放在顺序节点前面会挡住后面的节点，此时用 parallel；
- 树里不能给黑板赋值。
- 战术集已有某阶段的状态树时，优先让状态函数只写一句 `tactic("…")` 直接引用它；
  引用的战术集树已包含的工作（如搜索）不要在并行分支里再写一遍，否则会争用同一独占资源；
- 状态图的触发若与战术集任务的触发含义相同，照"触发条件的含义"写（如计时用 after 事件），不要猜测字段取值；
- 状态树里不要用 assert 重复写进入该状态的转换条件（进入时它已成立，之后的变化由状态图的转换处理）；
  assert 只用于树内部的分支判断，否则条件一变树就整体失败、什么也不做；
- 条件读描述战场状态的字段；请求编号、执行状态等接口字段不能用；带取值列表的字段只能与列出的取值比较。

```yaml gaps``` 块（只有需要缺口时才写）：
gaps:
  <缺口ID>:
    kind: subtree | action
    description: 要做什么
    reason: 为什么前面几级组合方式不行
    clauses: [条款ID]
    capability_class: ground_vehicle
    params: {名称: {type: double, unit: m, required: true, range: [50, 500]}}
    blackboard: {reads: [字段], writes: [字段]}
    resources: [资源]
    result: {running: …, success: …, failure: {codes: [代码], meaning: …}, terminates: true, max_duration_s: 180}
    stub: {success_after_s: 30, result: success}       # 补齐前仿真中的理想化执行
    acceptance: [{id: …, given: {…}, ticks: 50, expect: success}]
  能力缺口（kind: action）另写 lifecycle、completion、failure_codes、observable；只描述需要什么，不写实现。"""

VERIFICATION_GUIDE = """```yaml verification``` 块：
tests:            # L2 单步测试：给定状态、黑板、事件，执行一次更新后检查
  - id: 英文ID
    given: {role: 角色, state: 状态, time_in_state_s: 0, blackboard: {字段: 值}, events: [事件ID],
            actions: {动作ID: success|failure|running}}   # actions 指定动作本次的结果，默认 success
    expect: {next_state: 状态}   # 一般只写 next_state；tree_status 只在确实要检查树的结果时写
scenarios:        # L3 二维仿真：平面坐标（米），x 向东、y 向北
  - id: 英文ID
    environment: {origin_deg: [纬度, 经度], duration_s: 200, step_s: 1, routes: {路线名: [[x, y], …]}}
    vehicles: {角色: [{position_m: [x, y], heading_deg: 90, max_speed_mps: 10}]}
    hostiles: [{id: h1, position_m: [x, y], velocity_mps: [vx, vy], appear_s: 0, remove_s: null}]
    events: [{at_s: 150, event: 事件ID}]
    faults: [{at_s: 60, role: 角色, sensor_operational: false}]
    expect: [{id: 英文ID, role: 角色, state_sequence: [状态, …]} 或 {reaches: 状态, within_s: 60} 或 {never: 状态}]
- 每个状态至少一条测试；每条转换至少被一条测试或想定覆盖；想定覆盖主流程和至少一个例外（如目标丢失、取消）。
- 路线的键用路线参数的 value。"""

EXAMPLE = """示例（虚构能力集）：
```yaml model
model: {display_name: 单车侦察, intent: 前出到观察点搜索目标，发现后报告并保持观察}
slices: [nav, sense]
roles:
  scout: {capability_class: ground_vehicle, count: 1, description: 侦察车}
parameters:
  obs_lat: {value: "30.01 deg", basis: inferred, rationale: 观察点}
  obs_lon: {value: "120.0 deg", basis: inferred, rationale: 观察点}
  lost_wait: {value: "20 s", basis: unknown, rationale: 等待重新发现的时限}
events:
  cancelled: {source: external, description: 上级取消任务}
  lost_timeout: {after: "20 s"}
implements: {rq_01: [scout.MOVE], rq_02: [scout.OBSERVE]}
```

```mermaid scout
stateDiagram-v2
  [*] --> MOVE
  MOVE --> OBSERVE : target_seen
  OBSERVE --> MOVE : lost_timeout
  SHUTDOWN --> DONE : stopped
  * --> SHUTDOWN : cancelled
  DONE --> [*]
```

```python conditions
target_seen = sense.target.valid
stopped = self.speed_mps <= 0.1 and time_in_state() >= 2 * s
```

```python scout
def MOVE():
    with parallel("all"):
        with repeat(until_done=True):
            nav.goto(lat_deg=obs_lat, lon_deg=obs_lon)
        tactic("sense.search")

def OBSERVE():
    with parallel("one"):
        nav.hold()
        tactic("sense.track")

def SHUTDOWN():
    nav.stop()
```"""


def _clauses(clauses: list[dict[str, Any]]) -> str:
    return "\n".join(f"- {c['id']}：{c['text']}" for c in clauses) or "（无）"


def _hints(
    roles: dict[str, Any], scenario: dict[str, Any], allow_mission_ref: bool, policy: str = "all"
) -> str:
    lines = []
    if roles:
        lines.append("角色（照用这些角色 ID）：" + json.dumps(roles, ensure_ascii=False))
    if scenario:
        shown = {
            k: {"type": v.get("type", "string"), "description": v.get("description", "")}
            for k, v in scenario.items()
        }
        lines.append(
            "想定提供的参数（在 parameters 中照抄名称与类型，value 写成名称本身）："
            + json.dumps(shown, ensure_ascii=False)
        )
    if policy == "subtrees":
        lines.append(
            "本次不能引用战术集任务和状态树：每个角色都要写自己的状态图和状态树，可引用战术集子树。"
        )
    else:
        lines.append(
            "本次允许整体引用战术集任务。"
            if allow_mission_ref
            else "本次不允许整体引用战术集任务：每个角色都要写自己的状态图（状态树仍可引用战术集）。"
        )
    return "\n".join(lines)


def structure_prompt(
    requirement: str,
    clauses: list[dict[str, Any]],
    index: str,
    roles: dict[str, Any],
    scenario: dict[str, Any],
    allow_mission_ref: bool,
    tactic_brief: str = "",
    state_names: dict[str, list[str]] | None = None,
    scenario_events: dict[str, str] | None = None,
    policy: str = "all",
) -> str:
    names = ""
    if scenario_events:
        names += (
            "\n想定会发出的外部事件（需求中的外部请求照用这些事件 ID，在 events 中声明为 external）："
            + "；".join(f"{k}（{v}）" for k, v in scenario_events.items())
        )
    if state_names:
        names += "\n状态命名（自己写状态图时使用这些名称，可按需求增减）：" + "；".join(
            f"{r}: {', '.join(v)}" for r, v in state_names.items()
        )
    return f"""{rules(policy)}

## 需求
{requirement}

## 需求条款
{_clauses(clauses)}
{tactic_brief}
## 约定
{_hints(roles, scenario, allow_mission_ref, policy)}{names}

## 能力集切片索引
{index}

## 本步只写 yaml model 和各角色的 mermaid 状态图
{MODEL_GUIDE}

{EXAMPLE}

只输出代码块，不要解释。"""


def control_prompt(
    requirement: str,
    clauses: list[dict[str, Any]],
    sheet: str,
    details: str,
    roles_needing_trees: list[str],
    states: dict[str, list[str]],
    pending: list[str] | None = None,
    mission_refs: list[str] | None = None,
    policy: str = "all",
) -> str:
    need = "；".join(f"{r}: {', '.join(states.get(r, []))}" for r in roles_needing_trees)
    if pending:
        need += (
            "\n状态图用到、还没有定义的触发（每个都要在 python conditions 中定义）："
            + ", ".join(pending)
        )
    if mission_refs:
        need += (
            "\n角色 " + ", ".join(mission_refs) + " 引用了战术集任务：任务的转换与触发已自带，"
            "不要为它们写条件；只在需要替换某状态的树时写该状态函数"
        )
    if policy == "subtrees":
        need += "\n每个非结束状态都要写状态函数（不能引用战术集状态树）；子树可引用或仿照新建。"
    return f"""{rules(policy)}

## 需求
{requirement}

## 需求条款
{_clauses(clauses)}

## 已写好的部分
{sheet}

## 能力集详细内容（只用这里的 ID 和字段）
{details}

## 本步写 python conditions、每个角色的 python 树块，以及需要时的 yaml gaps
需要状态树的状态（结束状态不写）：{need or "（无；引用战术集任务的角色可只写要替换的状态）"}
{CONTROL_GUIDE}

{EXAMPLE}

只输出这几个代码块，不要重复已写好的块，不要解释。"""


def verification_prompt(requirement: str, sheet: str, summary: str) -> str:
    return f"""你在为战术行为模型编写验证用例。

## 需求
{requirement}

## 战术稿
{sheet}

## 编译结果摘要
{summary}

## 本步只写 yaml verification
{VERIFICATION_GUIDE}

只输出这一个代码块，不要解释。"""


def repair_prompt(label: str, numbered: str, problems: list[str], guide: str, context: str) -> str:
    listed = "\n".join(problems)
    return f"""下面这个战术稿代码块有问题，请修改后输出完整的新代码块（同样的标签 ```{label}），只输出这一个代码块。

## 问题
{listed}

## 当前内容（行号是整份战术稿的行号）
{numbered}

## 写法说明
{guide}

## 相关上下文
{context}"""
