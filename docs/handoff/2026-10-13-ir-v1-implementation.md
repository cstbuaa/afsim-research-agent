# 交接：Tactical Behavior IR v1 实现（2026-10-13）

## 完成内容

按 `docs/design/tactical-behavior-ir-v1.md` 第 29 节完成第 1–7 步：

| 步骤 | 模块 | 说明 |
| --- | --- | --- |
| 1 Schema | `ir/models.py`、`ir/schema/behavior_spec.schema.json` | Pydantic 模型（未知字段即错误）；JSON Schema 由模型生成，测试保证同步 |
| 2 L1 校验 | `ir/loader.py`、`ir/validate.py` | 严格 YAML（禁 tab/锚点/别名/tag/重复键/插值/疑似凭据）；全部错误码 |
| 3 目录 | `ir/catalog/*.toml` | 动作 25 个（含别名）、操作数 10 类 + 函数 6 个、协同模式 4 种、意图 15 个，均带版本 |
| 4 规范化 | `ir/coordination.py`、`ir/normalize.py` | 协同展开（`generated_from`）、确认闸门、标准单位、完整表达式 AST、sha256 哈希 |
| 5 L2 | `ir/runtime.py`、`ir/l2.py` | 参考解释器（状态机 + 11 种节点）；转换覆盖检查 |
| 6 后端 | `ir/backends/{mermaid,btcpp_xml,python_sim}.py`、`ir/binding.py` | 确定性生成；BT.CPP XML 回读比对；生成模块锁定 IR 哈希 |
| 7 L3 | `ir/sim/{world,l3}.py`、`ir/pipeline.py`、`ir/review.py` | 二维仿真 + 10 种可观测量；`review.md` 按 §27.4 顺序 |

CLI：`python -m afsim_research_agent.ir {validate|test|build|simulate|schema}`。

## 示例包结果

`examples/armor_platoon_terrain_formation/`（排长车 + 3 僚车；楔形/纵队；长僚跟随、交替跃进、接敌报告）：

- L1：0 错误 0 警告；L2：14/14 通过，所有非兜底转换已覆盖；
- L3：3 个想定全部通过——城区前 200 m 全排同步转纵队（僚车 2 s 内跟随），纵队站位误差均值 9 m（容差 30 m）；接敌后约 37 s 一轮交替跃进；通信全断时僚车保持原状态、最小间距 136 m；
- 两次构建逐字节一致。

示例数值均为**示意值**（`user_specified`），不是条令数据。

实现过程中 L3 发现并修复了 3 个问题：仿真重新下发路线时从起点重走（掉头）；动作在 tick 结束后才生效导致新跃进被判“已到达”；示例接敌树完成后重启而重复报告（改为黑板标记防重复）。

## 测试

`tests/unit/ir/` 共 84 个测试（加载器、每个错误码的变异测试、闸门、单位、哈希、节点语义、后端确定性与回读、仿真、端到端构建、外来 generated 目录保护、Schema 同步）。

## 检索侧（同日）

- 评测基线（2 个问题）：金标发现率 0.75、采用率 0.25。armor 问题 54 个候选中 30 个采集失败，其中 dns_error 11、network_error 8（dtic、handle.net、globalsecurity 等域名本地无法解析）。
- 建议设置 `CROW_HTTP_PROXY`（代理负责解析公网域名）；TUN/fake-IP 代理另加 `CROW_ALLOWED_PROXY_CIDRS=198.18.0.0/15`。
- 站点熔断后跳过的候选改记 `host_circuit_open`，不再计入 network_error。

## 下一步

1. 第 8 步 Falcon：Crow 证据包 → `behavior_spec.yaml` 草稿（数值一律 `pending` 并带 evidence id）→ `build --draft` → 用户确认 → `build`。
2. 设置代理后重跑检索评测，确认采集成功率。
3. 根据需要补充更多示例（单平台空中拦截、车队护卫）以覆盖 `priority_selector`、`weighted_random`、`parallel`。
