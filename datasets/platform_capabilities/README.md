# 平台能力集（仅本机）

本目录不进入版本控制（`.gitignore` 只放行本说明）。能力集由装甲车工程（主版本）导出，派生自受控资料，只保存在本机。

```text
armored-vehicle-afsim-2.9/
├── index.yaml               导出：元数据、切片、条目数、源内容哈希
├── action_set/<切片>.yaml   导出：动作集（资源、黑板、原语、动作、战术步骤）
├── tactic_set/<切片>.yaml   导出：战术集（子树、状态树、任务状态机、条件）
├── ir_alignment.toml        Agent 侧：中文名称、IR 映射、组合映射、检索用语
├── group_members.toml       Agent 侧：各能力组包含的条目
└── capability_overrides.yaml（可选）缺口确认与插件实现的回写
```

**导出或更新**（在有工程的电脑上）：

```bat
python -m afsim_research_agent.capability export-lab --lab D:\afsim\projects\apc_behavior_lab
python -m afsim_research_agent.capability export-lab --lab D:\afsim\projects\apc_behavior_lab --check
```

导出只写 `index.yaml`、`action_set/`、`tactic_set/`，不覆盖 Agent 侧的两个文件。

**换一台电脑**：把整个 `armored-vehicle-afsim-2.9/` 目录复制过去，或用 `CROW_CAPABILITY_CATALOG` 指向它。没有能力集时，能力核对记为 `CAP000` 并跳过，生成照常进行；依赖能力集的单元测试会自动跳过。

状态含义、导出时的清除规则和判定方法见 `docs/design/capability-catalog-integration-v1.md` §14。
