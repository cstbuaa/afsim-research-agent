# AFSIM Research Agent

[English](README.md) | **简体中文**

一个基于大模型的智能体：用户用自然语言提出战术需求，系统把它变成可在 AFSIM 中运行的行为模型。系统会调研需求、提出完整的候选战术、编写行为模型、自动检查与修复，最后在 AFSIM 中运行并生成回放。当前研究对象是装甲排（地面车辆：机动、编队、感知、交战、目标共享）。

```text
自然语言需求
  -> Crow     调研：调研问题 -> 网络 / 文献检索 -> 带引用的证据
              -> 候选战术 -> 红队评审（另一个模型）-> 选定战术
  -> Falcon   建模：战术稿（状态机 + Python 风格行为树 + 测试）
              从已验证骨架起草或从零起草，只使用能力集里的能力
  -> IR 2.0   分层行为模型 -> 检查 L1 / L2 / L3 + 需求约束
  -> 修复     ReAct 修复循环（约 30 个工具动作），直到检查通过
  -> AFSIM    想定 + 运行配置 -> mission 运行 -> 判定 -> .aer 回放
```

大模型只负责提出方案；凡是能检查的部分都由确定性代码负责：能力引用、编译到 AFSIM、想定绑定、判定、文件安全。

## 现状（2026-11）

| 部分 | 状态 |
| --- | --- |
| Crow 调研与战术提案 | 可用；证据闸门、红队评审、战术选择 |
| Falcon v3 起草 | 可用；骨架起草（编队、打击、队形变换）和从零起草 |
| 检查 | L1 静态规则、L2 单步测试、L3 二维多车仿真（含团队模型）、效果约束（`destroys`、`formation_shape` 等） |
| 修复循环 | 可用；结构化工具、绕过检查拦截、AFSIM 闭环 |
| AFSIM 后端 | 可用；输出 advanced behavior tree，按模型生成想定，分支想定，回放 |
| 工作台 | 可用；LangGraph 智能体 + 网页前端，人工确认，回放面板 |
| 小模型后训练 | 数据生成和 SFT 已完成（Qwen3-8B QLoRA）；GRPO 奖励函数已写好，尚未训练 |

部分结果（详见 `docs/handoff/`）：

- 编队类需求，骨架起草对比从零起草：AFSIM 通过 5/10 对 0/10。
- 补充多车交战与通信模型后，通过二维检查的模型在 AFSIM 中的通过率为 86%（之前为 60%）。
- 通用大模型驱动修复循环，在 100 份真实失败稿件上：L1 未通过的稿件中 84.8% 修到 L1 通过；完全修好的稿件 AFSIM 通过 12/14。
- SFT 修复模型在留出的注入错误上：修改步修好率 0.3% → 91.1%（注入错误较模板化，另用真实失败稿件测试集评估泛化）。

## 目录结构

```text
src/afsim_research_agent/
  agent.py          由 `langgraph dev` 提供的智能体（在 orchestrator/ 中组装）
  orchestrator/     阶段工具、工作区规则、人工确认、工作台 HTTP 接口、命令行
  crow/             需求规划、调研、战术提案、红队、战术选择
  web/ literature/  搜索、网页读取、PaperQA2 证据问答
  knowledge/ evidence/ domain/   本地检索、来源质量、战术词表
  capability/       能力目录：实验室导出、分组、约束
  falcon/v3/        战术稿起草、骨架、提示词、修复循环与工具、分诊
  ir/v2/            IR 2.0：模型、归一化、需求约束、效果判定、二维仿真、
                    AFSIM 编译器、想定接口与判定
  replay/           AFSIM .aer 回放生成
  training/         骨架数据生成器、需求回译、错误注入、样本渲染、
                    判分、真实稿件评估、强化学习奖励
  ir/ falcon/       IR 1.0 和 Falcon v1（早期版本，保留作参考）
frontend/           基于 deep-agents-ui 的工作台前端
scripts/            环境检查、工作台启动模板、训练脚本
docs/               design/、handoff/、analysis/、runbooks/
tests/unit/         不联网的确定性测试
```

## 安装

需要：Python 3.11+、`uv`（或 conda 环境）、阿里云百炼 API 密钥、Node.js（前端）；运行 AFSIM 需要已授权的 AFSIM 安装。

```bash
cp .env.example .env          # 把 API 密钥写进 .env；不要提交
uv sync --group dev
```

```dotenv
DASHSCOPE_API_KEY=sk-your-real-api-key
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
DASHSCOPE_CHAT_MODEL=qwen3.8-flash
DASHSCOPE_EMBEDDING_MODEL=text-embedding-v4
```

其他设置：`TAVILY_API_KEY`（网络搜索）、`ORCHESTRATOR_MODEL`（总控模型）、`FALCON_CHAT_MODEL`（起草与修复）、`FALCON_AFSIM_BINDING`、`FALCON_AFSIM_KIT`、`AFSIM_MISSION`（AFSIM 运行）、`CROW_HTTP_PROXY`、`CROW_CONTACT_EMAIL`。

### 能力集与本地数据

能力集从实验室的 AFSIM 工程导出，只保存在本地，不在本仓库中。本地绑定、想定配置、运行结果和训练数据放在 `_local/` 和 `data/` 下，不提交。生成的模型包总是写到新目录，不会覆盖已有的 AFSIM 文件。

## 工作台

```bash
uv run langgraph dev --n-jobs-per-worker 2      # 智能体：127.0.0.1:2024，assistant 为 "afsim_research"
cd frontend && yarn install && yarn dev        # 前端：http://127.0.0.1:3000
```

Windows 上可以把 `scripts/start_workbench.example.ps1` 复制到 `_local/workbench/`，填入本地路径后运行。

- 智能体调用各阶段工具：调研、提案、选择、起草、检查、修复、设置想定、二维仿真、AFSIM 运行；
- 会改变决定或调用外部平台的工具，会先暂停等待确认（`ask_human` 使用 LangGraph 的 `interrupt`）；
- 回放面板列出 AFSIM 运行结果及其 `.aer` 录像。

命令行用法：

```bash
python -m afsim_research_agent.orchestrator tools                  # 工具与扩展接入情况
python -m afsim_research_agent.orchestrator check --package <pkg>  # 离线检查组装是否正确
python -m afsim_research_agent.orchestrator run "<需求>"             # 在终端交互运行
```

## Crow：调研与战术

```bash
python -m afsim_research_agent.crow.cli --requirement "<需求>" \
  --max-web-results 2 --max-sources 12 --library data/library --gap-rounds 1
```

- 调研问题按“情况 → 动作 → 例外 → 依据”拆分。
- 来源：Tavily（通用网页、条令类域名、技术报告类域名）、OpenAlex、Semantic Scholar、arXiv、CORE，以及本地文献库（`data/library/`，例如手动保存的知网论文）。
- 每一轮：发现 → 初筛（去重、DOI 核验、撤稿、A–D 质量分级）→ 大模型筛选 → 获取 → 引文扩展。每个候选都记入台账，并写明未采用的原因。
- PaperQA2 基于本次运行的语料逐题回答，并给出引用；证据不足的问题会追加一轮有限的补充检索。
- 交给 Falcon 之前先过证据闸门：A/B 级来源和强证据要足够。
- 战术提案给出完整战术：角色分工、队形几何、带结束条件的步骤、例外处理、决策权、所需能力。战术可以来自检索，也可以从相似场景迁移，并由另一个模型评审。
- 产物：`mission_profile.yaml`、`evidence_package.json`、`tactic_selection.yaml`、`capability_gaps.yaml`。

运行目录：`data/runs/crow-research/<时间戳>/`。

## Falcon v3：战术稿 → IR 2.0

```bash
python -m afsim_research_agent.falcon crow-request <战术目录> --model-id <id>   # 由 Crow 结果生成请求
python -m afsim_research_agent.falcon draft3 <request.yaml> [--afsim-run <运行配置>]
python -m afsim_research_agent.falcon fix <模型包> --max-steps 30 --with-l3      # 在副本上修复
```

- **战术稿** 包含：`model` 块（角色、事件），每个角色一份 Mermaid 状态机和一份 Python 风格行为树，条件块，以及 `verification` 块（L2 测试、L3 想定）。战术稿编译成分层的 IR 2.0 模型包。
- **骨架起草（默认）：** 模型只回答一份选择：骨架、模块开关、需求给出的数值、角色对应、覆盖不了的条款。稿件由程序从已验证的骨架生成。骨架族：
  - 编队（K1–K6）；
  - 打击：分配 / 校验 / 选武器 / 开火 / 观察，每一步都有失败出口，可选再攻击、交战时限、停火命令、摧毁目标；
  - 队形变换：纵队、横队、楔形、梯队。
- **需求约束：** 从需求中提取，在二维和 AFSIM 轨迹上判定，包括：
  - 取消响应、前进、保持距离；
  - 先后顺序（先停车后关雷达）；
  - `destroys`（摧毁数量、角色、时限）；
  - `formation_shape`（队形、间距、容差、保持时间）。

## 检查与修复

| 级别 | 能发现的问题 |
| --- | --- |
| L1 静态检查 | 不存在的动作或参数、读取了却没有动作写入的字段、进入状态即成立的条件、僚车下达长车指挥、车辆数不符、没有出口的状态、路线名错误 |
| L2 单步测试 | 转换错误、缺少事件处理、取消后不进入关机 |
| L3 二维仿真 | 卡住的状态、成员从不开火、同一拍内来回跳转、任务过期、射线被友车挡住、需求约束不满足 |

修复循环（`falcon/v3/fixer.py`，工具在 `falcon/v3/tools.py`）是 ReAct 循环，每步一个 JSON 动作：

- **查看类：** `check`、`show`、`explain_check`、`eval_condition`、`who_writes`、`history`、`simulate` 等；
- **结构化修改：** `set_condition`、`set_trigger`、`add_transition`、`use_tree_event`、`set_state_tree`、`set_expectation` 等；
- **其他：** `try_edit`、`revert`、`ask_human`、`run_afsim`。

每一步的提示都从当前状态重建：当前检查结果、最近 5 步、本轮试过的修改。以下修改会被拒绝：重复提交的修改、写成常量的条件、在条件中引用事件、删除测试或想定。

## AFSIM

`ir/v2/afsim/` 负责：

- 把模型包编译成 AFSIM 的 advanced behavior tree；
- 按模型生成想定：敌方距离和运行时长由模型和需求决定，用户指定的值优先；
- 运行 `mission`，并判定事件输出：发现目标、距离、武器命中与摧毁、队形几何；
- 生成 `.aer` 回放。

分支想定在向用户提问之后，覆盖模型的例外分支。

## 小模型后训练

训练在 Linux GPU 服务器上运行；判分在保存检查器和能力集的机器上运行。见 `scripts/train/README.md`。

| 步骤 | 模块 | 结果 |
| --- | --- | --- |
| 模型生成 | `training/pilot_fp.py`、`strike_fp.py`、`reform_fp.py` | 300 个编队/感知模型，全部通过检查，38 种结构；抽检 10 个，AFSIM 全部通过 |
| 需求回译 | `training/backtranslate.py` | 4 种风格共 1200 条，经规则检查和大模型事实核对收下 954 条；定稿 934 对 |
| 样本渲染 | `training/render.py` | 用线上提示词渲染结构稿、控制稿、验证块三类样本；按模型切分 |
| 错误注入 | `training/inject.py` | 10 类错误，3925 次注入，5628 条修复样本，每条都经过复检 |
| SFT | `scripts/train/` | Qwen3-8B QLoRA（r=32、全部线性层、只对回答部分算损失） |
| 真实稿件评估 | `training/realeval.py` | 修复循环在真实失败稿件上运行，可选 AFSIM 闭环 |
| 强化学习奖励 | `training/reward.py` | GRPO 轨迹奖励（草稿）：修复结果、势函数进展、留出想定、绕过 / 无效 / 只看不改惩罚、需求忠实度闸门 |

## 早期版本：IR 1.0 与 Falcon v1

`ir/`（IR 1.0、`behavior_spec.yaml`、联合仿真协议 `tbe-step/1`、`integrations/` 中的 C++ 引擎宿主骨架）和 `falcon/` v1（`draft`、`confirm`、`status`）是早期的接口。它们仍保留并有测试，但新的工作使用 Falcon v3 和 IR 2.0。

## 测试

```bash
pytest -q                       # 不联网的单元测试
ruff check src tests
pytest -m live -s tests/live    # 可选：真实调用 Tavily / OpenAlex / DashScope
```

需要能力集的测试在保存能力集的机器上运行。

## 文档

- `docs/design/`：IR 2.0、Falcon v3 需求约束与检查、能力集语义、总控、引擎协议。
- `docs/handoff/`、`docs/analysis/`：进展记录与实验结果。
- `scripts/train/README.md`：训练环境与流程。

`THIRD_PARTY_NOTICES.md` 列出了第三方组件；`frontend/` 基于 deep-agents-ui（见 `frontend/UPSTREAM.md`）。
