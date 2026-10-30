# 战术行为模型中间表示 IR 2.0（七层分层）设计稿

> 状态：**已确认**（2026-10-29）；第 1–4 步已实现（`src/afsim_research_agent/ir/v2/`）
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
                       {compare: {left: {bb: perception.selected_target.range_m}, operator: lte,
                                  right: {parameter_ref: range_band_max}}}]}
```

- **v1 操作数的兼容**：`track_value.current_target_valid` 这类 v1 操作数通过对照表（`ir_alignment.toml` 新增 `blackboard` 键）换成对应的黑板字段。没有对应字段的，按能力缺口处理。
- **函数**：首期只保留 `time_in_state_gte`，由运行时提供。
- **事件**：不是黑板条件的转换触发写在 `events`，来源为 `tactic`（战术集任务状态机发出，带原文 `text`）、`timer`（`after_s`）、`message` 或 `external`（如外部取消）。转换的 `on` 只能引用条件或事件。

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

- 按角色写：`state_trees.<角色>.<状态>`。每个状态映射到一个子树：本模型子树，或战术集的子树 / 状态树。
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
- **安全状态**：`safe` 默认取 `SHUTDOWN`（存在时）；`global` 转换对所有非终止状态生效。
- **可以整体引用战术集的任务状态机**：`missions.leader: {tactic: <战术集任务 ID>, role: <战术集角色>, params: {…}}`，只能覆盖该任务公开的参数。展开后的状态树映射可在 `state_trees` 中补充或替换。

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

### 4.1 实现（第 2 步）

| 模块 | 内容 |
| --- | --- |
| `ir/v2/models.py` | 结构模型；`ir schema --ir-version 2.0` 生成 `ir/schema/behavior_spec_v2.schema.json` |
| `ir/v2/contracts.py` | 从能力集取动作契约、战术集条目、切片黑板与资源；读取对照表的 `[blackboard]` 键 |
| `ir/v2/normalize.py` | 规范化与 L1：契约与单位、战术集展开、黑板读取、租约推导、并行资源冲突、状态机安全路径 |
| `ir/v2/from_tactic.py` | 战术集任务状态机 → IR 2.0：按 `role == X` 拆角色，按显式、按角色或按名称找状态树，触发条件转为 `tactic` 事件 |

内部模型的要点：

- 节点编号为"角色.状态.路径"，即动作叶节点的请求编号。
- 每个状态列出租约：独占资源、退出时释放的资源，以及保留的资源。契约写明终止后不释放的资源（如已选目标）会被保留，模型用 `arbitration.hold` 声明的也会保留。
- 战术集内容按原样展开，并标出来源。其中的 `timeout` 等工程写法只在战术集内部出现，不是 IR 节点；编译时按工程原生实现处理。

命令行：

```text
python -m afsim_research_agent.ir v2-check <behavior_spec.yaml> [--capability-set DIR] [--out normalized.json]
python -m afsim_research_agent.ir v2-from-tactic (<任务 ID> … | --all) [--capability-set DIR] [--out 新目录]
```

### 4.2 运行时与二维仿真（第 3 步）

| 模块 | 内容 |
| --- | --- |
| `ir/v2/runtime.py` | 每个角色实例：先执行当前状态的树，再求值转换（一拍最多一次，按优先级）；节点语义按 §7；动作叶节点按契约申请、续用、释放资源，失败的请求不保留所有权 |
| `ir/v2/sim/world.py` | 平面二维世界：车辆运动学（加速度、转向率）、传感器（开关延迟一拍、理想探测）、敌方目标出现 / 消失 |
| `ir/v2/sim/actions.py` | 通用动作模型：到点、沿路线、保持位置、定速、停车、转向；开 / 关传感器、更新航迹、选最近目标、维持目标、清除目标。完成判据取自契约的 `success` |
| `ir/v2/sim/run.py` | L3 想定（车辆、敌方、外部事件、故障、期望）与 L2 单步测试 |

仓库代码不含平台名称。能力集与通用模型的对应写在本机对照表中：

- `[sim."<动作 ID>"]`：用哪个通用模型；
- `[sim_fields]`：模型的通用键对应哪个黑板字段；
- `[tactic_condition."<名称>"]`：战术集条件的含义；
- `[event."<任务 ID>"]`：任务触发条件的含义，可以是外部事件、黑板表达式、当前树的结果或计时。

没有模型的动作按理想化处理（立即成功），没有含义的触发不会生效，两者都会在结果中列出。

命令行：

```text
python -m afsim_research_agent.ir v2-test <behavior_spec.yaml>
python -m afsim_research_agent.ir v2-simulate (<behavior_spec.yaml> | --tactic <任务 ID>) [--scenario-file F] [--scenario ID]
```

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

### 5.3 编译器、运行与评测（第 4 步）

| 模块 | 内容 |
| --- | --- |
| `ir/v2/afsim/compiler.py` | 每个角色一个 `WSF_STATE_MACHINE`，每个状态一棵 `advanced_behavior_tree`；组合与装饰节点一一对应；同一目标的转换合并为一个 `next_state`；状态 `on_exit` 取消运行中的叶节点并按租约释放资源 |
| `ir/v2/afsim/binding.py` | 后端绑定与运行配置的格式（两者都放在工程一侧，不进仓库） |
| `ir/v2/afsim/evaluate.py` | 调用 mission.exe，按 `IR_STATE` 行取状态序列，与期望及手写版运行证据对照，给出每次转换的时间差 |

动作叶节点的两种生成方式（由绑定决定）：

- **脚本叶节点**：绑定给出调用工程公开接口的脚本，返回 `RUNNING` / `SUCCESS` / `FAILURE:<失败码>`。编译器把它包成函数和叶节点，并记录运行状态，供退出状态时取消。
- **工程叶节点**：沿用工程已有的叶节点。编译器在它前面加一个输入叶节点，二者组成一个 sequence。输入叶节点每次新激活时发一个新的请求编号；上一个请求结束后，再被执行就视为新激活（与原生无记忆语义一致）。

与 §2.4 的差异：请求编号按"模型_角色"每个实例一个，而不是每个叶节点一个。原因是工程的感知接口用同一请求续租和释放资源（例如选目标后由维持目标续用）。工程叶节点每次激活仍用独立的请求编号。

外部事件由运行文件在指定时刻写入平台辅助数据，触发它的转换会把它清掉。战术集触发条件按对照表的含义编译：外部事件、黑板表达式、当前树的结果（在树外包一层记录结果的节点）或计时。

命令行：

```text
python -m afsim_research_agent.ir v2-afsim compile (<spec> | --tactic <任务 ID>) --binding B.toml --run R.toml --out <新目录>
python -m afsim_research_agent.ir v2-afsim run <包目录> [--mission mission.exe]
python -m afsim_research_agent.ir v2-afsim judge <包目录>
```

### 5.4 战术集树的读法与编队回归（第 5 步）

工程战术集里的树是描述性的，编译时按下面的读法展开：

- `sequence` / `selector` 读作会停在运行中子节点的版本（`*_with_memory`）。工程动作要跨多拍完成，若每拍从头执行，同一动作会被反复重新下达（实测：路线动作每拍重新分配路线）。
- 标了 `no_target_result: RUNNING` 的子树，在没有目标时保持运行而不是失败。编译为 selector[子树, 返回 Running 的叶节点]。

绑定新增的通用钩子（都在本机绑定中使用）：

| 钩子 | 用途 |
| --- | --- |
| `meta.scripts` | 绑定自带的辅助脚本（例如按 `formation_leader` 找僚车） |
| `{p?:<参数>}` | 叶节点没给参数时，读平台辅助数据 `ir_param_<参数>`（战术集叶节点没有参数） |
| `enter` / `exit` | 使用该动作的状态在进入 / 退出时执行（例如重置站位误差、取消僚车运动） |
| `[external]` | 外部事件的另一种来源（例如想定脚本写入的结束请求标志） |

编队与目标保持距离的结果（工程手写想定，换上生成的长车状态机）：

| 路径 | 结果 |
| --- | --- |
| 按 IR 2.0 编写的模型（用 parallel 表达"边走边搜"和"原地等待时重新获取"，引用战术集的感知子树） | 状态序列与手写版相同；集结、展开、各转换时刻相差 0–5 s；想定驱动的接近、后撤、传感器丢失、重新获取、结束清理标记全部出现，清理检查为真 |
| 战术集任务原样转换 | 停在搜索前进：工程路线叶节点以想定给出的终点判定完成，地面运动在终点前约 10 m 停下，始终判为未完成 |

即使路线完成判定解决，战术集原样转换后仍有两处结构问题，需要在工程战术集中修改：

- 搜索前进子树把"沿路线前进"和"搜索"写成先后顺序，而手写版是边走边搜；
- 目标丢失等待子树把"保持位置"（一直运行）放在"重新获取"之前，重新获取永远轮不到执行。

把这两处顺序改为 parallel，即可与编写模型的结构一致。

**2026-10-30 工程战术集修改与复测**（经用户同意，原文件备份在 `_local\lab_backup\20261030\`）：

| 子树 | 修改 |
| --- | --- |
| 搜索前进 | parallel（全部成功）：路线（完成后保持成功，不再重复下达）、保持纵队、搜索 |
| 目标丢失等待 | parallel（一个成功即可）：保持位置、重新获取 |
| 恢复搜索 | parallel（全部成功）：保持纵队、搜索 |
| 保持距离 | parallel（全部成功）：维持目标、长车保持距离、僚车横队站位 |

导出器同时保留 parallel 的 `success_policy` / `threshold` 与 repeat 的 `until_done` / `count`。复测结果：工程自带的编队与感知测试通过；战术集原样转换后在手写想定中状态序列与手写版一致，接近、后撤、丢失、重新获取、清理标记全部出现。展开早 10 s，这是因为手写版在进入搜索前进 10 s 后才开雷达，战术集没有这个延迟。之后各转换比手写版早 4 s。单车感知仍与手写版完全一致。

## 6. 校验与错误码（新增）

| 编码 | 含义 |
| --- | --- |
| `ACT001` | 动作 ID 不在能力集中（写入 needs 或换用已有动作） |
| `ACT002` | 动作参数不符合契约（缺必填、类型、单位、范围） |
| `TAC001` | 引用的战术集子树或任务状态机不存在，或覆盖了未公开的参数 |
| `BB001` | 条件读取了未导入、也未声明的黑板字段 |
| `ARB001` | 并行分支争用同一独占资源 |
| `FSM001` | 非终止状态没有到达安全状态的路径 |
| `FSM002` | 状态没有对应的状态树，或状态树映射了不存在的状态 |

沿用 IR 1.0 的 `IR001`/`IR002`（版本、结构）、`ID001`、`REF001`（条件、事件、子树、参数引用）、`TYPE001`、`UNIT001`、`TREE001`（子节点数、递归、重复设置）、`ROLE001`。

## 7. AFSIM 2.9 行为树语义（已用本机文档和探针确认）

2026-10-29 在本机 2.9 上用 mission.exe 跑了四轮通用语法探针（只含通用节点，不含工程名称），结论如下。工程叶节点的具体写法和绑定写入 `_local` 的模板说明，不进仓库。

| 节点 | 原生语义 |
| --- | --- |
| `sequence` / `selector` | 无记忆，每拍从第一个子节点重来；前置条件为假视为失败 |
| `*_with_memory` | 从上拍 Running 的子节点继续 |
| `priority_selector` / `weighted_random` | 子节点前置条件返回的数值即优先级 / 权重 |
| `parallel` | 每拍重跑全部子节点（含已成功的）；达到成功策略即成功，全部结束仍未达到即失败；默认 `succeed_on_one`；没有失败策略 |
| `inverter` | 成功、失败互换 |
| `succeeder` / `negator` | 恒成功 / 恒失败，实测**不执行**子节点 |
| `repeat count` | 每拍执行一次子节点，n 次后成功；子节点失败也计数；完成后保持成功，不再执行子节点 |
| `repeat until_done` | 子节点返回成功或失败即结束，返回成功 |
| `repeat duration_s` | 时长内每拍执行子节点，到时成功 |
| 树根 | 默认 parallel，可用 `root_node_type` 改 |

其他结论：

- 叶节点：`precondition` 返回布尔或数值；`execute` 返回 Running / Success / Failure；`on_new_execute` 在每次新激活时执行一次；叶节点私有变量跨拍保留。没有可靠的中断回调，取消放在状态的 `on_exit`。
- 状态机：每个状态内挂一棵树；`next_state` 按书写顺序求值，第一个为真者生效；**同一目标状态只能写一个 `next_state`**（重复会报 "Duplicate next_state"）。
- 一次 `STATE_MACHINE.Evaluate`：先执行当前状态的树，再求值转换；发生转换时依次执行 `on_exit`、新状态的 `on_entry`，并在**同一拍**继续执行新状态的树、求值它的转换，直到停在某个状态（探针 t01 / t02）。
- 初始状态的 `on_entry` 不会执行，所以编译时加一个 `IR_BOOT` 状态，第一拍立即转入初始状态。
- **只有 precondition 的节点永远返回失败**，即使 precondition 为真（探针 w01–w05，与文档"条件节点只返回成功或失败"的说法不同）。编译器给条件叶节点加 `execute return Success();`，这样条件为真时返回成功。
- parallel 的第一个子节点返回 Running 时，其余子节点照常执行（探针 v01–v03）。
- **内部仿真须改为原生语义**：IR v1 运行时的 negator（等同 inverter）、succeeder（透传 Running）、repeat_count / repeat_for_duration（子节点失败即失败）、repeat_until_done（带条件和次数上限）、parallel（记住已成功子节点、有失败策略）与原生不同。第 3 步统一到上表，v1 模型由 `ir migrate` 转换，原生无法表达的写法给出迁移错误。

## 8. 实施步骤

| 步 | 内容 | 验收 |
| --- | --- | --- |
| 1 | 读工程原生行为树写法，提取模板，写入 `_local` | 已完成（2026-10-29）：§7 有结论，模板说明在 `_local` |
| 2 | IR 2.0 schema、规范化、错误码；对照表新增 `blackboard` 键 | 已完成（2026-10-29）：单元测试；6 个任务状态机无模型转换后全部通过 L1 |
| 3 | 内部模型上的 L2，运动、感知的二维仿真语义；运行时节点语义改为原生（§7） | 已完成（2026-10-29）：节点语义测试逐条复现探针输出；单车感知任务 3 个 L3 想定全部通过，无理想化动作、无未定义触发 |
| 4 | AFSIM 编译器与通用叶节点模板（运动 + 感知）、运行器、评测器 | 已完成（2026-10-29）：单车感知任务编译后在工程手写版的想定中运行，状态序列与 6 次转换的时刻都与手写版一致（误差 0 s）；运动叶节点另用到点停车模型验证 |
| 5 | 三车与目标保持距离：战术集回归，再由需求经 Falcon 生成 | 战术集路径与编写模型路径均已通过（2026-10-30）：状态序列与手写版一致，想定标记全部出现，清理检查为真；工程战术集按 §5.4 修改并重新导出；Falcon 路径待第 6 步 |
| 6 | Falcon 战术稿 v3（命名空间调用、战术集引用、按切片检索）；`ir migrate` | 装甲排在线生成通过 |
| 7 | 其余切片逐个补叶节点并回归 | 每个切片用其任务状态机回归通过 |
