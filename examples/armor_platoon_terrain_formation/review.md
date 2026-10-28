# 审查报告：装甲排地形自适应编队（示例）

**状态：** ✅ 已通过 L1/L2/L3，生成物已输出

- 模型：`armor_platoon_terrain_formation` 0.1.0（意图 `formation_transition`）
- 角色：platoon_leader×1, wingman×3
- IR 哈希：`sha256:3a1e881cc3b05d393137f936c99aa0dfedd6d2fc234485a0e98386ab6d44f376`

## 1. 待确认参数与规则

无。

## 2. 推断规则

无。

## 3. 冲突证据

无。

## 4. 假设

| ID | 内容 | 影响 | 需确认 | 状态 |
| --- | --- | --- | --- | --- |
| `assumption_comms` | 排内所有车辆具备可用的语音/数据通信。 | coord_leader_follower, coord_contact_report | 是 | 已确认 |

另有 23 项为用户指定值（无需证据，但请确认取值是否合适）。

## 5. 校验结果

错误 0，警告 1。

| 级别 | 代码 | 位置 | 说明 |
| --- | --- | --- | --- |
| warning | CAP002 | `capability` | 3 项平台能力缺口未处理，生成 AFSIM 后端前需处理（见 capability_gaps.yaml） |

## 6. L2 单步语义测试

通过 15/15。

| 测试 | 角色 | 结果 | 失败原因 |
| --- | --- | --- | --- |
| `leader_travels_in_wedge` | platoon_leader | ✅ | — |
| `leader_switches_to_column_near_urban` | platoon_leader | ✅ | — |
| `leader_returns_to_open` | platoon_leader | ✅ | — |
| `leader_contact_preempts_urban_entry` | platoon_leader | ✅ | — |
| `leader_contact_in_urban` | platoon_leader | ✅ | — |
| `leader_leaves_contact_after_hold` | platoon_leader | ✅ | — |
| `leader_stays_in_contact_during_hold` | platoon_leader | ✅ | — |
| `leader_cannot_move_without_mover` | platoon_leader | ✅ | — |
| `leader_acknowledges_report` | platoon_leader | ✅ | — |
| `wingman_keeps_station` | wingman | ✅ | — |
| `wingman_follows_leader_transition` | wingman | ✅ | — |
| `wingman_reports_contact` | wingman | ✅ | — |
| `wingman_urban_contact` | wingman | ✅ | — |
| `bounding_stops_at_standoff` | wingman | ✅ | — |
| `overwatch_member_waits_its_turn` | wingman | ✅ | — |

转换覆盖：
- platoon_leader：已覆盖 7，未覆盖 无
- wingman：已覆盖 5，未覆盖 无

## 7. L3 闭环想定（内置二维仿真）

### ✅ `open_to_urban_transition`

追溯：open_to_urban, coord_leader_follower, wedge, column；事件时间：{'leader_near_urban': 278.0}

| 期望 | 观测量 | 结果 | 实测 | 阈值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `e_followers_switch` | role_state | ✅ | 280.0 | travel_urban | first true at 280.0s, deadline 308s |
| `e_wedge_kept` | formation_error_rms | ✅ | 7.963 | 30.0 | mean over [0, 150]s |
| `e_column_kept` | formation_error_rms | ✅ | 9.001 | 30.0 | mean over [280, 400]s |
| `e_no_collisions` | min_interval | ✅ | 45.833 | 10.0 | min over [0, 481]s |

### ✅ `contact_bounding_overwatch`

追溯：open_to_contact, coord_bounding, coord_contact_report；事件时间：{'hostile_appears': 60.0}

| 期望 | 观测量 | 结果 | 实测 | 阈值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `e_leader_contact` | role_state | ✅ | 139.0 | contact | first true at 139.0s, deadline 180s |
| `e_wingmen_contact` | role_state | ✅ | 140.0 | contact | first true at 140.0s, deadline 210s |
| `e_report_sent` | messages_sent | ✅ | 3 | 1 | count |
| `e_report_not_repeated` | messages_sent | ✅ | 3 | 3 | count |
| `e_bounds_alternate` | messages_sent | ✅ | 8 | 4 | count |
| `e_wingmen_bound` | actions_issued | ✅ | 7 | 2 | count |
| `e_standoff_kept` | hostile_range_min | ✅ | 795.258 | 775.0 | min over [0, 601]s |

### ✅ `partial_comm_loss_transition`

追溯：open_to_urban, coord_leader_follower, column；事件时间：{'lossy_net': 0.0, 'leader_near_urban': 278.0}

| 期望 | 观测量 | 结果 | 实测 | 阈值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `e_followers_switch_lossy` | role_state | ✅ | 290.0 | travel_urban | first true at 290.0s, deadline 323s |
| `e_column_kept_lossy` | formation_error_rms | ✅ | 4.807 | 30.0 | mean over [290, 410]s |

### ✅ `comm_loss_during_transition`

追溯：open_to_urban, coord_leader_follower；事件时间：{'jam': 0.0}

| 期望 | 观测量 | 结果 | 实测 | 阈值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `e_leader_switches` | role_state | ✅ | 279.0 | travel_urban | first true at 279.0s, deadline 360s |
| `e_wingmen_stay` | role_state | ✅ | None | travel_open | held for the whole window |
| `e_transition_attempted` | messages_sent | ✅ | 72 | 1 | count |
| `e_spacing_safe` | min_interval | ✅ | 135.657 | 10.0 | min over [0, 361]s |

> 内置仿真为简化二维运动学，用于检查协同逻辑与时序，不代表车辆动力学或武器效果；
> 战术效果仍需人工审查。

## 8. 平台能力核对

结论：**需确认**；缺口 3 项，未处理 3 项。缺口不影响 L1/L2/L3 与现有后端，生成 AFSIM 后端前需在 `capability_decisions.yaml` 中逐项处理（confirmed / alternative / pending_plugin）。

核对粒度：粗粒度：只精确核对核心能力组，其余缺失或未确认时简化处理。目录 `afsim_2_9_capabilities`（sha256:47707da18c2a…）。

| 缺口 | 能力 | 类型 | IR 元素 | 角色 | 行为树节点 | 处理 |
| --- | --- | --- | --- | --- | --- | --- |
| `cap-gap-b0dcee2305` | 路线导航 · 走完路线 | 需确认 | mover_value.route_complete | platoon_leader | leader_move_open, leader_move_urban | 未处理 |
| `cap-gap-4935405097` | 地形类别 · 当前地形类别 | 需确认 | terrain_value.class | platoon_leader | platoon_leader_transitions | 未处理 |
| `cap-gap-d6c37285a4` | 地形类别 · 到某类地形的距离 | 需确认 | terrain_value.distance_to_class | platoon_leader | platoon_leader_transitions | 未处理 |

**简化处理 5 项**（不计为缺口；需要时在 `capability_decisions.yaml` 的 `refine` 中加入该能力组即可精确核对）

| IR 元素 | 能力组 | 简化方式 | 角色 |
| --- | --- | --- | --- |
| `report_contact` | 通信报告 | 理想通信：消息即时送达、不丢失 | wingman |
| `send_message` | 通信报告 | 理想通信：消息即时送达、不丢失 | platoon_leader, wingman |
| `standoff_reached` | 观察发现 | 理想观察：探测范围内的目标直接可见，敌我属性取仿真真值 | platoon_leader, wingman |
| `time_in_state_gte` | 任务控制 | 由行为模型运行时提供（状态机、计时、黑板变量） | platoon_leader |
| `track_value.nearest_hostile_range` | 观察发现 | 理想观察：探测范围内的目标直接可见，敌我属性取仿真真值 | platoon_leader, wingman |

<details><summary>逐项对照</summary>

| IR 元素 | 类别 | 结论 | 目录条目 | 角色 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `blackboard` | blackboard | 部分可用 | behavior_variables:execution_state.variable | platoon_leader, wingman | — |
| `bound_to_position` | action | 可实现 | point_navigation | platoon_leader, wingman | — |
| `bounding_turn` | function | 部分可用 | message_exchange:communication_message.received_type, behavior_variables:execution_state.variable | platoon_leader, wingman | 跃进轮次由收到的跃进完成消息推算 |
| `dependency:route_following_ground` | dependency | 部分可用 | route_navigation, ground_navigation | platoon_leader, wingman | — |
| `dependency:visual_search` | dependency | 部分可用 | eo_ir_search, target_detection | platoon_leader, wingman | — |
| `dependency:voice_data` | dependency | 部分可用 | message_exchange, radio_comm | platoon_leader, wingman | — |
| `follow_route` | action | 可实现 | route_navigation:mobility_action.follow_route | platoon_leader | — |
| `hold_overwatch_position` | action | 可组合实现 | motion_halt | platoon_leader, wingman | — |
| `keep_station` | action | 部分可用 | own_kinematic_state:platform_state.position, point_navigation:mobility_action.go_to_location, speed_control:mobility_action.set_speed, target_detection, target_tracking:track_state.latitude, track_identification:track_state.reported_type | wingman | 保持站位需要领车位置：领车广播（发送+接收）、友方航迹，或由运动组件固定偏移跟随（不支持运行中换队形）；另有 2 种替代写法 |
| `message_received` | function | 部分可用 | message_exchange:communication_message.received_type | platoon_leader, wingman | — |
| `mover_value.at_destination` | operand | 可实现 | point_navigation:mobility_state.at_destination | platoon_leader, wingman | — |
| `mover_value.available` | operand | 可实现 | platform_status_monitoring:platform_state.damage_fraction | platoon_leader | 运动部件可用：组件可操作状态，或以平台损伤程度近似；另有 1 种替代写法 |
| `mover_value.route_complete` | operand | 需确认 | route_navigation:route_state.end_of_path | platoon_leader | — |
| `report_contact` | action | 简化处理 | track_reporting:communication_action.report_contact | wingman | 通信报告：理想通信：消息即时送达、不丢失 |
| `send_message` | action | 简化处理 | message_exchange:communication_action.send_message | platoon_leader, wingman | 通信报告：理想通信：消息即时送达、不丢失；另有 2 种替代写法 |
| `set_blackboard` | action | 部分可用 | behavior_variables:execution_action.set_variable | platoon_leader, wingman | — |
| `standoff_reached` | function | 简化处理 | target_tracking:track_state.latitude, own_kinematic_state:platform_state.position, track_identification:track_state.affiliation | platoon_leader, wingman | 观察发现：理想观察：探测范围内的目标直接可见，敌我属性取仿真真值；与最近敌方距离不超过安全距离 |
| `terrain_value.class` | operand | 需确认 | terrain_classification:environment_state.terrain_class | platoon_leader | — |
| `terrain_value.distance_to_class` | operand | 需确认 | terrain_classification:environment_state.distance_to_terrain_class | platoon_leader | — |
| `time_in_state_gte` | function | 简化处理 | state_timing:execution_state.time_in_state | platoon_leader | 任务控制：由行为模型运行时提供（状态机、计时、黑板变量） |
| `track_value.nearest_hostile_range` | operand | 简化处理 | target_tracking:track_state.latitude, own_kinematic_state:platform_state.position, track_identification:track_state.affiliation | platoon_leader, wingman | 观察发现：理想观察：探测范围内的目标直接可见，敌我属性取仿真真值；最近敌方距离 = 航迹位置与自身位置计算距离，并按敌我属性筛选 |

</details>
