# 项目现状总结（2026-10-13）

## 1. 总体链路

```text
自然语言需求
  → Tactical Crow：检索规划 → 多源检索 → 筛选/采集 → PaperQA 证据问答 → 证据包 + 准入门槛   [已完成 v0.3.1]
  → Tactical Falcon：证据包 → behavior_spec.yaml 草稿 → 审查清单 → 文件式确认              [v1 已完成，待在线评测]
  → 行为模型 IR：L1 校验 → 确认闸门 → 规范化 → L2 单步测试 → 后端生成 → L3 闭环想定          [已完成 v1]
  → 外部引擎：联合仿真（tbe-step/1 + C++ 桥接）/ 原生后端（一致性用例包 + 轨迹判定）        [框架完成，待接入真实引擎]
```

## 2. 已开发内容

### 2.1 Tactical Crow（检索与证据，v0.2–v0.3.1）

| 能力 | 说明 |
| --- | --- |
| 检索规划 | 术语表注入、多类检索式（网页/学术/条令/技术报告/中文）、缺口补充轮 |
| 多源检索 | Tavily、OpenAlex、Semantic Scholar、arXiv、CORE、本地文献库、引文滚雪球 |
| 筛选与去重 | URL/DOI/标题/内容哈希去重、DOI 校验、撤稿过滤、条令版本替代、LLM 摘要打分（0–3） |
| 采集 | 并行下载、PDF/HTML 识别、备用链接、站点熔断、SSRF 防护、显式代理 |
| 证据 | PaperQA2 语料与问答、A/B/C/D 质量分级、准入门槛（Falcon 输入条件）、运行清单、报告 |
| 评测 | 金标评测集（12 问题 / 22 金标草稿）、召回/精度统计、诊断输出 |

当前基线（2 个问题）：金标发现率 0.75，采用率 0.25；主要瓶颈是采集（DNS/网络），需配置代理。

### 2.2 行为模型 IR v1（设计第 1–7 步）

| 模块 | 说明 |
| --- | --- |
| 规范与校验 | 严格 YAML、Pydantic 模型、JSON Schema、全部错误码（IR/ID/REF/UNIT/TYPE/BEH/ACT/RES/TREE/FSM/DEP/ASM/EVD/PAR/ROLE/FORM/COORD/BIND/SCN） |
| 目录 | 25 个中立动作、10 类操作数 + 6 个函数、4 种协同模式、15 个战术意图 |
| 证据与确认 | `basis`（明确/推断/冲突/未知/用户指定 + 确认状态），未确认项阻塞编译 |
| 规范化 | 标准单位、协同模式展开、表达式 AST、内容哈希 |
| 测试 | L2 单步语义测试 + 转换覆盖；L3 二维闭环仿真 + 10 种可观测量 |
| 后端 | Mermaid、BehaviorTree.CPP XML（回读校验）、python_sim |
| 产出 | 一键构建、`review.md` 审查报告、确定性生成、不覆盖外来文件 |
| 示例 | 装甲排地形自适应编队（4 车，3 种协同，14 个 L2 + 3 个 L3，全部通过） |

### 2.3 Tactical Falcon v1（设计第 8 步）

| 模块 | 说明 |
| --- | --- |
| 门槛与证据规范化 | Crow 门槛未通过即拒绝；主张抽取 + 引文逐字核验，证据 ID 由内容哈希决定；同义合并，数值区间不重叠由代码判为冲突；写出 `evidence_package.json` |
| 起草 | 需求拆成原文条款 → 骨架（角色、状态、协同模式，条款覆盖检查）→ 6 段结构化生成；basis 规则由代码强制 |
| 校验修复 | `ir test --draft` 驱动，只重生成出错的段，最多 3 轮 |
| 审查与确认 | `confirmations.yaml`、`falcon_review.md`、`falcon_trace.json`；`falcon confirm` 保留注释写回，确认后可直接 `ir build` |

详见 `docs/handoff/2026-10-13-falcon-v1-implementation.md`。

### 2.4 外部引擎对接

| 模块 | 说明 |
| --- | --- |
| 联合仿真 | `EngineAdapter` 协议、按步同步运行器、观测→操作数映射、两种消息模式、轨迹记录与判定 |
| 单步控制协议 tbe-step/1 | TCP + 长度前缀 JSON，10 种请求，统一错误码与时间语义；Python 客户端/参考服务端；17 项一致性检查 |
| C++ 桥接骨架 | C++17 无依赖，网络线程/引擎线程分离，`IEngine` 抽象接口 + 演示引擎；已通过一致性检查并复现示例结果 |
| 原生后端支持 | 后端开发者指南（17 条执行语义）、一致性用例包导出与比对、实验室引擎适配器模板 |

## 3. 文件框架

```text
afsim-research-agent/
├── src/afsim_research_agent/
│   ├── crow/            检索规划、研究流程、准入门槛、清单、报告、CLI
│   ├── literature/      候选收集与采集、LLM 筛选、元数据校验、本地文献库、PaperQA 适配
│   ├── web/             搜索/学术 API、安全下载、网页与 PDF 读取、SSRF 防护
│   ├── domain/          战术术语表、条令版本替代（TOML 数据）
│   ├── evidence/        质量分级、去重、合并
│   ├── knowledge/       词法/语义/混合检索
│   ├── evaluation/      检索评测
│   ├── falcon/          需求 + 证据 → IR 草稿、审查清单、文件式确认
│   └── ir/              行为模型 IR
│       ├── models.py loader.py validate.py coordination.py normalize.py
│       ├── runtime.py（参考解释器） l2.py conformance.py review.py pipeline.py binding.py
│       ├── catalog/     actions / operands / coordination / intents（TOML）
│       ├── schema/      behavior_spec.schema.json
│       ├── backends/    mermaid、btcpp_xml、python_sim
│       ├── sim/         二维仿真与 L3 判定
│       └── cosim/       引擎接口、联合仿真运行器、tbe-step/1 协议、客户端/服务端、
│                        一致性检查、运动学引擎、参考引擎、轨迹、单位换算
├── examples/armor_platoon_terrain_formation/   示例模型包（含生成物与报告）
├── integrations/
│   ├── lab_engine/          实验室引擎 Python 适配器模板与对接指南
│   └── engine_bridge_cpp/   tbe-step/1 C++ 引擎宿主骨架
├── eval/retrieval_gold.toml  检索金标评测集
├── docs/design/             IR 设计、单步协议、后端开发者指南、引擎对接
├── docs/handoff/            各阶段交接文档
├── scripts/run_checks.sh    quick / eval / ir 检查
└── tests/                   单元测试（IR 107 个，Falcon 20 个）与 live 测试
```

常用命令：`python -m afsim_research_agent.falcon {draft|confirm|status}`；`python -m afsim_research_agent.ir {validate|test|build|simulate|cosim|serve|protocol-check|conformance|judge|schema}`；`bash scripts/run_checks.sh quick`。

## 4. 待开发与优化

### 4.1 优先级高

1. ~~**Falcon（设计第 8 步）**~~ v1 已完成（P1–P3，脚本化模型测试 20 项）；**待做 P4 在线评测**：需要在 Mac 上跑一次 Crow 装甲排需求，产出真实 `research_result.json` 后运行 `falcon draft` 并记录评测指标。
2. ~~**框架问题（来自示例测试分析）**~~ 已修复（2026-10-13，见设计文档 §30.6、后端指南 S18–S20）：
   - 同步转换消息只发一次 → 发起方重同步心跳（默认 5 s）。20 个随机种子下全部僚车按时切换：25% 丢包 8/20 → 20/20，50% 丢包 1/20 → 20/20，75% 丢包 0/20 → 17/20；"始终未切换"在各丢包率下均为 0。
   - 交替跃进无停止条件 → `standoff_distance` + `standoff_reached`，跃进朝向最近敌/目标航点并留出 `stop_short`。接敌距离 1.0/1.5/2.0 km 时最近距离约 795 m（standoff 800 m，原为约 200 m 并越过敌方）。
   - 同周期多份报告只确认一次 → 核实为设计行为（确认广播给全部报告方），已写入文档。
   - 另：网络联合仿真开启 `TCP_NODELAY`，单想定约 40 s → 4 个想定共 2 s。
3. **真实引擎接入**（在授权环境中）：实现 `IEngine`，通过 `protocol-check` 与 `cosim` 对比；之后再做原生后端，用一致性用例包和轨迹判定验证。

### 4.2 优先级中

4. **L3 测试强化**：增加 `ir sweep`（多随机种子 + 参数扫描；部分丢包想定与对敌距离期望已加入示例）；用扫描结果校准参数范围（如纵队间距上限）。
5. **检索采集**：配置 `CROW_HTTP_PROXY` 后重跑评测；按失败类别继续优化；完善金标评测集（需你审核）。
6. **更多示例**：单平台空中拦截、车队护卫，覆盖 `priority_selector`、`weighted_random`、`parallel`。

### 4.3 优先级低 / 后续

7. Deep Agents 编排：把 Crow、Falcon、IR 工具挂到 Agent 上，形成对话式工作流。
8. 物理模型线（C++ 插件）与行为模型线的接口约定。
9. 协议演进：gRPC/Protobuf 版本、批量步进、订阅式状态推送（视引擎侧需要）。

## 5. 资料边界

AFSIM 受控资料（文档、脚本、接口名称、示例代码）不进入本仓库和对话；AFSIM 相关实现由授权环境完成，与本项目只通过中立接口（IR、tbe-step/1、轨迹、一致性用例包）交互。
