# 战术行为模型中间表示（Tactical Behavior IR）1.0 设计

> 状态：已确认（2026-10-13），第 1–7 步已实现；实现中确定的细节见第 30 节
> 基于：用户提供的 `tactical-behavior-ir-design.md`，并合并 Tactical Crow → Falcon 方案
> 日期：2026-10-13

## 0. 相对原稿的修订说明

| 编号 | 修订 | 原因 |
| --- | --- | --- |
| R1 | **后端中立**：IR 只使用逻辑能力和逻辑组件；具体仿真平台的类型名、组件名、模板都放到 `resolved_context.json` 和各后端编译器中 | 同一份 IR 可编译到多个后端；IR 本身不含任何平台私有信息 |
| R2 | **证据与确认状态**：参数、条件阈值、状态转换、协同规则增加 `basis`（证据状态 + 证据引用 + 确认状态） | 衔接 Crow 证据包；把原稿“不得猜测参数”“网络资料不能直接授权参数”落实为可校验字段 |
| R3 | **多平台协同**：新增 `roles`、`formations`、`coordination`、`messages`；一个模型包可包含多个角色，每个角色有自己的状态机与树 | 编队、长僚、交替跃进等战术本质上是多平台行为，原稿仅覆盖单平台处理器 |
| R4 | **动作目录中立化**：`fly_to_*` 泛化为 `move_to_*`，新增地面/编队动作（`keep_station`、`assume_formation`、`follow_route_with_offset` 等） | 支持地面分队、无人机集群等非空中平台 |
| R5 | **三级测试**：L1 静态校验、L2 单步语义测试（原稿 `tests`）、L3 闭环想定测试（新增 `scenarios`） | 行为树逐 tick 正确不等于战术效果正确；需要在仿真中观测队形、间距、时机 |
| R6 | **生成物按后端分目录**：`generated/<backend>/…`；本仓库实现 `python_sim`、`btcpp_xml`、`mermaid` 三个后端，其他后端通过插件接口在各自授权环境中实现 | 保持编译器确定性与可替换性 |
| R7 | 新增错误码 `EVD001`、`PAR001`、`ROLE001`、`FORM001`、`COORD001`、`BIND001`、`SCN001` | 覆盖新增字段 |

未修订的部分（YAML 约束、标识符规则、表达式 DSL、资源模型、行为语义、树节点类型、状态机规则、规范化规则、Agent 生成规则）保持原稿语义，下文仅在需要处补充。

## 1. 目的

本规范定义领域化 Agent 生成战术行为模型时使用的结构化中间表示（IR）。IR 是稳定契约：

- 大模型负责把用户意图和 Crow 证据转换为 IR 草稿；
- 确定性代码负责校验、规范化、绑定后端组件、生成可运行文件和执行测试；
- 人工审查点位于 IR 规范化之前（假设与参数确认）和 L3 想定测试之后（战术效果评估）。

## 2. 处理链路

```text
用户战术需求
  -> Tactical Crow：研究计划、检索、证据包（evidence_package.json）
  -> Tactical Falcon：需求解析 + 证据映射
  -> behavior_spec.yaml                      # 权威源文件，用户可编辑
  -> L1 Schema 校验 + 语义校验
  -> 人工确认：假设、候选参数（basis.confirmation）
  -> 后端绑定（resolved_context.json）
  -> normalized_ir.json                      # 只读编译输入
  -> L2 单步语义测试（tests）
  -> 后端编译器 → generated/<backend>/…
  -> L3 闭环想定测试（scenarios）→ validation_report.json
  -> 人工审查战术效果，回到 behavior_spec.yaml 迭代
```

## 3. 模型包结构

```text
<model-id>/
  behavior_spec.yaml          # 权威源文件
  evidence_package.json       # Crow 证据包快照（只读，含哈希）
  resolved_context.json       # 逻辑名 → 后端类型/组件名绑定（按后端分节）
  normalized_ir.json          # 规范化后的编译输入
  validation_report.json      # L1/L2/L3 结果、警告、假设、未解析项
  review.md                   # 人工审查报告（由报告生成器产出）
  generated/
    manifest.json             # 输入哈希、IR 版本、动作目录版本、模板版本
    python_sim/             # 可在内置二维仿真中运行
      roles/<role>.py
      run_scenarios.py
    btcpp_xml/
      <role>.xml
    mermaid/
      <role>_state_machine.mmd
      <role>_<tree>.mmd
    <external_backend>/       # 外部后端插件生成物（本仓库不实现）
```

`generated/` 为确定性生成物，不作为人工修改的权威来源。

## 4. YAML 基本格式约束

与原稿第 5 节一致：UTF-8；两空格缩进；`snake_case` 键名；标识符 `[a-z][a-z0-9_]{0,63}`；同一命名空间唯一；描述可用中文；禁止自定义 tag、锚点、别名、插值；不得包含凭据。

**补充**：跨命名空间引用使用点号限定名，例如 `platoon_leader.mission.current_task`（角色.黑板.变量）。

## 5. 顶层结构

```yaml
schema_version: "1.0"

model: {}
provenance: {}
assumptions: []
execution: {}
roles: []            # R3：一个或多个角色；单平台模型只有一个角色
formations: []       # R3：编队几何（可为空数组）
coordination: []     # R3：协同模式（可为空数组）
messages: []         # R3：角色间消息类型（可为空数组）
parameters: {}
resources: []
conditions: []
behaviors: []
trees: []
tests: []
scenarios: []        # R5：闭环想定（可为空数组，但见第 21 节最低要求）
```

**相对原稿的变化**：

- 原 `target`、`dependencies`、`blackboards`、`state_machine` 移入每个 `roles[]` 条目（每个角色是一个独立受控平台类型，有自己的依赖、黑板和状态机）。
- `conditions`、`behaviors`、`trees`、`parameters`、`resources` 仍为模型级共享定义，由角色引用。

必填：`schema_version`、`model`、`execution`、`roles`（至少 1 个）、`parameters`、`conditions`、`behaviors`、`trees`、`tests`。空集合使用 `[]`，不得省略。未知顶层字段为错误。

## 6. 模型元数据

与原稿第 7 节一致，`model.intent` 来自受控战术意图词表（与 Crow 的 `tactical_vocabulary.toml` 概念 ID 对齐，例如 `movement_to_contact`、`formation_transition`、`defensive_counter_air`；未知用 `custom`，要求人工审核）。

```yaml
model:
  id: armor_platoon_terrain_formation
  display_name: 装甲排地形自适应编队
  version: 0.1.0
  description: 装甲排在开阔与城区地形间机动，按地形切换队形，遭遇接触时转入交替跃进。
  intent: formation_transition
  authoring_mode: agent_generated
  domain_concepts: [unit.tank_platoon, formation.wedge, formation.column, movement.techniques]
```

## 7. 来源、证据、事实与假设（R2）

### 7.1 来源

```yaml
provenance:
  requirements:
    - source_id: user_req_001
      kind: user_input
      locator: conversation
      summary: 研究装甲车在不同地形下的编队方式，并在接敌时转换。
  evidence_package:
    package_id: crow_20261013_0915
    content_hash: sha256:<hash>
    readiness: passed            # Crow Falcon 门槛结果：passed | failed | overridden
  references:
    - source_id: pdf:138b1c4ddbb73d05
      kind: crow_source          # crow_source | user_document | standard | other
      locator: "ATP 3-20.15, ch.3"
      quality_tier: A
      content_hash: sha256:<hash>
      classification: public     # public | user_private
```

约束：

- `kind: crow_source` 的引用必须能在 `evidence_package.json` 中找到同一 `source_id`。
- 来源摘要描述结论，不大段复制原文；原文摘录只保存在证据包中。
- `classification` 只允许 `public` 和 `user_private`。`user_private` 资料不得进入公网检索请求或公共索引。

### 7.2 证据依据 `basis`

所有影响战术语义的数值和规则（参数、条件阈值、状态转换、协同规则、编队几何）都必须带 `basis`：

```yaml
basis:
  status: explicit          # explicit | inferred | conflicting | unknown | user_specified
  evidence: [ev_003]        # 证据包中的 evidence_id；explicit/conflicting 至少 1 条
  rationale: 条令给出开阔地形楔形队形的车间距范围。   # inferred 必填
  confirmation: pending     # pending | confirmed | rejected
  confirmed_by: null        # 确认后记录人员标识
```

规则：

- `explicit`：至少 1 条证据直接支持。
- `inferred`：必须写 `rationale`，至少引用 1 条间接证据或 1 条假设。
- `conflicting`：至少 2 条相互冲突的证据，并说明所选值的理由；必须人工确认。
- `unknown`：值可以为占位，但该项 `confirmation` 未变为 `confirmed` 前阻塞编译。
- `user_specified`：用户直接给定，无需证据；`confirmation` 自动为 `confirmed`。
- **来自网络资料的数值一律先为 `pending`**，经用户确认后才能进入 `normalized_ir.json`（对应原稿“网络资料不能直接授权参数”）。

### 7.3 假设

与原稿一致；新增 `affects` 字段列出受影响的 ID，便于报告聚合：

```yaml
assumptions:
  - id: assumption_001
    statement: 所有车辆具备可用的排内语音/数据通信。
    affects: [cond_comms_ok, coord_leader_follower]
    requires_confirmation: true
```

## 8. 执行配置

与原稿第 9 节一致，`architecture` 允许 `script_processor`、`behavior_tree`、`state_machine`、`state_machine_with_behavior_trees`（推荐后两者之一）。补充：

```yaml
execution:
  architecture: state_machine_with_behavior_trees
  controller_name: tactical_controller     # 原 processor_name，改为中立命名
  update_interval: {value: 1, unit: sec}
  deterministic: true
  random_seed_parameter: null
  debug: {behavior_tree_trace: true, state_transitions: true, script_writes: false}
```

## 9. 角色（R3）

每个角色对应一个受控平台类型，拥有自己的依赖、黑板、状态机和行为树入口。

```yaml
roles:
  - id: platoon_leader
    count: 1
    capability_class: ground_vehicle       # ground_vehicle | air_vehicle | surface_vessel | static_node
    command:
      superior: null
      subordinates: [wingman]
    dependencies:
      mover: {required: true, capability: route_following_ground}
      sensors:
        - {id: vehicle_sight, capability: visual_search, required: true}
      comms:
        - {id: platoon_net, capability: voice_data, required: true}
      weapons: []
    blackboards:
      - id: mission
        scope: platform
        variables:
          - {name: current_formation, type: string, default: wedge}
          - {name: contact_reported, type: bool, default: false}
    state_machine: {}                      # 结构同原稿第 19 节
  - id: wingman
    count: 3
    capability_class: ground_vehicle
    command: {superior: platoon_leader, subordinates: []}
    dependencies: {…}
    blackboards: [...]
    state_machine: {}
```

约束：

- 依赖只写逻辑能力（`capability`），**不得**出现任何仿真平台的类型名或组件名；这些只出现在 `resolved_context.json`（R1）。
- `command` 关系必须构成有向无环图，且只引用已定义角色。
- 黑板 `scope` 第一版仍只允许 `platform`；跨角色信息交换只能通过 `messages`（第 11 节）或 `coordination` 预定义模式实现，不提供隐式共享内存。
- 必需能力在目标后端未绑定时报 `DEP001`；可选能力缺失时，引用它的行为必须有降级分支。

## 10. 编队几何（R3）

```yaml
formations:
  - id: wedge
    reference: leader              # 以长车为原点、前进方向为 +x
    slots:
      - {role_instance: wingman[0], forward: {parameter_ref: wedge_depth_neg}, right: {parameter_ref: wedge_half_width_neg}}
      - {role_instance: wingman[1], forward: {parameter_ref: wedge_depth_neg}, right: {parameter_ref: wedge_half_width}}
      - {role_instance: wingman[2], forward: {parameter_ref: wedge_depth_neg_2}, right: {literal_distance: {value: 0, unit: m}}}
    tolerance: {parameter_ref: station_tolerance}
    basis: {status: explicit, evidence: [ev_003], confirmation: pending}

  - id: column
    reference: leader
    slots:
      - {role_instance: wingman[0], forward: {parameter_ref: column_interval_neg}, right: {literal_distance: {value: 0, unit: m}}}
      - …
    tolerance: {parameter_ref: station_tolerance}
    basis: {status: inferred, evidence: [ev_007], rationale: …, confirmation: pending}
```

约束：`role_instance` 下标不得越界；同一编队内槽位不得重复；所有偏移必须是带单位的距离（`FORM001`）。

## 11. 协同与消息（R3）

### 11.1 协同模式

协同模式是有限的、可校验的模板，由编译器展开为各角色的条件、行为和消息，避免模型自由编写跨平台逻辑。

```yaml
coordination:
  - id: coord_leader_follower
    pattern: leader_follower          # leader_follower | bounding_overwatch | report_and_wait | synchronized_transition
    leader: platoon_leader
    followers: wingman
    formation_ref: {blackboard_value: platoon_leader.mission.current_formation}
    basis: {status: explicit, evidence: [ev_003], confirmation: pending}

  - id: coord_bounding
    pattern: bounding_overwatch
    groups:
      bounding: [platoon_leader, wingman[2]]
      overwatch: [wingman[0], wingman[1]]
    bound_distance: {parameter_ref: bound_distance}
    active_when: {condition_ref: in_contact}
    basis: {status: inferred, evidence: [ev_011], rationale: …, confirmation: pending}
```

各模式的展开规则、所需消息和动作在“协同模式目录”中定义（与动作目录同级，版本化）。目录外的模式为错误（`COORD001`）。

### 11.2 消息

```yaml
messages:
  - id: msg_formation_change
    from: platoon_leader
    to: wingman
    fields:
      - {name: formation, type: string}
    delivery: {latency: {value: 2, unit: sec}, reliability: 1.0}
```

发送通过动作 `send_message`；接收通过条件操作数 `message_value` 和条件函数 `message_received(<message_id>)`。消息字段只允许 `bool | int | double | string`。

## 12. 参数与单位（R2）

在原稿第 11 节基础上，每个参数增加 `basis`：

```yaml
parameters:
  open_interval:
    type: distance
    value: 125
    unit: m
    minimum: 100
    maximum: 150
    basis: {status: explicit, evidence: [ev_003], confirmation: pending}
  urban_interval:
    type: distance
    value: null
    unit: m
    basis: {status: unknown, evidence: [], confirmation: pending}
  station_tolerance:
    type: distance
    value: 15
    unit: m
    basis: {status: user_specified, confirmation: confirmed}
```

参数类型、单位换算、范围检查规则同原稿。`value: null` 仅允许 `status: unknown`，且阻塞编译（`PAR001`）。

## 13. 表达式 DSL

沿用原稿第 13 节（`all/any/not/condition_ref/literal/compare`，禁止内嵌脚本）。操作数来源在原稿基础上**改名并补充**：

| 操作数 | 说明 |
| --- | --- |
| `literal`、`parameter_ref`、`blackboard_value` | 同原稿 |
| `self_value` | 原 `platform_value`；位置、速度、航向、油量/能量状态等 |
| `mover_value`、`sensor_value`、`weapon_value`、`track_value`、`simulation_value` | 同原稿，均引用逻辑组件 ID |
| `terrain_value`（新增） | 当前位置地形类别、通视、坡度等；由后端地形服务提供 |
| `formation_value`（新增） | 本平台相对指定编队槽位的误差、当前编队 ID |
| `team_value`（新增） | 受控聚合量：最近友邻距离、编队平均误差等（只读，后端计算） |
| `message_value`（新增） | 最近收到的指定消息字段 |
| `function_call` | 来自函数白名单，声明参数与返回类型 |

条件仍须无副作用、返回布尔值；数值阈值必须引用带 `basis` 的参数，**不得在条件中写裸数字**（`literal` 数字仅允许 0、1 和布尔/字符串常量；违反为 `EVD001` 警告）。

## 14. 条件定义

同原稿第 14 节。示例：

```yaml
conditions:
  - id: entering_urban
    description: 前方 200 m 内进入城区地形。
    expression:
      compare:
        left: {terrain_value: {property: distance_to_class, class: urban}}
        operator: lte
        right: {parameter_ref: urban_transition_distance}
  - id: in_contact
    expression:
      any:
        - {blackboard_value: mission.contact_reported}
        - compare:
            left: {track_value: {property: nearest_hostile_range}}
            operator: lte
            right: {parameter_ref: contact_range}
```

## 15. 行为定义

完全沿用原稿第 15 节语义（前置条件、memory、claims、actions + issue_policy、success、failure、timeout、messages）。补充一项：

- `basis`（可选）：当行为编码了一条来自文献的战术规则（例如“接敌后先占据掩护位置再报告”）时必须填写，以便报告追溯。

## 16. 动作白名单（R4）

原稿动作改为平台中立名称（括号内为旧名，作为别名保留，规范化时统一）：

| 类别 | 动作 |
| --- | --- |
| 机动 | `move_to_position`（`fly_to_position`）、`move_to_platform`（`fly_to_platform`）、`move_to_track`（`fly_to_track`）、`set_heading`、`set_speed`、`set_altitude`（仅 air/surface 能力）、`follow_route`、`halt` |
| 编队（新增） | `assume_formation`、`keep_station`、`follow_route_with_offset`、`bound_to_position`、`hold_overwatch_position` |
| 传感器 | `turn_sensor_on`、`turn_sensor_off`、`orient_sensor` |
| 交战 | `fire_weapon`（默认禁止 `every_tick`） |
| 通信 | `send_message`、`report_contact` |
| 控制 | `turn_controller_on`、`turn_controller_off`、`set_blackboard`、`delete_blackboard_value`、`wait` |
| 资源 | `set_bingo_fuel`（泛化为 `set_reserve_threshold`） |

动作目录条目定义与原稿一致（参数 Schema、单位、返回语义、副作用、所需能力、占用资源、是否允许每 tick、各后端模板映射）。**后端模板映射位于各后端编译器包内，不写入 IR 或动作目录的公共部分**（R1）。

## 17. 资源模型

同原稿第 17 节。补充默认资源：`movement_control`（原 `flight_control`）、`sensor_control`、`weapon_control`、`comm_control`。

## 18. 行为树

同原稿第 18 节（节点类型、`node_id` 唯一、parallel 策略、priority/weighted 评估、装饰器、子树无环）。

## 19. 状态机

同原稿第 19 节，位于每个 `roles[].state_machine`。补充：

- 每个 `transitions[]` 条目增加 `basis`（战术触发规则的证据）。
- 新增可选 `synchronize_with: <coordination_id>`：表示此转换需由协同模式同步（例如长车切换队形后僚车同步切换），由编译器展开为消息发送/接收。

```yaml
state_machine:
  id: leader_mission
  initial_state: travel_open
  states:
    - id: travel_open
      tree: travel_open_tree
      terminal: false
      transitions:
        - transition_id: open_to_contact
          to: contact
          priority: 100
          when: {condition_ref: in_contact}
          basis: {status: explicit, evidence: [ev_011], confirmation: pending}
        - transition_id: open_to_urban
          to: travel_urban
          priority: 80
          when: {condition_ref: entering_urban}
          synchronize_with: coord_leader_follower
          basis: {status: inferred, evidence: [ev_007], rationale: …, confirmation: pending}
        - transition_id: open_self
          to: travel_open
          priority: 0
          when: {literal: true}
```

## 20. 测试（L2：单步语义测试）

与原稿第 20 节一致：给定状态、参数、黑板、平台/组件状态，期望下一状态、选中路径和发出的动作。新增：

- `given.role`：指定被测角色（多角色模型必填）；
- `given.messages`：注入的消息；
- `expect.sent_messages`：期望发出的消息。

最低覆盖仍为原稿五类，且**每个角色**至少覆盖正常路径与一条高优先级转换。

## 21. 闭环想定（L3，R5）

```yaml
scenarios:
  - id: open_to_urban_transition
    description: 开阔地行进 2 km 后进入城区，期望转为纵队。
    environment:
      terrain_map: synthetic/open_then_urban.yaml    # 后端无关的合成地形描述
      duration: {value: 15, unit: min}
      random_seed: 42
    initial:
      platoon_leader: {position: [0, 0], heading_deg: 90, state: travel_open}
      wingman: {formation: wedge}
    events: []
    expect:
      - id: e1
        observable: role_state
        role: wingman
        equals: travel_urban
        within: {after_event: leader_enters_urban_zone, value: 60, unit: sec}
      - id: e2
        observable: formation_error_rms
        formation: column
        lte: {parameter_ref: station_tolerance}
        during: {from: e1, duration: {value: 120, unit: sec}}
    traces_to: [open_to_urban, coord_leader_follower]   # 关联被验证的转换/协同 ID
```

可观测量白名单（第一版）：`role_state`、`formation_id`、`formation_error_rms`、`min_interval`、`max_interval`、`speed`、`time_to_transition`、`messages_sent`、`actions_issued`、`contact_reaction_time`。

最低要求：

1. 每个状态转换至少被一个 L2 测试覆盖；
2. 每个带 `basis` 的协同规则和编队几何至少被一个 L3 想定的 `traces_to` 覆盖；
3. 至少一个想定包含“通信降级/消息丢失”事件。

## 22. 校验级别和错误码

沿用原稿第 21 节（`IR001`–`ASM001`），新增：

| 错误码 | 含义 |
| --- | --- |
| `EVD001` | 战术数值或规则缺少 `basis`，或 `explicit`/`conflicting` 缺少证据引用 |
| `PAR001` | 参数为 `unknown`/`pending` 或值为空，阻塞编译 |
| `ROLE001` | 角色未定义、指挥关系成环、`role_instance` 越界 |
| `FORM001` | 编队槽位重复、偏移无单位、容差缺失 |
| `COORD001` | 协同模式不在目录中，或其引用的角色/消息/条件无法解析 |
| `BIND001` | 所选后端无法为某个逻辑能力提供绑定 |
| `SCN001` | 想定引用了未知可观测量、角色或事件，或覆盖率不满足第 21 节 |

报告格式同原稿（含 `path`、`source_id`、`suggestion`），新增可选字段 `evidence_ids`。

## 23. 规范化规则

同原稿第 22 节；另外：

- 动作别名统一为中立名称（R4）；
- 协同模式展开为显式的条件、行为、消息和转换，并标注 `generated_from: <coordination_id>`；
- 只包含 `basis.confirmation == confirmed` 的数值；任何 `pending` 均阻塞（不得在规范化时默认确认）。

## 24. 后端绑定与编译器输入保证（R1、R6）

`resolved_context.json` 按后端分节：

```json
{
  "backend": "python_sim",
  "roles": {
    "platoon_leader": {
      "platform_template": "sim.ground_vehicle",
      "capabilities": {"route_following_ground": "sim.GroundRouteMover", "visual_search": "sim.VisualSensor"}
    }
  }
}
```

编译器只接受：Schema 与语义校验通过、所有阻塞假设与参数已确认、所有必需能力已绑定、L2 测试通过、内容哈希一致的 `normalized_ir.json`。编译器拒绝原始脚本、未知动作/函数/协同模式和未绑定能力。

**后端插件接口**：`compile(normalized_ir, resolved_context, catalog_versions) -> generated files + manifest`。本仓库实现 `python_sim`、`btcpp_xml`、`mermaid`；其他仿真平台后端由具备相应使用权限的团队在其环境中按同一接口实现，不进入本仓库。

## 25. 安全与资料边界

- 公网检索请求不得包含本地私有文件内容、真实想定数据、平台参数、路径或凭据。
- 网络资料与用户私有资料分别记录来源和 `classification`。
- Agent 只能把公开事实转化为**候选**参数（`confirmation: pending`）。
- 生成物不得覆盖用户已有文件，输出到新的模型包目录。
- 每次生成保存输入哈希、IR 版本、动作目录版本、协同模式目录版本、模板版本。

## 26. 最小完整示例（单角色）

原稿第 25 节示例在本版中的写法差异：

- `target` + `dependencies` + `blackboards` + `state_machine` 移入 `roles[0]`；
- `afsim_platform_type`、`expected_type`、`afsim_name` 改为 `capability_class` 与逻辑 `capability`，具体名称进入 `resolved_context.json`；
- `fly_to_track` 改为 `move_to_track`；
- `engage_range`、`intercept_speed` 增加 `basis`（例如 `user_specified`）。

多角色完整示例见 `examples/armor_platoon_terrain_formation/behavior_spec.yaml`（实现阶段提供）。

## 27. Agent 生成规则

沿用原稿第 26 节，补充：

1. 每条战术规则先在 Crow 证据包中查找支持证据，填写 `basis`；找不到时标为 `unknown` 并列入待确认项，**不得补造数值**；
2. 多平台需求必须建模为多个 `roles` 和目录内的 `coordination` 模式，不得用单角色加任意消息拼接；
3. 为每个协同规则与编队生成至少一个 L3 想定；
4. 在 `review.md` 中按“待确认参数 → 推断规则 → 冲突证据 → 假设”顺序列出审查清单。

## 28. 验收标准

在原稿第 27 节基础上增加：

- 同一 `normalized_ir.json` 可确定性生成 `python_sim`、`btcpp_xml`、`mermaid` 三种产物，且 L2 结果一致；
- 每个战术数值和规则可追溯到证据或用户确认；
- 能表达长僚跟随、编队切换同步、交替跃进三种协同模式；
- L3 想定可在内置二维仿真中自动判定通过/失败。

## 29. 实现计划

1. IR JSON Schema（含 `basis`、`roles`、`formations`、`coordination`、`messages`、`scenarios`）；
2. Pydantic 数据模型 + 校验器（L1，错误码齐全）；
3. 动作目录、函数白名单、协同模式目录（版本化）；
4. 规范化器（单位、别名、协同展开、哈希）；
5. L2 单步语义测试执行器（不依赖任何后端）；
6. 后端：`mermaid` → `btcpp_xml` → `python_sim`；
7. 内置二维仿真（地面/空中简化运动学、合成地形、消息延迟）与 L3 想定执行器；
8. Falcon：证据包 → `behavior_spec.yaml` 草稿 → `review.md`。

## 30. 实现说明（2026-10-13，v1）

代码位于 `src/afsim_research_agent/ir/`，示例包 `examples/armor_platoon_terrain_formation/`。实现中确定或补充的内容：

### 30.1 相对本稿的小幅调整

| 项 | 调整 | 原因 |
| --- | --- | --- |
| 后端名 | `py_trees_sim` 改名为 `python_sim` | 生成代码运行在内置参考解释器上（与 L2 同一套语义），不依赖 py_trees 库；避免两套语义实现 |
| 错误码 | 新增 `EVD002`（basis 待确认/被拒，阻塞规范化）、`TEST001`（L2 测试失败）、`L3001`（L3 期望未满足） | 区分“缺证据”(EVD001) 与“未确认”，并让测试失败可定位 |
| 编队偏移 | `DistanceRef` 增加 `scale`（仅槽位偏移可用），如 `{parameter_ref: column_interval, scale: -2}` | 避免为“负的间距”“两倍间距”定义冗余参数 |
| 操作数 | 新增 `comm_value`（通信可用性）、`memory_value`（行为内持久变量，仅行为表达式可用）；`platform_value` 作为 `self_value` 别名 | 原稿行为 memory 无读取途径；通信降级需要可观测 |
| 测试 | `tests[].category` 取值为原稿五类；缺类为 `SCN001` 警告；`given` 增加 `instance`、`elapsed`（状态/行为已运行时长）、`coordination`（协同相位覆盖） | 让五类覆盖与超时测试可表达 |
| 消息 | `from`/`to` 可为角色列表 | 交替跃进完成消息在多个角色间广播 |
| 意图词表 | `catalog/intents.toml`（15 项，含 `custom`） | 落实 §6 受控词表 |

### 30.2 执行语义（L2、内置仿真、python_sim 后端共用）

1. 每 tick：投递消息 → 按优先级评估当前状态转换（同优先级按声明顺序）→ 执行（可能已切换的）新状态的树。
2. 状态切换时清空树记忆（带记忆组合节点的进度）并中止所有活动行为。
3. 行为“激活”从它被 tick 且处于非活动态开始；前置条件只在激活时检查；动作先下发、再评估 failure → success → wait/超时；本 tick 未被 tick 的活动行为视为被中止。
4. `once_per_activation` 在一次激活内只下发一次；行为成功后再次被 tick 是新的激活。因此“一次性”步骤应放在 `sequence_with_memory` 中，或用黑板标记防重复（示例的接敌报告即采用后者）。
5. `message_received` 只在消息到达的那个 tick 为真；`message_value` 保留最近一次值，未收到时取字段默认值。
6. 外部动作在下发时立即作用于世界，`mover_value.at_destination` 等完成标志总是对应最新命令。
7. 协同模式展开：`leader_follower` 生成编队广播/同步转换消息、`announce_formation`、`keep_station`；`bounding_overwatch` 生成 `bound`/`announce_bound`/`overwatch` 与子树 `<id>__tree`，相位由各实例收到的完成消息独立推算；`report_and_wait` 生成报告/确认消息与行为；带 `synchronize_with` 的转换在跟随角色中展开为优先级 90（可配置）的消息触发转换。

### 30.3 L3 判定语义

- `within`：条件首次成立时间 ≤ 锚点 + 窗口；该时间可作为后续期望的锚点。
- `during`：状态类观测量在窗口内每个采样都成立；数值序列取聚合（`formation_error_rms`/`speed` 取均值，`min_interval` 取最小，`max_interval` 取最大，计数类取窗口内次数）。
- 两者都无：状态类看运行结束时刻，数值类在全程聚合。
- 内置仿真为二维点质量运动学 + 矩形合成地形 + 距离传感器 + 带延迟/丢包的消息，只用于检查协同逻辑与时序。

### 30.4 已知限制

- 同一 tick 内到达的多条同类消息只触发一次 `message_received`；`report_and_wait` 的确认因此每周期一条，但它广播给全部报告方，不影响结果（见 30.6）。
- 被多个状态共用的子树共享节点记忆。
- `weighted_random` 使用实例级确定性随机数（`random_seed_parameter` 与实例号）。
- BT.CPP XML 中战术叶节点为自定义节点（`TacticalBehavior`、`CheckCondition`、`EvaluateTransitions` 等），需宿主程序按同一 `normalized_ir.json` 实现；生成时会把 XML 回读并与 IR 结构逐节点比对。
- 第 8 步（Falcon：证据包 → `behavior_spec.yaml` 草稿 → `review.md`）尚未实现。

### 30.5 外部引擎联合仿真（2026-10-13 增补）

- `ir/cosim/`：`EngineAdapter` 协议（`reset/apply_commands/send_messages/apply_event/step/close`），按步同步的 `CoSimRunner`，观测到操作数的映射 `ObservationWorld`，命令参数解析（如 `keep_station` 补充 `leader_entity/forward_m/right_m`），两种消息模式（运行器投递 / 引擎投递）。
- 外部引擎能力绑定：`bindings/<engine>.json` → `bind_external`，必需能力缺失为 `DEP001`。
- 运行轨迹（JSON Lines）与 `judge_trace`：联合仿真与引擎原生运行使用同一 L3 期望判定。
- 参考引擎（内置物理套上适配器协议）与内置仿真逐项结果一致，作为协议一致性测试。
- 对接指南与模板：`integrations/lab_engine/`。

### 30.6 框架问题修复（2026-10-13，目录 `coordination-2026.10.2`）

依据示例包测试分析（`analysis/2026-10-13-armor-example-test-analysis.md`）修复三个框架问题：

| 问题 | 修复 | 新增/变更字段 |
| --- | --- | --- |
| 同步消息只发一次、丢失后不重发 | 发起方按间隔重发当前同步状态与队形（心跳），见后端指南 S18 | `coordination[].resync_interval`（时间，≥0；缺省取目录 `default_resync_s = 5 s`，0 关闭）→ 规范化为 `resync_interval_s` |
| 交替跃进无停止条件，会一直推进到敌方 | 新函数 `standoff_reached(coordination)`；展开时生成条件 `<c>__standoff_reached`，`my_turn` 加上 `¬standoff`，`bound` 的失败条件为 standoff；`bound_to_position` 朝目标跃进并留出 `stop_short`；未设停止条件时给出 `COORD001` 警告。见 S19 | `coordination[].standoff_distance`（DistanceRef）、`coordination[].objective`（航点 ID，可选，跃进方向）；动作参数 `toward`（新参数类型 `target`）、`stop_short`；目录 `standoff_margin_m = 10 m` |
| 一个周期多份报告只确认一次 | 核实为设计行为：确认消息广播给全部报告方，等待中的报告方都会成功。写入 S20，并改写 30.4 的限制说明 | — |

新增 L3 观测量 `hostile_range_min`（全程或窗口内与最近敌的最小距离）；轨迹步骤行增加可选字段 `nearest_hostile_m`。外部引擎协议（`tbe-step/1`）消息格式不变，只扩展了 `bound_to_position` 的 `args`，并建议两端设置 `TCP_NODELAY`。
