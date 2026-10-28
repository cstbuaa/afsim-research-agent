# 实验室仿真引擎对接指南

目标：由实验室引擎负责物理世界（运动、地形、传感器、通信），由行为模型负责决策，并用与内置仿真相同的 L3 期望判定战术效果。

## 两个阶段

| 阶段 | 方式 | 决策在哪里执行 | 用途 |
| --- | --- | --- | --- |
| 1 联合仿真 | 本目录的适配器 + `ir cosim` | 本项目的参考解释器（与 L2 同一套语义） | 先打通闭环，验证模型在实验室引擎中的战术效果 |
| 2 原生运行 | 实验室引擎后端（编译 `normalized_ir.json` 为引擎的行为树/脚本） | 实验室引擎 | 脱离外部进程、大规模运行；用 `ir judge` 对原生运行轨迹做同样的 L3 判定 |

阶段 2 的正确性检查：同一想定分别用联合仿真和原生运行各跑一次，两份轨迹的 L3 结果应一致（同一引擎物理下决策相同）。

## 阶段 1：需要实现的内容

一个 Python 类，模板见 `lab_engine_adapter.py`（所有 `TODO(lab)` 处）：

| 方法 | 作用 |
| --- | --- |
| `reset(setup)` | 按想定创建实体（位置、航向、速度、部件），返回 t=0 观测 |
| `apply_commands(commands)` | 把本步的中立动作转为引擎命令 |
| `send_messages(messages)` | 仅 `messaging = "engine"` 时：交给引擎通信模型投递 |
| `apply_event(event)` | 想定事件（如 `spawn_hostile`）；不支持时返回 False |
| `step(dt)` | 引擎推进 dt 秒，返回新观测 |
| `close()` | 释放连接 |

适配器可以直接调用引擎的 Python 接口；如果用 C++ 插件，可用 pybind11 包一层；也可以是网络客户端。

### 约定

- 坐标：本地东-北平面米制（+x 东，+y 北）；航向：正北顺时针度；速度 m/s；时间秒。引擎使用经纬度或其他航向约定时，用 `afsim_research_agent.ir.cosim.convert` 中的 `LocalFrame`、`heading_from_engine/heading_to_engine`、`speed_from_engine/speed_to_engine` 转换。
- 实体编号与部件绑定写在模型包的 `bindings/lab_engine.json`（示例 `bindings.example.json`）；必需能力没有绑定时报 `DEP001`。
- 观测中的部件用 `behavior_spec.yaml` 里的逻辑 ID（如 `vehicle_sight`）。

### 每步需要读取的状态（`EntityObservation`）

| 字段 | 说明 | 驱动的模型查询 |
| --- | --- | --- |
| `x, y, heading_deg, speed_mps, alive` | 实体位姿与存活 | `self_value.*`，编队误差 |
| `mover.available / at_destination / route_complete` | 最近一次机动命令的完成状态 | `mover_value.*`（跃进完成、路线结束） |
| `components[<逻辑ID>]` | 传感器 `on/available`，武器 `quantity_remaining`，通信 `available` | `sensor_value/weapon_value/comm_value` |
| `tracks` | 本实体传感器探测到的目标（id、x、y、是否敌方） | `track_value.*`（最近敌距离等） |
| `terrain.class`、`terrain.distance_to[类别]` | 当前地形类别与到各类地形的距离 | `terrain_value.*`（接近城区） |
| `inbox` | 仅引擎投递消息时：收到的消息 | `message_received`、`message_value` |
| `fuel_remaining`、`damaged`、`altitude_m` | 可选 | `self_value.*` |

编队误差、友邻距离等团队量由运行器根据各实体位置计算，引擎不需要提供。

### 引擎会收到的命令（`Command.args`，单位已换算）

| 动作 | 主要参数 | 运行器补充的字段 |
| --- | --- | --- |
| `follow_route` | `route`、`speed` | `points`（路线点） |
| `move_to_position` | `position`、`speed` | `point` |
| `keep_station` | `leader`、`formation`、`max_speed` | `leader_entity`、`forward_m`、`right_m`（本车槽位） |
| `bound_to_position` | `distance`、`speed`、`toward`（`heading`/`nearest_hostile`/航点 ID）、`stop_short` | `toward_point`（`toward` 为航点时） |
| `halt`、`hold_overwatch_position` | — | — |
| `set_speed`、`set_heading`、`set_altitude` | 数值 | — |
| `turn_sensor_on/off`、`orient_sensor` | `sensor`、`azimuth` | — |
| `fire_weapon` | `weapon`、`track` | — |
| `send_message`、`report_contact` | — | 消息本身走消息通道，引擎可忽略此命令 |

完整动作目录见 `src/afsim_research_agent/ir/catalog/actions.toml`；模型只会下达它用到的动作，模板对未实现的动作会直接报错，便于发现缺口。

### 消息两种模式

- `messaging = "adapter"`：运行器按模型中声明的延迟和可靠度投递，并处理想定中的通信降级事件。
- `messaging = "engine"`：交给引擎的通信模型，延迟、丢包、遮挡更真实；收到的消息放入接收方观测的 `inbox`。

### 时序

每步：想定事件 → 投递消息 → 所有控制器基于当前观测决策 → 下发命令/消息 → 引擎推进 dt → 记录新观测。dt 默认取模型的 `update_interval`，可在想定 `environment.time_step` 中修改。新下发的机动命令在本步内视为"未完成"，避免读到上一条命令的到达标志。

## 运行与验证

```bash
# 0. 先构建模型包（生成 normalized_ir.json）
python -m afsim_research_agent.ir build examples/armor_platoon_terrain_formation

# 1. 参考引擎：验证联合仿真链路（结果应与内置仿真完全一致）
python -m afsim_research_agent.ir cosim examples/armor_platoon_terrain_formation --engine reference

# 2. 实验室引擎
python -m afsim_research_agent.ir cosim examples/armor_platoon_terrain_formation \
    --engine integrations/lab_engine/lab_engine_adapter.py:make_engine \
    --engine-name lab_engine --trace-dir runs/lab

# 3. 判定任意运行轨迹（包括阶段 2 的原生运行）
python -m afsim_research_agent.ir judge examples/armor_platoon_terrain_formation \
    --scenario open_to_urban_transition --trace runs/lab/open_to_urban_transition.jsonl
```

实验室引擎的结果与参考引擎不必完全相同（物理模型更细），但差异应能由运动学、探测或通信模型解释；若状态转换顺序不同，优先检查观测字段的单位、航向约定和 `at_destination` 语义。

## 轨迹格式（阶段 2 原生运行需输出）

JSON Lines，首行 `header`，之后为 `step/event/message/action/detection`，定义见 `src/afsim_research_agent/ir/cosim/trace.py` 文件头。`header.ir_hash` 必须等于所运行的 `normalized_ir.json` 的哈希，`step` 中的 `state` 为模型状态机的状态 ID。

## 阶段 2 需要的信息

实验室引擎的行为树/脚本格式说明（节点类型、黑板或变量机制、自定义节点/函数的注册方式、每步调度顺序），以及引擎内可用的机动、传感器、通信命令。拿到后按 `compile(normalized_ir, resolved_context, catalog_versions)` 接口写后端，生成到 `generated/lab_engine/`。
