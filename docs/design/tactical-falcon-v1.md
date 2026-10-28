# Tactical Falcon v1 设计：从需求与证据到行为模型草稿

> 状态：已确认（2026-10-13）；P1–P3 已实现（脚本化模型测试通过），P4 在线评测待真实 Crow 结果；实现中的调整见第 11 节
> 对应：IR 设计文档第 29 节第 8 步、§7（basis）、§27（Agent 生成规则）
> 已定决策：① 证据规范化在 Falcon 第一阶段完成，Crow 不改；② Crow 门槛未通过时拒绝运行；③ 人工确认先做文件式，测试稳定后再接入 Deep Agents

## 1. 定位

Falcon 位于 Crow 与 IR 工具链之间，是整条链路中**唯一由大模型产出行为模型**的环节。它负责起草，不做裁决：

```text
用户需求 ─▶ Crow（检索、证据、门槛）
         ─▶ Falcon：F0 门槛 → F1 证据规范化 → F2 需求拆解 → F3 骨架 → F4 分段填充 → F5 校验修复 → F6 审查材料
         ─▶ 人工确认（confirmations.yaml → falcon confirm）
         ─▶ ir build（L1 → 规范化 → L2 → 生成 → L3）─▶ 人工评估战术效果 ─▶ falcon revise（第二期）
```

| Falcon 负责 | Falcon 不做 |
| --- | --- |
| 把需求拆成可建模条款并逐条落到 IR 元素 | 检索资料（Crow） |
| 把 Crow 证据规范化为带稳定 ID 的主张，合并同义主张，标出冲突 | 校验、编译、仿真（IR 工具链，Falcon 只调用它当检查器） |
| 从目录中选动作、函数、协同模式，起草状态机、行为树、编队、消息 | 编造数值：无证据的值标 `unknown`，来自资料的值一律 `pending` |
| 为每个影响战术语义的数值和规则填写 `basis` | 替用户确认参数、评判战术效果 |
| 生成 L2 测试与 L3 想定（每个协同规则和编队至少一个想定） | 输出特定仿真平台脚本（只输出中立 IR） |

**设计原则：大模型只做"提议"，所有约束由确定性代码执行。** 模型从不直接写 YAML；它按 Pydantic 结构输出 JSON 片段，由组装器写出 `behavior_spec.yaml`。凡是可以机械检查的（引文是否出自原文、数值是否落在证据范围内、用户给定值是否真出现在需求里、目录项是否存在），都在模型输出之后由代码检查，不靠提示词约束。

## 2. 输入

### 2.1 请求文件 `falcon_request.yaml`

文件式交互的入口，用户可编辑：

```yaml
requirement: |
  研究装甲排在开阔地和城区的编队方式：开阔地用楔形，接近城区时转纵队，
  纵队车距 50 米；接敌后交替跃进，与敌保持安全距离。
model_id: armor_platoon_v2            # 模型包 ID，输出目录名
crow_result: data/runs/<run>/research_result.json
platforms:                            # 可选：平台与数量提示
  - {role_hint: platoon_leader, capability_class: ground_vehicle, count: 1}
  - {role_hint: wingman, capability_class: ground_vehicle, count: 3}
terrain_classes: [open, urban]        # 可选，必须是目录中的地形类别
user_parameters:                      # 可选：用户直接给定的数值 → basis.status = user_specified
  column_interval: {value: 50, unit: m}
target_engine: integrations/lab_engine/bindings.example.json   # 可选：只选该引擎支持的动作
backends: [python_sim, btcpp_xml, mermaid]
```

### 2.2 Crow 结果

`CrowResearchResult`（Crow 输出的 `research_result.json`）：研究计划（研究问题）、每个问题的 PaperQA 回答与证据片段（`citation`、`excerpt`、`summary`、`location`、`score`、`source_id`、`source_quality_tier`）、来源清单（`kind` 为 `local_document`/`local_code` 的视为用户私有资料）、门槛结论 `readiness`、运行清单。

### 2.3 目录与 Schema

动作、操作数和函数、协同模式、意图词表、地形类别（`ir/catalog/*.toml`，带版本号），`behavior_spec.schema.json`。各阶段只把与本阶段相关的目录片段放进提示词。

## 3. 输出（新模型包目录，不覆盖已有文件）

```text
models/<model_id>/
  falcon_request.yaml        # 请求快照
  evidence_package.json      # F1：规范化证据包（只读，含内容哈希）
  behavior_spec.yaml         # F4/F5：IR 草稿（权威源文件，用户可编辑）
  confirmations.yaml         # F6：待确认清单（用户填写决定）
  falcon_review.md           # F6：审查报告
  falcon_trace.json          # F6：追溯矩阵 + 运行记录
  validation_report.json     # F5：ir test --draft 的结果
```

目标目录已存在时拒绝运行（`FAL002`），除非使用 `--out` 指定新目录。IR 的 `ir build` 之后产生的 `review.md`、`normalized_ir.json`、`generated/` 与 Falcon 文件并存，互不覆盖。

## 4. 处理阶段

### F0 输入门槛（确定性）

| 检查 | 不通过时 |
| --- | --- |
| `research_result.json` 存在且能解析 | `FAL001`，退出 |
| `readiness` 存在且 `ready == true` | `FAL001`，**拒绝运行**；列出未通过的检查项和覆盖不足的研究问题，提示补充资料（Crow 追加检索轮次、加入本地资料库）后重跑 Crow |
| 请求文件中的地形类别、能力类别、`user_parameters` 单位合法 | `FAL003`，退出 |

不提供强制运行开关。证据包的 `readiness` 字段因此只会是 `passed`。

### F1 证据规范化（模型 + 确定性检查）

目的：把"按研究问题组织的 PaperQA 证据片段"变成"带稳定 ID 的主张"，供 `basis.evidence` 引用。

1. **片段去重（确定性）**：同一 `source_id` + `location` + 摘录文本的片段只保留一份，记录它回答过哪些研究问题；片段 ID `ex_<sha256 前 10 位>`。
2. **主张抽取（模型）**：逐批送入片段，模型对每个片段输出 0–N 条主张：

   ```text
   ClaimDraft:
     statement        一句话主张（中文）
     topic            formation | spacing | transition | contact | bounding | communication |
                      speed | terrain | sensing | other
     subject          主体（如 "坦克排"、"僚车"）
     condition        适用条件（如 "开阔地形"、"接近城区"），可空
     quantity         可选：{name, min, max, unit}，单值时 min = max
     quote            片段中支持该主张的原文子串
   ```

3. **引文核验（确定性）**：`quote` 必须是片段原文的子串（忽略空白和大小写差异）；数值主张的数字必须出现在 `quote` 中；单位必须能被 `ir/units.py` 识别。不通过的主张丢弃，并在 `falcon_trace.json` 中记录原因。
4. **证据条目**：每条通过核验的主张生成一条证据 `ev_<sha256(source_id|location|quote|statement) 前 10 位>`，结构沿用现有 `models.Evidence`（`evidence_id`、`claim`、`excerpt`、`source`、`location`、`relevance_score`、`quality_score`）。ID 只由内容决定，与生成顺序无关。
5. **同义合并（模型 + 确定性）**：
   - 模型在同一 `topic` 内把主张分组为"同一命题"，输出分组及组间关系 `agrees | refines | conflicts`；冲突关系必须给出双方 ID 和一句理由。
   - 确定性规则补充冲突：同一命题组内的数值主张，换算到规范单位后区间不相交，即标为冲突（不依赖模型判断）。
   - 每组生成一个命题 `cl_<序号>`，结构沿用 `models.EvidenceBundle`（`claim`、`supporting`、`conflicting`）。
6. **写出 `evidence_package.json`**：

   ```json
   {
     "package_id": "falcon_<crow run id>",
     "content_hash": "sha256:...",
     "crow": {"run_id": "...", "gate_version": "falcon-gate-2026.10.1", "readiness": "passed"},
     "sources":  [{"source_id": "...", "title": "...", "locator": "...", "quality_tier": "A", "classification": "public"}],
     "evidence": [{"evidence_id": "ev_...", "claim": "...", "excerpt": "...", "source_id": "...", "location": "...",
                   "topic": "spacing", "quantity": {"name": "interval", "min": 50, "max": 100, "unit": "m"},
                   "relevance_score": 0.8, "quality_score": 0.82, "questions": ["q1"]}],
     "claims":   [{"claim_id": "cl_001", "statement": "...", "supporting": ["ev_..."], "conflicting": ["ev_..."]}],
     "dropped":  [{"excerpt_id": "ex_...", "reason": "quote not found in excerpt"}]
   }
   ```

   与 IR 现有加载器兼容（`evidence[].evidence_id`、`sources[].source_id`、`package_id`）。

### F2 需求拆解（模型 + 确定性检查）

模型把需求拆成条款：

```text
RequirementClause:
  id          rq_01 …
  text        需求原文片段（必须是原文子串，确定性核验）
  kind        behavior | condition | formation | coordination | constraint | parameter | out_of_scope
  roles       涉及的角色提示
  user_values 原文中用户直接给出的数值 {name, value, unit}（数字必须出现在 text 中）
```

`user_values` 与请求文件的 `user_parameters` 合并，是 `user_specified` 的唯一合法来源。`out_of_scope` 条款必须写理由，列入审查报告。

### F3 骨架（模型 + 确定性检查）

模型输出：执行架构、角色（能力类别、实例数）、每个角色的状态（意图词表）及转换概要、协同模式（只能从目录选）、编队、消息清单。每个元素列出它实现的条款 `implements: [rq_..]`。

确定性检查：协同模式、意图、能力类别在目录中；所有非 `out_of_scope` 条款至少被一个元素实现；有 `target_engine` 时，骨架所需动作都在引擎能力内。未覆盖的条款带回模型重试一次，仍未覆盖则记入审查报告。

### F4 分段填充（模型 + 确定性强制）

按以下顺序逐段生成，每段的结构化输出 Schema 直接取自 `ir/models.py` 中对应模型的 JSON Schema 片段，提示词中给出骨架、已生成的段、相关目录片段和相关命题（按 topic 和条款检索，不全量注入）：

1. `parameters`：参数（含 `basis`）与假设
2. `structure`：编队几何、协同配置、自定义消息（之后立即展开协同模式，把生成的行为、条件、消息、子树 ID 提供给后续段）
3. `conditions`：条件（表达式 DSL）
4. `behaviors`：行为（动作、`issue_policy`、成功/失败条件），可追加黑板变量
5. `control`：行为树（扁平节点列表）、状态机转换
6. `verification`：L2 测试（五类覆盖）、L3 想定（每个协同规则和编队至少一个）

**basis 强制规则（模型输出之后由代码执行，覆盖模型的选择）：**

| 情况 | 处理 |
| --- | --- |
| 引用了证据的值 | `confirmation` 一律置为 `pending` |
| 状态为 `explicit`，但值不在所引证据数值区间内（换算单位后） | 降为 `inferred`，`rationale` 追加"值不在证据范围 [a, b] 内"，列入审查 |
| 所引证据属于同一冲突命题 | 状态置为 `conflicting`（需 ≥2 条证据与理由，不满足时降为 `unknown`） |
| 状态为 `user_specified`，但值不在 F2 的 `user_values` / `user_parameters` 中 | 降为 `unknown`（防止把无依据的数值写成用户给定） |
| 既无证据也非用户给定 | `unknown`，值保留为占位，`pending` |
| 引用了不存在的证据 ID | 删除该引用；删除后若不满足状态要求，按上表降级 |

组装器把各段合并为 `BehaviorSpec`，用固定字段顺序写出 YAML，并在每个由 Falcon 生成的元素上记录 `implements`（写入 `falcon_trace.json`，不进入 IR）。

### F5 校验修复（确定性驱动，模型修复）

1. 运行 `ir test <pkg> --draft`（L1 + L2，跳过确认闸门）。
2. 有错误时按诊断路径归到 F4 的段，只重新生成出错的段：提示词中带上诊断码、路径、消息和建议。
3. 最多 3 轮。仍有错误时照常写出所有文件，状态记为 `draft_invalid`，错误列在审查报告首段。
4. 可选 `--with-l3`：用占位值运行 L3 想定，结果只作参考，报告中标明"数值未确认"。

### F6 审查材料（确定性）

- `confirmations.yaml`：每个 `pending`/`unknown` 的 basis 一项，按"待确认参数 → 推断规则 → 冲突证据 → 假设"排序：

  ```yaml
  items:
    - target: parameters.wedge_interval        # IR 中的路径
      value: {value: 100, unit: m}
      status: explicit
      evidence:
        - {id: ev_3f2a..., claim: "开阔地楔形队形车距 50–150 m", tier: A, locator: "…, ch.3"}
      note: null
      decision: pending                        # 用户填写：confirm | reject | override
      override: null                           # decision = override 时填写 {value, unit}
  ```

- `falcon_review.md`：门槛结论；条款覆盖表（条款 → IR 元素 → 证据）；审查清单（同上顺序，带证据摘要）；冲突命题；被丢弃的主张数量及原因；F5 结果。
- `falcon_trace.json`：条款→元素→basis→证据→来源的完整映射；各阶段耗时、模型名、提示词哈希、token 用量。只保存解析后的结构化输出，不保存原始模型响应。

## 5. 人工确认（文件式）

```bash
python -m afsim_research_agent.falcon confirm models/armor_platoon_v2 --by <姓名或标识>
```

- 读取 `confirmations.yaml`，把决定写回 `behavior_spec.yaml` 对应 `basis`：
  - `confirm`：`confirmation: confirmed`，`confirmed_by` 记录确认人；
  - `reject`：`confirmation: rejected`（规范化阶段由 `EVD002` 阻塞）；
  - `override`：数值改为用户给定值，`status: user_specified`，`rationale` 记录原值及原证据 ID。
- 写回只改动目标字段，保留格式和注释（`falcon/yamlpatch.py`，按 PyYAML 节点位置就地修改，见第 11 节）；用户在 `behavior_spec.yaml` 中的手工修改保留。
- 写回前检查 `behavior_spec.yaml` 的路径是否仍然存在、当前值是否与清单中的值一致；不一致的条目跳过并报告（用户已手工修改）。
- 完成后提示运行 `ir build`；仍有 `pending`/`rejected` 时列出剩余项。

`python -m afsim_research_agent.falcon status <pkg>`：显示待确认、已确认、已驳回数量，以及能否进入正式构建。

## 6. 模块与命令

```text
src/afsim_research_agent/falcon/
  __init__.py
  __main__.py      # draft | confirm | status（第二期：revise）
  models.py        # 请求、条款、主张草稿、骨架、段输出、追溯记录
  llm.py           # FalconModel 协议；默认 DashScope（沿用 Crow 配置，可用 FALCON_CHAT_MODEL 覆盖）
  gate.py          # F0
  evidence.py      # F1（片段去重、主张抽取、引文核验、合并、冲突、写包）
  requirements.py  # F2
  skeleton.py      # F3
  fill.py          # F4（分段生成，协同展开后提供生成的 ID）
  enforce.py       # F4 basis 强制规则
  assemble.py      # 组装、扁平树还原、引用列表、YAML 写出
  repair.py        # F5
  review.py        # F6
  confirm.py       # 文件式确认、status
  yamlpatch.py     # 保留注释的就地修改
  pipeline.py      # draft 流程（暂存目录，完成后改名）
  prompts.py       # 各阶段提示词模板（中文），带版本号
```

`src/afsim_research_agent/evidence/merge.py` 承载与 Falcon 无关的通用部分：资料中单位写法的识别、单位换算后的区间冲突判定。

```bash
python -m afsim_research_agent.falcon draft falcon_request.yaml [--out models/<id>] [--with-l3] [--max-repair 3]
python -m afsim_research_agent.falcon confirm models/<id> --by <name>
python -m afsim_research_agent.falcon status models/<id>
python -m afsim_research_agent.ir build models/<id>
```

退出码：0 成功；2 门槛拒绝（`FAL001`）；3 输入或目录错误（`FAL002`/`FAL003`）；4 草稿仍有 L1/L2 错误（`draft_invalid`，文件已写出）。

## 7. 资料边界与安全

- Falcon 不读取 Crow 未处理过的本地文件；送入模型的内容只限于 Crow 结果中已有的摘录与摘要，以及请求文件。
- 本地来源（Crow 的 `kind` 为 `local_document`/`local_code`，证据包中记为 `classification: user_private`）：其摘录只发送给与 Crow 相同的模型服务（Crow 已经把它们交给这一服务处理过），不进入任何公网检索请求；审查报告只引用 ID 和简短主张，不复制原文。
- 报告和追溯文件不保存原始模型响应、密钥、本地绝对路径。
- 生成物只写入新的模型包目录。
- 只输出中立 IR；不读取、不生成任何特定仿真平台的受控资料或脚本。

## 8. 测试与验收

**单元测试（脚本化假模型，不联网）：**

| 测试 | 验证 |
| --- | --- |
| 门槛 | `ready = false`、缺少 readiness、文件损坏 → 拒绝，退出码 2/3，不写任何文件 |
| 证据 ID 稳定 | 同一输入两次运行 ID 与 `content_hash` 一致；打乱片段顺序 ID 不变 |
| 引文核验 | `quote` 不在原文、数字不在 `quote`、单位不可识别 → 丢弃并记录 |
| 冲突判定 | 区间不相交 → 冲突（即使模型标为 agrees）；单位换算后相交 → 不冲突 |
| basis 强制 | 证据值一律 `pending`；越界降级；伪造 `user_specified` 降级；悬空证据 ID 删除 |
| 修复循环 | 注入 L1 错误 → 只重生成对应段；3 轮不收敛 → `draft_invalid` 并写出文件 |
| 确认写回 | confirm/reject/override 正确写回且保留注释；值已被手改的条目跳过 |
| 不覆盖 | 目标目录已存在 → `FAL002` |
| 端到端 | 用录制的装甲排 Crow 结果 + 假模型输出，产出的草稿 `ir test --draft` 通过，确认后 `ir build` 通过 |

**在线评测（`-m live`，真实模型）**，以装甲排需求为基准，记录：L1 是否在 ≤3 轮修复内通过；条款覆盖率；有证据支持的 basis 比例；被丢弃主张比例；`user_specified` 降级次数（应为 0）；确认后 L3 通过情况。与手写示例包 `examples/armor_platoon_terrain_formation` 对比结构差异，作为人工评估材料。

**验收标准（v1）：**

1. 门槛未通过时拒绝运行，且不产生任何文件；
2. 草稿中每个参数、条件阈值、转换、协同规则都有 `basis`，所有证据引用都能在 `evidence_package.json` 中找到；
3. 无任何未经用户给定的 `user_specified`，无任何来自资料却非 `pending` 的值；
4. 装甲排基准需求：L1 + L2 草稿在 ≤3 轮修复内通过，条款覆盖率 100%（含显式 `out_of_scope`）；
5. 文件式确认后 `ir build` 可直接运行。

## 9. 实施计划

| 阶段 | 内容 | 产出 |
| --- | --- | --- |
| P1 | 模型与请求文件、F0 门槛、F1 证据规范化、`evidence_package.json` | `falcon` 包骨架、证据单元测试 |
| P2 | F2 需求拆解、F3 骨架、F4 分段填充、组装器、basis 强制 | 可写出草稿 |
| P3 | F5 校验修复、F6 审查材料、`confirm`/`status` 命令 | 文件式闭环 |
| P4 | 在线评测（装甲排基准）、问题修正、文档 | 评测报告 |
| 第二期 | `falcon revise`（按驳回项、用户反馈和 L3 结果局部重生成，已确认项锁定）；接入 Deep Agents 对话式确认 | — |

## 10. 风险与待观察

- **模型能力**：默认 `qwen3.8-flash` 生成行为树和表达式可能偏弱；分段生成、目录片段注入和修复循环用来缓解，必要时用 `FALCON_CHAT_MODEL` 换更强的模型。
- **结构化输出深度**：嵌套较深的 Schema（行为树）可能超出 `json_schema` 模式的支持范围；必要时改为扁平节点列表（`id`、`type`、`children: [id]`），由组装器还原为树。
- **主张 ID 稳定性**：ID 由内容哈希决定，但模型对同一片段的措辞可能变化；温度固定为 0，并把主张抽取结果缓存在包目录中，修订时复用。
- **证据粒度**：`excerpt` 是 PaperQA 切分出的原文片段（`summary` 才是模型改写的摘要）；引文核验只对 `excerpt` 进行。片段切分可能截断句子，被截断的主张会在核验中丢弃，丢弃比例在评测中跟踪。

## 11. 实现说明（2026-10-13，v1）

代码：`src/afsim_research_agent/falcon/`；测试：`tests/unit/falcon/`（20 项，脚本化模型，不联网）。相对本稿的调整：

| 项 | 调整 | 原因 |
| --- | --- | --- |
| 确认写回 | 不引入 `ruamel.yaml`；`falcon/yamlpatch.py` 按 PyYAML 节点的源位置只改目标值，缺失的键插在同级键后面 | 当前环境无法安装新依赖；PyYAML 节点自带精确位置，足以保留注释和格式（测试覆盖） |
| 分段顺序 | 改为 参数与假设 → 编队/协同/消息 → 条件 → 行为 → 行为树与转换 → 测试与想定 | 行为树要引用协同模式生成的行为与子树，协同配置须先于树生成；`structure` 段之后即展开协同，把生成的 ID 提供给后续段 |
| 黑板变量 | `behaviors` 段可追加 `blackboard_variables`（角色、变量、类型、默认值） | 行为里用 `set_blackboard` 时可以同时声明变量，免得骨架阶段遗漏 |
| 行为树 | 一律用扁平节点列表（`node_id`、`children: [id]`），组装器还原并检查重复、缺失、环、不可达节点 | 第 10 节已预见：深层递归 Schema 在结构化输出中不稳定 |
| 目标引擎 | 绑定文件只列能力（`capabilities`），因此检查的是骨架中角色依赖的能力（`platform:<类别>`、机动、传感器、通信、武器），而不是动作 | 与 `bindings/<engine>.json` 现有格式一致 |
| 修复循环 | 同一轮里既有模型错误又有测试错误时，先只修模型，测试留到下一轮 | 测试失败常常只是被测模型有错；避免让模型去改测试迎合错误的行为 |
| 组装问题 | 组装器发现的问题（转换指向未知状态、树节点缺失等）以 `FALASM` 与 IR 诊断一起进入修复循环 | 这类问题在 YAML 层面看不到 |
| 提示词 | `falcon/prompts.py`（单文件，带 `PROMPT_VERSION`），而非 `prompts/` 目录 | 便于打包与版本记录 |
| 原子写入 | 先写 `<目录>.partial`，完成后改名；拒绝或异常时删除，不留下半成品 | 落实"拒绝时不产生任何文件" |
| 错误码 | 新增 `FAL004`：模型输出多次重试后仍无法使用 | 区分输入错误与模型问题 |

确认与 IR 的衔接：`user_specified` 只在参数值能与需求原文或请求文件中的数值对上（换算单位后相等），或规则元素实现了某条需求条款时保留，并自动记为 `confirmed`（`confirmed_by: falcon_request`）；其余一律 `pending`，由 `confirmations.yaml` 逐项决定。

在线评测（P4）需要一份真实的 Crow 运行结果；评测记录将补充在本节之后。


## 12. 修订：参考值直接通过检验（2026-10-17）

**用户确定**：无法确认的具体数值先用合理参考值，可以直接通过检验，但要标注哪些参考值无依据，便于之后人工复核调参。

**做法**（`ir/normalize.py` 确认闸门、`ir/references.py`）：

- 参数（`parameters`）未经确认时按**参考值**处理：不再报 `PAR001` 阻塞，改记信息 `PAR002`（"参考值 300 m（unknown，无依据），按参考值构建，待人工复核调参"）。只有被否决（rejected）的参数仍然阻塞。规则、协同、转换等其他要素的依据与需确认的假设仍按原闸门处理。
- `review.md` 新增第 1b 节"参考值（已用于构建，待人工复核调参）"：参数、取值、依据（⚠ 无依据 / 证据直接给出 / 由证据推断 / 证据冲突）、证据编号、说明；无依据的排在前面。第 1 节"待确认参数与规则"不再列这些参数。`validation_report.json` 增加 `reference_values`。
- Falcon：`falcon_review.md` 审查清单中的参数一栏改为"参考值：可直接构建，待人工复核调参"，无依据的标 ⚠；`falcon status` 不再把未确认的参数计为阻塞项。用 `falcon confirm` 确认或覆盖后，参数不再出现在参考值列表中。
- `ir build --strict-parameters` 恢复原来的严格闸门（未确认的参数也阻塞）。

测试：IR 归一化新增参考值用例（通过、列出、严格模式阻塞），端到端构建用例改为"未确认参数通过并列入 review.md、被否决参数阻塞"。全部单元测试 447 项通过，另 11 项为容器内桩模块导致的已知失败。


## 13. 接收 Crow 选定的战术（P4 / C6，2026-10-17）

Falcon 的输入从"证据 + 需求"扩展为"证据 + 需求 + 用户在 Crow C5 选定的战术"。请求文件新增 `tactic_selection`：

```yaml
model_id: armor_platoon_tc01
tactic_selection: data/runs/<run>/<tactics>/   # 战术目录，或其中的 tactic_selection.yaml
# requirement、crow_result、platforms 可以省略：
#   requirement 用 Crow 的需求，crow_result 取战术目录或其上级目录的 research_result.json，
#   platforms 取 mission_profile.yaml 的 own_forces
```

**F0 门槛**（写任何文件之前拒绝，`FAL003`）：

- 选择文件无法读取，或不是 `TacticSelection`、没有 `confirmed_by`；
- 选定的候选不在 `tactic_candidates.yaml`；
- 候选在选择后又被修改过（选择记录的版本与候选当前版本不同）；
- 阶段名不是该候选的步骤阶段。

**生成过程中的处理**（代码：`falcon/tactic.py`）：

| 项 | 做法 |
| --- | --- |
| 需求 | 请求中的需求（省略时用 Crow 的需求），加上"用户选择备注"。备注里用户写的数值按 user_specified 处理 |
| 战术条款 | 选定的步骤、队形和例外转为条款，`origin: tactic`，编号为 `tc_xx_s<n>` / `g<n>` / `x<n>`（n 对应战术卡中的序号），和需求条款一起要求骨架覆盖。只选部分阶段的候选，只把这些阶段的步骤列为条款，队形和例外仅作参考 |
| 依据 | 复用 Crow 的证据包 `evidence_package.json`，不重跑 F1，战术卡引用的证据 ID 可以直接作为 basis。战术条款带上卡片的证据（不在证据包中的引用会去掉并记录）。找不到证据包时重新规范化，并说明卡片证据无法沿用 |
| 提示词 | 骨架和各段提示词都附上"选定战术"块：规则说明加上战术摘要（名称、版本、选用范围、战术描述、角色、队形、步骤、例外、假设、用户备注）。规则：实现战术而不是重新设计；组合时按顺序衔接各阶段；战术里没有资料依据的数值（含"参考值"假设）定义为参数，basis 写 unknown（按 §12 作为参考值构建，标注无依据） |
| 能力颗粒度 | 请求没写 `capability_refine` 时沿用 Crow 标注时的细化组 |
| 能力缺口 | 选定候选的 Crow 预估缺口写入 `crow_capability_gaps.yaml`。草稿的 IR 核对对照这些缺口（同样需要 / 按 IR 用法不构成缺口 / 未使用，IR 新发现的标 ★新）。之后 `ir build` 自动读取该文件，不需要 `--crow-gaps` |
| 记录 | `selected_tactics.yaml`：选择记录、选定的战术卡和红方反制。`falcon_trace.json` 增加 `tactic` 摘要 |
| 审查报告 | `falcon_review.md` 新增 §0b"选定战术"：选择人、日期、备注、否决项；候选表（版本、选用阶段、来源、平台可行性、红方评价）；战术中的参考值假设；红方反制（v1 只记录，后续转为 L3 想定）。§1 条款覆盖表同时列出战术条款 |

crow-tactic-proposal-v1 §7 设想的三类卡片（选择规则 / 战术程序 / 协同战术）属于 Falcon v2 生成层，目前未实现。v1 用"战术条款 + 战术摘要"把选定战术交给现有的分段生成。

测试：`tests/unit/falcon/test_falcon_tactic.py` 共 4 项：

- 条款、证据与摘要；
- 过期或错误的选择被拒绝；
- 端到端：复用证据包、各阶段提示词含战术、缺口对照、审查报告、`ir build` 自动对照；
- 用户的需求和平台优先于 Crow 的。

全部单元测试 451 项通过，另 11 项为容器内桩模块导致的已知失败。

### 13.1 首次在线运行（armor-eval-03，tactics_v2_rt3 / tc_01，2026-10-17）

**接入部分正常**：

- 选择校验通过，复用了 Crow 证据包，没有重跑 F1；
- tc_01 的 5 个步骤、3 个队形、5 个例外共 13 条战术条款，加上 6 条需求条款，骨架全部覆盖；
- 状态机按战术阶段建出 travel_open → transition_warning（转换前警戒）→ travel_urban → contact → bounding_overwatch；
- 红方反制与 Crow 能力缺口已记入模型包。

**草稿未通过**（`draft_invalid`）。离线重放模型输出后发现以下问题，均已修正：

| 问题 | 原因 | 修正 |
| --- | --- | --- |
| 只报 2 个错误，实际有 46 个 | 一条理由中含反引号，IR 加载器在加载阶段就拒绝了文件，后续校验没有运行 | 理由去掉模板/代码标记、截到 300 字（`enforce.py`）；描述等文本字段同样清理（`autofix.py`） |
| 修复循环回退到最差的草稿 | 回退比较的是错误数，加载失败的草稿只显示 2 个错误，被当成"最好"，第 3 轮 24 个错误的草稿被丢弃 | 评分改为（能否加载, 错误数），加载失败的草稿比任何能加载的都差；审查报告的修复轮次标注"草稿无法加载" |
| 把条款编号当证据 | 模型把 `tc_01_s1`、`rq_04` 写进 `basis.evidence` | 代码把条款编号换成该条款带的证据；提示词写明 evidence 只写 ev_ 编号 |
| 报告接收方不存在 | `report_and_wait` 的接收方写成骨架外的 `superior_unit` | 提示词要求报告双方都是骨架中的角色（排内：僚车 → 排长） |
| 角色没有声明通信、传感器、武器 | 骨架只声明了机动部件，后续段却用了通信、射击、观察 | 粗粒度下按平台类别补默认部件，并标"简化处理"（地面车辆：platoon_net / voice_data，vehicle_sight / visual_search，main_gun / direct_fire） |
| 协同生成元素被重复定义 | 模型在条件段自行写了 `<协同>__*` 条件 | 删除模型写的副本 |
| 无站位的"编队"、照抄示例 ID 的 `traces_to` | 模型把"两组交替"写成编队；想定追溯到提示词示例中的 ID | 没有被引用的空编队删除；`traces_to` 中不存在的对象删除 |
| 部分战术条款只挂在角色或状态上 | 骨架层只能把条款挂到角色和状态，后续段没有具体实现 | 审查报告 §1 标出"仅角色/状态级"的条款；提示词要求落到转换、行为或协同上，IR 无法表达的写入 out_of_scope |

修正后重放同一份模型输出：错误从 46 个降到 31 个。剩下的是生成质量问题，留给修复循环或 Falcon v2 解决：

- 引用未定义的参数 `travel_speed`；
- 子树节点缺少 `tree`；
- 消息未定义；
- 想定没有覆盖协同与编队。

参数取值也需要人工复核：

- 楔形车距 10 m 引用的是步兵士兵间隔；
- 冲刺 3–5 s 是单兵跃进的时间，不适用于坦克跃进。

两处证据都与装甲排不对应，应在 `confirmations.yaml` 中改写（override）。

测试：新增 `tests/unit/falcon/test_falcon_fixes_p4.py` 4 项。全部单元测试 455 项通过，另 11 项为桩模块导致的已知失败。
