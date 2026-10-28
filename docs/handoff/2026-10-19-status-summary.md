# 项目现状总结（2026-10-19）

上一版：`handoff/2026-10-17-status-summary.md`。本版补充 10-17 至 10-19 的进展，主要有：

- Falcon P4：读取 Crow 选定的战术；
- Falcon v2：战术稿生成层；
- 大模型生成行为树的文献调研；
- 按调研结论落地 Python 行为树与封闭词表；
- 单步测试只写意图；
- 提示词通用化；
- 换用 qwen3.8-max 的多轮在线迭代。

## 1. 总体链路

```text
自然语言需求
  → Tactical Crow
      C1 任务画像 → C2 研究规划 → C3 检索与证据包                [完成]
      C4 战术提出 + C4b 平台能力标注（粗颗粒度）                 [完成]
      C5 红队评审 → 回应/修订 → 约束复核 → 评审报告 → 用户选择   [完成]
      C6 交给 Falcon（tactic_selection.yaml）                     [完成（P4）]
  → Tactical Falcon
      v1：分段 JSON → behavior_spec.yaml                          [完成，保留]
      v2：战术稿（YAML + Mermaid + Python 条件 + Python/XML 行为树 + needs + 验证）
          → 编译器 → behavior_spec.yaml + 源码映射 + 记录的测试结果
                                                                  [完成；max 在线已接近一次通过]
  → 行为模型 IR：L1 → 确认闸门（参考值放行）→ 规范化 → L2 → 后端 → L3（含忠实模式）
                                                                  [v1 完成]
  → 平台能力核对：IR/战术/战术稿 needs ↔ AFSIM 2.9 目录 → 缺口登记 → 确认/开发 → 回写 → 复核
                                                                  [K1–K5 完成；needs 来源已接入]
  → 后端：Mermaid / BT.CPP XML / 二维仿真 [完成]；AFSIM 后端 [未开始，已有能力闸门]
  → 外部引擎：tbe-step/1 协议 + C++ 桥接骨架 [框架完成，待接入真实引擎]
```

## 2. 本阶段完成的内容

### 2.1 Falcon P4 与 v2 生成层

| 项 | 内容 |
| --- | --- |
| P4 选定战术 | 读取 `tactic_selection.yaml`：选定步骤、编组、例外转为战术条款（`origin: tactic`）；复用 Crow 证据包；Crow 缺口写入 `crow_capability_gaps.yaml`，`ir build` 自动对照 |
| v2 战术稿 | 模型写 `tactic_sheet.md`，编译器生成 v1 同构的骨架与分段记录，复用组装、强制规则和检查。每个 IR 元素都可回指到战术稿的块和行（`source_map.json`），报错带行号 |
| 生成与修复 | 三次调用（结构 → 控制 → 验证）。结构和控制阶段各有早期修复；之后按块修复，保留最好的一版，某一块越修越差就单独撤回；`falcon compile` 可从人工修改后的战术稿重新编译 |
| 语义检查 | 编译时检查一批常见错误：在树里检查本状态的转换条件、重复 leader_follower、跟随者自己写 KeepStation、端口类型与参数类型不符、重复转换、自回环、用错协同条件的角色、条件名被当作测试输入等。每条都给出原因和正确写法 |

### 2.2 文献调研与据此落地的两项优化

- **调研**：`analysis/2026-10-18-llm-bt-generation-survey.md`（另有 Claude 文档版）。按八类方法整理约 30 篇工作，结论如下：
  - 代码或中间表示再确定性转换（Code-BT）效果最好；
  - 封闭词表加局部重生成（BTGenBot-2）同样有效；
  - 生成之后的验证是最薄弱的环节。
- **Python 行为树**（借鉴 Code-BT）：
  - 新增 ```` ```python <角色> ```` 块，函数即树，用 `ast` 按固定规则转成与 XML 相同的节点，不执行代码；
  - 支持 if/else、try/except、with 控制结构、断言、黑板赋值、协同调用；
  - 默认采用 Python 写法，`--tree-format xml` 可切回；
  - 示例改写版 `examples/armor_platoon_terrain_formation/tactic_sheet_python.md`，编译结果与 XML 版完全相同。
- **封闭词表**（借鉴 BTGenBot-2、CommandSwarm，与能力目录打通）：
  - 动作表由能力目录生成，带目录条目、结论、限制和简化方式；
  - 平台不具备的动作和读取、自造节点在编译时拒绝；
  - 词表外需求写入 ```` ```yaml needs ````，生成 `capability_needs.yaml`，由 IR 能力核对列为缺口（来源 `falcon_sheet`，编号规则与 Crow 一致，能和 Crow 缺口合并）；
  - 词表中已有、只是待确认的能力不允许登记为 needs。

### 2.3 单步测试只写意图（方案 1）

- 战术稿的测试只写下一状态、转换和黑板值。
- 动作、消息、树状态等机械结果由首次运行记录到 `observed_expectations.yaml`，审查报告第 9 节列出，供人工核对。人可以修改这些值，`falcon compile` 按修改后的值严格比较；删去的条目会重新记录。
- 测试失败时，提示会写明期望转换的条件读取了哪些输入、测试没给哪项。输入包括操作数、函数和协同消息。

### 2.4 提示词通用化

- 固定文字只讲写法和通用语义：
  - 组件能力从能力词表列出；
  - 编队和协同改为可选；
  - 协同模式的要点只在用到时出现。
- 第二个示例 `examples/uav_route_recon/`（两架侦察无人机加地面站，只用 report_and_wait）一次编译通过，L2 与 L3 全部通过。
- 回归测试保证提示词里不出现装甲排专有词，也不出现未使用的协同规则。

### 2.5 在线运行记录（armor-eval-03 / tc_01，战术稿方式）

| 模型包 | 模型 | 结果 | 说明 |
| --- | --- | --- | --- |
| `armor_tc01_sheet_v1` | flash | 不收敛，约 25 个问题 | XML 写法；暴露出"用转换条件守门"等语义错误 |
| `armor_tc01_sheet_py1` | flash | 23 → 17 | 速度端口填距离参数、重复转换、needs 误登记 |
| `armor_tc01_sheet_py2_max` | max | 12 | 只剩 model 块的写法问题，已改为编译器接受 |
| `armor_tc01_sheet_py3_max` | max | 1（后续暴露 11 条 L2） | 引出方案 1 |
| `armor_tc01_sheet_py4_max` | max | 2 | 想定期望缺字段 |
| `armor_tc01_sheet_py5_max` | max | 2 | 删掉 1 行自回环后 `draft_valid`；相应检查已加入 |

结论：
- qwen3.8-max 写战术稿明显好于 flash。用 `FALCON_CHAT_MODEL=qwen3.8-max` 切换，单次约 5–7 分钟。
- 剩余错误集中在验证块，随编译器检查逐轮减少。

## 3. 工程目录（变化部分）

```text
src/afsim_research_agent/  （约 3.7 万行 Python）
├── falcon/        (11.1k) 新增 tactic.py（P4）、sheet/ 子包
│   └── sheet/     (5.5k) blocks btxml mermaid pyexpr pytree compiler locate prompts generate recompile values
├── capability/    (2.8k) constraints 增加 details/refs/allowed；ir_check 增加 sheet_needs
├── ir/            (11.1k)  crow/ (6.5k)
examples/
├── armor_platoon_terrain_formation/  tactic_sheet.md（XML 版）  tactic_sheet_python.md
└── uav_route_recon/                  tactic_sheet.md（通用性示例）
data/runs/crow-research/armor-eval-03/models/   各次 Falcon 运行的模型包（见 2.5）
docs/design/falcon-surface-formats-v2.md         §9–§10.7 记录 v2 实现与每轮在线复盘
docs/analysis/2026-10-18-llm-bt-generation-survey.md
tests/unit/falcon/test_falcon_sheet.py  test_falcon_sheet_closed.py（29 项）
```

模型包内新增的文件：
- `tactic_sheet.md`、`source_map.json`；
- `observed_expectations.yaml`；
- `capability_needs.yaml`；
- `crow_capability_gaps.yaml`、`selected_tactics.yaml`。

常用命令（新增或变化）：

```text
FALCON_CHAT_MODEL=qwen3.8-max python -m afsim_research_agent.falcon draft <请求.yaml> [--generator v2 --tree-format python|xml --sheet-calls 1|3]
python -m afsim_research_agent.falcon compile <模型包>       # 人工改战术稿或记录结果后重编译（不调模型）
```

测试：云端全量 493 通过，另 11 项文献/网页测试仅因桩模块失败；ruff 只剩 `tests/unit/crow/test_proposal.py` 一处早已存在的 E501。

## 4. 后续待开发

按建议顺序：

1. **新任务端到端试跑。** 选一个与装甲排完全不同的任务（无人机侦察、防空值班、舰艇拦截），从 Crow 到 Falcon 在线跑通，检验通用化后的提示词。
2. **max 再跑一次装甲排**（`armor_tc01_sheet_py6_max`）。检验自回环、协同条件、覆盖路由等新检查能否让模型自己一次收敛，并人工核对第 9 节记录的测试结果。
3. **能力组扩展到其他平台类别。** `groups.toml` 目前只定义了地面车辆的核心组。空中、水面、固定节点需要各自的核心组，否则粗颗粒度判定会偏严或偏松（通用化后会直接用到）。
4. **结构稿示例按平台类别切换**，并补充若干类型的任务示例：单平台、无编队、多角色异构。
5. **规划器辅助子树**（调研建议，中优先级）：LLM 只给目标条件，规划器补前置条件。需要先在 AFSIM 动作上补充前置/效果元数据，可先在机动、编队类动作上试点。
6. **运行期失败定位**：用引擎 step 协议的状态轨迹，把仿真中的失败映射回战术稿的块与行，只重写失败的子树。
7. 延续上一版的项目：
   - C5 代码一致性检查；
   - 反制战术转 L3 想定；
   - 参考值调参（`ir sweep`）；
   - IR v1.1 信息来源建模；
   - 能力目录 0.3.0 重整；
   - AFSIM 后端；
   - demo 数据集转 IR 及 Deep Agents 编排。

## 5. 待优化问题

### 5.1 质量

- **小模型写不好战术稿。** flash 在语义层面错误多，修复不收敛；max 可用但成本约高一个数量级。可考虑：
  - 结构和控制用 max、验证用 flash；
  - 或用通过编译的战术稿做少样本检索或微调（调研低优先级项）。
- **验证块仍是主要错误来源。** 想定期望写错不再阻断单步测试，但想定本身的合理性（事件位置、时间窗口）没有检查。
- **记录的测试结果需要人工核对。** 首次运行即视为正确；如果行为树本身有错，记录的就是错误结果，第 9 节表格需要逐条确认。
- **通用化只经过离线验证。** 新提示词还没有在非装甲任务上在线运行过。
- **以往的问题仍在**：红队区分度有限；能力标注缺少金标集；参考值未校准。

### 5.2 设计

- **编译器规则不断增加。** 目前大量语义检查写在 `compiler.py`（约 2.4k 行）里，需要整理成可配置的规则表，并给出规则清单文档。
- **协同模式知识分散。** 协同模式的语义（谁收哪条消息、生成什么）分散在 `ir/coordination.py`、编译器的 `GENERATED_*` 表和提示词的 `PATTERN_*` 表里。新增模式时要改多处，应收敛到协同目录 `coordination.toml`。
- **二维仿真仍泄露真值**（不变）：跟随者读取领车真实位置，探测不分敌我。
- **用户确认不等于运行验证**（不变）。

### 5.3 工程

- **Mac 端无版本控制**（不变），同步靠逐文件比对。建议初始化 git，并把 `_local/`、`.env` 列入忽略。
- **云端测试依赖桩模块**，完整测试仍需在 Mac 上执行。
- **修复提示偏长**（约 25k 字，附整份战术稿），可只附相关块以降低成本。

### 5.4 资料边界（不变）

- 受控资料、原生接口名、绑定只存本地 `_local/`；
- 公网请求不含本地资料、想定数据、平台参数；
- 密钥与 `.env` 不进入代码、文档、对话；
- 生成物不覆盖用户已有 AFSIM 文件。
