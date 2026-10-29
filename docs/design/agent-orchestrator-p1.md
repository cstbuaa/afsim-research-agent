# Deep Agents 总控骨架与扩展接口（P1，2026-10-23）

承接 `agent-loop-deepagents-v1.md` §3、§5 的 P1：把现有各环节接到 Deep Agents 主 agent 上，并为正在开发的能力预留接口。

## 1. 结构

```text
src/afsim_research_agent/
  agent.py                 langgraph dev 入口：agent = build_agent()
  orchestrator/
    results.py             StageResult：每个工具的统一返回（状态、摘要、产物路径、问题、下一步）
    workspace.py           Workspace：路径规则（工作区相对路径、_local 禁止、只在 runs/ 下新建）
    stages.py              各环节的纯 Python 封装，不依赖 Deep Agents，可单独测试和脚本调用
    extensions.py          五个扩展接口与注册表
    tools.py               TOOL_SPECS：工具清单 → LangChain StructuredTool
    graph.py               build_agent()：组装 deep agent
    testing.py             离线检查用的脚本化聊天模型
    cli.py / __main__.py   tools / check / probe / run 四个子命令
```

依赖方向：`stages`、`extensions`、`workspace`、`results` 不导入 LangChain 或 Deep Agents；只有 `tools`、`graph`、`cli`、`testing` 导入。确定性环节因此可以不经过 agent 直接测试。

## 2. 运行组织

| Deep Agents 机制 | 本工程的用法 |
| --- | --- |
| `tools=` | `TOOL_SPECS` 中的 20 个工具加 `ask_human` |
| `backend=FilesystemBackend(root_dir=data/, virtual_mode=True)` | 内置的 `ls / read_file / grep` 与各工具使用同一套工作区路径（如 `/runs/...`） |
| `permissions=` | `_local` 读写都拒绝；只允许写 `/runs/**`；其余写操作拒绝（先匹配先生效） |
| `interrupt_on=` | `select_tactic`、`apply_confirmations`、`platform_export`、`draft_capability_plugin` 执行前暂停，可批准、修改参数或拒绝 |
| `ask_human` 工具 | 在工具内调用 LangGraph `interrupt()`，用户回答后继续；可写入 `decisions.yaml` |
| `checkpointer=` | CLI 默认内存检查点；`--checkpoint-db` 用 SQLite 可续跑；`langgraph dev` 由服务端提供 |
| `middleware=[TodoListMiddleware()]` | `write_todos` 规划工具（`todos=False` 可关闭） |
| `subagents=`、`skills=` | 预留参数，P2 再加 |

**0.7.23 的默认中间件里没有 `write_todos` 规划工具**（只有个别模型调优配置会加），所以 `build_agent` 自己加上 LangChain 的 `TodoListMiddleware`。阶段顺序同时写在系统提示词里，并由 `overview` 工具给出。

## 3. 工具

| 工具 | 分组 | 调用模型 | 需批准 | 封装的现有代码 |
| --- | --- | --- | --- | --- |
| `overview` | meta | | | 流程、扩展状态、最近运行 |
| `crow_research` | crow | ✓ | | `crow.cli.prepare_mission_profile` + `run_research_with_result` |
| `crow_propose` | crow | ✓ | | `crow.propose.run_proposal(redteam=True)` |
| `select_tactic` | human | | ✓ | `crow.select.build_selection` |
| `falcon_draft` | falcon | ✓ | | 写 `falcon_request_<id>.yaml` + `falcon.pipeline.run_draft`（generator v2） |
| `falcon_check` | falcon | | | `falcon.repair.check_package` |
| `falcon_compile` | falcon | | | `falcon.sheet.recompile.recompile_package` |
| `falcon_fix` | falcon | ✓ | | `falcon.sheet.fixer.fix_package`（在副本上） |
| `falcon_properties` | falcon | ✓ | | `falcon.sheet.fixer.write_properties` |
| `confirmation_status` | human | | | `falcon.confirm.package_status` |
| `apply_confirmations` | human | | ✓ | `falcon.confirm.apply_confirmations` |
| `ir_build` | ir | | | `ir.pipeline.build_package` |
| `simulate` | ir | | | 对草稿跑 L3 想定（设计文档中待做的 `simulate` 工具） |
| `capability_gaps` | ir | | | 读 `capability_gaps.yaml` |
| `list_actions` | extension | | | 动作集 |
| `find_tactics` | extension | | | 战术集 |
| `plan_format` / `compile_plan` | extension | | | 行为树生成器 |
| `platform_export` | extension | | ✓ | 仿真平台后端（AFSIM） |
| `draft_capability_plugin` | extension | | ✓ | 能力插件开发 |

每个工具返回 `StageResult` 的 JSON：`status` 为 `ok / failed / blocked / needs_human / not_configured`，问题最多列 12 条，完整内容留在文件里，路径一律是工作区路径。预期内的失败（路径不合法、检查未通过、目录已存在）返回 `blocked` 或 `failed`，不抛异常。

## 4. 扩展接口

接口定义在 `orchestrator/extensions.py`。没有注册实现时，对应工具返回 `not_configured`，agent 会告诉用户该能力未接入并按现有流程继续。

| 槽位 | 接口 | 方法 | 对应的开发工作 |
| --- | --- | --- | --- |
| `action_library` | `ActionLibrary` | `actions(platform_class)`、`get(name)` | 动作集：取代过细的能力目录 |
| `tactic_library` | `TacticLibrary` | `tactics()`、`get(name)`、`match(query, limit)` | 战术集：由动作编排的战术模板 |
| `plan_compiler` | `PlanCompiler` | `format_guide()`、`validate(plan)`、`compile(plan, out_dir)` | 模型只写高层描述，由确定性程序生成行为树 |
| `platform_backends[名]` | `PlatformBackend` | `export(package_dir, out_dir)`、`run(export_dir)` | AFSIM 模板映射、运行、读回报错 |
| `plugin_builder` | `CapabilityPluginBuilder` | `draft(gap, out_dir)`、`verify(draft_dir)` | agent 构建面向仿真平台的能力插件 |

数据类型：`ActionSpec`、`TacticSpec`、`CompileOutput`、`PlatformRun`。`ActionSpec.native` 存放平台原生名称与绑定，**不会出现在工具结果中**。

注册方式（三选一）：

```python
# 1. 代码里
from afsim_research_agent.orchestrator.extensions import get_registry
get_registry().platform_backends["afsim"] = AfsimTemplateBackend(...)

# 2. 环境变量（逗号分隔），每个钩子收到注册表
#    AFSIM_AGENT_EXTENSIONS=my_ext.actions:setup,my_ext.afsim:setup
def setup(registry):
    registry.action_library = MyActionLibrary()

# 3. 包的 entry point，组名 afsim_research_agent.extensions
```

**内置默认（2026-10-29）**：动作集与战术集默认由装甲车能力集提供（`capability/libraries.py`，见 `capability-catalog-integration-v1.md` §14）。`get_registry()` 在加载完用户钩子之后只填空槽位，用户注册的实现优先。

`ExtensionRegistry.check()` 检查已注册的实现是否满足接口，`python -m afsim_research_agent.orchestrator tools` 会列出接入情况和问题。`CatalogActionLibrary` 把现有 `ir/catalog/actions.toml` 适配成动作集接口，作为对照和示例，不会自动注册。

## 5. 检查方法

```bash
conda activate afsim_agent
pytest tests/unit/orchestrator                     # 13 项；接线测试在装有 deepagents 时运行
python -m afsim_research_agent.orchestrator tools
python -m afsim_research_agent.orchestrator check --package /runs/<运行>/models/<包>
python -m afsim_research_agent.orchestrator probe --package /runs/<运行>/models/<包> --trials 5
```

- `check` 不调用模型：用脚本化模型驱动真实的 `create_deep_agent`，验证工具可见、工具返回结构化结果、需批准工具暂停与拒绝、`ask_human` 暂停与恢复。
- `probe` 调用 qwen：对只读工具测试"选对工具"和"参数正确"的比例，报告写到 `runs/agent-probe/`。参数正确率低于 0.9 时返回非零，说明需要为 qwen 加一层 JSON 动作协议的适配（`agent-loop-deepagents-v1.md` §6 的风险）。

## 6. 本地网页界面

```bash
python -m afsim_research_agent.orchestrator ui            # http://127.0.0.1:18765
python -m afsim_research_agent.orchestrator ui --demo     # 不调用模型，用脚本化过程演示界面
```

- 只监听本机（127.0.0.1），只用 Python 标准库，没有新增依赖。
- 三栏：左侧会话列表、agent 的计划（`write_todos`）、扩展接入情况；中间对话与工具卡片（状态、摘要、问题、产物链接）；右侧文件浏览（同样受工作区规则约束，`_local` 不可见）。
- 需要人决定时，输入框上方出现高亮面板：`ask_human` 给出选项按钮和自由回答；需批准的工具显示参数（可编辑）并可批准、按修改后的参数执行或拒绝（附理由）。
- 会话列表存在 `runs/agent-ui/threads.json`；对话状态存在 `runs/agent-ui/checkpoints.sqlite`（需要 `langgraph-checkpoint-sqlite`，未安装时用内存检查点，重启后不能续跑）。
- 实现：`orchestrator/web/session.py`（后台驱动 agent、把消息和中断转成事件）、`web/server.py`（HTTP 接口）、`web/static/index.html`（单页）。浏览器轮询事件，运行中约 0.7 秒一次。
- `ORCHESTRATOR_MODEL` 可单独指定总控模型，未设置时用 `DASHSCOPE_CHAT_MODEL`。

## 7. 暂缓测试（用户选择"先搁置、先看仿真"）

战术意图没定时，相关单步测试往往写不对（如引用了模型里没有的消息），会让整个稿件无法规范化为 IR，仿真也跑不了。现在支持把这类测试**暂缓**：

```yaml
tests:
  - id: T16
    deferred: true
    deferred_reason: "排长报告后是否等待僚车确认尚未决定"
    role: platoon_leader
    ...
```

- 编译器：带 `deferred: true` 的测试保留在战术稿中，但不编译进 IR、不参加 L2；它所写的转换仍算"已覆盖（待定）"，以免覆盖检查逼纠错循环抢先写一个替代测试。每个暂缓测试产生一条警告，写进审查报告。
- 纠错循环：不能新增或去掉 `deferred` 标记（工具层拒绝），只能由人决定。
- 工具 `defer_tests(package, test_ids, reason, decision)`：执行前暂停等批准；在战术稿中插入标记、记入 `decisions.yaml`、重新编译。
- 决定后删掉 `deferred` 两行，按决定改写测试，再 `falcon_check`。

## 8. 端到端测试记录（2026-10-23，网页界面）

接着 `armor-eval-03` 的检索结果和已选战术 tc_01 运行：
- 流程：`overview` → 读选定战术 → `falcon_draft` 两次（第 1 次 FAL004 失败、第 2 次生成 `armor_tc01_ui_e2e_r2`，draft_invalid）→ `falcon_check` → `falcon_fix`（23 步，21 次读 2 次改，停滞后 needs_human）→ agent 自查后 `ask_human` 给出 A–D 四个选项 → 用户选 D → 当时没有暂缓机制，仿真被 SCN001 阻断 → agent 如实汇报未通过。
- 人机交互路径（计划显示、提问面板、回答后继续、如实汇报）全部走通。
- 暴露的问题与处理：
  - 生成与纠错用了 `qwen3.8-flash`（`.env` 未设 `FALCON_CHAT_MODEL`）→ `overview` 与 `falcon_draft` 结果显示生成模型名；建议设为 max。
  - 总控用英文汇报 → 系统提示词第一句要求始终用中文。
  - 失败的请求文件挡住同名重试 → 请求文件已存在时另起带时间的文件名。
  - 选项 D 无法执行 → 新增暂缓测试（§7）。对该包暂缓 T16、T18 后，稿件可以规范化，L2 与二维仿真得以运行，并暴露出被挡住的真实问题：4 个测试未通过（排长在 `urban_transit` / `report_contact` / `out_of_control` 都直接转入 `engage_enemy`），想定 0/1 通过。

## 9. 未做 / 后续

- 子 agent（研究员、红队、测试设计、AFSIM 编码员）与 skills（战术稿写法、协同规则）：P2。
- `falcon_draft` 失败后自动进入 `falcon_fix`：目前由 agent 按提示词决定是否调用。
- 长耗时工具（`crow_research`）的进度流式输出：目前只在结束时返回。
- 扩展接口的具体实现：动作集、战术集、行为树生成器、AFSIM 模板后端由各自的开发工作接入。
