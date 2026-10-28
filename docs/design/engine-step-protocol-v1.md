# 引擎单步控制协议 tbe-step/1

> 状态：v1（2026-10-13）　实现：Python 客户端 `ir/cosim/remote.py`、Python 参考服务端 `ir/cosim/server.py`、C++ 桥接骨架 `integrations/engine_bridge_cpp/`、一致性检查 `ir protocol-check`

本协议用于外部决策程序（行为模型解释器、Agent）按步驱动仿真引擎。协议与具体引擎无关；引擎专有类型只出现在引擎宿主（bridge）内部的 `IEngine` 实现中。

## 1. 部署形态

```text
决策端（Mac / 任意机器）                 引擎主机（例如 Windows）
Python: CoSimRunner                      引擎宿主（C++ 或 Python）
  └─ RemoteEngineAdapter  ── TCP ──▶  网络线程：收发帧、排队
                                         引擎线程：Session → IEngine → 仿真引擎
```

- 一个 TCP 连接对应一次运行（run）；客户端严格"一问一答"，不流水线发送。
- 两端都应设置 `TCP_NODELAY`，且每帧（4 字节长度 + JSON）用一次写调用发出。否则 Nagle 算法与延迟确认叠加，每次请求会多等约 40 ms，一次 400 步想定约慢 40 s（参考实现已处理：`net.hpp::set_no_delay`、`remote.py`、`server.py`）。
- 引擎对象只由引擎线程访问；网络线程只负责收发和排队（C++ 骨架即按此实现，引擎可以运行在进程主线程上）。

## 2. 帧与信封

- 帧：4 字节大端无符号长度 + UTF-8 JSON；单帧上限 64 MB。
- 请求：`{"protocol": "tbe-step/1", "run_id": str, "sequence_id": int, "type": 请求类型, "body": {...}}`
- 响应：`{"protocol", "run_id", "sequence_id"（回显）, "type": "<请求类型>Result" | "Error", "ok": bool, "body": {...}, "error": {"code", "message"} | null}`
- `sequence_id` 在连接内严格递增；**每个请求都会消耗序号，包括被拒绝的请求**。`run_id` 在连接内不可变。

## 3. 请求类型

| 请求 | body | 响应 body | 说明 |
| --- | --- | --- | --- |
| `Hello` | `client`, `versions` | `engine`, `engine_version`, `protocol`, `messaging`（`adapter`/`engine`）, `commands`, `events`, `step.modes` | 必须是第一条请求 |
| `LoadScenario` | `setup`（ScenarioSetup） | `entities`（引擎实体 ID）, `observation` | 创建实体，t=0 |
| `Reset` | 可选 `seed` | `observation` | 以已加载的想定重新开始 |
| `ApplyActions` | `commands`（Command 列表） | `accepted`, `rejected[{index, reason}]` | 不支持的动作进入 `rejected`，不是错误 |
| `SendMessages` | `messages` | `accepted` | 仅 `messaging = engine` 时可用，否则 `UNSUPPORTED` |
| `ApplyEvent` | `event` | `supported` | 想定事件，如 `spawn_hostile` |
| `Step` | `dt`, `requested_time` | StepResult | 推进一个决策周期 |
| `RunUntil` | `time` | StepResult | 不下发命令地推进到指定时间 |
| `GetState` | — | `observation` | 不推进时间；可返回 `UNSUPPORTED` |
| `Terminate` | — | — | 结束会话 |

StepResult：`requested_time`, `actual_sim_time`, `observation`, `events[]`, `warnings[]`, `done`, `end_reason`（`duration`/`terminated`/`engine`/null）。

错误码：`BAD_REQUEST`（格式、序号、参数、时间倒退）、`BAD_STATE`（未 Hello、未加载、已终止）、`UNSUPPORTED`、`VERSION`、`ENGINE_ERROR`。

## 4. 时间语义

- "一步"= 一个决策周期 `dt`（默认取行为模型的 `update_interval`）。引擎内部可以执行任意多个内部事件或帧。
- 引擎必须推进到 `actual_sim_time ≥ requested_time`；事件驱动引擎可以越过请求时间，客户端以 `actual_sim_time` 为准，越过量记入告警。
- 时间不得倒退：`requested_time` 或 `RunUntil.time` 小于当前时间 → `BAD_REQUEST`。

## 5. 每个决策周期的请求顺序（CoSimRunner）

```text
[ApplyEvent]*  →  （控制器决策）  →  ApplyActions  →  [SendMessages]  →  Step
```

本周期新下发的机动命令，其完成标志（`mover.at_destination` 等）在本周期内视为未完成；下一次观测以引擎返回为准。

## 6. 数据结构（规范单位）

- 坐标：本地东-北平面米制（+x 东，+y 北）；航向：正北顺时针度；速度 m/s；时间 s。
- `ScenarioSetup`：`scenario`, `entities[{engine_id, role, instance, capability_class, components, position[x,y], heading_deg, speed_mps}]`, `routes{name: [[x,y],…]}`, `waypoints`, `terrain{default_class, regions[{class, x_min, x_max, y_min, y_max}]}`, `time_step_s`, `duration_s`, `random_seed`。
- `Command`：`entity`, `command`（中立动作名）, `args`, `role`, `instance`, `behavior`。运行器补充的字段：`follow_route.points`、`move_to_position.point`、`keep_station.leader_entity/forward_m/right_m`、`move_to_platform.target_entity`。`bound_to_position` 的参数：`distance`（m）、可选 `speed`、`toward`（`heading` / `nearest_hostile` / 航点 ID）、`stop_short`（m）；`toward` 为航点时运行器补充 `toward_point: [x, y]`。引擎语义：朝目标点（航点，或本实体传感器范围内最近敌）移动 `min(distance, 当前距离 − stop_short)`，无目标时沿当前航向移动 `distance`。
- `EntityObservation`：`x, y, heading_deg, speed_mps, alive, damaged, altitude_m, fuel_remaining, mover{available, at_destination, route_complete, moving}, components{逻辑ID: {...}}, tracks[{id, x, y, hostile, range_m}], terrain{class, distance_to{类别: 距离|null}}, inbox[{message, fields, sender}]`。
- JSON 不能表示无穷大：距离"无"用 `null` 或省略。

## 7. 一致性检查

```bash
python -m afsim_research_agent.ir protocol-check --host <引擎主机> --port 47100
```

17 项检查：握手、错误码（未加载即推进、未知类型、重复序号、时间倒退）、加载与初始状态、命令接受/拒绝、10 步时间单调且到达请求时间、`follow_route` 确实沿路线移动、`halt` 保持静止、`RunUntil`、`GetState`、`Reset` 恢复初始状态、`Terminate`。检查不依赖任何行为模型。

## 8. 版本演进

- 新增可选字段不升版本；改变已有字段语义或必选字段升为 `tbe-step/2`。
- 引擎在 `Hello` 中声明支持的动作和事件；客户端不得依赖未声明的能力。
