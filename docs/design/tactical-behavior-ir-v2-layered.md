# 战术行为模型中间表示 IR 2.0（七层分层）设计稿

> 状态：**设计稿，待确认**（2026-10-29）
> 基于：`tactical-behavior-ir-v1.md`、`capability-catalog-integration-v1.md` §14（动作集 / 战术集）、装甲车工程的七层结构
> 已确认的决定（2026-10-29）：
> - IR 借鉴七层结构；
> - 第 2 层直接使用动作集 ID（如 `movement.move_to_point`）；
> - AFSIM 后端用方案 B：原生 `WSF_STATE_MACHINE` + `advanced_behavior_tree`；
> - 叶节点由后端的通用模板生成，模板放在 `_local`，每个切片用工程已有的想定回归验证。

## 0. 为什么改

IR v1 已覆盖七层的大部分内容，但存在三个问题：

1. **层次压平了。** 可复用的子树和状态专用的树混在 `trees` 里；动作是 IR 自有的 25 个命令，与平台的动作契约（生命周期、失败码、完成判据、资源）脱节。
2. **每次从零生成。** 战术集里已经验证过的子树、任务状态机用不上，模型要重新写一遍。
3. **到 AFSIM 要跨层翻译。** IR 的行为（多条命令加成功/失败表达式）要拆成平台叶节点，映射关系复杂，容易出现"相似但不同"的替代。

v2 让 IR 的层与动作集 / 战术集、与 AFSIM 的原生结构一一对应。模型只写上三层，下面各层由程序引用、推导、校验。

## 1. 七层与 IR 段落

| 层 | IR 2.0 段落 | 谁写 | AFSIM（方案 B）落点 |
| --- | --- | --- | --- |
| 1 能力 | `capability` | 程序 | 引用的切片与能力集版本 |
| 2 原子动作 | 叶节点 `action: <动作集 ID>` + `params` | 模型从动作集中选 | 通用叶节点模板生成的 `advanced_behavior` |
| 3 黑板 | `blackboard` | 程序导入，模型补充 | 平台辅助数据（按工程绑定） |
| 4 仲裁 | `arbitration` | 程序推导，模型只写持有者 | 工程现有的资源租约 |
| 5 战术子树 | `subtrees` | 模型：优先引用战术集，必要时新建 | `advanced_behavior_tree` 的组合节点 |
| 6 状态树 | `state_trees` | 模型 | 每个状态一棵 `advanced_behavior_tree` |
| 7 任务状态机 | `missions` | 模型 | 每角色一个 `WSF_STATE_MACHINE` |
| 验证 | `tests` / `scenarios` | 模型 | L2 在 Python 中；L3 在二维仿真与 AFSIM 中 |

不变的部分：`model`、`provenance`、`assumptions`、`parameters`（含 `basis` 与确认）、`roles`、`formations`、表达式 DSL、测试与想定的写法。

## 2. 各层字段

### 2.1 第 1 层 `capability`（程序填写）

```yaml
capability:
  set: armored_vehicle_afsim_2_9
  content_hash: sha256:…          # 导出能力集的哈希；变化后提示重新核对
  slices: [movement, perception, formation_standoff]
```

规范化时，由程序根据下面各层实际引用的条目求出，模型写了也会被覆盖。

### 2.2 第 2 层：动作只在叶节点上引用

不再有独立的动作表。叶节点直接写动作集 ID 和参数：

```yaml
- {type: action, action: movement.move_to_point,
   params: {latitude_deg: $rally_lat, longitude_deg: $rally_lon, target_speed_mps: 8}}
```

规范化时的校验：

- **ID 必须在能力集中**，并适用于角色的平台类别；不在能力集中的写进 `needs`，成为开发缺口（`falcon_sheet` 来源，编号规则不变）。
- **参数按动作契约检查**：必填、类型、单位、范围。单位统一换算，`$name` 引用 `parameters`。
- **生命周期、失败码、完成判据和资源取自契约**，不由模型描述。v1 中行为的 `success` / `failure` 表达式在 v2 中不再需要。
- **战术步骤**（只在某个战术内部实现、没有独立契约，如 `formation.target_line_slots`）可以引用，判为部分可用，并带出说明。

### 2.3 第 3 层 `blackboard`

- **导入**：用到的切片的黑板字段自动导入，例如 `perception.selected_target.valid`、`movement.derived.range_to_target_m`。
- **自定义**：模型只声明本战术新增的字段，放在 `mission.*` 命名空间：

```yaml
blackboard:
  mission.contact_reported: {type: bool, default: false}
```

- **条件写成黑板字段上的表达式**，沿用 v1 的 DSL，操作数改为 `bb:`：

```yaml
conditions:
  - id: target_in_band
    expression: {all: [{bb: perception.selected_target.valid},
                       {compare: {left: {bb: perception.selected_target.range_m}, op: lte,
                                  right: {param: range_band_max}}}]}
```

- **v1 操作数的兼容**：`track_value.current_target_valid` 这类 v1 操作数通过对照表（`ir_alignment.toml` 新增 `blackboard` 键）换成对应的黑板字段。没有对应字段的，按能力缺口处理。
- **函数**（`time_in_state_gte` 等）保留，由运行时提供。

### 2.4 第 4 层 `arbitration`（程序推导）

- 每个动作叶节点的资源要求取自契约，例如运动独占、传感器控制独占。
- **请求编号**按"平台.状态.叶节点"固定生成，持有者为角色；同一状态内重复执行算续租，切换状态时先取消旧请求。
- **模型只需写例外**：同一资源需要跨状态保持时，写 `arbitration.hold: [{resource: sensor_control, from: SEARCH, to: TRACK}]`。
- **静态检查**：
  - parallel 的两个分支争用同一独占资源 → 报错；
  - 状态切换时没有释放路径 → 由程序补上，并在报告中注明。

### 2.5 第 5 层 `subtrees`

```yaml
subtrees:
  - id: standoff_track                       # 新建
    root:
      type: sequence
      children:
        - {type: tactic, tactic: perception.track_selected_target}   # 引用战术集
        - {type: action, action: formation.command_standoff}
        - {type: action, action: formation.target_line_slots}
```

- **节点类型**与 AFSIM 2.9 原生节点一一对应（语义以原生为准，见 §7）：
  - 组合：`sequence`、`sequence_with_memory`、`selector`、`selector_with_memory`、`priority_selector`、`weighted_random`、`parallel`（只有成功策略：`one` / `all` / `threshold`）；
  - 装饰：`inverter`、`succeeder`、`negator`、`repeat`（`count` / `until_done` / `duration_s` 三选一）；
  - 叶：`condition`、`action`；引用：`tactic`（战术集子树）、`subtree`（本模型子树）。
- 原生没有的写法（超时、失败策略、带条件的重复）不进 IR；需要时用条件节点加 selector 表达。
- **优先引用战术集**：生成前先用 `find_tactics` 检索；引用时只能覆盖战术集子树公开的参数，不能改结构。
- **新建子树**只能由动作、条件、战术集子树和本模型子树组成，不能嵌入脚本。

### 2.6 第 6 层 `state_trees`

```yaml
state_trees:
  leader:
    SEARCH_ADVANCE: search_advance
    STANDOFF_TRACK: standoff_track
  wing:
    SEARCH_ADVANCE: column_follow
    STANDOFF_TRACK: line_follow
```

- 每个状态映射到一个子树：本模型子树或战术集子树。
- 一个状态没有映射时，规范化给出错误；终止状态不需要映射。

### 2.7 第 7 层 `missions`

```yaml
missions:
  leader:
    initial_state: ASSEMBLE
    states: [ASSEMBLE, SEARCH_ADVANCE, CONTACT_DEPLOY, STANDOFF_TRACK, TARGET_LOST_HOLD, SHUTDOWN, COMPLETE]
    terminal: [COMPLETE]
    transitions:
      - {from: SEARCH_ADVANCE, to: CONTACT_DEPLOY, on: target_acquired, priority: 10}
      - {from: STANDOFF_TRACK, to: TARGET_LOST_HOLD, on: target_lost}
    global: [{to: SHUTDOWN, on: cancelled}]
```

- 结构与工程的 `missions/*.fsm.yaml` 一致：
  - `on` 引用条件或事件（计时、消息、外部取消）；
  - 一个状态有多条转换时按 `priority` 排序。
- **必须有安全终止路径**：每个非终止状态都要能到达 `SHUTDOWN` 或等效的安全状态，否则规范化报错。
- **可以整体引用战术集的任务状态机**：`missions.leader: {tactic: formation_standoff.formation_standoff_tracking}`，只覆盖参数。

### 2.8 角色与协同

- `roles` 不变：类别、数量、上下级。
- `formations`、`coordination` 保留：
  - 已有切片能实现的协同（分层编队的命令发布与槽位采用、目标共享）改为引用对应动作；
  - 不能实现的协同（交替跃进轮次）作为缺口登记。
- 首期 AFSIM 中，凡未经通信切片实现的协同都按共享黑板处理，并作为假设写入报告（见 AFSIM 交接文档 §6.3）。

## 3. 模型写什么、程序做什么

| 内容 | 模型 | 程序 |
| --- | --- | --- |
| 任务状态机、状态树 | 写 | 校验可达性、安全路径 |
| 子树 | 引用或新建 | 校验节点、展开引用 |
| 动作 | 选 ID、给参数 | 契约校验、单位换算、资源推导 |
| 黑板 | 声明新增字段、写条件 | 导入切片字段、检查读写 |
| 仲裁 | 只写例外 | 推导持有、续租、释放 |
| 能力 | — | 记录版本、核对缺口 |
| 参数、假设、测试、想定 | 写 | 确认流程与 L1–L3 |

## 4. 规范化与兼容

- **内部模型**：v2 规范化为内部模型。叶节点为"动作集动作 + 参数"，带契约信息；条件为黑板表达式。L2 单步测试、L3 二维仿真都改为在内部模型上运行。
- **二维仿真要执行动作集的动作**：
  - 首期实现运动和感知两个切片，语义取自契约，包括完成判据、失败码、资源；
  - 其他切片先按能力组的简化方式给出理想化语义，并在报告中标出。
- **v1 兼容**：
  - v1 模型包继续可用；
  - `ir migrate`：用对照表把 v1 命令换成动作集 ID（如 `move_to_position` → `movement.move_to_point`），v1 的 `trees` 拆成子树和状态树；
  - 换不过去的写进缺口清单。
- **Falcon 战术稿**：
  - Python 行为树写法保留，叶节点改为带命名空间的调用，如 `movement.move_to_point(latitude_deg=…, …)`，战术集子树用 `tactic("perception.track_selected_target")`；
  - 提示词按需求检索相关切片的条目和候选战术，不再列出 IR 自有的 25 个命令。

## 5. AFSIM 后端（方案 B）

| IR 2.0 | AFSIM 2.9 |
| --- | --- |
| `missions.<role>` | 每个角色实例一个 `WSF_STATE_MACHINE` 处理器；转换按优先级生成 |
| `state_trees` + `subtrees` | 每个状态一棵 `advanced_behavior_tree`；战术集子树按引用展开 |
| 组合与装饰节点 | 同名原生节点；`repeat` → `decorator repeater repeat <n> / until_done / for <时长>`；`parallel` → `success_policy` |
| 动作叶节点 | 通用叶节点模板生成的 `advanced_behavior`（§5.1）；运动切片直接用工程已有的原生叶节点 |
| 条件叶节点 | 只读取绑定黑板字段的条件叶节点 |
| 黑板 | 按工程绑定换成平台辅助数据的名称 |
| 仲裁 | 工程的资源租约：申请、续租、取消、释放 |

### 5.1 通用叶节点模板

每个动作的本地绑定（`_local/afsim/bindings/actions.*`）给出五项：

```text
start    首次执行：申请资源并下达命令（公开接口）
renew    后续执行：续租，必要时重下命令
cancel   被中断或状态切换：取消并释放
status   执行黑板中的状态字段 → 运行 / 成功 / 失败
request  请求编号字段（平台.状态.叶节点）
```

模板逻辑固定：

1. 首次执行时调用 `start`；之后每次调用 `renew`。
2. 读 `status`：运行中返回 Running；成功返回 Success；失败或已取消返回 Failure，并带回失败码。
3. 被上层中断时调用 `cancel`。

叶节点不包含任何战术逻辑。动作的完成判据都在工程的公开接口中实现。

### 5.2 用工程自己的七层做回归

工程的战术集本身就是七层结构的 v2 等价物，可以直接作为编译器的回归输入：

```text
战术集（某切片的任务状态机 + 状态树 + 子树）
  → 转成 IR 2.0（程序完成，无需模型）
  → AFSIM 编译器 → mission.exe
  → 与该切片手写实现的运行证据对照（状态序列、关键事件、测试标记）
```

- **首期**：先跑运动和感知（`single_vehicle_perception`），再跑三车与目标保持距离（`formation_standoff_tracking`）。
- **之后**：每补一个切片的叶节点，就用该切片的任务状态机做一次回归。
- **意义**：这样验证的是"编译器加叶节点模板"本身，与模型生成的质量无关。

## 6. 校验与错误码（新增）

| 编码 | 含义 |
| --- | --- |
| `ACT001` | 动作 ID 不在能力集中（写入 needs 或换用已有动作） |
| `ACT002` | 动作参数不符合契约（缺必填、类型、单位、范围） |
| `TAC001` | 引用的战术集子树或任务状态机不存在，或覆盖了未公开的参数 |
| `BB001` | 条件读取了未导入、也未声明的黑板字段 |
| `ARB001` | 并行分支争用同一独占资源 |
| `FSM001` | 非终止状态没有到达安全状态的路径 |
| `FSM002` | 状态没有对应的状态树 |

## 7. AFSIM 2.9 行为树语义（已用本机文档和探针确认）

2026-10-29 在本机 2.9 上用 mission.exe 跑了四轮通用语法探针（只含通用节点，不含工程名称），结论如下。工程叶节点的具体写法和绑定写入 `_local` 的模板说明，不进仓库。

| 节点 | 原生语义 |
| --- | --- |
| `sequence` / `selector` | 无记忆，每拍从第一个子节点重来；前置条件为假视为失败 |
| `*_with_memory` | 从上拍 Running 的子节点继续 |
| `priority_selector` / `weighted_random` | 子节点前置条件返回的数值即优先级 / 权重 |
| `parallel` | 每拍重跑全部子节点（含已成功的）；达到成功策略即成功，全部结束仍未达到即失败；默认 `succeed_on_one`；没有失败策略 |
| `inverter` | 成功、失败互换 |
| `succeeder` / `negator` | 恒成功 / 恒失败 |
| `repeat count` | 每拍执行一次子节点，n 次后成功；子节点失败也计数 |
| `repeat until_done` | 子节点返回成功或失败即结束，返回成功 |
| `repeat duration_s` | 时长内每拍执行子节点，到时成功 |
| 树根 | 默认 parallel，可用 `root_node_type` 改 |

其他结论：

- 叶节点：`precondition` 返回布尔或数值；`execute` 返回 Running / Success / Failure；`on_new_execute` 在每次新激活时执行一次；叶节点私有变量跨拍保留。没有可靠的中断回调，取消放在状态的 `on_exit`。
- 状态机：每个状态内挂一棵树；`next_state` 按书写顺序求值，第一个为真者生效；状态机在处理器 `on_update` 中每拍求值一次，树在同一拍执行。
- **内部仿真须改为原生语义**：IR v1 运行时的 negator（等同 inverter）、succeeder（透传 Running）、repeat_count / repeat_for_duration（子节点失败即失败）、repeat_until_done（带条件和次数上限）、parallel（记住已成功子节点、有失败策略）与原生不同。第 3 步统一到上表，v1 模型由 `ir migrate` 转换，原生无法表达的写法给出迁移错误。

## 8. 实施步骤

| 步 | 内容 | 验收 |
| --- | --- | --- |
| 1 | 读工程原生行为树写法，提取模板，写入 `_local` | 已完成（2026-10-29）：§7 有结论，模板说明在 `_local` |
| 2 | IR 2.0 schema、规范化、错误码；对照表新增 `blackboard` 键 | 单元测试；战术集的 6 个任务状态机都能无模型转成 IR 2.0 并通过 L1 |
| 3 | 内部模型上的 L2，运动、感知的二维仿真语义；运行时节点语义改为原生（§7） | 现有 L2 测试迁移通过；单车感知任务的 L3 通过；节点语义测试与探针结果一致 |
| 4 | AFSIM 编译器与通用叶节点模板（运动 + 感知）、运行器、评测器 | 单车感知回归与手写版一致 |
| 5 | 三车与目标保持距离：战术集回归，再由需求经 Falcon 生成 | 两条路径都与手写版一致 |
| 6 | Falcon 战术稿 v3（命名空间调用、战术集引用、按切片检索）；`ir migrate` | 装甲排在线生成通过 |
| 7 | 其余切片逐个补叶节点并回归 | 每个切片用其任务状态机回归通过 |
