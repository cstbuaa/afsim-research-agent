# 能力集语义补充与结构优化（v1 草案）

> 状态：草案（2026-10-30），待确认
> 基于：`capability-catalog-integration-v1.md`（动作集 / 战术集）、`tactical-behavior-ir-v2-layered.md`、`falcon-v3-composition-and-gaps.md` §12（在线结果）

## 0. 问题

Falcon v3 在线运行（作业 035–040，同一需求 12 遍）中，7 遍失败里 5 遍是**转换条件写错**；行为树结构多数与战术集一致。原因是能力集只给出字段的名称、类型和取值，没有回答三件事：

1. 这个字段由谁写入（哪个动作或战术步骤）；
2. 什么时候写入、什么时候有意义（例如某个误差字段只在执行某个站位步骤时更新）；
3. 动作或子树什么时候结束、成功和失败分别意味着什么。

模型只能按名字推测，例如用一个有取值列表、但平台从不写入的字段去判断"集结完成"。静态检查查不出来；二维仿真也只能按我们手写的模型推测，037 那次仿真多写了一个字段，反而让错误的条件通过了。

本地能力集现状（导出后统计）：

| 内容 | 覆盖 |
| --- | --- |
| 动作的读写字段（`blackboard.reads/writes`） | 56 个动作中 6 个（只有运动切片） |
| 动作的完成判据（`success`） | 56 个中 6 个 |
| 战术步骤（无独立契约的动作） | 有 3 个，读写、完成条件全无 |
| 字段的含义说明 | 无 |
| 战术集条目的说明 | 94 个中 10 个 |
| 切片说明 | 空 |
| 任务触发条件的含义 | 只在代理侧对照表（本地 `ir_alignment.toml`），不在能力集中 |

## 1. 目标

- 能力集回答上面三个问题，并且是**结构化**的，程序能据此检查，不只是给模型看的文字；
- 主数据仍在平台工程中维护，导出器带出，Falcon 和编译器使用；
- 不引入原生名称、脚本、路径（导出器的过滤规则不变）。

## 2. 结构扩展（能力集格式 1.1）

### 2.1 字段

```yaml
fields:
  <slice>.target.valid:
    type: boolean
    description: 当前选定的目标是否有效
    written_by: [<slice>.select_target, <slice>.maintain_target, <slice>.clear_target]  # 导出器由动作的 writes 汇总
    updated: while_running        # 写入者运行时持续更新 | on_completion | on_start | always（平台每拍更新）
    initial: false                # 想定开始时的值
  <slice>.line_slot_error_m:
    type: number
    units: m
    description: 僚车到横队站位的距离（不是纵队站位）
    written_by: [<slice>.line_slots]
    updated: while_running
    reset_on_start: 999.0         # 写入者启动时先置为该值（未到位）
    initial: 999.0
  <slice>.shape:
    type: string
    enum: [COLUMN, LINE]
    description: 设计中的队形字段
    written_by: []                # 没有写入者：不能用作条件
```

- `written_by` 由导出器从动作和战术步骤的 `writes` 汇总，不手写；
- `updated` 和 `reset_on_start` 写在动作的 `writes` 上（见 2.2），字段上的是汇总结果；
- 枚举值可以各自带说明：`enum_meaning: {TOO_FAR: 距离大于期望带上限, …}`。

### 2.2 动作与战术步骤

战术步骤（目前"无独立契约"）补成和动作一样的最小契约：

```yaml
- id: <slice>.line_slots
  kind: tactic_step
  description: 指挥僚车占领面向目标的横队站位
  reads: [<slice>.target.valid, <slice>.target.bearing_deg]
  writes:
    - {field: <slice>.line_slot_error_m, updated: while_running, reset_on_start: 999.0}
  result:
    running: 有僚车距站位大于 20 m
    success: 所有僚车距站位不超过 20 m
    failure: {codes: [NO_TARGET], meaning: 没有有效目标}
    persistent: false             # true：一直运行，不会自己结束（如保持位置）
  resources: [...]
```

已有动作契约补齐 `reads / writes / result`；运动切片已有的 `success` 判据保留，并归入 `result`。

### 2.3 战术集条目

```yaml
- id: <slice>.search_for_target
  description: 刷新航迹，在搜索期间选择最近的有效敌方目标
  result: {running: 尚未选到目标, success: 已选定目标, persistent: false}
  no_target_result: RUNNING
  # 以下由导出器汇总，不手写：
  interface: {reads: [...], writes: [...], resources: [...]}
```

### 2.4 任务触发条件

把触发条件的含义从代理侧对照表移进能力集的任务定义：

```yaml
transitions:
  - {from: ASSEMBLE, to: ADVANCE, when: COLUMN_READY,
     meaning: {after_s: 2.0}}
  - {from: DEPLOY, to: TRACK, when: LINE_READY,
     meaning: {expression: "<slice>.line_slot_error_m <= 20 m (两侧)"}}
  - {from: SHUTDOWN, to: COMPLETE, when: CLEANUP_COMPLETE,
     meaning: {tree: success}}
```

`meaning` 的写法与对照表 `[event]` 相同；对照表中已有的内容可以直接迁移。

### 2.5 切片

```yaml
slices:
  - id: <slice>
    description: 一句话说明这个切片提供什么能力、适用什么任务
```

供结构稿阶段按需求选择切片。

## 3. 使用方式

| 使用者 | 用到的内容 | 效果 |
| --- | --- | --- |
| Falcon 提示词 | 字段说明、写入者与更新方式；动作 / 子树的结果语义；触发条件含义 | 模型不必按名字推测 |
| 编译器 / 规范化（新检查） | `written_by`、`updated` | 条件读取的字段在该状态中无人写入时报错（"本状态中没有动作更新该字段"）；`written_by: []` 的字段不能用作条件；树事件只能用在会结束（非 persistent）的状态树上 |
| 二维仿真 | `writes`（更新方式、启动重置值）、`result` | 战术步骤的仿真模型由契约生成，不再手写，也不会写出平台不写的字段 |
| 纠错循环 | 同上 | `explain` 可以直接说明"该字段由 X 写入，本状态没有 X" |

"本状态中有没有写入者"的判定：状态树（含引用子树展开后）中的动作或战术步骤的 `writes`，加上 `updated: always` 的字段（平台每拍更新，如本车位置）。

## 4. 主数据与工作流程

主数据在平台工程中（能力目录、动作契约、黑板定义、战术、任务）；导出器读取并带出。补充步骤：

1. **抽取草稿**（我来做）：从工程的动作实现、战术脚本和适配层中，整理每个字段的写入点、更新时机、启动重置值，以及每个动作 / 战术步骤的结束条件，生成一份草稿（本地文件，不进仓库、不进公网）。
2. **人工确认**（你来做）：逐条核对草稿，尤其是字段含义和结束条件。
3. **写回工程**：确认后写入工程（新增战术步骤的契约文件、补充动作契约、黑板定义加说明）。按以往做法，先备份、比较哈希再写，原文件不覆盖。
4. **导出与检查**：导出器支持格式 1.1，导出时检查一致性：
   - `writes` 中的字段必须在黑板定义里；
   - 条件、触发含义读取的字段必须有写入者，否则列为警告；
   - 字段说明缺失列为警告。
5. **接入**：Falcon 提示词、编译器检查、仿真模型生成依次接入。
6. **回归**：同一需求连跑 5 次，统计 AFSIM 通过率；对比补充前（12 遍通过 5 遍）。

## 5. 分批范围

| 批次 | 切片 | 理由 |
| --- | --- | --- |
| 1 | 编队对目标保持距离、感知与跟踪、运动 | 当前回归任务用到；在线失败集中在这里 |
| 2 | 通信与目标共享、集中式目标分配 | 多车协同任务 |
| 3 | 多武器交战、连—排分层编队 | 其余 |

## 6. 安全

- 草稿和工程文件都在本地；导出器的过滤规则照旧（原生名称、路径、受控标记、平台参数不带出）。
- 字段说明、结束条件只写战术含义（"僚车到横队站位的距离"），不写实现（脚本名、辅助数据键）。
- 补充后的能力集仍属于"公开的形式层"，与现在一样可以交给模型。

## 7. 待确认

1. 主数据放在工程中（推荐），还是先放在代理侧的覆盖文件，验证后再迁回工程？
2. 第 1 批的范围是否合适？
3. 格式版本升到 1.1，导出器保持对 1.0 的兼容（缺少的字段视为"未知"，不报错只警告）。
