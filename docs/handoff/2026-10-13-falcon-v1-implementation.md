# Tactical Falcon v1 实现交接（2026-10-13）

设计：`docs/design/tactical-falcon-v1.md`（已确认；实现中的调整见第 11 节）。

## 1. 完成情况

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| P1 | 请求文件、F0 门槛、F1 证据规范化、`evidence_package.json` | ✅ |
| P2 | F2 需求拆解、F3 骨架、F4 分段填充、组装器、basis 强制规则 | ✅ |
| P3 | F5 校验修复、F6 审查材料、`confirm` / `status` 命令 | ✅ |
| P4 | 真实模型在线评测（装甲排基准） | ⏳ 需要一份真实的 Crow `research_result.json` |

测试：`tests/unit/falcon/` 21 项全部通过（脚本化模型，不联网）；IR 原有 107 项不受影响；ruff 通过。

## 2. 用法

```bash
python -m afsim_research_agent.falcon draft falcon_request.yaml      # → models/<model_id>/
# 编辑 models/<model_id>/confirmations.yaml，逐项填写 decision: confirm | reject | override
python -m afsim_research_agent.falcon confirm models/<model_id> --by <姓名>
python -m afsim_research_agent.falcon status  models/<model_id>
python -m afsim_research_agent.ir build models/<model_id>
```

请求文件格式见设计文档 §2.1；`crow_result` 指向 Crow 输出目录中的 `research_result.json`（相对请求文件所在目录）。

退出码：0 成功；2 Crow 门槛拒绝（FAL001）；3 输入或模型包错误（FAL002/FAL003）；4 草稿已写出但仍有 L1/L2 错误。

## 3. 测试覆盖的行为（对应验收标准）

| 验收项 | 测试 |
| --- | --- |
| 门槛未通过时拒绝且不产生文件 | `test_gate_refuses_failed_readiness_without_writing`、`test_refusal_and_crash_leave_nothing`、`test_cli_draft_refusal_exit_code` |
| 证据 ID 只由内容决定 | `test_package_ids_stable_and_order_independent`（打乱报告与片段顺序，ID 与哈希不变） |
| 引文核验 | `test_quote_verification_rules`；示例中一条编造的引文被丢弃 |
| 数值冲突不依赖模型 | `test_package_content_and_conflicts`（模型把 50–100 m 与 150–300 m 归为一致，代码仍判为冲突） |
| 本地资料不写路径 | `test_private_sources_have_no_local_path` |
| basis 强制 | `test_basis_rules_enforced`：越界降为 inferred、悬空证据删除、无证据 explicit 降为 unknown、未经用户给定的 user_specified 降为 unknown、资料值全部 pending |
| 只重生成出错的段 | `test_repair_regenerates_only_failing_section`（缺一棵树 → 只重生成 control） |
| 修复不收敛时照常写出并标记 | `test_repair_gives_up_and_marks_invalid` |
| 确认写回保留注释、确认后可正式构建 | `test_confirm_then_build_passes`（含 override；随后 `ir build` 状态 passed，L3 4 个想定通过） |
| 手改过的值不被覆盖 | `test_confirm_reject_and_hand_edit` |

测试数据（`tests/falcon_fixtures.py`）：5 段为测试编写的英文资料片段（非真实出版物原文），脚本化模型的分段答案由示例包 `examples/armor_platoon_terrain_formation` 改写而来。

## 4. 待办

1. **P4 在线评测**：按 `docs/runbooks/falcon-online-eval.md` 执行；指标由 `falcon evaluate <pkg> --reference examples/armor_platoon_terrain_formation` 计算并写入 `falcon_eval.json`。
2. 根据评测调整提示词（`falcon/prompts.py`，改动时更新 `PROMPT_VERSION`）；必要时用 `FALCON_CHAT_MODEL` 换更强的模型做对照。
3. 第二期：`falcon revise`（按驳回项、反馈和 L3 结果局部重生成，已确认项锁定）；接入 Deep Agents 的对话式确认。
