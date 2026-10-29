# 平台能力目录接入流程（设计稿 v1）

状态：**已确认，K1–K5 已实现**　日期：2026-10-17（修订 7：K5 缺口登记、开发队列、回写与忠实仿真）

相关：
- `handoff/2026-10-16-status-summary.md`
- `design/crow-tactic-proposal-v1.md` §18（Crow 能力标注）
- `analysis/2026-10-16-armor-example-catalog-coverage.md`（手工核对，可作为第一个测试用例）
- `handoff/2026-10-14-demo-dataset-ir-conversion.md` §2（目录两层分离、缺失分类的原始约定）

## 0. 问题

AFSIM 2.9 能力目录已有运动、通信、武器 3 个领域的 0.3.0 版本，但流程中还没有任何环节读取它。现有的能力相关检查都停留在"组件级"：

| 位置 | 现在做什么 | 局限 |
| --- | --- | --- |
| Falcon 门槛 `falcon/gate.py` | 从 `target_engine` 绑定文件读一组能力 ID | 只有 ID 集合，没有接口、状态、限制 |
| Falcon 骨架 `falcon/skeleton.py` | 角色依赖（mover/sensors/comms/weapons）必须在该集合内；提示词列出这些 ID | 只能判断"有没有电台"，不能判断"能不能发消息" |
| IR 校验 `ir/validate.py` | 动作要求角色具备某类组件（如 `send_message` 需要 comms）、平台类别匹配 | 同上，组件级 |
| 绑定 `ir/binding.py` | 逻辑能力 ID → 仿真组件；`bind_external` 绑定实验室引擎 | 能力 ID 是另一套词汇（`route_following_ground`、`voice_data`），与目录 ID 不一致 |
| Crow C4 `required_capabilities` | 模型自由填写（如 `air_mover`） | 没有受控词表 |
| Crow C5 | 设计中有"能力检查"，`CROW_CAPABILITY_CATALOG` 配置项已预留 | 未实现 |

手工核对表明，真正的缺口在"接口级"：发送消息、读取最近敌方、判断是否在城区、领车位置来源。接入的核心是把检查从"组件有没有"细化到"动作和读取能不能做"。

## 1. 调研要点

| 工作 | 做法 | 对本项目的启示 |
| --- | --- | --- |
| LLM-Modulo（Kambhampati 等，2024） | 模型负责生成，外部验证器负责判定；硬约束只能由可靠的验证器把关，模型不能自我验证；验证意见回灌给模型修改 | 能力可行性是硬约束，应由**代码**依据目录判定；模型只负责提出和修改 |
| SayCan（Ahn 等，2022） | 每一步 = 语言模型给出"有用程度" × 技能的可执行性 | 战术是否"有用"由条令与证据决定，平台能否执行由目录决定；两者分开打分，不互相替代 |
| BTGenBot（2024） | 提示词中列出可用动作，并给出每个动作的用法示例；生成后做静态检查，删除未识别的动作，再在仿真中运行 | 生成阶段把可用动作与读取写进提示词（受控词表），生成后仍要做确定性检查 |
| Gorilla（2023） | 先按任务检索相关 API 文档，再让模型生成调用，以减少臆造接口 | 目录有 72 项且还会增长，不宜全文塞入提示词；按领域、子类、关键词检索相关条目即可（`index.yaml` 已按此设计） |
| LATM / CRAFT（2023–2024） | 缺少工具时由模型制造工具，存入工具库，之后检索复用 | 与"能力插件开发 Agent"一致：缺口清单作为需求，产出的插件写回目录，后续流程即可检索使用 |

## 2. 接入点

```text
需求
 │  ①C1 任务画像 ── 涉及哪些领域、平台类别（轻量提示）
 ▼
Crow 检索与证据、C4 战术提出（不接入：战术研究不受平台限制）
 │  ②C4b 能力标注（预估）── 每个候选：所需能力、必要性、替代路径、状态 → 可行性等级
 │                         → capability_gaps.yaml（缺口编号从此确定）
 │  ③C5 展示与选择 ── 可行性与综合分并列展示；选定战术的缺口进入开发队列
 ▼
Falcon 生成
 │  ④生成约束  ── 提示词只列目标平台可用的动作与读取，未知项允许但标注
 ▼
IR 规范化
 │  ⑤能力一致性检查（精确）── IR 逐条对照目录；沿用 ② 的缺口编号，补充遗漏项
 │                           → capability_gaps.yaml（更新）+ 报告 + 行为树标注
 ▼
后端
 │  ⑥AFSIM 后端绑定 ── 目录接口 → 本地原生绑定（_local，只在授权环境）
 │  ⑦仿真忠实模式 ── 二维仿真只提供目录允许的信息（如不再读领车真值）
 ▼
 ⑧回写 ── 用户确认 / 插件实现 → 目录更新 → 重新标注与核对
```

| # | 接入点 | 性质 | 作用 | 优先级 |
| --- | --- | --- | --- | --- |
| ② | Crow C4b 能力标注 | 预估；模型只做要素→条目对应，状态由代码判定 | 用户选战术时看到实现代价；缺口编号在此确定 | **第一**（与 ⑤ 共用判定引擎） |
| ⑤ | IR 规范化之后的能力一致性检查 | 精确；纯代码 | 覆盖所有动作、操作数、函数、协同展开元素、角色依赖；补全契约、使用位置、验收用例 | **第一** |
| ③ | C5 展示与选择 | 展示 | 可行性独立一栏，不并入得分；只有选定战术的缺口进入开发队列 | 随 ② |
| ④ | Falcon 生成约束 | 软约束 + 生成后检查 | 减少目录外动作；替换现有组件级能力集合 | 第三 |
| ① | C1 任务画像 | 提示 | 明显超出平台的需求提前提醒 | 低 |
| ⑦ | 仿真忠实模式 | 仿真规则 | L3 提前暴露信息来源问题 | 中（与 IR v1.1 一起） |
| ⑥ | AFSIM 后端绑定 | 后端 | `bind_external` 从组件级扩到接口级 | 随 AFSIM 后端 |
| ⑧ | 回写与复查 | 流程 | 缺口闭环；跨运行汇总 | 随 ②⑤ 定格式 |

### 2.1 为什么标注放在候选生成之后

战术质量应来自条令与证据。若生成候选时就提供平台能力，模型会倾向提出"平台方便实现"的战术，与 SayCan "有用程度与可执行性分开评估"的做法相悖。因此 C4 生成时不提供目录，C4b 在候选定稿后单独标注；标注结果只展示，不删除、不降级候选。

### 2.2 为什么需要两级核对

| | Crow C4b（预估） | IR 核对（精确） |
| --- | --- | --- |
| 输入 | 自然语言战术 + 结构化要素 | 规范化 IR（含协同展开） |
| 粒度 | "跃进到位置""通报到位""判断是否接敌" | 每个动作、操作数、函数、消息 |
| 能发现 | 战术明确提到的能力 | 展开后才出现的需求：领车位置来源、跃进轮次的消息推算、状态计时等 |
| 用途 | 选择战术、尽早启动确认与开发 | 开发契约、验收用例、行为树标注、AFSIM 生成闸门 |

两级共用缺口编号；精确核对在预估基础上补充，不另起清单。

## 3. 核心部件设计

### 3.1 目录访问层（新模块 `afsim_research_agent/capability/`）

- **加载：** 读取 `index.yaml` 与各领域文件，兼容 0.2.0 单文件。只加载正式层，`_local/` 永不由此模块读取。
- **查询：**
  - 按领域、子类、关键词、战术用途检索；
  - 按 `ir_v1` 反查实现某个 IR 元素的能力接口；
  - 取状态、限制、依据（手册 / 用户确认 / 组合）。
- **平台配置：** 目标平台 = 目录 + 平台类别（如 ground_vehicle），按 `applicable_platform_classes` 过滤。
- **判定引擎：** 输入"所需能力 + 对应条目 + 必要性 + 替代路径"，输出状态、缺口类型、可行性等级。C4b 与 IR 核对共用。
- **版本：** 目录内容哈希写入候选卡（`catalog_version`）与模型包 `resolved_context.json`；目录变更后提示重新标注与核对。

### 3.2 词汇对齐

| 对齐 | 做法 |
| --- | --- |
| IR 动作、操作数、函数 → 目录接口 | 已有 `ir_v1` 字段，建立反向索引 |
| IR 角色依赖能力 ID（`route_following_ground` 等 12 个）→ 目录能力 | 新建对照表 `capability/aliases.toml`，如 `voice_data → [message_exchange, radio_comm]` |
| 协同模式 → 所需接口 | 由展开结果自动得到，如 `leader_follower` 需要发送和接收消息 |
| 目录未覆盖的 IR 元素 | 新增"执行环境"（黑板、状态计时）与"环境与区域"（地形类别、区域判断）两个领域 |

### 3.3 能力一致性检查（接入点 ⑤）

**输入：** `normalized_ir.json`、平台配置、Crow 的 `capability_gaps.yaml`（若有）。

**判定规则（由代码执行）：**

| 目录状态 | 结论 | 缺口类型 |
| --- | --- | --- |
| available / supported | 可实现 | — |
| composite 且依赖全部可用 | 可组合实现（记录组合方式） | — |
| partial | 部分可用（列出限制） | — |
| unknown | 需确认 | `confirm` |
| unavailable / 目录无对应 | 需开发（先查有无替代写法） | `develop` |
| 平台有、IR 无对应（`ir_v1: null`） | 需扩展 IR | `ir_extension` |

**输出：**

1. `capability_report.md`，并入 `review.md` 一节：逐条对照表，与手工核对格式一致。
2. `capability_gaps.yaml`（更新）：每条缺口补全
   - 契约：中立接口、参数、单位、返回值；
   - 使用位置：角色、状态、行为树节点；
   - 验收用例：从 L2 测试与 L3 想定中抽取；
   - 替代写法；
   - 来源：`crow_estimate` / `ir_check`。
3. 行为树标注：行为树 XML 中缺能力的节点加属性 `capability_status="gap:<id>"`，行为树照常生成。

**闸门：**
- 缺口不阻塞 L1/L2/L3 与现有后端；
- 生成 AFSIM 后端产物前，每条阻塞缺口需要用户处理：确认、接受替代写法，或标记"待插件"。

### 3.4 Crow C4b 能力标注（接入点 ②③）

详见 `design/crow-tactic-proposal-v1.md` §18。要点：

- 候选定稿后单独标注，不影响生成与排序；
- 所需能力分为 required / enhancing / substitutable，可替代项列出替代路径；
- 只有"必需且无可行替代"计为阻塞缺口；
- 可行性等级（ready / needs_confirmation / needs_development）与综合分并列展示；
- 只有用户选定战术的缺口进入开发队列。

### 3.5 生成约束（接入点 ④）

- 提示词中的"可用动作 / 读取"列表由目录生成：
  - 状态为可用或部分可用的正常列出；
  - 未知项列在"可用但需确认"一栏；
  - 平台没有的不列。
- 列表按需求检索相关子类，不全量放入。
- 生成后 ⑤ 照常检查。

### 3.6 缺口编号与开发队列

- **编号：** `cap-gap-` + 契约 ID 的内容哈希前缀。未对应到目录时，用规范化后的需要描述生成哈希。同一缺口在不同候选、不同运行中编号相同。
- **状态流转：** open → confirmed（用户确认）/ in_development → implemented / rejected。
- **进入开发队列：** 所属战术被用户选定（`selected: true`）。
- **优先级：** 阻塞程度 > 出现的战术数与运行数 > 可否组合替代。
- **交付内容：** 契约 + 使用位置 + 验收用例 + 本地实现线索。实现线索只在授权环境中由 `_local/` 补充。
- **闭环：** 实现后回写目录（依据：用户确认 / 插件实现），重新标注与核对，可行性随之更新。

## 4. 安全边界

- 只有正式层（`classification: public`）进入提示词与云端模型。
- `_local/` 的原生名称、绑定、页码只由授权环境中的 AFSIM 后端和插件开发流程读取，本仓库代码不读取。
- 目录文件放置建议：
  - 正式层复制到仓库 `datasets/platform_capabilities/afsim-2.9/`，纳入版本控制；
  - `_local/` 留在用户本地文件夹，路径通过配置给出，`.gitignore` 排除。

## 5. 实施计划

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| K1 | 目录访问层、判定引擎、词汇对齐表、新增"执行环境 / 环境与区域"条目 | 单元测试；装甲排示例每个 IR 元素都能映射或明确登记为缺失 |
| K2 | Crow C4b 能力标注 + `capability_gaps.yaml` + 报告可行性栏 | tactics_v2 候选离线回放得到可行性等级与缺口 |
| K3 | IR 能力一致性检查（沿用缺口编号）+ 报告 + 行为树标注，接入 `ir build` | 自动结果与 `analysis/2026-10-16-armor-example-catalog-coverage.md` 的手工结论一致 |
| K4 | Falcon 生成约束（替换 `target_engine` 组件集合） | armor-eval 在线运行中不出现目录外动作 |
| K5 | 回写流程、开发队列与跨运行汇总；仿真忠实模式（与 IR v1.1 一起） | 确认一条缺口后重新标注，可行性相应变化 |

## 6. 已确认（2026-10-16）

1. Crow 完成战术生成时即标注所需能力、可实现项与必需但未开发项；缺口交由能力插件开发 Agent 或人工开发；缺能力的节点照常生成行为树并标注。
2. 闸门：缺口只阻塞 AFSIM 后端产物生成，不阻塞 Falcon 输出、L1/L2/L3 与现有后端。
3. 目录正式层复制进仓库 `datasets/platform_capabilities/afsim-2.9/` 做版本控制；`_local/` 保留在用户本地文件夹，由 `.gitignore` 排除。
4. 新增"执行环境"（`execution_environment`：黑板/脚本变量、状态计时、仿真时间）与"环境与区域"（`environment_zone`：地形类别、区域内判断、到区域距离）两个领域。

## 参考

- [LLMs Can't Plan, But Can Help Planning in LLM-Modulo Frameworks](https://arxiv.org/html/2402.01817v2)
- [Do As I Can, Not As I Say: Grounding Language in Robotic Affordances](https://ar5iv.labs.arxiv.org/html/2204.01691)
- [BTGenBot: Behavior Tree Generation for Robotic Tasks with Lightweight LLMs](https://arxiv.org/html/2403.12761v2)
- [Gorilla: Large Language Model Connected with Massive APIs](https://ar5iv.labs.arxiv.org/html/2305.15334)
- [Large Language Models as Tool Makers](https://arxiv.org/pdf/2305.17126)
- [CRAFT: Customizing LLMs by Creating and Retrieving from Specialized Toolsets](https://proceedings.iclr.cc/paper_files/paper/2024/hash/af31604708f3e44b4de9fdfa6dcaa9d1-Abstract-Conference.html)

## 7. K1 实现说明（2026-10-16）

**新增模块** `src/afsim_research_agent/capability/`

| 文件 | 内容 |
| --- | --- |
| `catalog.py` | `CapabilityCatalog.load()`：读取 0.2.0 单文件与 `catalog/*.yaml`，领域文件中的条目替换同 ID 的旧条目；只接受 `classification: public`；拒绝读取 `_local/` 与 `*.local.*`；内容哈希。查询：`get`、`resolve("能力:接口")`、`by_ir_v1`（反向索引）、`for_platform`、`search`（战术用途、关键词、名称的短语匹配）。默认路径为仓库 `datasets/platform_capabilities/afsim-2.9/`，可用 `CROW_CAPABILITY_CATALOG` 覆盖 |
| `assess.py` | 判定引擎：状态 → 结论（ready / composable / partial / confirm / ir_extension / develop）；多项同时需要取最差，替代路径取最好；平台类别不适用记为 develop；`need_ir` 时无 IR 对应的接口记为 ir_extension；`map_ir_element` / `assess_ir_element`；`map_binding`；`summarize`（可行性等级与阻塞缺口）；`gap_id`（契约哈希） |
| `aliases.toml` | 词汇对齐：IR 角色依赖 12 个能力 ID → 目录能力；目录未直接链接的 30 个 IR 元素 → 目录接口组合（含替代路径）或"由模型定义确定"。`override` 用于直接链接掩盖了依赖的情况：`keep_station` 显式要求领车位置（消息广播 / 友方航迹 / 固定偏移跟随三选一） |
| `ir_elements.py` | 从 `normalized_ir.json` 列出实际使用的动作、操作数、函数、黑板与角色依赖及其位置（供 K3） |

**目录**：新增执行环境类（5 项）与环境与区域类（3 项）。依据：脚本变量（759 页）、脚本读取仿真时间（807 页）、全局区域定义（227、799 页）；其余为 unknown。

**测试**：`tests/unit/capability` 14 项。

- 合成目录：加载、合并、密级与本地层拒绝、判定规则、组合与替代、可行性汇总、缺口编号稳定性。
- AFSIM 2.9 目录：IR 1.0 全部 73 个元素均有映射且引用可解析；12 个角色依赖 ID 均可映射；检索。
- 装甲排示例：18 个元素的结论与手工核对一致（地形一项因新增环境类，由"缺失"变为"需确认"），三项角色依赖均为部分可用。

全部单元测试中仅原有 11 项文献/网页测试因容器内桩模块失败，与本次改动无关。

**判定结果中值得注意的两处**：
- 友方航迹作为领车位置来源时，要求同时具备目标探测与身份识别，因此只算"部分可用"，不再是"可实现"；
- `mover_value.available` 取"组件可操作状态"与"平台损伤程度"中较好的一项，按"可实现"计，是近似。

## 8. K2 实现说明（2026-10-16）

Crow C4b 能力标注已实现，细节见 `design/crow-tactic-proposal-v1.md` §18.8。对本设计的两处补充：

1. **判定引擎**：`ir_expressible()` 判断接口能否被 IR 使用（直接链接、能力级链接、或出现在 IR 元素的组合映射中）。只有完全不可达时，Crow 的需要才记为"需扩展 IR"；0.2.0 未记录映射的接口不算。
2. **`summarize()`** 接受一个需要对应多个缺口编号（替代路径或组合中有多处未具备）。

Crow 标注在在线运行中的问题与修正见 `design/crow-tactic-proposal-v1.md` §18.9。

## 9. K3 实现说明（2026-10-16）

IR 能力一致性检查（接入点 ⑤）已实现，接入 `python -m afsim_research_agent.ir build`。

**代码**：`capability/ir_check.py`。

| 部分 | 做法 |
| --- | --- |
| 元素与位置 | `used_elements()` 新增状态机转换条件（按所在角色的平台类别判定）。每个使用位置归到所属的行为、条件、转换、协同或角色依赖 |
| 角色归属 | 从角色的行为树与状态机出发，沿条件引用求闭包，得到每个行为、条件被哪些角色用到；按这些角色的平台类别判定，取最差 |
| 判定 | 与 Crow 共用 `assess_ir_element()` / `assess()`；缺口编号 = `gap_id(契约)`，与 Crow 相同。未映射的 IR 元素以 `ir:<元素名>` 为契约 |
| 行为树节点 | 条件里的缺口沿条件引用传到使用它的行为节点与条件节点；转换条件的缺口记在 `<角色>_transitions` 节点 |
| 验收用例 | 选中路径经过缺口节点或发出缺口动作的 L2 测试；包含相关角色的 L3 想定 |
| Crow 对照 | `--crow-gaps <Crow 的 capability_gaps.yaml> --crow-candidate tc_02`：Crow 的缺口逐条标为"IR 中仍需要 / IR 中不构成缺口 / IR 未使用"；IR 新发现的缺口标 `new_in_ir` |
| 用户处理 | 模型包中的 `capability_decisions.yaml`：`decisions: {缺口编号: {decision: confirmed / alternative / pending_plugin, note}}` |

**输出**（只写派生文件，不改 `behavior_spec.yaml`）：

- `capability_gaps.yaml`（phase `ir_check`，format_version 0.2.0）：结论、未处理缺口、每条缺口的契约、IR 元素、使用位置、角色、行为树节点、验收用例、替代写法、来源、处理决定；逐项对照表。
- `review.md` 第 8 节"平台能力核对"。
- `validation_report.json` 的 `capability` 摘要；`generated/manifest.json` 记录目录版本。
- 行为树 XML：缺口节点前插入注释 `<!-- capability_status="gap:<编号>" 需确认 <能力名> -->`。**与 §3.3 的差异**：用注释而不是 XML 属性，因为 BehaviorTree.CPP 4 会拒绝未声明为端口的属性，缺口未处理时行为树也必须能加载。

**闸门**：缺口只产生警告 `CAP002`，不影响 L1/L2/L3 与 python_sim、btcpp_xml、mermaid。名称以 `afsim` 开头的后端在仍有未处理缺口时报错 `CAP001` 并跳过（AFSIM 后端尚未实现，闸门已就位）。能力目录不可用时记 `CAP000` 并跳过核对。`--no-capability` 可关闭。

**装甲排示例**：结论"需确认"，缺口 7 项，全部为需确认：发送消息、立即上报接触、航迹敌我属性（`standoff_reached` 与最近敌方距离共用）、走完路线、当前地形类别、到某类地形的距离、状态计时。与 `analysis/2026-10-16-armor-example-catalog-coverage.md` 和 K1 的逐元素结论一致；领车位置来源（`keep_station`）为"部分可用"，在逐项对照中注明另有两种替代写法。示例包已按新流程重新生成。

**测试**：`tests/unit/capability/test_ir_check.py` 6 项（示例结论、使用位置与节点、Crow 对照、处理决定与闸门、行为树注释后结构校验仍通过、构建输出与跳过）。全部单元测试 411 项通过，另 11 项为容器内桩模块导致的已知失败。


## 10. K4 实现说明（2026-10-16）

Falcon 生成约束（接入点 ④）已实现。

**代码**：`capability/constraints.py`；Falcon 的 `prompts.catalog_actions / catalog_operands`、`fill.catalog_text`、`pipeline.run_draft`。

| 部分 | 做法 |
| --- | --- |
| 分级 | IR 词表全部 72 项（动作、操作数属性、函数）逐项按平台类别判定：可实现 / 可组合 / 部分可用 → 正常列出；需确认 → 标【需确认】；需开发或平台类别不适用 → 不列 |
| 平台类别 | 请求中 `platforms` 与骨架角色的平台类别之和。混合编成时，只要有一类具备就列出，并标【仅 某类】；各类都不具备才不列 |
| 提示词 | 条件、行为、控制、验证四段的目录片段前加一段约束说明；骨架（协同模式）与参数段不变 |
| 生成后核对 | 草稿归一化后的 IR 直接跑 K3：模型包写入 `capability_gaps.yaml`，`falcon_review.md` 第 8 节"平台能力核对"，`validation_report.json` 记录摘要；`falcon_trace.json` 的 `capability_constraints` 记录约束（需确认与未列出的条目） |
| 开关 | `falcon draft --no-capability` 关闭；`--capability-catalog DIR` 指定目录；目录不可用时照旧生成并在生成记录中说明 |

**地面车辆的结果**：不列 1 项（`set_altitude`）；需确认 22 项，包括发送消息、上报接触、控制器开关、等待、燃油、走完路线、武器状态、通信可用、最近敌方距离与敌方数量、地形四项、仿真周期计数、状态计时、到达安全距离。

**未做**：
- 协同模式目录没有标注各模式依赖的动作（如 leader_follower 需要发送消息）。协同展开生成的元素不受提示词约束，由生成后的 K3 核对覆盖。
- §3.5 的"按需求检索相关子类"：IR 词表只有 72 项，全部列出，暂不需要检索。

**测试**：`tests/unit/falcon/test_falcon_capability.py` 4 项（分级、混合编成、提示词列表、草稿全流程的约束与核对、关闭）。全部单元测试 415 项通过，另 11 项为容器内桩模块导致的已知失败。


## 11. 粗细两档判定（2026-10-16）

**问题**：K2–K4 都按最细的接口判断。Crow 每个候选列 15–23 项需求，生成提示词有 22 项【需确认】（燃油、弹药余量、仿真周期、控制器开关等），初步方案被细节淹没，还会把模型带偏。

**用户确定的原则**：地面车辆的核心任务是机动、编队、地形判断；其他能力也参与仿真，缺失时做默认或简化处理，用户需要时再细化或开发。

**做法**：

| 部分 | 内容 |
| --- | --- |
| 能力组 `capability/groups.toml` | 10 个战术能力组：机动、编队、地形判断（地面车辆核心组），观察发现、通信报告、火力、指挥与任务、保障与状态、任务控制、电子与网络对抗。每组列出目录能力、细节能力 / 细节接口和简化方式；目录 121 项每项恰好归入一组（测试保证）。IR 中不在目录里的元素（黑板、仿真时间等）单独归组 |
| 策略 `capability/granularity.py` | `CapabilityPolicy(granularity, refine)`。coarse（默认）：核心组与用户细化的组逐接口判定；其他组缺失或未确认时结论为"简化处理"（`simplified`，排在部分可用与需确认之间），不计缺口、不影响可行性等级；细节项不进提示词。fine：全部逐接口判定（即 K2–K4 的行为） |
| Crow C4b | 目录摘要按能力组排列，核心组在前并注明"核心：逐项核对 / 辅助：缺失时简化处理"，细节项不列；提示词要求 5–10 项、合并同组同类需求、每项写 group；辅助组中目录没有的部分按简化处理，不成为需开发缺口。报告显示"简化处理 N 项"与"按粗粒度判定"说明。`crow.propose` / `crow.annotate` 增加 `--granularity`、`--refine` |
| Falcon K4 | 粗粒度下细节项不列（地面车辆 15 项：燃油、武器与通信可用状态、弹药余量、传感器与控制器开关、仿真周期等）；辅助组元素不再标【需确认】，只剩核心组的 5 项（走完路线、地形四项）。请求文件增加 `capability_granularity`、`capability_refine` |
| IR 核对 K3 | 缺口只来自核心组与细化组；其余单列"简化处理"表（元素、能力组、简化方式、角色）。粒度来自 `ir build --granularity / --refine`，或模型包 `capability_decisions.yaml` 的 `granularity`、`refine`；Falcon 草稿会生成该文件的模板 |
| 迭代 | 用户在 `refine` 中加入能力组（如 comms），该组恢复逐接口判定与细节项；缺口编号与细粒度时相同 |

**装甲排示例（粗粒度）**：缺口由 7 项降为 3 项，全部在核心组：走完路线、当前地形类别、到某类地形的距离。简化处理 5 项：发送消息与上报接触（理想通信）、最近敌方距离与到达安全距离（理想观察，敌我属性取仿真真值）、状态计时（由行为模型运行时提供）。

**AFSIM 闸门**：只要求处理核心组与细化组的缺口；简化处理项在 `capability_gaps.yaml` 中写明简化方式，由 AFSIM 后端按此提供默认实现。需要精确落地时用 `--granularity fine` 或把对应组加入 `refine`。

**测试**：Crow 2 项、IR 核对 4 项、Falcon 2 项新增或改写；全部单元测试 421 项通过，另 11 项为容器内桩模块导致的已知失败。


## 12. K5 实现说明（2026-10-17）

缺口闭环（接入点 ⑧）与仿真忠实模式（接入点 ⑦）已实现。命令：`python -m afsim_research_agent.capability <命令>`。

**缺口登记表** `capability/registry.py`，默认 `data/capability/gap_registry.yaml`（`--registry` 或 `CROW_GAP_REGISTRY` 可改）：

| 命令 | 作用 |
| --- | --- |
| `ingest FILE...` | 收入 Crow（`tactics/capability_gaps.yaml`）或 IR 核对（模型包 `capability_gaps.yaml`）的缺口；按编号合并，同一运行重复收入不重复计数 |
| `select FILE --candidate tc_02` | 选定战术：该候选的缺口进入开发队列，并在 Crow 缺口文件中标 `selected: true`。IR 核对的缺口来自已建模的模型，自动视为选定 |
| `queue [--out dev_queue.md]` | 开发队列：选定且未处理的缺口，按阻塞程度（需开发 > 需扩展 IR > 需确认）> 选定战术数 > 运行数 > 无替代写法优先排序；每条给出契约、使用位置、验收用例、替代写法；实现线索只在本地层 |
| `confirm / start / implement / reject / reopen ID --by NAME` | 状态流转 open → confirmed / in_development → implemented / rejected；`reopen` 撤销。每次记录操作人、日期与说明 |
| `merge ID... --into ID` | 同一能力的不同措辞合并（如 tc_02 的四条扇区缺口）；被合并的编号作为别名，之后的运行自动落到合并后的记录 |
| `recheck DIR` | 用当前目录重新判定已有结果，不调用模型：Crow 战术目录（沿用原映射，重算可行性与缺口，保留选定标记）或模型包（重跑 IR 核对，更新 `capability_gaps.yaml`） |

**回写**：`confirm`（依据 `user_attested`）与 `implement --plugin P`（依据 `plugin_implemented`）把缺口的目录引用写入目录根目录的 `capability_overrides.yaml`；`CapabilityCatalog.load()` 最后应用它（接口状态改为 supported，能力状态至少为 partial），目录版本哈希随之改变。用覆盖文件而不直接改目录分卷，因为分卷由生成脚本从手册重建，直接修改会被覆盖。目录中没有对应条目的缺口（以需求文字为契约），需用 `--as 能力ID:接口` 指明由哪个目录条目提供；`recheck` 会把它加入原需求的映射。尚未在目录中的新能力，应先加入目录生成脚本。

**验收**（K5 标准"确认一条缺口后重新标注，可行性相应变化"）：测试中 tc_01 选定后队列为"隐蔽阵地判断（需开发）、当前地形类别（需确认）"；确认地形类别、以插件实现隐蔽阵地判断后 `recheck`，tc_01 由"需开发"变为"可实现"；`reopen` 后回写被移除，重新判定回到"需确认"。

**仿真忠实模式**：`ir build --faithful-sim` 在常规 L3 之外再跑一遍：未处理缺口对应的动作与读取在仿真中不提供（读取为算子目录的"未知"默认值，如无敌情、默认地形；动作无效），简化处理项照常理想化。未达成的期望只记警告 `L3F01`，写入 `validation_report.json` 的 `l3_faithful` 与 `review.md` 第 7b 节，不影响构建状态。装甲排示例（粗粒度）不提供走完路线、当前地形类别、到某类地形的距离后，4 个想定中 3 个未达成（全排认不出城区，不转纵队），说明地形判断是这个战术的前提。

**未做**：
- 忠实模式只覆盖动作与算子读取；函数（如状态计时）与领车位置真值（`keep_station` 直接读领车位置）留待 IR v1.1 引入"信息来源"后处理。
- 跨运行的自动近似合并（相似措辞自动建议合并）未做，目前人工 `merge`。

**测试**：`tests/unit/capability/test_registry.py` 6 项（覆盖文件加载、选定与队列、确认与插件实现后重新判定、状态流转校验、合并、IR 缺口与重新核对、忠实仿真）。全部单元测试 427 项通过，另 11 项为容器内桩模块导致的已知失败。


## 13. 封闭词表与词表外需求（2026-10-18）

**K4 的变化**：生成约束从"建议"改为"约束"。

- **提示词**：Falcon v2 的动作表由能力目录生成，包括对应的目录条目、结论、限制和简化方式。
- **编译时拒绝**：战术稿编译器拿到约束后，以下情况作为空洞报错：
  - 用到平台不具备的动作、操作数或函数（需开发，或混合编成中本角色的平台类别没有）；
  - 用到 IR 目录中不存在的节点。
- **不变的部分**：细节项按简化处理照常允许；K3 仍在生成后完整核对。

**新的缺口来源 `falcon_sheet`**：
- 战术稿 `yaml needs` 登记目录和 IR 词表都没有的能力，例如"施放烟幕"。
- 模型包写入 `capability_needs.yaml`，由 K3 的 `check_ir(sheet_needs=…)` 列为缺口。`ir build` 自动读取。
- 编号规则与 Crow 相同：引用 Crow 缺口时用其编号，否则取 `gap_id(need)`。所以 `capability ingest / queue / confirm` 的闭环（K5）不需要改动。

详见 `design/falcon-surface-formats-v2.md` §10。


## 14. 按动作集 / 战术集重建能力目录（2026-10-29）

**决定**（用户确认）：
- 目录按装甲车工程的七层结构重建，分为动作集（能力、动作、黑板、仲裁）和战术集（战术子树、状态树、任务状态机）；
- 只收工程已实现的内容；
- 以装甲车工程为主版本，导出正式层到仓库；
- 现有模块一次迁移到新格式，只保留一份目录。

**新目录**：`datasets/platform_capabilities/armored-vehicle-afsim-2.9/`，**只在本机**（用户决定：派生自受控工程，不进 GitHub；`.gitignore` 只放行 `datasets/platform_capabilities/README.md`）。原 `afsim-2.9/`（从手册整理的 80 项）已删除。换电脑时复制整个目录或用 `CROW_CAPABILITY_CATALOG` 指向它；没有能力集时能力核对记 `CAP000` 跳过，依赖它的单元测试自动跳过（`tests/conftest.py` 的 `capability_set`）。

| 文件 | 内容 |
| --- | --- |
| `index.yaml` | 格式 `CapabilitySet 1.0.0`、七个切片、条目数、源内容哈希 |
| `action_set/<切片>.yaml` | 资源与仲裁、黑板字段、原语、动作契约、战术步骤 |
| `tactic_set/<切片>.yaml` | 战术子树、状态树、任务状态机、条件 |

规模：7 个切片，原语 33、动作 53、战术步骤 3（共 96 个条目，含 7 个切片条目），战术子树 39、状态树 49、任务 6。

**导出** `capability/lab_export.py`，命令 `python -m afsim_research_agent.capability export-lab --lab <工程目录> [--check]`：
- 读取范围：`catalog/`、`actions/`、`schema/`、`arbitration/`、`tactics/`、`behavior_trees/`、`missions/`。不读取 `bindings/`、`model/`、`scenarios/`、`fixtures/`、`tests/` 和 `_local`。
- 把各切片不同的写法（字段名、引用写法、两种状态机格式）统一为一种格式。
- 与动作同名的原语并入动作。
- 战术引用了、但没有契约的步骤登记为 `tactic_step`。
- 字段走白名单；每个字符串都扫描，含原生类型或符号、绑定引用、路径、资料标记或口径类参数的一律清除，清除清单只在本机输出。标识符本身不合格时整次导出失败。
- 写入前再扫描一遍；结果逐字节可复现；只写有变化的文件。

**Agent 侧补充**（不改动工程；因为列出了工程条目 ID，与能力集放在同一本机目录，导出不会覆盖）：
- `<能力集>/ir_alignment.toml`，取代 `aliases.toml`，内容为：
  - 96 个条目的中文名称、IR 直接映射、检索用语；
  - IR 元素的组合映射，如 `keep_station` = 采用槽位 + 驶向槽位；
  - 运行时提供的元素：黑板、状态计时、仿真时间；
  - 角色依赖，`mission_processor` 记为运行时提供。
- `capability/groups.toml`（仓库）与 `<能力集>/group_members.toml`（本机）：
  - 仓库只保留组名、简化方式和 IR 元素归组，各组包含哪些条目放在本机文件；
  - 地形、保障、任务控制、电子对抗四组没有任何条目；
  - 新增"工程未实现的 IR 元素属于哪一组"，用于粗粒度判断是缺口还是简化处理；
  - 新增 `not_applicable`（地面车辆不适用高度）。

**判定的变化**：
- 状态：`implemented` → 可实现；`tactic_step` → 部分可用；目录外 → 需开发。新目录中不再有"需确认"。
- 实现了、但 IR 无法表达的条目（如指挥接替选举），在 Crow 标注时记为"需扩展 IR"。
- **Falcon 生成约束新增一档"工程未实现"**：IR 元素在目录中没有任何实现时，照常列出并标【工程未实现】，允许使用，生成后记为开发缺口。原因是这些缺口只阻塞 AFSIM 导出，不应阻塞战术稿和二维仿真（§6 第 2 条）。只有平台类别不适用的元素才不列出、编译时拒绝。
- 装甲排示例（细粒度）的缺口：
  - 发送消息、收到消息：工程只有特定的航迹、目标、任务、队形消息，没有通用消息；
  - 当前地形类别、到某类地形的距离；
  - 跃进轮次。

  粗粒度下，前两项改为简化处理，剩 3 项。走完路线、状态计时、最近敌方距离、编队保持由"需确认"变为可实现或可组合实现。

**扩展接口**：`capability/libraries.py` 用同一份目录实现动作集与战术集接口，内置为默认实现，只填空槽位，用户自己注册的实现优先。
- `list_actions`：动作与战术步骤，原语不列；
- `find_tactics`：战术子树与任务状态机，带阶段、角色与用到的动作闭包，按中文用语匹配。

**测试**：`tests/unit/capability` 39 项，其中新增导出与库测试 5 项（导出用合成的小工程，不需要真实工程）。Crow、Falcon、编排层相关测试按新目录改写。有能力集时全部 550 项通过；没有时 96 项跳过、其余通过（另有 11 项文献 / 网页测试在容器内因桩模块失败，与本次无关）。
