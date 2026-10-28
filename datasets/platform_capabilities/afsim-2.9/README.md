# AFSIM 2.9 平台能力目录（正式层）

本目录只含正式层（`classification: public`），可提供给生成 Agent 与云端模型。原生名称、绑定和证据页码在用户本地 `afsim-2.9/_local/`，不进入本仓库（`datasets/.gitignore` 排除 `_local/` 与 `bindings.local.json`）。

| 文件 | 内容 |
| --- | --- |
| `capability_catalog.yaml` | 0.2.0 单文件目录，尚未重整的领域仍在此 |
| `catalog/index.yaml` | 全部领域的检索索引：领域 → 子类 → 关键词 / 战术用途 / IR 映射 |
| `catalog/mobility.yaml` | 运动类 0.3.0 |
| `catalog/communication.yaml` | 通信类 0.3.0 |
| `catalog/weapon_employment.yaml` | 武器类 0.3.0（含从资源类迁入的武器库存） |
| `catalog/execution.yaml` | 执行环境类 0.3.0：行为变量、状态计时、等待、控制器开关、决策周期 |
| `catalog/environment.yaml` | 环境与区域类 0.3.0：地形类别、区域判断、通视 |

**更新方式**

1. 在本地 `afsim-2.9/_local/tools/` 运行生成脚本，生成 `catalog/` 与 `_local/`；
2. 只把 `capability_catalog.yaml` 与 `catalog/*.yaml` 复制到这里。

**读取方式**

代码通过 `afsim_research_agent.capability` 读取本目录（见 `design/capability-catalog-integration-v1.md`），路径可用 `CROW_CAPABILITY_CATALOG` 覆盖。
