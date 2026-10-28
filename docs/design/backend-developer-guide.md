# 后端开发者指南：把行为模型编译到目标仿真平台

> 适用：在目标平台的授权环境中实现"原生后端"（把 `normalized_ir.json` 编译为平台自己的行为脚本/行为树并由平台执行）。
> 本指南只描述本项目定义的 IR 语义与验证方法，不涉及任何具体平台的格式。

## 1. 两种接入方式

| 方式 | 决策执行者 | 需要实现 | 验证 |
| --- | --- | --- | --- |
| 联合仿真 | 本项目参考解释器 | 引擎宿主（`IEngine` + tbe-step/1） | `ir protocol-check` + `ir cosim` |
| 原生后端 | 目标平台 | `compile(normalized_ir, resolved_context, catalog_versions)` + 运行轨迹输出 | `ir conformance compare` + `ir judge` |

建议先做联合仿真确认引擎侧物理与观测无误，再做原生后端；两者对同一想定的 L3 结论应一致。

## 2. 编译器输入保证

- 只接受 `draft == false`、`ir_hash` 校验通过的 `normalized_ir.json`，以及同一 `ir_hash` 的 `resolved_context`。
- 所有物理量已是规范单位（m、s、m/s、deg、ratio）；动作与操作数已是规范名称；协同模式已展开为普通的消息、条件、行为、树和转换（带 `generated_from`）。
- 生成物写入 `generated/<backend>/`，文件头写入模型 ID、版本和 `ir_hash`；不得覆盖用户文件。

## 3. 执行语义清单

原生后端必须逐条实现。每条后面给出可直接对照的 L2 用例（见 `ir conformance export` 生成的 `l2_cases.json`）。

| 编号 | 语义 | 对照用例（示例包） |
| --- | --- | --- |
| S1 | 每个决策周期顺序：投递消息 → 评估当前状态的转换 → 执行（可能已切换的）新状态的树 | `leader_contact_preempts_urban_entry`（转换后执行的是 contact 树） |
| S2 | 转换按 `priority` 降序、同优先级按声明顺序评估，第一个为真的触发；兜底转换 `fallback` 指向自身时不重置任何状态 | `leader_stays_in_contact_during_hold` |
| S3 | 切换到不同状态时：清空所有组合节点记忆、中止所有活动行为、记录进入时间（供 `time_in_state_gte`） | `leader_leaves_contact_after_hold` |
| S4 | 带 `synchronize_with` 的转换触发时，发送 `<协同ID>__transition` 消息，字段 `to_state` | `leader_switches_to_column_near_urban`（sent_messages 首项） |
| S5 | `sequence` 每周期从第一个子节点重新评估；`sequence_with_memory` 从上次运行中的子节点继续，失败或全部成功后复位 | `leader_travels_in_wedge`（selected_path 只到第 3 个子节点） |
| S6 | `selector` 每周期从头评估；`selector_with_memory` 从运行中的子节点继续；`priority_selector` 按 fitness 降序（同值保持顺序）；`weighted_random` 选中后保持到该子节点完成 | 单元测试 `test_ir_normalize_l2` |
| S7 | `parallel`：已成功的子节点在本轮内锁存；失败数达到失败策略先判失败，再判成功；未完成时失败的子节点下周期重启 | 单元测试 `test_decorators_and_parallel` |
| S8 | 装饰器：`inverter`/`negator` 互换成功失败；`succeeder` 失败→成功；`repeat_count` 成功累计到 N；`repeat_until_done` 失败重试（可设上限）；`repeat_for_duration` 到时成功 | 同上 |
| S9 | 行为激活：节点被执行且不处于活动态时开始激活，**仅在此时**检查前置条件（失败直接返回失败）；重置行为记忆 | `leader_cannot_move_without_mover`（前置条件不满足，行为失败） |
| S10 | 激活内：先按 `issue_policy` 下发动作（`once_per_activation` 每次激活一次、`once_per_model_lifetime` 全程一次、`every_tick` 每周期），再依次判定 failure → success → `wait` 到时 → 超时 | `leader_travels_in_wedge`（先发消息再判成功）、单元测试 `test_wait_and_timeout_semantics` |
| S11 | 本周期未被执行到的活动行为视为被中止（下次被执行时是新的激活，会重新下发 once 动作） | 单元测试 `test_once_per_activation_and_halting` |
| S12 | `set_blackboard`/`delete_blackboard_value` 立即生效（同一周期后续节点可读到）；外部动作立即交给引擎，完成标志对应最新命令 | `leader_travels_in_wedge`（blackboard） |
| S13 | `send_message` 以字段默认值补齐未给出的字段；`report_contact` 自动填 `contact_range`、`contact_count`（若消息声明了这些字段） | `wingman_reports_contact` |
| S14 | `message_received` 只在消息到达的那个周期为真；`message_value` 保持最近一次的值，未收到时取字段默认值；消息不投递给发送者自己 | `wingman_follows_leader_transition` |
| S15 | 交替跃进相位：按收到的（含自身发送的）`bound_complete` 次数计算；当前移动组 = 相位偶数为 bounding、奇数为 overwatch；组内成员本相位完成后不再移动 | `overwatch_member_waits_its_turn`，示例想定 `contact_bounding_overwatch` |
| S16 | 平台无法提供某个观测量时使用动作/操作数目录中的默认值（如无目标时最近敌距离为无穷大） | `leader_travels_in_wedge`（无 tracks 输入） |
| S17 | `selected_path`：从根节点起，每个组合节点取本周期最后一个被执行的子节点，直到叶节点；用于排查时与平台的执行日志对照 | 所有带 `selected_path` 的用例 |
| S18 | 重同步心跳：`leader_follower` 的长机、`synchronized_transition` 的发起者，在树执行之后检查：若当前状态是某同步转换的目标，且距上次发送 `<协同ID>__transition` 已满 `resync_interval_s`，重发 `{to_state: 当前状态}`；`leader_follower` 同样按间隔重发 `<协同ID>__formation`（字段 `formation` 取 `formation_ref` 当前值）。计时从首个周期开始（单周期 L2 用例不会出现心跳）；本周期已正常发送过则只重置计时；`resync_interval_s = 0` 关闭 | 单元测试 `test_resync_can_be_disabled`、`test_resync_repairs_lossy_synchronization`；示例想定 `partial_comm_loss_transition` |
| S19 | 交替跃进停止条件：设置了 `standoff_distance` 时，`<协同ID>__standoff_reached` = 最近敌距离 ≤ standoff + `standoff_margin_m`（目录默认 10 m）；该条件为真时不再轮到任何组跃进（`my_turn = bounding_turn ∧ ¬standoff`），正在执行的 `bound` 以失败结束。`bound_to_position` 参数 `toward`（`heading` / `nearest_hostile` / 想定航点 ID）与 `stop_short`：朝目标移动 `min(distance, 当前距离 − stop_short)`，已在 stop_short 以内则原地；`nearest_hostile` 但无探测目标时退化为沿航向 | `bounding_stops_at_standoff`、单元测试 `test_bounding_stops_at_standoff_and_aims_at_threat`、`test_bound_target_geometry`；想定 `contact_bounding_overwatch` 期望 `e_standoff_kept` |
| S20 | `report_and_wait` 的确认是广播：接收方每个周期最多发一条 `<协同ID>__ack`，投递给全部报告方实例；同一周期到达多份报告时只确认一次，所有等待中的报告方都据此成功（设计如此，不是丢确认） | `wingman_reports_contact`；示例想定 `contact_bounding_overwatch` |

## 4. 验证流程

```bash
# 1) 导出一致性用例包（含参考结果）
python -m afsim_research_agent.ir conformance export <模型包> --out conformance/

# 2) 在目标平台中逐条回放 l2_cases.json：按 given 设置状态、黑板、观测和消息，
#    执行一个决策周期，把结果按 reference 的字段名写成 results.json：
#    {"ir_hash": "...", "cases": {"<用例ID>": {"next_state": ..., "fired_transition": ...,
#      "tree_status": ..., "selected_path": [...], "issued_actions": [...], "sent_messages": [...],
#      "blackboard": {...}}}}
python -m afsim_research_agent.ir conformance compare --pack conformance/ --results results.json

# 3) 在目标平台运行 L3 想定，按 ir/cosim/trace.py 文件头的格式输出轨迹，然后判定
python -m afsim_research_agent.ir judge <模型包> --scenario <想定ID> --trace run.jsonl
#    与 conformance/l3_reference.json 中内置仿真的测量值对比
```

`next_state` 与 `fired_transition` 必须报告；其余字段报告多少比多少，未报告的字段跳过。

## 5. 可以反馈给本项目的内容

- `compare` 和 `judge` 的输出、轨迹文件（只含状态 ID、位置、消息/动作名称）；
- 用中立方式描述的需求：需要新增的动作、操作数、协同模式、执行时序约定、生成物组织要求。

请勿发送目标平台的脚本、示例、文档原文、类型/接口名称或从中直接翻译的内容。
