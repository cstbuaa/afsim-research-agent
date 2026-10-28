# 生成的行为树如何测试：方法与技巧调研（2026-10-19）

背景：py3 到 py6 的在线运行里，验证块（L2 单步测试、L3 想定）一直是主要错误来源。模型写的测试常出现三类问题：
- 输入给不对；
- 意图和行为树对不上；
- 覆盖不全。

而一旦测试本身有错，就分不清错在测试还是错在树。本文调研测试生成程序和行为树的做法，并对照本项目给出可落地的建议。

## 1. 方法概览

| 方法 | 代表工作 | 核心做法 | 与本项目的关系 |
| --- | --- | --- | --- |
| 测试与实现由不同的代理分别生成 | AgentCoder（Huang 等，2023） | 写测试的代理看不到实现代码。消融实验：测试准确率在 HumanEval 上从 61.0% 升到 87.8%，在 MBPP 上从 51.8% 升到 89.9% | 我们的验证稿能看到整份战术稿（包括行为树），测试容易顺着树写，或被树的细节带偏 |
| 写性质，不写具体样例（PBT） | PGS（He 等，2025）；CHEKPROP（Etemadi 等，2025，面向 CPS，用 Hypothesis） | 模型只写不变式或性质，输入由工具生成；失败时反馈最短的反例。比测试驱动方法的 pass@1 相对提高 23–37%。CHEKPROP 抽取性质的召回率 94%，精确率 72% | 模型最常错的是"给什么输入"。可以让编译器根据条件自动构造输入，模型只表达意图 |
| 轨迹上的时序性质 | RoboEval / CodeBotler（Hu 等，2024） | 在符号模拟器里执行程序，记录技能调用轨迹，用类 LTL 的时序语言检查；每个任务有多个初始世界状态，必须全部通过；只检查性质，不要求固定脚本 | 对应我们的 L3 期望：可以补充"多个初始条件"和更多时序算子 |
| 证伪 / 基于搜索的想定生成 | Scenic + VerifAI（Viswanadha 等，2021） | 想定写成参数分布，采样器（交叉熵、多臂老虎机）搜索违反规约的参数；规约可以是 STL 或度量函数；并行仿真 | 红方反制战术转成 L3 想定（待办），以及参考值调参（`ir sweep`）都可以采用 |
| 模型检验 | BehaVerify（Serbinowska、Johnson，2022）；有状态行为树形式化（Serbinowska 等，2024） | 把 PyTrees 行为树连同黑板译成 nuXmv 模型，检查 LTL / CTL 性质，给出反例；可处理 2 万节点量级 | 我们的 IR 是有限状态机加行为树加离散化条件，可以导出到 nuXmv 检查死锁、可达性和安全性质 |
| 用规划搜索违反约束的执行 | Köckemann 等，ICAPS 2023 | 设计期离线搜索可能违反数据约束或顺序约束的执行序列，输出反例轨迹；在商用机器人和 3000+ 节点的随机树上评估 | 可用来检查"消息未收到却进入下一状态"之类的约束 |
| 变异测试评价测试质量 | 工业机器人变异测试（Santos 等，2025） | 设计领域变异算子（运动指令、夹爪动作、传感器噪声）；测试集能"杀死"多少变异体，就衡量了它的充分性；领域算子产生的无效或等价变异体更少 | 可以在 IR 上做变异（改转换目标、取反条件、改阈值、删动作），用存活的变异体告诉模型缺了什么测试 |
| 蜕变测试 | Adigun 等，2022（无人机避障仿真） | 没有精确判据时，检查输入变化与输出变化之间应满足的关系 | L3 很难写出精确期望，可以改写成关系，例如"敌人更远，接敌时刻不应更早" |
| 规约、注释、代码三方一致 | Clover（Sun 等，2024） | 三者两两核对，任何一对不一致即判为不可信 | 条款与战术、测试、行为树之间可以两两核对 |
| 多候选相互印证 | CodeT（Chen 等，2023） | 生成多份代码和多份测试，按"代码通过测试"的一致程度选出最可信的组合 | 成本高；可以只在验证块上用：多次采样测试，取多数一致的期望 |

## 2. 对本项目有用的技巧

按"收益 / 成本"排序。

### T1. 测试输入由编译器构造，模型只写意图（PBT 思路）

- 现状：模型自己给 `given`，常漏掉条件需要的输入，或给错。py6 中 13 条转换没有测试，另有 1 个测试的意图与树不符。
- 做法：
  - 编译器已经知道每条转换的条件表达式和读取的输入（`transition_checklist`）；
  - 对比较类条件求一组满足的取值和一组不满足的取值，例如 `distance_to_class.urban <= 200 m` 取 150 m 和 250 m；
  - 由此为每条转换生成"条件为真，更高优先级的条件为假"的测试骨架；
  - 模型只确认期望的下一状态，或者只回答"两个条件同时成立时谁优先"。
- 收益：转换覆盖和输入正确性由工具保证，模型的错误集中到真正需要判断的地方，也就是意图。
- 注意：自动生成的测试是拿模型自己去检验自己，只能发现可执行性、优先级和树阻断转换这类问题，发现不了意图错误。意图需要独立的依据，见 T3、T4。

### T2. 优先级用决策表提问，不让模型写测试

- 找出同一状态下可能同时成立的出边条件对，例如接敌和接近城区，列成表让模型（或人）填写"谁优先"。
- 编译器再把表格转成 `priority_transition` 测试。
- 比起让模型自己想出优先级测试，这样不会遗漏，也不需要模型构造输入。

### T3. 测试和行为树分开生成

- 验证稿提示里去掉行为树块，只给状态图、条件、协同、条款和战术（AgentCoder 的结论）。
- 测试与树不一致时就是真正的分歧。分歧交给条款和战术裁决，`_fired_hint` 会指出是测试漏了输入，还是条件漏读了输入。
- 附带好处：验证稿提示变短。

### T4. 用变异得分衡量测试质量

- 变异算子：
  - 转换目标换成另一个状态；
  - 条件取反；
  - 阈值参数放大或缩小一倍；
  - 删除一个动作；
  - 交换两条出边的优先级；
  - 去掉 `[sync]`。
- 每个变异体都跑一遍 L2（毫秒级），统计被杀死的比例，写进审查报告。
- 存活的变异体直接说明缺什么测试，例如"把 contact 的阈值翻倍后没有任何测试失败"。这条信息可以作为修复提示，也能提醒人工复核第 9 节的记录结果。
- 解决的问题：记录的测试结果本质上是快照测试，快照只能证明"和第一次一样"。变异得分能说明测试本身有没有判别力。

### T5. L3 期望改为"性质 + 多个初始条件 + 鲁棒度"

- 每个想定按 seed、敌人位置、出发位置自动派生 3–5 个变体，期望必须全部满足（RoboEval 的多世界状态做法）。
- 期望结果输出余量（STL 鲁棒度），而不只是通过或失败。例如 `hostile_range_min ≥ 775 m`，实测 812 m，余量 37 m。余量可以给参考值调参提供梯度，也能看出哪些期望离失败只差一点。
- 补充少量蜕变关系，作为没有精确期望时的判据：
  - 敌人出现得更晚，接敌时刻不应更早；
  - 丢包率提高，确认消息数不应增加；
  - 路线整体平移，状态序列不变。

### T6. 静态图检查与模型检验

- 先做不依赖外部工具的部分：
  - 每个状态都可达；
  - 不存在无出边的非终止状态；
  - 每个协同条件都有发送方；
  - 每个状态里，非兜底转换的条件都至少读取一个可变输入，否则这条转换永远不会或总是触发。
- 再做：把 IR 导出为 nuXmv 模型（参照 BehaVerify 的编码），检查 LTL 性质，例如：
  - G(contact → F report)：接敌后最终会报告；
  - "收到确认之前不进入 bounding"。
- 反例轨迹可以直接转成 L2 / L3 测试。

### T7. 证伪式想定搜索（中长期）

- 用参数分布描述红方反制战术（伏击位置、时机、通信干扰强度），由采样器搜索使期望失败的参数，把找到的反例加入想定库。
- 这条与待办中的"反制战术转 L3 想定"是同一件事。

## 3. 建议的落地顺序

1. **T1 + T2**：自动构造输入和测试骨架，模型只写意图和优先级。预计可以消除目前验证块里的大部分错误。工作量中等，主要是条件求解：比较、逻辑组合，函数部分用 `FUNCTION_INPUTS` 映射。
2. **T4**：变异得分。实现简单（变异 IR 后重跑 L2），可以直接回答"记录的测试结果可不可信"。
3. **T3**：验证稿提示去掉行为树。改动小，可以和下一次在线运行一起验证。
4. **T5、T6**：配合 L3 和 AFSIM 后端推进。
5. **T7**：与红队反制想定一起做。

## 4. 本次已落地的相关修改

见 `docs/design/falcon-surface-formats-v2.md` §10.8：
- 按阶段计分；
- 只有想定出错时 L2 仍运行；
- 编译期转换覆盖检查与转换清单；
- 失败原因提示；
- 想定阈值的量纲检查与"约束恒成立"检查。

## 参考

- AgentCoder: Multi-Agent Code Generation with Effective Testing and Self-optimisation — https://arxiv.org/html/2312.13010v3
- Use Property-Based Testing to Bridge LLM Code Generation and Validation (PGS) — https://arxiv.org/html/2506.18315v1
- LLM-based Property-based Test Generation for Guardrailing Cyber-Physical Systems (CHEKPROP) — https://arxiv.org/html/2505.23549v1
- Deploying and Evaluating LLMs to Program Service Mobile Robots (CodeBotler / RoboEval) — https://arxiv.org/html/2311.11183v3
- Parallel and Multi-Objective Falsification with Scenic and VerifAI — https://arxiv.org/pdf/2107.04164
- BehaVerify: Verifying Temporal Logic Specifications for Behavior Trees — https://arxiv.org/html/2208.05360v2
- Formalizing Stateful Behavior Trees — https://arxiv.org/pdf/2411.14165
- Planning for Automated Testing of Implicit Constraints in Behavior Trees (ICAPS 2023) — https://ojs.aaai.org/index.php/ICAPS/article/view/27247
- Mutation Testing for Industrial Robotic Systems — https://arxiv.org/abs/2511.14432v1
- Metamorphic Testing in Autonomous System Simulations — https://arxiv.org/abs/2209.11031v1
- Clover: Closed-Loop Verifiable Code Generation — https://ai.stanford.edu/blog/clover
- CodeT: Code Generation with Generated Tests（Chen 等，ICLR 2023）
