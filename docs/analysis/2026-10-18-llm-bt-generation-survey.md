# 大模型生成行为树方法调研

2026-10-18

大模型生成行为树（BT）已从“直接让 LLM 写 XML”演进为“LLM 负责语义与骨架、确定性程序负责结构与正确性”：中间表示转换、符号规划、搜索修复与闭环验证是主流方向，与 Falcon v2 的战术单（tactic sheet）思路一致。

## 方法族总览

现有工作可归为八类，越往下 LLM 的职责越窄、确定性组件越多，结构正确率也越高。

| 方法族 | 核心思路 | 代表工作 | 优势 | 主要局限 |
| --- | --- | --- | --- | --- |
| 直接提示生成 | LLM 按节点库直接输出 BT XML/JSON，配合 CoT、分阶段提示、少样本示例 | LLM-as-BT-Planner、Phase-Step、Naher 等 | 实现最简单，零训练 | 长树结构易错，节点越界，难以保证可执行 |
| 小模型微调 | 在自生成的 (指令, BT) 数据上 LoRA/QLoRA 微调 1B–10B 模型 | LLM-BRAIn、LLM-MARS、BTGenBot、BTGenBot-2、CommandSwarm | 可本地部署，延迟低，节点词表收敛 | 依赖数据规模与质量，推理能力有限 |
| 代码/中间表示转换 | LLM 先写 Python/DSL，再用 AST 规则确定性映射成 BT | Code-BT、Behavior Branch、PORTAL | 利用 LLM 代码能力，结构由编译器保证 | 需设计 API 与映射规则 |
| LLM + 符号规划 | LLM 只给目标条件或启发式，规划器构造可证明正确的 BT | OBTEA、HBTP、MRBTP、BETR-XP-LLM | 可证 sound/complete，树最优或近优 | 需完整前置/效果模型，领域建模成本高 |
| LLM + 搜索/演化/综合 | LLM 给初始树或草图，MCTS/GP/程序综合在空间内搜索修复 | LLM-MCTS-BT、LLM+GP、BtBot | 能跳出 LLM 单次输出的局部错误 | 搜索代价高，需评价函数 |
| 闭环反馈与运行时修复 | 执行失败后由 LLM/VLM 改写子树并更新 | LLM-BT、LLM-HBT、LLM-CBT、SRDrone、BTGenBot-2 | 适应环境变化，可持续改进 | 依赖仿真或真实执行环境 |
| 多模态生成 | VLM 结合图像/视频生成或补充条件节点 | Wake 等（Microsoft）、Izzo 等 2026 | 将感知直接接入条件判断 | 视觉条件难以验证 |
| 评估与验证 | LLM 文本仿真、形式化模型检验、基准集 | BeSimulator、Yang 等（NuSMV）、BTSIMBENCH | 无需物理仿真即可筛选，或给出可证保证 | LLM 仿真可信度有限；形式化需建模 |

兵棋与游戏领域（Liu 等 2026，Gameloft FSE 2025）基本复用上述前两三类，再加知识图谱或设计规则约束。

## IGDL 综述的分类框架

[Li 等 2024（IGDL/TAIIC，arXiv 2401.08089）](https://arxiv.org/abs/2401.08089) 把 BT 生成分为传统方法与 LLM 方法两大支，并提出了统一的 BTGen 训练与验证框架。

**传统方法**：人工设计；演化计算（遗传编程、文法演化）；强化学习；示教学习；基于规划（LTL、分层/HTN、主动推理、Colledanchise 的反应式规划）；案例推理；基于 BT 文法的 MCTS。

**LLM 方法**三条路线：

- 提示工程：分阶段（Phase-Step）、SoT、CoT、ToT/GoT/XoT、Structured-CoT、Chain-of-Code。综述建议“先生成控制结构、再填充叶节点”和“先写代码、再转 BT”。
- 微调：LLM-BRAIn（Alpaca 7B，8.5k 条 self-instruct 数据）、BT 嵌入检索、P-tuning/LoRA/QLoRA。
- 智能体：记忆/RAG、行动（节点库、ToolCoder/CRAFT/LATM 式工具生成）、规划（PDDL、MCTS、LATS、RAP、LLM-DP）、角色（多智能体分工）、自我精炼（Self-Refine、Reflexion）。

**BTGen 框架**（BTGen = M ⊕ D，模型与数据并重）：

1. 数据模式五字段：Name、Description、XML、Nodes Metadata、Implementations。
2. 数据合成：self-instruct/evol-instruct + 节点库 + MCTS 式组合生成。
3. 训练：领域预训练 + 指令微调。
4. 多级验证：语法 → 节点合法性 → 仿真执行 → 任务达成。

综述讨论的主要挑战（归纳）：高质量数据稀缺、长序列与复杂结构推理、节点库与领域知识对齐、可验证性与安全、评价基准不统一。

## 直接生成：提示与微调

直接生成的关键已从“模型够大”转向“词表封闭 + 静态校验 + 局部重生成”：BTGenBot-2 用 1B 模型加闭词表校验，零样本成功率从 57.69% 升至 84.61%。

| 工作 | 方法 | 关键结果 |
| --- | --- | --- |
| [LLM-BRAIn](https://arxiv.org/abs/2305.19352)（Lykov & Tsetserukou 2023） | Alpaca 7B 在 8.5k 条 self-instruct 生成的 (指令, BT) 对上微调 | 生成树在人工评价中接近人工编写水平 |
| [LLM-MARS](https://arxiv.org/abs/2312.09348)（Skoltech 2023） | Falcon-7B + LoRA，约 7.5k 对；多机器人任务分配与问答 | 多机器人场景下 BT 生成与任务问答一体 |
| [BTGenBot](https://arxiv.org/abs/2403.12761)（Izzo 等，IROS 2024） | ≤ 7B 模型微调 + 静态语法检查，仿真与实车验证 | 证明轻量模型可用 |
| [BTGenBot-2](https://arxiv.org/abs/2602.01870)（2026） | Llama-3.2-1B QLoRA，5,204 对；闭词表校验；运行失败时只重生成出错子树 | 零样本 SR 57.69% → 84.61%；Isaac Sim 52 任务基准 |
| [小 VLM 多模态 BT](https://arxiv.org/abs/2603.06084)（Izzo 等 2026） | Gemma-3 4B，图像 + 指令生成 BT | SR 87% |
| [CommandSwarm](https://arxiv.org/abs/2605.07764)（Majeed & Majid 2026） | 安全过滤 + 白名单解析器 + Falcon3-10B LoRA（2,063 对），群体无人机 | 白名单解析在执行前过滤不安全或越界节点 |
| [LLM-as-BT-Planner](https://arxiv.org/abs/2409.10444)（Ao 等 2024） | 一步生成、迭代生成、人在回路、递归 ICL 四种策略；装配任务 | 微调改善小模型的结构正确性，但不提升推理 |
| [Ao 等（人类反馈）](https://arxiv.org/abs/2409.09435)（2024） | 人类自然语言反馈逐步修正装配 BT | 支持人在回路的迭代修正 |
| [Phase-Step 提示](https://paperswithcode.com/paper/robot-behavior-tree-based-task-generation) | 分阶段-分步骤提示 + BT 嵌入检索示例，生成分层任务 | 不需预定义原语任务；消融验证了分阶段提示 |
| [Naher & Sukthankar](https://sbp-brims.org/)（SBP-BRiMS 2025） | 军事/应急场景 BT 的提示工程比较 | 具体化提示 + 低温度可消除语法错误 |

共性结论：大模型零样本在长树上容易出现节点越界和控制节点误用；小模型微调主要解决格式与词表，推理仍需外部结构支持。

## 代码/中间表示再确定性转换

让 LLM 写它擅长的代码或 DSL，再由规则编译成 BT，是目前结构正确率提升最明显的方向：Code-BT 在 GPT-4o 上的任务成功率为 83.33%，而让 LLM 直接转换为 66.67%。

| 工作 | 方法 | 关键结果 |
| --- | --- | --- |
| [Code-BT](https://www.ijcai.org/proceedings/2025/)（Zhang、Li 等，IJCAI 2025，含 IGDL 作者 Fu Li） | 三步：API 选择 → 受规则约束的 Python 代码 → AST 规则映射为 BT XML；执行反馈回修代码 | AST 映射 83.33% vs LLM 转换 66.67%（GPT-4o） |
| [Behavior Branch](https://arxiv.org/abs/2402.07442)（Ito & Takahashi 2024） | Code Llama 生成可执行分支代码，并入游戏智能体 BT | 代码形式便于增量添加行为 |
| [PORTAL](https://arxiv.org/abs/2503.13356)（腾讯 2025） | LLM 离线生成 BT DSL 策略，结合量化指标与视觉反馈的双重反馈迭代 | 策略离线生成、在线零推理开销，可用于大量游戏场景 |

共性：中间表示把“语义”和“结构”分离，结构错误变为编译错误，可以精确定位到代码行后回给 LLM 修复。

## LLM + 符号规划

这一类只让 LLM 输出目标或启发式，树由反向扩展规划器构造，因此能保证可靠性（sound）与完备性；代价是需要每个动作的前置条件与效果模型。主要来自国防科大（NUDT）与瑞典 ABB/KTH 团队。

| 工作 | LLM 的职责 | 规划器的职责 | 关键结果 |
| --- | --- | --- | --- |
| [OBTEA + LLM 意图理解](https://arxiv.org/abs/2405.07474)（Chen 等，IJCAI 2024） | 把自然语言指令转为一阶逻辑目标 | 最优 BT 扩展算法（OBTEA）生成代价最优的树 | 树由算法保证正确且最优，结果质量取决于 LLM 给出的目标是否正确 |
| [HBTP](https://arxiv.org/abs/2406.00965)（Cai 等，NUDT 2024） | 给出启发式动作路径、缩小动作空间，并根据反馈反思 | 沿启发路径优先反向扩展 | 大动作空间下规划时间显著下降 |
| [MRBTP](https://arxiv.org/abs/2502.18072)（Cai 等，AAAI 2025） | 可选插件：为各机器人预规划任务相关子树 | 多机器人并行反向扩展，意图共享避免重复 | 证明 sound 与 complete；LLM 插件加速并提升协作效率 |
| [BETR-XP-LLM](https://arxiv.org/abs/2409.13356)（Styrud 等 2024） | 给出目标条件，并在规划器卡住时补全缺失的前置条件 | 反应式 BT 扩展规划 | LLM 只在必要时介入，其余全部确定性 |

对我们的启示：战术场景的动作（机动、开火、报告）前置/效果较难完整建模，全规划不现实，但“LLM 给目标、规划器补前置”可用于局部子树（如交战前提、阵型保持）。

## LLM + 搜索、演化与程序综合

当没有完整的动作模型时，可以把 LLM 当作初始解或编辑建议的来源，再用搜索在评价函数下修复和优化。

| 工作 | 方法 | 关键结果 |
| --- | --- | --- |
| LLM-MCTS-BT（Wang 等，含 Fu Li，AAMAS 2026 扩展摘要） | LLM 提出对树的编辑动作，MCTS 搜索编辑序列，LLM 充当评分器 | ALFWorld 成功率 82.5% |
| LLM + GP（Verdaguer-Gonzalez 等，SIMPAR 2025） | LLM 生成初始种群，遗传编程在仿真适应度下进化 | LLM 初始解为 GP 提供语义合理的起点，GP 修正 LLM 的细节错误 |
| [BtBot 草图综合](https://doi.org/10.1145/3763178)（Zhang、Chen、Hong，NUDT，OOPSLA 2025） | LLM 生成带空洞的 BT 草图，程序综合按示例搜索填充并修复 | 将 LLM 的“大致正确”与综合器的“精确正确”结合 |

草图 + 空洞（hole）的思路与 Falcon v2 的 `SheetProblem` 空洞机制高度相似：可以把部分空洞交给编译器枚举填充，而非全部回给 LLM。

## 闭环反馈与运行时修复

闭环类方法把“生成”变为“生成 → 执行 → 定位失败节点 → 局部改写”的循环，共同做法是只重写失败子树而非整棵树。

| 工作 | 方法 | 关键结果 |
| --- | --- | --- |
| [LLM-BT](https://arxiv.org/abs/2404.05134)（Zhou 等，ICRA 2024） | ChatGPT 生成步骤 → BERT 解析为结构化动作 → 构建 BT；环境变化时在线更新 | 对外部干扰具有鲁棒性 |
| [LLM-HBT](https://arxiv.org/abs/2510.09963)（2025） | 异构多机器人；失败驱动的 BT 扩展 | 按需扩展而非重新生成 |
| LLM-CBT（IROS 2025） | 无人机-无人车协同的闭环 BT 生成 | 空地协同场景验证 |
| [SRDrone](https://arxiv.org/abs/2508.15501)（2025） | XML BT；连续状态评估，分层修改，经验库积累 | 无人机任务自改进 |
| [VLM 驱动 BT](https://arxiv.org/abs/2501.03968)（Wake 等，Microsoft 2025） | VLM 生成 BT，并引入由 VLM 判断的视觉条件节点 | 视觉条件代替手写感知判断 |
| [Chekam 等](https://arxiv.org/abs/2508.09621)（2025） | 插件式节点库 + LLM 生成 | 成功率约 94% |
| [BTGenBot-2](https://arxiv.org/abs/2602.01870)（2026） | 运行时错误只重生成对应子树 | 见前文 |

Falcon v2 已有编译期的块级修复与回滚；缺的是“仿真运行期”的失败定位——可通过引擎 step 协议的状态轨迹把失败映射回战术单中的块与行。

## 评估、验证与基准

生成后如何判断“对不对”是当前最薄弱的环节，已出现三种思路：LLM 文本仿真、形式化模型检验、任务基准。

| 工作 | 方法 | 关键结果 |
| --- | --- | --- |
| [BeSimulator](https://arxiv.org/abs/2409.15865)（Wang 等，含 Fu Li，2024） | LLM 作为文本仿真器，逐节点推演状态变化并判断执行结果 | 提出 BTSIMBENCH（75 棵 BT） |
| [联合验证与精炼](https://arxiv.org/abs/2410.14865)（Yang 等，UT Austin 2024） | 程序 → 自动机 → NuSMV 模型检验，用验证通过的计划微调模型 | 非 BT 专用，但可直接用于 BT 转自动机 |
| [Dendron](https://arxiv.org/abs/2404.07439)（Kelley 2024） | 反向：用 BT 结构化约束 LLM 智能体行为 | 说明 BT 的可解释性优势 |
| BTGenBot-2 基准 | Isaac Sim 52 个任务 | 可复现的导航/操作任务集 |

多级验证顺序（综述与各工作的共识）：语法 → 词表/接口 → 结构规则 → 形式化或 LLM 推演 → 真实仿真。Falcon 的 check\_package + IR 诊断覆盖前三级，后两级尚缺。

## 兵棋与游戏领域的应用

兵棋/游戏领域的工作仍以“LLM 生成 + 领域知识校验”为主，尚未见到将规划器或形式化验证用于战术 BT 的公开工作。

- **Liu 等，《大模型赋能的战术兵棋决策智能体构建方法》**（《系统仿真学报》38(3), 2026）：LLM 把战术知识注入为 BT 分支，用知识图谱校验分支合理性。
- **Paduraru 等（FSE 2025 Industry，Gameloft）**：商业游戏 NPC 的 LLM BT 生成与设计师协作流程。
- **[PORTAL](https://arxiv.org/abs/2503.13356)（腾讯）**：离线生成策略 BT，面向大量游戏场景。
- **[Naher & Sukthankar](https://sbp-brims.org/)（SBP-BRiMS 2025）**：军事与应急场景的 BT 提示工程对比。

AFSIM 的特殊性在于多角色协同、状态机与 BT 混合、参数带物理单位，这些在上述工作中都未完整处理。

## 对 Falcon 的启示

Falcon v2（战术单 → 确定性编译 → IR）属于“中间表示再转换”一族，与 Code-BT 的结论一致，方向正确；下一步收益最大的是收紧词表与把部分空洞交给确定性程序。

| 建议 | 借鉴来源 | 在 Falcon 中的落点 | 优先级 |
| --- | --- | --- | --- |
| 条件块继续用 Python 表达式 + AST 编译；推广到行为片段 | Code-BT | `sheet/pyexpr.py`，可考虑用 Python 函数写小型子树 | 高 |
| 闭词表：生成前给出完整动作表，编译时拒绝词表外节点；词表外需求转为能力缺口 | BTGenBot-2、CommandSwarm | `sheet/prompts.py` action\_table + `crow_capability_gaps.yaml` | 高 |
| 空洞由编译器枚举填充（如参数引用、默认成功条件），仅无法枚举时回给 LLM | BtBot 草图综合 | `SheetProblem` + autofix | 中 |
| 局部子树用规划器生成：LLM 只给目标条件，规划器补前置 | BETR-XP-LLM、OBTEA、HBTP | 新增可选的 L2 子树扩展器，先在阵型/机动类动作上试点 | 中 |
| 运行期失败定位到块与行，只重写失败子树 | BTGenBot-2、SRDrone、LLM-HBT | 引擎 step 协议轨迹 + source map | 中 |
| LLM 文本推演作为快速预筛，在跑真仿真前淘汰明显错误的候选 | BeSimulator | verification 块的场景逐步推演 | 低 |
| 多候选 + 搜索：生成多个战术单，按检查得分或仿真结果挑选/编辑 | LLM-MCTS-BT、LLM+GP | `draft_sheet` 的 best-score 机制扩展为 beam | 低 |
| 用已有约 40 个示例与通过验证的草稿构建微调/检索数据，采用综述的五字段模式 | IGDL BTGen、LLM-BRAIn、Yang 等 | demo 数据集 → 战术单格式的 few-shot 检索库 | 低 |

待确认问题：规划器方向是否值得在 AFSIM 动作上补充前置/效果元数据？这决定了中优先级第 4 项是否可行。

## Sources

- [Li 等，A Study on Training and Developing LLMs for Behavior Tree Generation（arXiv 2401.08089）](https://arxiv.org/abs/2401.08089)
- [LLM-BRAIn（arXiv 2305.19352）](https://arxiv.org/abs/2305.19352)
- [LLM-MARS（arXiv 2312.09348）](https://arxiv.org/abs/2312.09348)
- [BTGenBot（arXiv 2403.12761）](https://arxiv.org/abs/2403.12761)
- [BTGenBot-2（arXiv 2602.01870）](https://arxiv.org/abs/2602.01870)
- [小 VLM 多模态 BT 生成（arXiv 2603.06084）](https://arxiv.org/abs/2603.06084)
- [CommandSwarm（arXiv 2605.07764）](https://arxiv.org/abs/2605.07764)
- [LLM-as-BT-Planner（arXiv 2409.10444）](https://arxiv.org/abs/2409.10444)
- [Ao 等，人类反馈 BT（arXiv 2409.09435）](https://arxiv.org/abs/2409.09435)
- [Phase-Step 提示的机器人 BT 任务生成](https://paperswithcode.com/paper/robot-behavior-tree-based-task-generation)
- [Behavior Branch（arXiv 2402.07442）](https://arxiv.org/abs/2402.07442)
- [PORTAL（arXiv 2503.13356）](https://arxiv.org/abs/2503.13356)
- [OBTEA + LLM（arXiv 2405.07474）](https://arxiv.org/abs/2405.07474)
- [HBTP（arXiv 2406.00965）](https://arxiv.org/abs/2406.00965)
- [MRBTP（arXiv 2502.18072）](https://arxiv.org/abs/2502.18072)
- [BETR-XP-LLM（arXiv 2409.13356）](https://arxiv.org/abs/2409.13356)
- [BtBot 草图综合（OOPSLA 2025，DOI 10.1145/3763178）](https://doi.org/10.1145/3763178)
- [LLM-BT（arXiv 2404.05134）](https://arxiv.org/abs/2404.05134)
- [LLM-HBT（arXiv 2510.09963）](https://arxiv.org/abs/2510.09963)
- [SRDrone（arXiv 2508.15501）](https://arxiv.org/abs/2508.15501)
- [VLM 驱动 BT（arXiv 2501.03968）](https://arxiv.org/abs/2501.03968)
- [Chekam 等（arXiv 2508.09621）](https://arxiv.org/abs/2508.09621)
- [BeSimulator（arXiv 2409.15865）](https://arxiv.org/abs/2409.15865)
- [Yang 等，联合验证与精炼（arXiv 2410.14865）](https://arxiv.org/abs/2410.14865)
- [Dendron（arXiv 2404.07439）](https://arxiv.org/abs/2404.07439)

以下仅据检索摘要，未打开全文：Code-BT（IJCAI 2025）、LLM-MCTS-BT（AAMAS 2026）、LLM+GP（SIMPAR 2025）、LLM-CBT（IROS 2025）、Naher & Sukthankar（SBP-BRiMS 2025）、Liu 等（《系统仿真学报》2026）、Paduraru 等（FSE 2025）。
