"""Prompts of the v3 tactic sheet (Chinese): structure, control, verification and block
repair (design falcon-v3-composition-and-gaps §3, §8).

Examples use a made-up capability set (``nav`` / ``sense``), never a real one.
"""

from __future__ import annotations

import json
from typing import Any

PROMPT_VERSION = "falcon-v3-2026.11.04a"

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


COARSE_STRUCTURE = """## 状态划分（本次要求粗粒度）
- 一个状态是一个阶段（等待、执行、判断、保持、收尾），不是一个动作。能由一棵树依次做完的连续步骤
  （如 校验 → 选择 → 准备 → 执行 → 观察结果）放进同一个状态的树里依次调用，不要每一步设一个状态；
- 只有这些情况才另设状态：要等外部事件、计时或其他车辆的消息；行为不同（执行 / 保持 / 收尾）；
  要按结果走不同去向（判断）；周期性重复的起点；
- 每个角色一般 4–7 个状态（含 SHUTDOWN 和结束状态）。战术集任务把阶段切得更细，只参照它的阶段先后
  和触发含义，不要照搬它的状态；
- 阶段的出口用树事件：在 model events 声明 `<名字>: {source: tree, result: success}`（树做完）和
  `<名字>: {source: tree, result: failure}`（树失败，如校验不通过），状态图用它们作转换；
- 判断（如是否再次攻击）单设一个状态，树里只放判断动作：动作成功 / 失败分别用 success / failure 树事件
  转往两个去向；要重复执行时转回执行状态，不要用上一轮留下的字段（开火次数、毁伤时刻、分配数、
  任务有效）做出口；
- 字段条件只用于等待类状态的出口（如"收到有效任务""发现目标"）和全局转换。
例（虚构能力集）：WAITING --task_ok--> WORK（树：校验 → 选择 → 执行 → 观察）；WORK --work_done--> REVIEW；
WORK --work_failed--> HOLD；REVIEW（树：判断是否再做）--again--> WORK；REVIEW --enough--> HOLD；
HOLD --task_ok--> WORK；* --> SHUTDOWN : cancelled。"""

COARSE_CONTROL = """## 状态树的写法（本次要求粗粒度）
- 执行类状态的树把这一阶段的步骤按顺序写在一个函数里（依次调用战术集子树 / 动作），这样树做完 =
  阶段完成，状态图用树事件转出；
- 判断类状态的树只放判断动作（成功 = 走一路，失败 = 走另一路），不要再包 selector 把失败变成成功；
- 等待类状态的树只做等待期间要持续做的事（如保持、感知），出口用字段条件。"""


def rules(policy: str) -> str:
    return RULES_SUBTREES if policy == "subtrees" else RULES


MODEL_GUIDE = """```yaml model``` 块：
- model: {display_name, intent}
- 条件不能是常量 True / False；外部请求一律写成 external 事件，不写成条件。
- slices: [本任务要用的能力集切片 ID]（从下面的切片索引中选；下一步只展示这些切片的详细内容，
  条件只能读这些切片的黑板字段：要判断目标、传感器等状态，就要选上对应的切片）
- roles: {角色ID: {capability_class, count, description}}；角色 ID 用小写英文。需求写了几辆我方车辆
  （“三辆车组成纵队”“长车带两辆僚车”“排长车和两辆成员车”）就写够几辆：分出长车 / 僚车（成员）角色，
  同类车用 count；编队、协同、目标共享类能力要多辆车才能运行。
  若整体引用战术集任务：在角色下写 mission: {tactic: <战术集任务 ID>, role: <可选>, params: {…}}，此时不写该角色的状态图，
  也不要为它的转换写条件或事件（任务自带）；只有需求与任务的某个状态明显不同时，才在下一步替换该状态的树。
- parameters: {名称: {value: "10 s" | 数 | 字符串, description, basis: explicit|inferred|unknown|user_specified, rationale}}
  数值写成"数 单位"（m、km、m/s、km/h、s、min、deg）。想定提供的参数照抄给出的名称和类型，value 写成名称本身。
- blackboard: {mission.<名称>: {type: bool|int|double|string, default}}（只声明能力集没有的新字段，一般不需要）
- events: {事件ID: {source: external, description}}、{事件ID: {after: "30 s", description}}（进入状态后计时）
  或 {事件ID: {source: tree, result: success}}（本状态的树完成时，如关机清理做完；不要用字段去猜"做完了没有"）；
  每个事件都要在状态图中作为某条转换的触发（直接写事件 ID），外部请求不要再写成条件
- assumptions: [{statement}]（模型依赖、需要人确认的前提）
- implements: {条款ID: [角色.状态 或 子树名]}（每条需求条款都要出现）

```mermaid <角色ID>``` 块（每个不引用战术集任务的角色一张 stateDiagram-v2）：
- 只写平铺的状态，不要写复合状态（`state X { … }`）；状态名用大写英文（如 SEARCH、TRACK）；`[*] --> 初始状态`；`A --> B : 触发`，同一状态的多条转换先写的优先；
- 触发写事件 ID、条件名（下一步在 python conditions 中定义）或简单表达式（`not target_seen`）；
- 任何阶段都要响应的情况写 `* --> 目标 : 触发`（全局转换，优先于各状态自己的转换）；
- 必须有安全收尾：含 SHUTDOWN 状态（或在 model 中说明的安全状态），每个状态都能到达它；
  结束状态写 `X --> [*]`，结束状态不执行树。"""

CONTROL_GUIDE = """```python conditions``` 块：每行 `条件名 = 表达式`：
- 黑板字段直接写路径：`sense.target.valid`；`and` / `or` / `not`；比较 `==  !=  <  <=  >  >=`；
- 数量写 `500 * m` 或 `"500 m"`；参数和其他条件直接写名字；
- 计时：`time_in_state() >= 10 * s`；`elapsed_since(某时刻字段) >= 5 * s`。
- 一个状态的出口条件读的字段，要由这个状态（或进入它之前的状态）的树里的动作写入：等融合航迹就在本状态
  运行融合子树，等采用共享目标就在本状态运行采用动作；否则状态永远离不开。
- “做完 / 到达 / 校验通过”这类出口：读本状态动作执行之后才会变化的字段，或用本状态的树事件；
  不要读进入时就已经成立的初值（如 `not engagement.task.valid` 在进入校验状态时就为真）。
- 需求约束要求事件之后做的动作（如“收到结束请求后停火”要求 engagement.cease_fire），要出现在这个事件
  进入的状态的树里（直接调用或用含它的子树），否则约束无法满足、也无法被想定检查。

```python <角色ID>``` 块（每个角色一个）：用 Python 函数写树，只会被解析、不会执行。
- 函数名与状态名相同（大写）的是该状态的树；小写函数名是子树，可被调用 `name()`；
- 动作：`切片.动作(参数=值)`，参数按契约填写（数量写 `30 * s` 或 `"30 s"`，参数名直接写）；
- 战术集子树 / 状态树：`tactic("id")`，有公开参数时写 `tactic("id", 参数=值)`；缺口占位：`gap("id", 参数=值)`；
- 可调参数：动作和子树的参数标有类别。[设计] 参数（队形、间距、保持距离、速度、时效、开火上限等）
  由你按战术和资料给值：在 model 的 parameters 中定义（写清依据），再在调用处写 `参数=参数名`；
  不写则用默认值。[平台] 参数是车辆性能，不要改；[想定] 参数（路线、坐标）由想定接口给出（point= / route=）。
  直接引用战术集树（状态函数只写一句 `tactic("…")`）时，它的公开参数自动取 parameters 中同名参数的值；
- 条件：`assert 条件名或表达式`（不成立时该分支失败）；
- 函数体里依次写多句 = 带记忆的顺序（逐个完成，运行中的子节点下次继续）；
- 控制节点：`with parallel("all"):`（"one" 一个成功即成功，或写数字），`with sequence():`、`with selector():`（带记忆），
  `with reactive_sequence():`、`with reactive_selector():`（每次从头检查），`with repeat(until_done=True):`（完成一次后保持成功，
  避免重复下达；也可 count=次数、duration=时长），`with inverter():`、`with succeeder():`，`with keep_running():`（下面的节点失败时报告"运行中"，
  如没有目标时继续搜索，而不是让整棵树失败）；
  `if 条件: …`、`else: …`；`try: …` `except: …`（前者失败时执行后者）；
- 门控：`with after(10 * s):`（进入本状态满 10 s 后才执行下面的节点，之前保持运行），
  `with when(条件):`（条件成立时才执行，不成立时等待），`with until(条件):`（执行下面的节点直到条件成立，
  然后成功）。"过一段时间再做""满足条件才做""做到条件成立为止"都用它们写，不要自己用 selector 和 assert 拼；
- 同一时刻要持续做的几件事（边走边搜、边跟踪边保持队形）放进 parallel；并行分支不能同时占用同一独占资源；
- 一直运行的动作（如保持位置）放在顺序节点前面会挡住后面的节点，此时用 parallel；
- 树里不能给黑板赋值。
- 战术集已有某阶段的状态树时，优先让状态函数只写一句 `tactic("…")` 直接引用它；
  引用的战术集树已包含的工作（如搜索）不要在并行分支里再写一遍，否则会争用同一独占资源；
- 状态图的触发若与战术集任务的触发含义相同，照"触发条件的含义"写（如计时用 after 事件），不要猜测字段取值；
- 状态树里不要用 assert 重复写进入该状态的转换条件（进入时它已成立，之后的变化由状态图的转换处理）；
  assert 只用于树内部的分支判断，否则条件一变树就整体失败、什么也不做；
- 条件读描述战场状态的字段；请求编号、执行状态等接口字段不能用；带取值列表的字段只能与列出的取值比较；
- 写条件前看字段说明：由哪个动作写入、何时更新；"未实现"的字段不能用；某状态的转换条件只能读该状态的树
  会更新的字段（例如横队站位误差只由横队展开步骤写入，纵队状态里它不会变化）；动作或子树"成功"的含义
  以"结果"为准（例如保持距离步骤每拍成功只表示指令已下达，不表示已进入距离带）；
- "某阶段的工作做完了"（如集结完成、收尾完成）：有字段表达到位程度就读字段；否则用树事件——在 model 的
  events 中声明 `名字: {source: tree, result: success}`，状态图里用它作转换（本状态的树成功时触发）。
  树事件只在整棵树结束时触发：沿路线行驶（follow_route）、移动到点、停车这类动作会自行完成，
  "走完路线/到达位置/停稳"就用树事件，不要写 range_to_target_m 与 arrival_tolerance_m 的比较；
  树里有一直运行的分支（保持位置、跟踪、repeat(until_done=True)）时树不会结束，
  "发现目标""目标丢失"这类时刻要用字段条件（如 perception.selected_target.valid），不要用树事件；
- 能力标签：[单车可用/需≥N车] 是能运行它的最少车辆数，车辆数不够的角色不能用（单车不用编队类子树）；
  [自行结束/满足条件才结束/不会结束] 说明它的树会不会结束：用树完成事件转出的状态，树里只能是
  [自行结束] 的；[不会结束] 的（保持、跟踪）放在最后一个状态，或用字段条件、外部事件转出；
  [长车执行，指挥僚车] 的动作和子树由长车运行、带动僚车（编队站位、按距离指挥）：只放在长车角色的
  状态树里，长车的路线、搜索也写在同一棵树中；僚车角色不用这类子树，也不写自己的路线或移动，
  只做感知、上报等自己的事，由长车带动；同一条路线在相邻状态中照写即可（运行时接着走，不会重来）；
  由长车编队指挥带动的僚车只写两个状态：FOLLOW（跟随，树里只做感知，如搜索）和 SHUTDOWN（收尾，
  树里安全关闭传感器），FOLLOW --> SHUTDOWN 由结束请求触发，SHUTDOWN 用树完成事件转到结束；
  不要照长车再写一套"搜索 → 展开 → 跟踪 → 丢失"的阶段：僚车按自己的感知切换会与长车不同步，
  阶段由长车的状态图决定；
- 阈值先宽松：条件和参数中的距离、到达、静止判定先取较大的容差（20 m 以上），计时留足余量，
  先保证模型在 AFSIM 中能跑通，以后再收紧；需求明确给出的数值照用；
- 位置和路线只写想定接口中的名字：`point=<名字>`（展开为经纬度）、`route=<名字>`（路线）；
  不写经纬度、路线名的具体值，也不要为位置另设参数。已在阵位：停车（movement.stop）后保持当前位置
  （movement.hold_position 不带位置）；需要前出：movement.follow_route(route=<接口路线>)；
  去某一点：movement.move_to_point(point=<接口位置>)；
- 上级的命令（开火命令、新指令、展开命令）用想定接口中的事件，在 model 的 events 中声明为
  `名字: {source: external}`，用它触发相应状态；接口标为"可选"的命令可以不用（不用即永远不执行），
  不要为了用上它而把它接到与它无关的状态；不要用计时、毁伤或其他状态才写入的字段代替命令。需求禁止主动开火时，
  进入开火状态只能由外部命令事件触发；
- 转换条件读的是"进入本状态时已经成立"还是"本状态新产生的变化"要分清：开火计数大于 0、毁伤时刻有效、
  分配数大于 0 这类字段在上一轮之后仍然成立，状态会一进入就转出、一拍内连跳；要等本轮的结果，用树事件
  （本状态的工作完成）、计时，或与进入时的值比较的字段；
  条件里不能写 `动作.result`、`tactic(...)` 或常量 True。

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
    environment: {duration_s: 200, step_s: 1}
    hostiles: [{id: h1, position_m: [x, y], velocity_mps: [vx, vy], appear_s: 0, remove_s: null}]   # 可省略
    events: [{at_s: 150, event: 事件ID}]
    faults: [{at_s: 60, role: 角色, sensor_operational: false}]
    expect: [{id: 英文ID, role: 角色, state_sequence: [完整的状态访问序列，从初始状态到最后]} 或 {visits_in_order: [只检查这些状态按此顺序出现]} 或 {reaches: 状态, within_s: 60（从仿真开始计）} 或 {reaches: 状态, after_event: 事件, within_s: 10（从该事件起计）} 或 {never: 状态}]
- 每个状态至少一条测试；每条转换至少被一条测试或想定覆盖；想定覆盖主流程和至少一个例外（如目标丢失、取消）。
- 想定 events 只能写模型 events 里的外部事件；目标丢失、传感器故障、车辆不能动这类情况用 hostiles 的
  remove_s 或 faults 制造（不是事件）。故障要在后面的流程（如结束时的关机）之前用第二条 faults 恢复，
  除非这个想定就是要测故障下的收尾。
- 出发位置、路线、车辆位置和敌方基准位置由想定绑定给出（见"想定绑定"）：environment 只写 duration_s
  和 step_s，不写 origin_deg 和 routes，不写 vehicles；hostiles 省略时按绑定放置敌方，hostiles: [] 表示没有敌方，需要制造目标
  出现、丢失、移动时再写 hostiles（平面坐标以出发位置为原点，米，x 向东、y 向北）。
- 有周期性重复（定时再评估、重发任务、补攻）的角色，期望用 visits_in_order 或 reaches，不用
  state_sequence（重复会让完整序列多出循环）。
- 用交战（engagement）时想定写了 vehicles，多车横向排开（与敌方方向垂直，间隔 ≥ 50 m），不要和目标排成一线：射线 30 m
  内有友车不能开火。
- 判断状态（如校验任务：有效 → 继续，无效 → 保持）的两个出口都要有；对应的测试用 actions 把检查动作
  模拟成 success / failure，而不是只改黑板。
- 用威胁分配（threat_allocation）周期下发任务时，重发间隔要短于任务有效期（自下发起 3 s），否则成员
  校验时任务已过期。"""

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
    obligations: str = "",
    interface: str = "",
    coarse: bool = False,
) -> str:
    names = ""
    if scenario_events and not interface:
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
{_scenario_section(interface)}{_obligations_section(obligations)}## 约定
{_hints(roles, scenario, allow_mission_ref, policy)}{names}

## 能力集切片索引
{index}

## 本步只写 yaml model 和各角色的 mermaid 状态图
{MODEL_GUIDE}
{chr(10) + COARSE_STRUCTURE + chr(10) if coarse else ""}
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
    obligations: str = "",
    interface: str = "",
    coarse: bool = False,
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

{_scenario_section(interface)}{_obligations_section(obligations)}## 已写好的部分
{sheet}

## 能力集详细内容（只用这里的 ID 和字段）
{details}

## 本步写 python conditions、每个角色的 python 树块，以及需要时的 yaml gaps
需要状态树的状态（结束状态不写）：{need or "（无；引用战术集任务的角色可只写要替换的状态）"}
{CONTROL_GUIDE}
{chr(10) + COARSE_CONTROL + chr(10) if coarse else ""}
{EXAMPLE}

只输出这几个代码块，不要重复已写好的块，不要解释。"""


def verification_prompt(
    requirement: str, sheet: str, summary: str, obligations: str = "", interface: str = ""
) -> str:
    cover = (
        "\n想定要能覆盖下面的需求约束：让约束的前提（相关动作开始、外部事件发生）在想定中出现，"
        "程序会在每个想定的运行轨迹上检查它们。\n"
        if obligations
        else ""
    )
    return f"""你在为战术行为模型编写验证用例。

## 需求
{requirement}
{_obligations_section(obligations)}{cover}
{_scenario_section(interface)}## 战术稿
{sheet}

## 编译结果摘要
{summary}

## 本步只写 yaml verification
{VERIFICATION_GUIDE}

只输出这一个代码块，不要解释。"""


def _scenario_section(text: str) -> str:
    if not text:
        return ""
    return (
        "## 想定接口（想定提供的位置、路线、外部事件；模型只用这些名字，具体值由想定绑定给出）\n"
        f"{text}\n\n"
    )


def _obligations_section(text: str) -> str:
    if not text:
        return ""
    return (
        "## 需求约束（程序会在二维仿真和 AFSIM 运行中按动作开始时刻检查，模型必须满足）\n"
        f"{text}\n"
        "时间上的先后与延迟用状态图的计时事件或树中的 `with after(…)` / `with when(…)` 实现。\n\n"
    )


OBLIGATIONS_GUIDE = """把需求条款中关于"什么时候做"的要求写成可以在运行轨迹上检查的约束。程序记录每个动作
开始执行的时刻和外部事件发生的时刻，按下面三类检查（动作 ID 只能用下面列出的）：

- not_before：动作 action 第一次开始，不早于动作 after 第一次开始后 min_s 秒。
  例："前进约 10 秒后开始搜索" → {kind: not_before, action: <开始搜索的动作>, after: <前进的动作>, min_s: 10}
  只有需求或条款写明了间隔时才用 not_before，min_s 用写明的数值；只要求先后时用 precedes；
- precedes：动作 then 开始前，动作 first 已经开始过。
  例："先停车，再展开" → {kind: precedes, first: <停车的动作>, then: <展开的动作>}
- response：外部事件 on_event 发生后 within_s 秒内动作 action 开始。
  例："收到结束请求后关闭传感器" → {kind: response, on_event: <事件>, action: <关传感器的动作>, within_s: 10}

效果约束检查车辆**实际做到了什么**（按 AFSIM 仿真中每秒采样的位置和速度判定，不看调用了哪个动作）；
roles 写角色 ID 列表（省略表示全部己方车辆）：
- keeps_distance：这些车辆与任一敌方车辆的距离始终不小于 min_m 米。
  例："与敌保持安全距离" → {kind: keeps_distance, min_m: <距离>}
- stays_still：动作 action 开始后（或外部事件 on_event 后），车辆在 duration_s 秒内（省略则到运行结束）
  位移不超过 tol_m 米（默认 20）。例："收到结束命令后停车" → {kind: stays_still, on_event: <事件>}
- not_all_moving：任何时刻这些车辆不同时处于机动状态。例："不得使全排同时暴露/同时机动"
- moves_in_turn：这些车辆分次机动，至少 min_bounds 次（默认 2）、每次至少 min_m 米（默认 20），
  相邻两次由不同车辆完成。例："交替跃进""一部掩护一部前进，再交换"
- advances：这些车辆合计向敌方接近至少 min_m 米。例："前出""向东推进"
  min_m 取需求明确给出的前出距离；需求里"约 X 公里处有敌"是与敌的距离，不是要走的距离，
  未给出前出距离时取保守值（如 100 m），并且不得让车辆进入要求保持的安全距离以内
- destroys：这些车辆（开火方）摧毁至少 min_count 辆敌方车辆（省略表示想定中全部敌方车辆），按武器命中
  与毁伤事件判定；可选 within_s（从开始计的时限）。例："打击直至敌方被摧毁""歼灭来犯之敌" → {kind: destroys}
  只在需求要求毁伤结果时写；只要求"开火""射击"时不写；
- formation_shape：这些车辆以 leader_role 角色的车辆为基准排成 shape 队形（column 纵队、line_abreast
  横队、wedge_forward 楔形（成员在后两侧）、wedge_rear 倒楔形（成员在前两侧）、echelon_left /
  echelon_right 左 / 右梯队），纵向间距 spacing_m、横向间距 lateral_m（默认同 spacing_m），每个槽位
  偏差不超过 tol_m（默认 15 m），在动作 action 开始（或外部事件 on_event）后 within_s 秒内成形并保持
  hold_s 秒（默认 5）。例："接命令后成横队展开" → {kind: formation_shape, shape: line_abreast,
  leader_role: <长车角色>, on_event: <命令事件>, within_s: 90}；间距用需求或能力参数给的值，没有就省略

规则：
- **每个战术条款（tc_ 开头）至少写一条约束**；涉及位置、距离、机动、停车、轮换的条款优先用效果约束：
  只检查"调用了动作"的约束不能证明战术被实现；没有约束的条款，模型即使运行通过也只算"未验证"；
- 只写需求明确提出的时间或先后要求；动作本身就有的顺序（同一个子树内部）不写；
- precedes 和 not_before 按两个动作各自**第一次**开始的时刻判定：只用于整个任务中第一次出现就体现条款
  的动作对（如"先停车再展开"）；在多个阶段都会执行的动作（如纵队跟随、保持位置在开始阶段就会执行）
  不要用来表达"某阶段之后才做"——这类条件性的要求（"丢失 30 秒后恢复纵队"）不写约束；
- 约束检查的是动作是否按时开始，不规定用哪个动作实现：不要因为写约束而选用与本需求无关切片的动作；
- 选最直接体现条款的动作：搜索开始用开机 / 开始搜索的动作，不用读取航迹类动作；
- 时长取需求给的数值（"约 10 秒"写 10）；没有给时长的 response 用 20；
- 数值先取宽松值，先保证模型能跑通、约束能判定，以后再收紧：容差取 20 m 以上，响应时间留足余量，
  前出距离等下限取需求能接受的最小值；需求明确给出的数值照用；
- 两辆不同车辆之间的先后（如"掩护车就位后跃进车出发"）照常写 precedes：按完成时刻跨车判定；
- id 用英文蛇形；clause 写条款编号（可写多个，逗号分隔）；description 用一句中文复述要求（不超过 40 字，不写推理过程）；
- 没有可检查的要求就返回空列表。"""


def obligations_prompt(
    requirement: str,
    clauses: list[dict[str, Any]],
    actions: str,
    events: dict[str, str],
    roles: dict[str, str] | None = None,
) -> str:
    ev = "\n".join(f"- {k}：{v}" for k, v in events.items()) or "（无）"
    rl = "\n".join(f"- {k}：{v}" for k, v in (roles or {}).items()) or "（未给出）"
    return f"""你在把战术需求中的时间、先后与效果要求写成可检查的约束。

## 需求
{requirement}

## 需求条款
{_clauses(clauses)}

## 外部事件
{ev}

## 角色
{rl}

## 可用的动作（ID — 说明）
{actions}

## 要求
{OBLIGATIONS_GUIDE}

按给定的结构输出。"""


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


MAP_GUIDE = """把每个战术条款对到能力集的动作上，判断现有能力能不能真正实现它。

规则：
- 只有动作说明**本身**就做这件事才算能实现；不能拿做别的事的动作冒充（例如：重新计算编队槽位
  不等于交换掩护/跃进角色；保持位置不等于报告"到位"；给状态起个名字不实现任何东西）；
- 条款需要车辆之间传递特定消息（如"Move""Set""就位"报告）时，要有发送和接收这类消息的动作；
  只有通用的航迹广播、目标共享，不算实现特定的协同消息；
- 能实现：actions 列出用到的动作 ID，how 用一句话说明怎么组合；missing 留空；
- 不能实现或只能部分实现：missing 写缺少的原子能力（如"按周期交换掩护/跃进角色""向掩护车发送到位报告"），
  actions 列出能用上的部分；
- 每个条款都要给出一项，clause 写条款编号。"""


def capability_map_prompt(
    clauses: list[dict[str, Any]], actions: str, roles: dict[str, str]
) -> str:
    rl = "\n".join(f"- {k}：{v}" for k, v in roles.items()) or "（未给出）"
    listed = "\n".join(f"- {c['id']}：{c['text']}" for c in clauses)
    return f"""你在核对选定的战术能否用现有能力集实现。

## 战术条款
{listed}

## 角色
{rl}

## 可用的动作（ID — 说明）
{actions}

## 要求
{MAP_GUIDE}

按给定的结构输出。"""
