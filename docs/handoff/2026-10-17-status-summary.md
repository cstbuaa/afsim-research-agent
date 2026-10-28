# 项目现状总结（2026-10-17）

上一版：`handoff/2026-10-16-status-summary.md`。本版补充 10-16 至 10-17 的进展：

- 平台能力接入流程 K2–K5；
- 能力颗粒度分层；
- Crow C5 红队评审与用户选择；
- 参考值放行与标注。

## 1. 总体链路

```text
自然语言需求
  → Tactical Crow
      C1 任务画像                                              [完成]
      C2 研究规划（六维度研究问题、模式假设）                   [完成]
      C3 检索与证据包                                          [完成]
      C4 战术提出 + 平台能力标注（C4b，粗颗粒度）               [完成]
      C5 红队评审 → 生成方回应/修订 → 约束复核 → 评审报告 → 用户选择 [完成，已在线运行 3 轮]
      C6 交给 Falcon（读取 tactic_selection.yaml）              [完成（P4，10-17）]
  → Tactical Falcon：证据包 + 战术 → behavior_spec.yaml 草稿 → 审查清单 → 文件式确认
                                     [v1 完成；能力词汇分层约束已接入；v2 生成层设计待确认]
  → 行为模型 IR：L1 → 确认闸门（参考值放行）→ 规范化 → L2 → 后端 → L3（含忠实模式）
                                                              [v1 完成]
  → 平台能力核对：IR/战术 ↔ AFSIM 2.9 目录 → 缺口登记 → 确认/开发 → 回写 → 复核
                                                              [K1–K5 完成]
  → 后端：Mermaid / BT.CPP XML / 二维仿真 [完成]；AFSIM 后端 [未开始，已有能力闸门]
  → 外部引擎：tbe-step/1 协议 + C++ 桥接骨架 [框架完成，待接入真实引擎]
```

## 2. 本阶段完成的内容

| 项 | 内容 |
| --- | --- |
| K2 Crow 能力标注 | 模型提出映射，复核轮只能删除映射或补充缺失项（`missing`）；每条需求带 `why`、`missing`、`reference`；多来源路径逐条校验 |
| K3 IR 能力核对 | `capability_gaps.yaml`、审查清单 §8、BT XML 中标注待补能力；AFSIM 闸门 `CAP000/001/002` |
| K4 Falcon 词汇约束 | 提示词按能力分层：可用 / 需确认【需确认】/ 不可用 / 隐藏 |
| 颗粒度分层 | `groups.toml` 定义 10 个能力组。地面车辆的核心组是机动、编队、地形，按细粒度判断；其余组记为"简化"，不算缺口。`--refine <组>` 按需细化 |
| K5 缺口闭环 | 缺口登记表 `data/capability/gap_registry.yaml`，状态可确认、开始、实现、否决、重开、合并；结果回写 `capability_overrides.yaml`，再用 `recheck` 离线复核；忠实 L3（`--faithful-sim`）屏蔽未补齐能力后运行想定 |
| C5 红队评审 | 红方用独立模型（deepseek-v4-pro），只看战术要素，评估整体合理性。生成方回应后按修订操作改稿，并保留各版本快照。修订后由红方复核约束。去除自我评价语句；修订中新出现的数字记为参考值假设。产出 `tactic_report.md`，用户用 `crow.select` 写 `tactic_selection.yaml`，所选战术的缺口进入开发队列 |
| 参考值放行 | 未确认的数值参数按参考值通过检验（提示 `PAR002`）；被否决的参数仍拦截（`PAR001`），`--strict-parameters` 恢复严格检查。审查清单 §1b 列出参考值，⚠ 无依据的排前；`validation_report.json` 新增 `reference_values`；`falcon status` 不再因参考值阻塞 |

已确认的设计原则：

- 初次生成用粗颗粒度，用户按需逐步细化。
- 非核心能力缺失时做默认或简化处理，不算缺口。
- 无法确认的数值先给合理参考值，不当作未完成能力。
- 红方关注战术整体合理性，不纠缠细节。
- 参考值可以通过检验，但必须标出哪些没有依据，供人工复核调参。

在线运行记录：

- K2：`tactics_v2_cap` ～ `cap3`；
- 红队：`tactics_v2_rt`、`rt2`、`rt3`。最后一轮红方评价为 低 / 中 / 低，每个候选回应 9–10 条。

## 3. 工程目录

```text
afsim-research-agent/          Mac：~/Desktop/deepagents-main/afsim-research-agent（约 3.0 万行 Python）
├── src/afsim_research_agent/
│   ├── crow/        (6.5k) mission planner research readiness dimensions        C1–C3
│   │                proposal proposal_models proposal_report propose            C4
│   │                capability_annotation annotate                             C4b 能力标注
│   │                redteam selection_report select                            C5
│   │                catalog/(tactic_patterns.toml innovation.toml) cli report manifest
│   ├── capability/  (2.6k) catalog assess granularity(+groups.toml) aliases.toml
│   │                ir_elements ir_check constraints registry __main__(CLI)
│   ├── falcon/      (4.6k) gate evidence requirements skeleton prompts fill assemble enforce
│   │                autofix repair review confirm evaluate pipeline llm yamlpatch
│   ├── ir/          (6.7k) models loader validate normalize references coordination runtime
│   │                l2 binding conformance review pipeline units diagnostics
│   │                catalog/ schema/ backends/(mermaid btcpp python_sim) sim/(world l3) cosim/
│   └── literature/ web/ evidence/ domain/ evaluation/ knowledge/   检索、采集、证据、术语、评测
├── datasets/platform_capabilities/afsim-2.9/
│   ├── capability_catalog.yaml   0.2.0 单文件（未重整领域）
│   └── catalog/  index mobility communication weapon_employment environment execution .yaml
│                 （本地另有 _local/：原生名称、绑定、证据，不进入公网和共享文档）
├── data/  capability/gap_registry.yaml  runs/  library/
├── examples/armor_platoon_terrain_formation/   示例模型包（含 capability_gaps.yaml）
├── integrations/ lab_engine/  engine_bridge_cpp/
├── docs/ design/ handoff/ analysis/ runbooks/
└── tests/unit/{crow,falcon,ir,capability,...}  38 个测试文件
```

常用命令：

```text
python -m afsim_research_agent.crow.cli ...                          C1–C3
python -m afsim_research_agent.crow.propose <run_dir> [--granularity --refine --no-redteam]
python -m afsim_research_agent.crow.redteam <tactics_dir>            单独重跑 C5
python -m afsim_research_agent.crow.select <tactics_dir> --candidate tc_01 --by 姓名
python -m afsim_research_agent.capability {ingest|select|queue|confirm|start|implement|reject|reopen|merge|recheck}
python -m afsim_research_agent.falcon {draft|confirm|status|evaluate}
python -m afsim_research_agent.ir build <包> [--faithful-sim --strict-parameters --refine 组 --crow-gaps ...]
```

测试：Mac 上 `pytest tests/unit/ir tests/unit/falcon` 145 通过，1 跳过。云端全量 447 通过；另有 11 项文献/网页测试仅因桩模块失败。

## 4. 后续待开发

按建议顺序：

1. ~~**C6 / Falcon P4：读取 `tactic_selection.yaml`。**~~ 已完成（tactical-falcon-v1 §13）：
   - 选定步骤、队形、例外转为战术条款，复用 Crow 证据包；
   - 携带 Crow 缺口（`crow_capability_gaps.yaml`，`ir build` 自动对照）与参考值假设；
   - 用户备注并入需求。
2. **重跑红队并选择战术**（`tactics_v2_rt4` → `crow.select`），用真实选择驱动第 1 项。
3. **C5 代码一致性检查。**
   - 修订后的步骤与例外是否引用存在的阶段和角色；
   - 用户约束是否仍满足（目前只靠红方模型复核）。
4. **反制战术 → L3 想定。**
   - 把红方的对手反制转成二维仿真想定；
   - 用仿真检验生成方的回应是否有效。
5. **参考值调参。**
   - `ir sweep`：在参数范围内扫描 L3 通过率，给出建议取值；
   - 结果回写参数的依据字段。
6. **IR v1.1。**
   - 信息来源建模：领车位置经消息获得，敌方需经识别；
   - 忠实模式覆盖领车位置与函数；
   - 区域/地形判断、恢复路线、运动模式；
   - 通信设备开关、停火、交战结果。
7. **能力目录。**
   - 用户确认关键接口：发送消息、区域判断、弹药/就绪读取；
   - 重整探测类、跟踪类到 0.3.0；
   - 其余领域完成后停用 0.2.0 单文件。
8. **Falcon v2 生成层**（`design/falcon-surface-formats-v2.md` 待确认）：战术稿编译器、错误回指、局部修复；另有 `falcon revise`。
9. **AFSIM 后端（授权环境）。**
   - 按目录契约生成原生调用；
   - 逐项运行验证 user_attested 接口；
   - 能力插件开发 Agent 读取缺口登记表。
10. **数据与编排。**
    - 约 40 个 demo 转为 IR 样本；
    - 用 Deep Agents 把 Crow、Falcon、IR、能力核对串成对话式工作流。

## 5. 待优化问题

### 5.1 质量

- **红队区分度有限。** 三轮多为"低/中"，反制与失效条件偏通用。需要更多需求样例评估，并考虑让反制进入 L3 验证（第 4 项）。
- **红方与生成方同一服务商。** 已换模型族，但未换提供方；独立性只到模型层。
- **能力标注依赖模型。** 映射准确率只在装甲排一个需求上做过人工检查，缺少标注金标集。
- **Falcon v1 在线错误率高。** 主因是模型不熟悉 IR 写法，有待 v2。
- **参考值没有校准。** 当前多为"⚠ 无依据"，只靠人工复核，需要第 5 项的扫描支撑。

### 5.2 设计

- **二维仿真仍泄露真值。** 跟随者读取领车真实位置，探测不分敌我；忠实模式目前只覆盖能力缺口，不覆盖信息来源。
- **用户确认不等于运行验证。** 目录中 available 的依据多为 user_attested。
- **颗粒度分组只定义了地面车辆的核心组。** 其他平台类别（空中、水面）需要各自的核心组。

### 5.3 工程

- **Mac 端无版本控制。** 同步靠逐文件比对，建议初始化 git，`_local/` 与 `.env` 列入忽略。
- **云端测试依赖桩模块。** 完整测试需在 Mac 上执行。
- **格式与代码清理。** 能力目录 0.2.0 与 0.3.0 并存；`crow/dimensions.py` 未经 ruff 格式化；`redteam.py`、`selection_report.py` 用文件级 E501 豁免。

### 5.4 资料边界（不变）

- 受控资料、原生接口名、绑定只存本地 `_local/`；
- 公网请求不含本地资料、想定数据、平台参数；
- 密钥与 `.env` 不进入代码、文档、对话；
- 生成物不覆盖用户已有 AFSIM 文件。
