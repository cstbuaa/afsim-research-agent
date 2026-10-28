# Tactical Crow 战术提出设计稿（v1）

状态：**已确认，P1–P2 已实现**　日期：2026-10-16（修订 6：C4 之后增加能力标注，见 §18）
相关文档：`docs/design/tactical-falcon-v1.md`（Falcon v1）、`docs/design/falcon-surface-formats-v2.md`（生成层 v2）、`docs/analysis/2026-10-15-mut-4v4-thesis-review.md`（论文研读）、`docs/handoff/2026-10-14-demo-dataset-ir-conversion.md`（数据集与能力目录）

## 0. 背景与目标

现状：Crow 只做"需求 → 研究问题 → 检索 → 证据"，规划提示词明确要求"不要提出战术"；Falcon 直接从证据生成 IR。结果是模型在一步里同时要理解战术、对应能力、写对 IR，错误多，也没有"选哪种战术"这个环节。

目标：用户用自然语言提出战术需求后，Crow：

1. 理解需求，整理成结构化的**任务画像**，有影响战术选择的歧义先问用户；
2. 按"情况 → 动作 → 例外 → 依据"等维度规划研究问题并检索证据；
3. **以检索为主、创新为辅**提出候选战术（默认 5 个）：优先从高关联度资料中提取成熟战术；检索到的战术数量不足、覆盖不了需求或通不过检查时，先补充检索，仍不足才由 Crow 适当创新补位（默认最多 2 个）；创新候选给出推导过程并逐项标明依据；
4. 经能力检查、一致性检查和**另一个模型的对抗推演**后，交给用户选择；
5. 选定的战术交给 Falcon 生成规则集和 IR。

非目标（v1）：仿真比较与自动排序（预留接口，见 §9）；自动执行平台文件；修改 IR 语义。

## 1. 总体流程

```
用户需求（自然语言）
 │
 ├─ C1 需求理解        → mission_profile.yaml        [v1]
 ├─ C2 研究规划        → research_plan（扩展）        [v1]
 ├─ C3 检索取证        → research_result.json（已有） [v1，小改]
 ├─ C4 战术提出（检索优先，不足时创新补位）→ tactic_candidates.yaml
 │                       [v1：检索 + 改编 + 同领域组合；v1.1：跨领域迁移]
 ├─ C4b 能力标注（候选生成之后，不参与筛选）→ capability_needs、capability_gaps.yaml  [§18]
 ├─ C5 筛选
 │    ├─ 能力检查、一致性检查（代码）                 [v1]
 │    ├─ 对抗推演（另一个模型）→ redteam_review.yaml   [v1]
 │    ├─ 生成方回应 → 修订后的候选                    [v1]
 │    ├─ 用户选择 → tactic_selection.yaml            [v1]
 │    └─ 仿真比较                                     [v2，预留]
 └─ C6 交给 Falcon：选定战术 + 证据包 → 规则集 → IR   [v1，规则集格式另见设计稿]
```

模块分工：C1–C5 属于 Crow；C6 起属于 Falcon。Falcon 的输入从"Crow 结果 + 需求"变为"Crow 结果 + 用户选定的战术"。

## 2. C1 需求理解：任务画像

### 2.1 格式

```yaml
# mission_profile.yaml
format: MissionProfile
format_version: 0.1.0
requirement: 研究装甲排在开阔地和城区的编队方式：开阔地用楔形，接近城区时转纵队，纵队车距 50 米；接敌后报告并交替跃进，与敌保持安全距离。
domain: ground                       # ground | air | sea | joint
echelon: platoon                     # 层级
mission:
  type: movement_to_contact          # 取自 IR 意图词表
  objective: 沿路线机动至城区边缘，接敌后保持安全距离推进
own_forces:
  - {role_hint: platoon_leader, count: 1, platform_class: ground_vehicle}
  - {role_hint: wingman, count: 3, platform_class: ground_vehicle}
capabilities_ref: datasets/platform_capabilities/<platform>/capability_catalog.yaml   # 有则引用
enemy:
  known: 不明
  assumed: [可能存在反装甲火力]       # 来源：assumption，需确认
environment:
  terrain: [open, urban]
  other: []
constraints:                         # 用户给定，user_specified
  - {text: 纵队车距 50 米, parameter: column_interval, value: 50, unit: m}
  - {text: 开阔地用楔形, kind: formation}
success_criteria:                    # 成功标准；v2 起直接作为仿真评估指标
  - {measure: reach_objective, description: 到达城区边缘}
  - {measure: hostile_range_min, comparator: gte, threshold: standoff_distance, description: 接敌后与敌保持安全距离}
  - {measure: own_losses, comparator: eq, threshold: 0, description: 无己方损失}
open_questions:                      # 未解决的歧义
  - {id: q1, text: 敌方规模和反装甲火力射程是否已知？, blocking: false, default_assumption: 未知，按排级反装甲威胁处理,
     answer: 未知，按排级反装甲威胁处理, answered_by: default}
  - {id: q2, text: 是否有上级支援火力？, blocking: false, default_assumption: 无, answer: 无, answered_by: default}
user_specified: [纵队车距 50 米, 开阔地用楔形, 接近城区转纵队, 接敌后报告并交替跃进]
```

### 2.2 规则

- 只整理用户说了的和必须补的；**不在这一步提出战术**；
- `open_questions` 最多 5 个，其中 `blocking: true` 最多 3 个（会使候选战术完全不同，如"是进攻还是防御"）。阻塞问题**在命令行直接提问**，回车采用默认假设；用户回答写入 `answer`（`answered_by: user`），并通过 `profile_context` 注入之后所有提示词。非阻塞问题按 `default_assumption` 继续，并在报告中列出；
- 非交互运行（`--no-input` 或输入不是终端）时所有问题按默认假设处理；可编辑 `mission_profile.yaml` 后用 `--mission-profile` 重跑，跳过画像生成；
- 代码规范化：任务类型不在意图词表 → `custom`；地形不在词表 → 提示；`user_specified` 与约束原文不在需求中 → 提示核对；所有改动记入 `notes`；
- 用户原话中的约束保持原样，标为 `user_specified`，后续不得改动；
- 能力目录存在时，`own_forces` 只能使用目录中的平台类型；目录未完成时留空引用，后续能力检查降级为提示。

## 3. C2 研究规划

### 3.1 研究问题模板

研究问题覆盖六个维度（来源：论文研读 §3.3）：

| 维度 | 问题示例 |
| --- | --- |
| 适用情况 | 在什么态势、地形、兵力对比下采用该类战术？ |
| 角色与几何 | 各单位担任什么角色？阵型、间距、方位如何规定？ |
| 动作序列与结束条件 | 分几步？每步做什么、何时算完成？ |
| 例外与中止 | 什么情况下不采用、中途放弃或转入其他战术？ |
| 协同与决策权 | 由谁决定？通过什么信息协同？下级能否拒绝？ |
| 效果衡量 | 用什么标准判断战术成功？ |

研究问题上限从 4 个提高到 6 个；每个问题至少覆盖一个维度，六个维度至少覆盖五个。

### 3.2 相似场景检索

规划阶段额外产出：

- `pattern_hypotheses`：根据任务画像推测可能适用的抽象战术模式（§5.3），如 `bounding_overwatch`、`formation_by_terrain`；
- `analogy_queries`（1–3 条）：针对这些模式在其他层级、其他领域的检索词，如"leapfrog movement naval escort"、"有人无人协同 交替掩护"。

### 3.3 提示词调整

删除"Do not propose tactics yet"中的禁止项，改为："只提出战术模式假设，不提出具体参数"。`PLANNER_PROMPT_VERSION` 升级。

## 4. C3 检索取证（小改）

- 检索流程不变；
- 门槛检查增加**维度覆盖**：每个维度至少有一条强证据，不足的维度进入补充检索轮次；
- Falcon 的证据规范化（F1）前移为 Crow 的最后一步，C4 直接使用规范化后的主张（`evidence_package.json`）。

## 5. C4 战术提出

### 5.1 候选战术卡

```yaml
# tactic_candidates.yaml（节选）
- id: tc_01
  name: 地形自适应编队行进 + 接敌交替跃进
  provenance: retrieved            # retrieved | adapted | transferred | novel
  patterns: [formation_by_terrain, bounding_overwatch]
  summary: 开阔地楔形行进，接近城区转纵队；接敌后先报告，再以两车一组交替跃进，保持安全距离。
  applicability:
    - {situation: open_or_rolling_terrain, evidence: [ev_380393baa2]}
  roles:
    - {role: bounding_group, members: [platoon_leader, "wingman[0]"]}
    - {role: overwatch_group, members: ["wingman[1]", "wingman[2]"]}
  geometry:
    - {formation: wedge, when: open_terrain, evidence: [ev_380393baa2]}
    - {formation: column, interval: 50 m, when: approaching_urban, basis: user_specified}
  steps:
    - {phase: travel, do: 楔形/纵队行进, until: 接敌}
    - {phase: report, do: 向排长报告敌情, until: 收到确认或超时}
    - {phase: bound, do: 交替跃进, until: 到达目标或与敌距离小于安全距离}
  exceptions:
    - {situation: 通信中断, then: 保持当前队形并停止跃进, basis: inferred}
  decision_authority: leader_assigns   # leader_assigns | autonomous | hybrid
  success_criteria_ref: [reach_objective, hostile_range_min, own_losses]
  required_capabilities: [ground_mover, radio, sensor]
  support:                         # 逐项依据核对
    applicability: supported
    geometry: supported
    steps: partial                 # 报告步骤有依据，跃进节奏无依据
    exceptions: inferred
  assumptions: [a1_comms_available]
  risks: [交替跃进期间掩护组与跃进组距离过大时失去火力支援]
```

### 5.2 来源类型及依据要求

| 来源 | 含义 | 依据要求 | 依据状态 |
| --- | --- | --- | --- |
| `retrieved` | 证据中直接描述的战术 | 主要要素都有引文支持 | 按要素：explicit / inferred |
| `adapted` | **对检索到的战术做改编创新**：经一个或多个创新操作（§5.6）改变角色、编成、顺序、节奏、欺骗要素等 | `derivation` 字段：源战术（引文）+ 使用的操作 + 改动说明与理由 | 未改动的要素保留原依据；改动部分 inferred |
| `combined` | **同领域要素重组**：按形态分析（§5.6）把不同来源的战术要素组合成新战术 | `derivation` 字段：每个要素的来源（引文）+ 组合理由 + 相容性检查结果 | 要素按各自依据；组合方式 inferred |
| `transferred` | 来自其他层级、场景或领域，类比迁移 | `analogy` 字段：原场景、原战术、角色/几何/触发条件的对应关系、必须成立的前提、已知差异 | 整体 inferred，需确认 |

规则：

- 优先级：`retrieved` > `adapted` > `combined` > `transferred`；后三类只在 §5.7 的补位条件满足时生成；
- v1 启用 `retrieved`、`adapted`、`combined`；`transferred` 依赖案例库，在 v1.1 启用；
- 改编创新和组合产生的候选，**创新只体现在改编和组合方式上**，不得凭空引入证据中没有的战术要素；需要新要素时，该要素单独标为 `unsupported` 并在报告中提示补充调研；
- 候选之间**必须有差异**：每个候选至少在主要模式、角色分配、机动方式或决策权之一上不同；
- 用户给定的约束（`user_specified`）所有候选都必须满足；
- 非 `retrieved` 的候选在报告中必须醒目标注"非条令直接给出"，并展示推导过程。

改编示例（片段）：

```yaml
- id: tc_02
  provenance: adapted
  derivation:
    sources: [{tactic: 交替跃进（两车一组）, evidence: [ev_043d113c04]}]
    operators: [regroup, add_deception]
    changes:
      - {op: regroup, from: "2 + 2 分组", to: "3 + 1 分组：三车掩护、一车前出侦察", why: 城区入口通视差，先由单车抵近侦察降低整组暴露}
      - {op: add_deception, what: 掩护组在侦察车前出时向另一方向短暂机动, why: 分散敌方注意力}
  support: {geometry: inferred, steps: partial}
```

### 5.3 抽象战术模式表（初稿）

与领域无关的模式，作为相似场景检索和跨领域迁移的桥梁。每个具体战术（条令、论文、demo）挂到一个或多个模式下。

| 模式 ID | 名称 | 抽象角色 | 示例实例 |
| --- | --- | --- | --- |
| `bounding_overwatch` | 交替掩护推进 | 跃进组、掩护组 | 地面交替跃进；海上蛙跳护航 |
| `traveling_overwatch` | 行进间掩护 | 先导、主体 | 地面行进间掩护 |
| `formation_by_terrain` | 依地形变换队形 | 编队全体 | 开阔地楔形、城区纵队 |
| `fix_and_maneuver` | 正面牵制、侧翼机动 | 牵制组、机动组 | 地面牵制迂回 |
| `envelopment` | 两翼包围 / 钳形夹击 | 左翼、右翼、正面 | 空战钳形夹击；地面两翼包围 |
| `penetration` | 正面突破 | 突击组、支援组 | 空战正面强攻 |
| `feint_decoy` | 佯动 / 诱饵 | 诱饵、主攻 | 空战多向诱饵 |
| `ambush` | 伏击 | 杀伤区、阻截、警戒 | 地面伏击 |
| `relay` | 接力（火力、制导、通信） | 发起方、中继、接替方 | 空战接续制导 |
| `rotation` | 轮换前出（磨盘） | 前出、待机、后撤 | 空战动态磨盘 |
| `sensor_shooter_split` | 探测与打击分离 | 探测、打击、评估 | 分布式杀伤链 |
| `massing_fires` | 集中火力 | 火力单元、指挥 | 火力集中射击 |
| `dispersion` | 疏开 | 编队全体 | 双向疏开 |
| `delay_withdraw` | 迟滞 / 边打边撤 | 掩护组、撤离组 | 交替后撤 |
| `cover_withdrawal` | 掩护撤离 | 掩护、撤离 | 自杀式拦截掩护脱离 |
| `elastic_defense` | 弹性防御 | 防线、预备队 | 空战弹性防御 |
| `report_and_wait` | 报告待令 | 报告方、接收方 | 接敌报告 |
| `tasking_with_ack` | 命令—应答 | 指挥方、执行方 | 长机分配战术、僚机确认 |

模式表由人维护（`crow/catalog/tactic_patterns.toml`），模型只能从表中选择模式，不得自创模式 ID；需要新模式时进入人工审核。

### 5.4 战术案例库

```yaml
# 案例条目
- case_id: case_air_relay_guidance
  name: 接续制导
  patterns: [relay]
  domain: air
  echelon: flight_of_four
  situation_features: {force_ratio: parity, terrain: none, threat: bvr_missile}
  roles: [...]
  steps: [...]
  source: {kind: expert, ref: 论文研读 2026-10-15}    # doctrine | expert | demo | simulation
  validated: false                                  # 仿真验证后置 true（v2）
```

- 来源：条令战术（Crow 证据中抽取并经人工确认后录入）、论文战术（`expert`）、demo 数据集（转换为 IR 后录入，`demo`）、仿真验证过的新战术（`simulation`，v2）；
- 检索按结构特征而不是文字：领域、层级、任务类型、兵力比、地形、模式、可用能力；
- 案例库属于本地数据：来自受控资料的条目标 `classification: user_private`，不得进入公网模型请求，检索时只返回中立摘要。

### 5.5 生成步骤

1. **模式选择**：根据任务画像和 `pattern_hypotheses`，选出 2–4 个候选模式组合（模型选，代码检查是否在模式表内）；
2. **取证与取案例**：为每个模式组合取相关主张（按主题）和相似案例（按结构特征，v1.1）；
3. **提取检索战术**：从证据中整理出直接给出的战术（`retrieved`）及其要素；
4. **排序填位**：检索战术按关联度、证据强度、差异性排序，填入候选（§5.7 第 1 步）；
5. **缺口判断与补位**：材料不足时先补充检索，仍不足再按 §5.6 生成创新候选补位（§5.7 第 2–3 步）；
6. **逐项核对依据**：适用条件、几何、步骤、例外分别对照引文核对（与 Falcon F1 的引文核验共用代码），不支持的部分降为 inferred。

### 5.6 创新补位的方法（仅在 §5.7 条件满足时使用）

创新是**针对缺口的补位**，不是泛泛的头脑风暴：每次创新都要指明它要补的缺口（未覆盖的需求条款、证据不足的维度、未通过检查的检索战术），并优先选择改动最小的方法。改动幅度从小到大依次为：改编算子（单一算子）→ 改编算子（组合）→ 形态分析组合 →（v1.1）跨领域迁移。

依据：大模型提出的想法新颖性可以很高，但可行性略弱，且数量一多就大量重复（Si 等，2024，100 多位研究者参与的评测）。补位时只生成少量针对性构想，并用代码去重。

**补位方法 A：形态分析（组合）**

借鉴一般形态分析法（Zwicky、Ritchey，瑞典国防研究机构长期用于方案和情景生成）：把战术拆成若干维度，每个维度列出证据中出现过的选项，跨维度组合产生新战术，再做**相容性检查**剔除自相矛盾的组合。

| 维度 | 选项示例（来自证据和模式表） |
| --- | --- |
| 编成与角色 | 2+2 交替、3+1（单车前出）、全排一体 |
| 队形几何 | 楔形、纵队、梯队、横队 |
| 机动方式 | 行进间掩护、交替跃进、连续跃进 |
| 火力运用 | 先压制后机动、边机动边射击、隐蔽不射击 |
| 欺骗 | 无、佯动、烟幕掩护 |
| 决策权 | 排长统一指挥、各组自主、混合 |
| 节奏 | 快速通过、逐段停顿观察 |
| 地形利用 | 沿道路、沿建筑掩蔽、避开开阔地 |

相容性规则由代码维护（如"隐蔽不射击"与"先压制后机动"不相容），模型只提出维度和选项，组合与剔除由代码完成，避免模型自己偷偷合并。

**补位方法 B：改编算子（改造已有战术）**

对每个检索到的战术，按下列算子各尝试一次，产生改编版本：

| 算子 | 含义 | 例子 |
| --- | --- | --- |
| `regroup` | 改变编成或角色比例 | 2+2 → 3+1 |
| `reorder` | 调换步骤顺序 | 先报告再跃进 → 先占掩护位置再报告 |
| `substitute_role` | 由其他单位承担某角色 | 掩护由排长车改为僚车 |
| `add_deception` | 加入佯动、诱饵或烟幕 | 侦察车前出时另一侧佯动 |
| `remove_dependency` | 去掉某个前提，使战术在更差条件下可用 | 通信中断时按预定信号交替 |
| `change_tempo` | 改变节奏或时机 | 连续跃进改为逐段停顿观察 |
| `exploit_terrain` | 换一种地形利用方式 | 沿道路改为沿建筑边缘 |
| `invert` | 反转方向或攻守关系 | 正面跃进改为侧翼迂回接近 |

**补位方法 C：情况扰动（应变战术，用于补"例外与中止"缺口）**

从任务画像的假设和 `open_questions` 出发，逐一反转关键假设（如"敌方有反装甲火力""通信中断""能见度差"），为每种情况提出应变版本。这些通常成为候选战术的"例外与中止"部分，也可能单独成为候选。

**补位构想的筛选**

1. 每个缺口生成 2–3 个构想，按改动幅度从小到大排列；
2. 去重，并去掉与已入选检索战术过于相似的构想；
3. 约束检查、一致性检查通过后，取改动最小且能补上缺口的一个入选；
4. 逐项核对依据。

补位构想全部保存在 `tactic_ideas.yaml`（含推导过程和对应缺口），未入选的也保留，用户可以从中补选。

### 5.7 候选组成策略（检索优先）

默认候选数 `CROW_MAX_CANDIDATES = 5`，其中创新候选（`adapted`/`combined`/`transferred`）不超过 `CROW_MAX_INNOVATIVE = 2`。

**第 1 步：检索战术排序填位**

从证据中提取的 `retrieved` 战术按以下顺序排序，依次填入候选：

1. 与需求的关联度：覆盖的需求条款数、领域/层级/任务类型是否一致（如装甲排需求优先用坦克排条令，而不是步兵班条令）；
2. 证据强度：A 级来源、explicit 要素占比；
3. 与已入选候选的差异：过于相似的合并为一个候选的变体。

**第 2 步：判断是否需要补位**，满足任一条件即为"材料不足"：

| 条件 | 说明 |
| --- | --- |
| 数量不足 | 可用的 `retrieved` 候选少于 3 个 |
| 覆盖不足 | 有需求条款没有被任何 `retrieved` 候选覆盖 |
| 检查不通过 | `retrieved` 候选因能力缺失或违反用户约束被淘汰后，剩余不足 3 个 |
| 证据维度缺口 | 某个关键维度（如例外与中止）在门槛评估中无强证据 |

**第 3 步：先补检索，再补创新**

1. 先针对缺口发起一轮补充检索（复用 Crow 现有的补充检索轮次），新证据中的战术按第 1 步处理；
2. 仍不足时才进入 §5.6 的创新补位，每个缺口最多补 1 个创新候选，总数不超过 `CROW_MAX_INNOVATIVE`；
3. 检索战术已足够时，不生成创新候选；用户在选择阶段可以显式要求"再给 1–2 个创新方案"。

**报告呈现**：候选按来源分组，检索战术在前；创新候选标明"为补哪个缺口而提出"及推导过程。

## 6. C5 筛选

### 6.1 代码检查

| 检查 | 内容 | 不通过时 |
| --- | --- | --- |
| 能力检查 | 读取 C4b 的能力标注（§18），汇总为可行性等级 | 不退回、不淘汰；可行性等级作为独立一栏展示，不并入候选得分 |
| 约束检查 | 是否满足全部 `user_specified` 约束 | 退回重新生成 |
| 一致性检查 | 步骤结束条件是否可达、例外是否指向已定义阶段、角色是否都有任务 | 退回修订 |

### 6.2 对抗推演（另一个模型）

**模型要求**：红方模型与生成模型不同，配置 `CROW_REDTEAM_MODEL`；与 `CROW_PROPOSER_MODEL` 属于同一系列时给出警告。理由：模型评估时会偏向自己的输出（Panickssery 等，NeurIPS 2024）。

**输入**：任务画像 + 候选战术卡的事实部分（角色、几何、步骤、例外、所需能力）。**不提供**生成方的理由（`summary` 以外的论证、`risks`），避免被说服。

**必须回答的问题**（结构化输出）：

```yaml
# redteam_review.yaml（每个候选一节）
- candidate: tc_01
  critical_assumptions:              # 战术依赖的前提，按脆弱程度排序
    - {assumption: 排内通信持续可用, fragility: high, why: 敌方可能实施通信干扰}
  counter_tactics:                   # 敌方最可能的 2–3 种反制
    - id: ct_01
      description: 敌反装甲小组在城区边缘建筑内伏击纵队先导车
      enemy_capabilities_needed: [反装甲武器, 城区隐蔽阵地]
      trigger: 纵队进入城区边缘 300 m 内
      expected_effect: 先导车损失，纵队受阻
      severity: high                 # low | medium | high
  failure_conditions:
    - 掩护组与跃进组间距过大，掩护火力覆盖不到跃进组
  capability_doubts:                 # 战术默认可做、但不一定具备的能力
    - 行进间准确判断与敌距离
  overall: {soundness: medium, main_weakness: 城区入口缺少侦察}
```

**生成方回应**：对每条反制和失效条件，生成方选择"修改战术"（产生修订版候选，保留版本号）或"说明不改的理由"。回应与红方意见一起保存。

**裁决**：不由模型裁决。双方意见并列提交给用户。

### 6.3 用户选择

提交给用户的 `tactic_report.md`（每个候选一页）：

- 一句话概述、来源类型（非条令直接给出的醒目标注）、所属模式；
- 依据强度：各要素的支持情况（有引文 / 推断 / 无依据）；
- 可行性摘要（§18.5）：可实现 / 需确认 / 需开发 / 需扩展 IR 的项数，可展开明细；
- 红方主要反制与生成方回应；
- 风险与需要确认的假设。

用户可以：选择一个候选、组合多个候选（如"tc_01 的行进阶段 + tc_02 的接敌阶段"）、要求修改，或全部否决并补充要求。选择结果写入：

```yaml
# tactic_selection.yaml
selected:
  - {candidate: tc_01, revision: 2, phases: all}
  - {candidate: tc_02, revision: 1, phases: [contact]}
rejected: [tc_03]
user_notes: 城区入口增加侦察步骤
confirmed_by: <用户>
```

## 7. C6 与 Falcon 衔接

Falcon 输入改为：证据包 + `mission_profile.yaml` + `tactic_selection.yaml` 及其引用的候选战术卡。Falcon 先把选定战术转为三类卡片（选择规则 / 战术程序 / 协同战术，格式另见规则集设计稿），再编译为 IR（v1 实现：战术步骤、队形、例外转为条款，战术摘要附在各阶段提示词中，见 tactical-falcon-v1 §13；三类卡片随 Falcon v2 实施）。候选战术卡中的依据核对结果直接作为 IR `basis` 的来源；红方的 `counter_tactics` 作为 L3 想定的候选事件（v1 仅记录，v2 起使用）。

## 8. Crow 运行目录新增文件

```
data/runs/crow-research/<run_id>/
  mission_profile.yaml        # C1
  research_result.json        # C2–C3（扩展：维度覆盖、模式假设）
  research_report.md
  evidence_package.json       # 证据规范化（由 Falcon F1 前移）
  tactic_ideas.yaml           # C4 缺口、创新补位的全部构想（入选/未入选/拒绝及理由）、检索战术池
  tactic_candidates.yaml      # C4 候选（含修订版本）
  tactic_candidates.md        # C4 可读报告：候选总表、缺口、逐项依据与引文
  tactic_trace.json           # C4 模型调用记录
  capability_gaps.yaml        # C4b 全部候选的能力缺口（预估，编号在后续 IR 核对中沿用）
  gap_retrieval/              # C4 补充检索的子运行（仅在材料不足且允许补充检索时）
  redteam_review.yaml         # C5 对抗推演与回应
  tactic_report.md            # C5 提交给用户的报告
  tactic_selection.yaml       # C5 用户选择
```

## 9. 为 v2/v3 预留

| 预留项 | v1 做法 | 后续用途 |
| --- | --- | --- |
| 任务画像 `success_criteria` | 写明测量项和阈值 | v2：仿真评估指标 |
| 候选战术卡 `simulation_results` | 字段为空 | v2：每个候选跑一组想定的结果 |
| 红方 `counter_tactics` | 结构化保存（触发条件、所需敌方能力、预期效果） | v2：转为 L3 想定中的敌方行为；v3：战术对抗比较 |
| 案例库 `validated` | 恒为 false | v3：仿真验证通过后存回案例库 |
| 基线战术 | 不生成 | v3：条令默认做法作为对比基线 |

## 10. 配置

| 设置项 | 说明 |
| --- | --- |
| `CROW_PROPOSER_MODEL` | 生成候选战术的模型；缺省用 `DASHSCOPE_CHAT_MODEL` |
| `CROW_REDTEAM_MODEL` | 对抗推演模型，缺省 `deepseek-v4-pro`（百炼上的 DeepSeek，与通义千问不同系列，支持结构化输出；思考模式默认开启，P3 实现时用 JSON 模式加自行解析）。测试后可自行替换 |
| `CROW_MAX_CANDIDATES` | 候选上限，缺省 5 |
| `CROW_MAX_INNOVATIVE` | 创新候选上限，缺省 2；设为 0 则只给检索战术 |
| `CROW_IDEAS_PER_GAP` | 每个缺口的补位构想数，缺省 3 |
| `CROW_ENABLE_TRANSFER` | 是否启用跨领域迁移，v1 缺省关闭 |
| `CROW_CAPABILITY_CATALOG` | 能力目录路径；缺省为空，能力检查降级为提示 |

## 11. v1 评测

在装甲排及另外 2 条需求上评测：

| 指标 | 计算 |
| --- | --- |
| 任务画像完整度 | 对照人工标注的画像，逐字段命中率 |
| 维度覆盖 | 六个维度中有强证据的个数 |
| 候选有效率 | 通过约束检查和一致性检查的候选比例 |
| 依据支持率 | 标为"有引文"的要素中真正被引文支持的比例（人工抽查） |
| 候选差异度 | 候选之间模式组合、角色分配不同的比例 |
| 检索战术占比 | 候选中 `retrieved` 的比例（预期多数） |
| 补位命中率 | 创新候选确实补上了所声明缺口的比例 |
| 创新可接受率 | 创新候选中用户认为"合理且有用"的比例 |
| 红方意见有用率 | 用户认为有价值的反制条目比例 |
| 用户采纳率 | 被选中或部分采用的候选比例 |

## 12. 实施计划

| 阶段 | 内容 |
| --- | --- |
| P1 ✅ | C1 任务画像（含歧义提问）；C2 研究问题模板与提示词更新；门槛增加维度覆盖 |
| P2 ✅ | 证据规范化前移；C4 检索战术提取与排序填位；缺口判断与补充检索；创新补位（改编算子、形态分析）；候选战术卡与依据核对 |
| P3 | C4b 能力标注 ✅（§18）；C5 对抗推演（独立模型）、生成方回应、评审报告与用户选择 ✅（§19）；C5 一致性代码检查（未做） |
| P4 ✅ | Falcon 接收 `tactic_selection.yaml`（tactical-falcon-v1 §13；三类卡片的规则集格式随 Falcon v2 生成层实施） |
| P5（v1.1） | 案例库初版；跨领域迁移 |

每个阶段配脚本化模型的单元测试，并用 armor-eval-03 的证据做一次在线演示。

## 13. 已确认（2026-10-15）

1. 研究问题上限提高到 6 个；
2. 阻塞问题在命令行直接提问，回答注入上下文；
3. 同意证据规范化（Falcon F1）前移到 Crow；
4. 红方模型先用 `deepseek-v4-pro` 测试，之后由用户自行选择；
5. 候选数 5、创新上限 2、四个"材料不足"条件维持。

## 14. P1 实现说明

| 文件 | 内容 |
| --- | --- |
| `crow/mission.py` | `MissionProfiler`（json_schema 结构化输出）、`normalize_profile`、`resolve_open_questions`、`profile_context`、读写 YAML；`MISSION_PROMPT_VERSION = crow-mission-2026.10.1` |
| `crow/dimensions.py` | 六个维度、关键词推断、模板问题；`PatternTable` |
| `crow/catalog/tactic_patterns.toml` | §5.3 的 18 个模式（`tactic-patterns-2026.10.1`） |
| `crow/planner.py` | 问题带维度标签（`PlannedQuestion`）；未标维度按关键词推断；覆盖不足 5 个维度且有余量时补模板问题；`pattern_hypotheses` 只保留表内 ID（≤3）；`analogy_queries` ≤3 并入第 1 轮网页检索；补充检索提示带维度；`crow-planner-2026.10.4` |
| `crow/readiness.py` | `dimension_coverage` 检查：至少 5 个维度各有 ≥1 条强证据（问题的强证据计入它标注的每个维度）；旧计划无维度标签时跳过；`falcon-gate-2026.10.2` |
| `crow/cli.py` | 新参数 `--mission-profile`、`--skip-profile`、`--no-input`；画像生成失败时提示并继续研究 |
| `crow/report.py` | 报告新增任务画像、问题维度、模式假设、相似场景检索式、维度覆盖表；运行目录新增 `mission_profile.yaml` |
| `config.py`、`.env.example` | §10 的配置项 |

测试：`tests/unit/crow` 37 项通过（新增 `test_mission.py` 7 项，规划、门槛、流程各若干项）。

## 15. P2 实现说明（2026-10-16）

**命令**

```
# 对已有 Crow 运行提出候选战术（旧运行没有 mission_profile.yaml 时先做 C1，会在命令行提问）
python -m afsim_research_agent.crow.propose <run_dir> [--evidence-package PATH] [--out DIR]
       [--no-retrieval] [--no-input] [--max-candidates N] [--max-innovative N]
```

Crow 主命令在门槛通过后自动执行 C4（`--no-propose` 关闭）。

**文件**

| 文件 | 内容 |
| --- | --- |
| `crow/proposal_models.py` | 候选战术卡、缺口、构想、结果的数据结构 |
| `crow/proposal.py` | 提示词、逐项核对、排序、缺口判断、补充检索、创新补位；`crow-proposal-2026.10.1` |
| `crow/proposal_report.py` | `tactic_candidates.yaml/.md`、`tactic_ideas.yaml`、`tactic_trace.json` |
| `crow/propose.py` | 命令行；证据包优先用已有文件，否则调用 Falcon F1 并写入运行目录（F1 前移）；`GapRetriever` 做一轮补充检索 |
| `crow/catalog/innovation.toml` | 9 个改编算子（按改动幅度排序）、8 个组合维度、6 条不相容规则 |

**代码核对规则**

- 引用了不存在的证据 ID → 移除；标为 evidence 但没有有效引用 → inferred；
- 元素中的数值（带单位）不在所引证据的原文或主张中 → inferred；标为用户指定但不在需求原文中 → inferred；
- 覆盖条款必须与任务画像的 `user_specified` 对应；模式必须在模式表中；
- 每节依据状态：supported / partial / inferred / unsupported / empty；
- 排序分 = 0.45 × 需求覆盖 + 0.2 × 单位匹配度 + 0.35 × 证据强度；特征 Jaccard ≥ 0.75 的检索战术合并为变体；与用户约束冲突的不入选。

**与设计稿相比的细化**（在 armor-eval-03 旧证据上试运行后加入）

1. 补位构想必须真的补上它对应的缺口：条款缺口要求 `covers` 包含该条款，维度缺口要求对应部分有内容，否则拒绝；
2. 参数类条款（含数值和单位，如"纵队车距 50 米"）未被证据覆盖时只做补充检索，不做创新补位，按用户指定处理；
3. 证据所述单位类型与任务不同（`context_match: different`，如装甲排需求检索到徒步步兵技术）的检索战术保留在候选中，但不计入"至少 3 个可用检索战术"；
4. 形态分析简化为：模型为每个维度选一个选项，代码检查选项是否在表内及是否不相容；暂不做全组合枚举；
5. 用户约束是否被每个候选满足、能力检查，放到 P3 的 C5 代码检查；
6. 所有模型调用温度为 0。

**测试**：`tests/unit/crow` 51 项通过（新增 `test_proposal.py` 14 项）；用 armor-eval-03 的真实证据包（23 条证据、4 个来源）和脚本化模型做了离线试运行，检查和报告符合预期。在线运行需在本机用 DashScope 执行。

## 16. 修订：候选必须是完整的自然语言战术（2026-10-16）

**问题**：armor-eval-03 在线试运行中，模型把需求拆成 4 个"战术"（楔形行进、转纵队、接敌报告、交替跃进），实际是同一战术的 4 个阶段；每个只覆盖 0.2–0.4 的需求条款。代码按数量判断"检索战术足够"，补充检索和创新补位都没有触发。另有步兵资料（士兵间隔 10 米、个人冲刺 3–5 秒）被当作装甲排依据，单位匹配度由模型自评为 exact。

**修改**

1. **两层提取**：先列出证据中的"战术动作"（`techniques`：队形、机动技术、转换、报告与命令程序、交战规则），再把它们组装成"完整战术"（`tactics`），完整战术注明用到的动作编号。战术动作只作为组成部分，不作为候选。
2. **完整战术 = 自然语言战术描述 + 结构化要素**：新增 `narrative`（150–500 字中文，从开始到结束：何时采用、谁按什么顺序做什么、阶段如何衔接、如何协同、例外如何处理、如何结束），报告中放在每个候选的最前面；结构化要素折叠在其后。
3. **完整性检查**（代码）：战术描述不少于 80 字；步骤至少 2 个且最后一步有结束条件；必须实现全部用户指定条款（`addresses`，冲突的列入 `conflicts_with_user`）。不完整的结果进入"不完整的提取结果"，不作为候选、不计入检索战术数量；创新补位的构想同样要求完整，否则拒绝。
4. **`addresses` 与 `covers` 分开**：`addresses` 是战术实现的全部条款（任何依据），`covers` 是其中有证据支持的条款（用于排序和条款缺口）。
5. **单位类型由代码判断**：按证据主体（士兵、步枪班、坦克排……）判断，主体不明时参考来源标题。证据主体属于其他类型单位时为 different，仅来源标题不同为 similar。只引用其他类型单位证据的要素降为 inferred；数值只出现在其他类型单位证据中的也降为 inferred；单位匹配度改为由代码计算，覆盖模型自评。
6. **战术描述中的数值**：既不在需求中也不在所引证据中的，给出核对提示。
7. **提示词**：加入一个完整的格式示例（取自无关领域，只示范结构）；要求全部中文，`formation` 只写队形名，例外不得写成前提条件；证据清单中显示证据主体。
8. **C1**：区分"回车采用默认"（记为用户确认）、"未读到回答"和"非交互运行"；编成未说明时要求模型按层级给出编成并提出非阻塞问题确认。

提示词版本：`crow-proposal-2026.10.2`、`crow-mission-2026.10.2`。测试：Crow 56 项通过。旧证据离线试运行：得到 2 个完整检索战术（楔形—纵队—报告—两组交替跃进；行进间掩护—纵队—先导占位后依次超越），1 个不完整结果被排除，触发"完整检索战术不足"，由 1 个改编（3+1 前出侦察）补位；步兵数值被正确降级。

## 17. 修订：相似度与"需确认"标注（2026-10-16）

依据 armor-eval-03 第二次在线运行（tactics_v2）：1 个完整检索战术 + 2 个创新补位；其中一个创新候选与检索战术用了几乎相同的战术动作，只是把"与敌保持安全距离"重新解释为"处于己方扇区火力覆盖内"；另一个（3+1 前锋侦察）把"开放纵队车距 50–100 米"挪用为前锋间距，描述中出现证据编号和"单兵"。

1. **相似度**改为比较战术动作、模式、队形和决策权（不再比较模型自拟的步骤名称和算子），阈值 0.7。用回放验证：换说法的候选（0.73）判为检索战术的变体，不再占创新名额；3+1 前锋侦察（0.56）保留。
2. **"需确认"标注**（`review_flags`，报告中紧跟战术描述，带 ⚠）：
   - 条款缺口由创新方案实现时："证据没有直接给出做法，条款理解需确认"；
   - 创新候选中沿用证据数值的要素：列出证据原适用条件（如"开放纵队行军"），提示用于新场景需确认（需求中出现的数值除外）；
   - 战术描述中的证据编号：自动去除并提示；
   - 车辆任务的战术描述中出现步兵用语（单兵、士兵、各班、或班、先导班等）：提示改写或确认。
3. 不处理（由 P3 红方审查和用户判断）：战术内部矛盾（如"前锋车牺牲距离"与"保持安全距离"）、创新候选排序高于检索战术（用户决定暂不处理）。

测试：Crow 56 项通过。

## 18. 修订：候选战术的能力标注（2026-10-16）

**目的**：Crow 交出候选战术时，每个候选都标明完成它需要哪些平台能力、能力目录中哪些已能实现、哪些必需但尚未开发。用户选择战术时能看到实现代价，选定后缺口即可交给能力插件开发 Agent 或人工开发。

### 18.1 位置与原则

- **位置**：C4 生成并核对完候选之后，单独一步（C4b）。生成候选时不提供能力目录，标注结果不参与排序、合并和筛选。战术是否正确由条令与证据决定，平台能否执行单独评估，避免倾向提出"好实现"而非"战术上正确"的战术。
- **定位是预估**：自然语言战术只能推出"跃进到指定位置""向排长通报到位"这一粒度。领车位置来源、跃进轮次的消息推算等需求，要到 IR 展开后才暴露。IR 生成后由能力一致性检查（`design/capability-catalog-integration-v1.md` §3.3）精确核对，沿用本节的缺口编号，补充遗漏项，不另起一份清单。
- **状态由代码判定**：模型只把战术要素对应到目录条目（借助目录的战术用途、关键词检索）。条目是否存在、状态如何由代码查目录得出；模型给出的目录 ID 不存在时视为未对应。

### 18.2 所需能力的提取

从候选战术卡的结构化要素逐条提取"所需能力"：

| 来源要素 | 典型所需能力 |
| --- | --- |
| `applicability`、`exceptions` 的情况 | 判断类读取：是否接敌、是否在城区、通信是否中断 |
| `steps.do` | 动作：跃进到位置、停车掩护、开火、通报 |
| `steps.until` | 结束条件读取：到达、收到确认、与敌距离 |
| `geometry` | 队形保持所需读取：自身位置、领车位置 |
| `decision_authority`、协同描述 | 消息：命令、报告、确认 |

每条所需能力标注必要性：

| 必要性 | 含义 |
| --- | --- |
| `required` | 缺了战术不成立（如交替跃进的"到位通报"） |
| `enhancing` | 缺了战术仍成立，效果变差 |
| `substitutable` | 有其他实现路径，列出 `alternatives`（如领车位置：消息广播 / 友方航迹） |

只有"必需且没有可行替代"的才计为阻塞缺口。可替代项按"替代路径中最好的状态"判定。

### 18.3 状态与缺口类型

| 目录查询结果 | 结论 | 缺口类型 | 交给谁 |
| --- | --- | --- | --- |
| available / supported | 可实现 | — | — |
| composite 且依赖全部可用 | 可组合实现 | — | — |
| partial | 部分可用（附限制） | — | — |
| unknown | 需确认 | `confirm` | 用户确认，记为 user_attested |
| 目录无对应、unavailable | 需开发 | `develop` | 能力插件开发 Agent 或人工 |
| 平台有，IR 无法表达（`ir_v1: null`） | 需扩展 IR | `ir_extension` | 本工程（IR 扩展） |

### 18.4 格式

候选战术卡新增 `capability_needs` 与 `feasibility`（替代自由文本的 `required_capabilities`，后者保留为由 `capability_needs` 汇总的目录 ID 列表）：

```yaml
capability_needs:
  - need: 跃进到指定位置
    from: steps[1].do
    necessity: required
    maps_to: [point_navigation]
    status: available
  - need: 向排长通报跃进到位
    from: steps[1].until
    necessity: required
    maps_to: [message_exchange.send_message]
    status: unknown
    gap: cap-gap-001
    gap_type: confirm
  - need: 判断是否在城区
    from: applicability[1]
    necessity: required
    maps_to: []
    status: missing
    gap: cap-gap-002
    gap_type: develop
  - need: 读取领车位置
    from: geometry[0]
    necessity: substitutable
    alternatives:
      - {via: 消息广播, maps_to: [message_exchange.send_message], status: unknown, gap: cap-gap-001}
      - {via: 友方航迹, maps_to: [target_tracking], status: supported}
    status: supported              # 取替代路径中最好的状态
feasibility:
  level: needs_confirmation        # ready | needs_confirmation | needs_development
  counts: {ready: 6, confirm: 1, develop: 1, ir_extension: 0}
  blocking_gaps: [cap-gap-001, cap-gap-002]
catalog_version: sha256:...        # 标注时所用目录的内容哈希
```

`capability_gaps.yaml`（运行目录，汇总全部候选）：

```yaml
gaps:
  - id: cap-gap-001
    contract: message_exchange.send_message      # 中立契约，取自目录
    gap_type: confirm
    used_by: [{candidate: tc_01, needs: [steps[1].until, geometry[0]]}, {candidate: tc_02, needs: [...]}]
    candidates_count: 2
    selected: false                               # 用户选定后置 true，才进入开发队列
    status: open                                  # open | confirmed | in_development | implemented | rejected
```

缺口编号由契约（或未对应时由规范化后的需要描述）的内容哈希决定，同一缺口在不同候选、不同运行中编号相同，可跨运行汇总。

### 18.5 报告与选择

- `tactic_candidates.md` / `tactic_report.md`：每个候选的战术描述之后一行可行性摘要，如"可实现 6 项；需确认 1 项（发送消息）；需开发 1 项（城区判断）"，可展开明细；
- 候选总表增加"可行性"一栏，与综合分并列，不合并；
- 全部候选都有阻塞缺口时，报告首段提示，并列出最常见的缺口。

### 18.6 交付开发

- 只有用户选定战术（`tactic_selection.yaml`）的缺口进入开发队列（`selected: true`），未选候选的缺口只保留记录；
- 跨战术、跨运行重复出现的缺口自动提高优先级（`candidates_count` 与历史出现次数）；
- 开发队列在 IR 生成后由精确核对补全（契约参数、使用位置、验收用例），再交给能力插件开发 Agent 或人工；
- 缺口补齐后回写能力目录（依据：用户确认 / 插件实现），重新标注即可得到新的可行性。

### 18.7 实施

依赖能力目录访问层（`design/capability-catalog-integration-v1.md` K1）。C4b 的模型调用只做"要素 → 目录条目"的对应，输入为候选的结构化要素与按领域检索出的目录摘要（只含正式层）；温度 0；对应结果由代码查表、判定、汇总。测试用 tactics_v2 的候选做离线回放。


### 18.8 实现说明（K2，2026-10-16）

**命令**

```
# C4 之后自动执行（有能力目录时）；--no-capability 关闭，--capability-catalog 指定目录
python -m afsim_research_agent.crow.propose <run_dir> [...]

# 对已有候选补做标注（覆盖原目录中的候选文件，--out 另存）
python -m afsim_research_agent.crow.annotate <tactics_dir> [--mission-profile] [--evidence-package] [--capability-catalog] [--out]
```

**文件**

| 文件 | 内容 |
| --- | --- |
| `crow/capability_annotation.py` | 提示词（正式层目录摘要，按平台类别过滤，不含离线工具）；`judge_need`（引用校验、必要性、替代路径、多平台类别取最差）；`judge_card`（可行性等级、`required_capabilities` 改由标注汇总）；`collect_gaps`；`crow-capability-2026.10.1` |
| `crow/annotate.py` | 对已有运行补做标注；写 `capability_trace.json` |
| `crow/proposal_models.py` | `NeedDraft`、`CapabilityNeed`、`FeasibilityInfo`、`CapabilityGap`；`TacticCard.capability_needs/feasibility`；`ProposalResult.capability_gaps/catalog_version` |
| `crow/proposal_report.py` | 总表"平台可行性"栏（与综合分分开）；每个候选在战术描述后的一行摘要（增强项单列）与折叠明细；"平台能力缺口"一节；`capability_gaps.yaml` |

**判定细节**

- 模型给出的目录引用逐个校验，不存在的引用删除并注明；全部无效时按"目录中没有对应能力"处理，缺口以需要描述的哈希编号。
- 标为可替代但没有有效替代路径时，按必需处理。
- 需要扩展 IR 的判定：只有当接口无法通过任何途径被 IR 使用（无 `ir_v1`、所属能力无 IR 链接、也不在 IR 元素的组合映射中）时才记为"需扩展 IR"。0.2.0 条目没有记录 IR 映射，不视为 IR 缺口。
- 标注失败（模型调用异常）只在该候选的 `notes` 中注明，不影响 C4 结果。

**离线回放（armor-eval-03 / tactics_v2，要素对应由人工代替模型给出）**

| 候选 | 可行性 | 阻塞缺口 |
| --- | --- | --- |
| tc_01 检索战术：楔形—纵队—接敌交替跃进 | 需确认 | 发送消息、立即上报接触、敌我属性（城区判断可由"收到排长转换指令"替代，不阻塞） |
| tc_02 改编：火力扇区保距跃进 | 需开发 | 另有"判断跃进位置处于掩护火力扇区内""判断扇区重叠不足"，目录中没有对应能力 |
| tc_03 组合：3+1 前锋侦察 | 需确认 | 发送消息、立即上报接触、地形类别、敌我属性；"吸引敌方注意"为增强项 |

全部 3 个候选共 9 项缺口，其中发送消息、立即上报、敌我属性被 3 个候选共用。回放发现并修正了"0.2.0 条目被误判为需扩展 IR"的问题。

**测试**：`tests/unit/crow/test_capability_annotation.py` 9 项；`tests/unit/capability` 15 项（新增 IR 扩展判定回归）。Crow 合计 65 项通过。在线运行需在本机用 DashScope 执行 `crow.annotate`。

### 18.9 首次在线运行与修正（2026-10-16）

armor-eval-03 / tactics_v2 在线标注（qwen3.8-flash）：3 个候选全部为"需开发"，缺口 16 项。核对后发现的问题与修正：

| 问题 | 例子 | 修正 |
|---|---|---|
| "需扩展 IR"被当成平台需开发 | "判断是否进入城区"对应到区域判断（平台有，IR 1.0 不能表达），3 个候选因此全部变为需开发 | Crow 只按平台能力定级：`summarize(development=("develop",))`；需扩展 IR 仍作为缺口列出，由 K3 在实际 IR 上精确判定 |
| "任选其一"被写成"同时需要" | maps_to 同时写区域判断和地形类别，按最差合并 | 提示词规定 maps_to 只放同时需要的条目，可替代的写 alternatives |
| 跨领域凑数 | 有效射程→导弹发射包线；火力扇区覆盖→传感器覆盖评估；安全距离→毁伤效果 | 提示词增加判定原则和反例；每项需求新增 `why`（为何恰好对应），报告"对应理由"列供人工复核 |
| 来源写成证据编号 | source: t6 | 校验来源路径，无效时在备注中标出 |
| 标注模型过弱 | 默认用提案模型 | 新增 `CROW_CAPABILITY_MODEL`，可为 C4b 单独指定模型 |

用新的定级规则离线重判这次的映射结果：3 个候选均为"需确认"。映射质量问题需重新在线运行才能验证。提示词版本 `crow-capability-2026.10.2`。

**第二次在线运行（deepseek-v4-pro，提示词 2026.10.2）**：3 个候选均为"需确认"，缺口 12 项；来源路径全部有效。仍有跨用途映射，且模型写出的"对应理由"是在为错误映射找说法，例如火力扇区覆盖、扇区重叠不足被对应到传感器覆盖评估（定义是对评估时段做事后覆盖统计），掩盖了目录中确实没有的"火力扇区覆盖判断"。原因之一是目录摘要只给名称、用途和接口，该条目没有用途字段，模型看不到定义。

修正（提示词 2026.10.3）：
- 目录摘要为每个条目加上定义（约 60 字），全文约 1.7 万字。
- 新增映射复核：每个候选多调用一次模型，逐条给出对应条目的完整定义、接口、典型用途和限制，只问"定义是否就是在做这件事"，不同用途的去掉。复核只能删除条目，不能新增；被去光的需求按"目录中没有"成为需开发缺口；明细中记"复核去掉 …：理由"。复核失败时保留原映射并在核对提示中标出。`CapabilityAnnotator(review=False)` 可关闭。
- 这符合 §18.1 的原则：映射由模型提出，由独立检查约束，状态仍由程序查目录决定（LLM-Modulo 的外部检查思路）。

**第三次在线运行（加入复核，提示词 2026.10.3）**：tc_01 需开发、tc_02 需开发、tc_03 需确认，缺口 16 项。tc_02 的火力扇区分配、覆盖确认、扇区纵深判断、重叠不足四项正确成为需开发，与离线回放一致；传感器覆盖评估的误用消失，命令权限判定的使用减少。新问题是复核过严：提示词把"可以拿来间接推算"列为去掉理由，复核因此删掉了正当的组成部分（"保持 50 米间距"中的自身位置，"缩短跃进距离"中的定点机动），使 tc_01 被误判为需开发。

修正（提示词 2026.10.4）：
- 映射与复核都区分"组成部分"和"不同用途"。由几个条目加比较、求距离、计时等简单计算即可完成的，算对应并保留；为别的用途设计的去掉。
- 新增 `missing`：条目只覆盖一部分时，maps_to 写已覆盖的部分，missing 写目录中没有的那部分（如"敌方火力范围"）。该需求按需开发处理，缺口以 missing 文字为契约，便于跨候选合并。复核也可以补写 missing。
- 去掉重复备注（"目录中没有对应能力"与判定理由重复）。

已知局限：以需求文字为契约的缺口，其编号随模型措辞变化；同一能力在不同候选中可能写法不同（tc_02 的四项扇区缺口本质上是同一个"火力扇区几何判断"）。缺口归并留到 K5 开发队列处理。

## 19. C5 红方审查、回应与选择（2026-10-17）

按 §6.2–6.3 实现。代码：`crow/redteam.py`（审查与回应）、`crow/selection_report.py`（`redteam_review.yaml`、`tactic_report.md`）、`crow/select.py`（`tactic_selection.yaml`）。

**流程**：C4 → C4b → C5。`crow.propose` 默认在能力标注之后进行（`--no-redteam` 关闭）；已有运行可单独执行 `python -m afsim_research_agent.crow.redteam <战术目录>`（`--no-response` 只审查不回应）。C5 不删除、不重排候选。

| 步骤 | 模型 | 做法 |
| --- | --- | --- |
| 红方审查 | `CROW_REDTEAM_MODEL`（缺省 deepseek-v4-pro） | 输入任务画像与候选的要素（适用情况、角色、几何、步骤、例外、决策权、所需平台能力及其判定），不给战术描述、假设、风险和证据编号。输出关键前提（按脆弱程度排序）、对手反制 2–3 条（所需对手能力、触发、预期效果、严重程度）、失效条件、能力疑问、总体评价。只评审不提方案 |
| 独立性 | — | 红方与生成模型属于同一系列（按模型名前缀，如 qwen）时给出警告，写入报告与评审文件 |
| 生成方回应 | 生成模型 | 必须逐条回应：每条反制、每个失效条件、脆弱程度为高的前提、每个能力疑问；修改（revise）或说明不改的理由（keep）。修改用操作表示：新增 / 修改步骤、新增 / 修改例外，各注明回应的意见编号；有修改时给出新的完整战术描述；不得违背用户指定约束。未回应的意见记入说明 |
| 修订版 | 代码 | 逐项应用修改；无法应用的修改（位置不存在、字段缺失）不采用并说明；修订后重新做完整性检查，不完整则整版不采用。采用时版本号加一，保存上一版（战术描述、步骤、例外），新增或修改的要素依据为"推断"，并加"需确认"标注；依据核对重算 |
| 重新标注 | 能力标注模型 | 修订版重新做 C4b 能力标注（粒度沿用），缺口汇总更新，已选定标记保留 |
| 报告 | — | `tactic_report.md`：总表（来源、综合分、平台可行性、红方评价、主要弱点、版本）；每个候选一页：概述、来源（创新补位醒目标注）、依据、平台能力摘要、战术描述、红方意见与回应（关键前提 / 反制 / 失效条件 / 能力疑问各一表，回应列在同一行）、修订内容、风险与需确认。裁决不由模型做出 |
| 选择 | 用户 | `python -m afsim_research_agent.crow.select <目录> --candidate tc_01 [--candidate tc_02:接敌,跃进] [--reject tc_03] --note ... --by 姓名` 写入 `tactic_selection.yaml`（候选、版本、阶段）；阶段名必须是该候选的步骤阶段；所选候选的能力缺口在 `capability_gaps.yaml` 中标为选定并进入缺口登记表的开发队列（K5） |

**未做**：§6.1 的一致性代码检查（结束条件可达、例外指向已定义阶段、角色都有任务）；红方反制转为 L3 想定（v2）。Falcon 读取 `tactic_selection.yaml`（P4）已完成，见 tactical-falcon-v1 §13。

提示词版本 `crow-redteam-2026.10.1`。测试：`tests/unit/crow/test_redteam.py` 8 项（模型系列与独立性警告、红方输入只含要素、修订与快照、无法应用与不完整的修订、审查失败不影响其他候选、修订后重新标注、报告与选择、选择命令写入开发队列）。全部单元测试 435 项通过，另 11 项为容器内桩模块导致的已知失败。用 armor-eval-03 的候选检查了红方输入（约 2600 字）。

**首次在线运行（armor-eval-03 tactics_v2_cap3，红方 deepseek-v4-pro，生成方 qwen3.8-flash）**：3 个候选都得到 3 条反制、4–5 个失效条件，均被修订为第 2 版。红方意见具体且切中要害：转换窗口伏击、静止掩护组被集火、通信干扰；失效条件指出了"通信中断时按预案继续交替跃进"实际不可执行、排长车被毁后没有接替顺序等战术内部问题。三个候选总体评价都是"低"，区分度不够。修正（提示词 `crow-redteam-2026.10.2`）：

| 问题 | 修正 |
| --- | --- |
| 能力疑问照抄了 C4b 的判定（"……（需确认）"），生成方以"属于实现问题"一律不改 | 红方只看到所需能力的描述，不看判定；能力疑问限定在战术层面；能力疑问不再要求生成方逐条回应（平台支持由能力核对负责），只在报告中列出 |
| 总体评价都为"低" | 给出评价标准：high = 常见反制下核心机制仍成立；medium = 可通过增加步骤或例外修补；low = 核心机制失效、需重新设计 |
| 修订描述末尾加了"此战术强化了……"的自我评价 | 提示词禁止；代码自动去掉并在说明中记录 |
| 修订引入了新数值（80%、70%、30 秒、30 米） | 提示词要求尽量不引入；代码检查新数值（不在需求和原战术中），加"需确认"标注 |
| 修订中出现内部编号（wing_vehicle_1） | 提示词要求使用角色名称 |
| 修订后重新标注时，缺口文件丢失了粒度信息 | 修复：重新标注后同步目录版本与粒度 |
| 粗粒度合并需求的来源写成多个路径（"geometry[0], geometry[1]"）被误报为无效 | 逐个路径校验 |

用新代码离线重放这次的修订：三个候选的新数值都被标出，tc_02、tc_03 的自我评价被去掉。

**第二次在线运行（提示词 2026.10.2）**：每个候选回应 9 条（能力疑问不再要求回应），能力疑问转为战术层面（如"无通信时能否仅靠视觉信号完成间距调整"）；评价出现区分（tc_02 中，tc_01、tc_03 低）；没有引入新数值；"不改"的理由有实质内容（如前锋车的牺牲是 3+1 结构的固有代价、50 米车距是用户约束）。新发现与修正（提示词 `crow-redteam-2026.10.3`、`crow-capability-2026.10.5`）：

| 问题 | 修正 |
| --- | --- |
| tc_03 修订把纵队改为"拉开横向间隔或交错队形"，违背用户约束"接近城区时转纵队、纵队车距 50 米"；生成方没有自己指出 | 修订采用后，由红方模型（独立于生成方）按用户约束逐条核对修订版；可能违背的写入"需确认"标注与说明，不自动否决，由用户决定 |
| "这种流程确保了……"式的自我评价未被去掉 | 扩大识别范围（这种 / 这样的 / 上述 + 流程 / 做法 / 安排……） |
| 修订后重新标注时，模型把目录引用写成"能力组:能力"（如 formation:formation_change），引用全部失效，tc_03 四项核心需求被判为需开发 | 代码去掉能力组前缀后再解析，并记录说明；提示词要求不加前缀 |
| 缺少的部分与需求不在同一能力组（如"掩护火力有效纵深"属于火力组，需求归在机动组），被按核心组判为需开发 | missing 可写成"组 ID:缺少的部分"（如 fires:掩护火力有效纵深），按缺少部分所在的组决定简化还是需开发 |

**第三次在线运行（能力标注改用 deepseek-v4-pro）**：约束核对生效，tc_03 被标出可能违背"纵队车距 50 米"（前锋与主力间距 50–100 米这一处其实是 tc_03 原设计就有的）；没有新增数值和自我评价；能力组前缀问题消失。三个候选修订后都变为"需开发"，缺口多数是修订新增的真实要求：任意两点 / 与友方的通视判断（目录只有与最近敌方的通视）、掩体位置选择。另有一项"交替跃进协调逻辑"被当成平台能力缺口。修正（`crow-capability-2026.10.6`）：

- 提示词说明协同与决策逻辑（谁何时跃进、角色轮换、指挥接替）由行为模型实现，不是平台能力；
- 红方评审记录修订前后的平台可行性，命令行输出与报告中显示"修订对平台可行性的影响：需确认 → 需开发"，让用户看到修订的实现代价。

**用户确定的两条原则（2026-10-17）**

1. **无法确认的具体数值用合理参考值，不算未完成的能力。** 射程、纵深、距离阈值、时间间隔、区域边界这类数据，平台或证据无法确认时，先取合理参考值，标为"参考值，可调整"，不记为能力缺口。
   - 能力标注：需求新增 `reference`（"名称≈参考值 单位"）；模型把数值写进 missing 时，代码按关键词（射程、纵深、距离、间距、间隔、阈值、时长、半径、坐标、范围等）识别并改为参考值。报告平台能力一行显示"参考值 N 项，可调整"。
   - 红方回应：允许直接给出合理参考值；修订引入的新数值记入候选的假设（"参考值（第 n 版，无依据，可调整）"），不再作为"需确认"标注。
2. **红方主要评审战术整体的合理性。** 提示词改为评审阶段衔接、协同关系和对常见反制的承受力，明确不评审具体数值、传感器精度、平台实现与措辞；关键前提最多 3 条、失效条件最多 3 条（只列结构性问题）、能力疑问最多 2 条。生成方的修改保持在战术层面（阶段、协同、例外处置），不为细节增加新的感知或判断要求。

提示词版本 `crow-redteam-2026.10.4`、`crow-capability-2026.10.7`。

说明：Falcon 与 IR 中的参数沿用原有机制——无依据的数值先给占位值，经 `falcon confirm` 确认后才能完整构建；这一步属于参数确认，不涉及能力缺口。
