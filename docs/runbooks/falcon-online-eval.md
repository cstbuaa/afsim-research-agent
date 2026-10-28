# Falcon 在线评测操作流程（P4）

> 对应设计：`docs/design/tactical-falcon-v1.md` §8。在 Mac 上执行，全程约 30–60 分钟（Crow 检索占大部分）。
> 评测目的：检查真实模型能否稳定产出可通过校验的行为模型草稿，并记录指标供调整提示词。

## 0. 准备（一次性）

```bash
conda activate afsim_agent
cd ~/Desktop/deepagents-main/afsim-research-agent

# 确认新模块可导入；失败时重装为可编辑安装
python -c "import afsim_research_agent.falcon, afsim_research_agent.ir; print('ok')" \
  || pip install -e . --no-deps

# 离线测试先过一遍（约 1 分钟，应全部通过）
pytest -q tests/unit/falcon tests/unit/ir
```

`.env` 中至少需要：`DASHSCOPE_API_KEY`、`DASHSCOPE_BASE_URL`、`DASHSCOPE_CHAT_MODEL`（Crow 和 Falcon 共用），以及 Crow 检索所需的 `TAVILY_API_KEY` 等。Falcon 可单独换模型：`FALCON_CHAT_MODEL`；单次调用超时 `FALCON_TIMEOUT_S`（默认 180 秒）。

为本次评测定一个运行目录名，后面的命令都用它：

```bash
RUN=data/runs/crow-research/armor-eval-01
```

## 1. 运行 Crow，取得证据包

```bash
python -m afsim_research_agent.crow.cli \
  --requirement "研究装甲排在开阔地和城区的编队方式：开阔地用楔形，接近城区时转纵队，纵队车距 50 米；接敌后报告并交替跃进，与敌保持安全距离。" \
  --output-dir $RUN \
  --max-web-results 3 --max-sources 12 --gap-rounds 1
# 有本地资料库时追加：--library data/library
```

结束时终端会打印 `Falcon 输入门槛：通过 / 未通过（…）`，同时生成 `$RUN/research_report.md` 和 `$RUN/research_result.json`。

**门槛未通过时**（Falcon 会拒绝运行，不提供强制开关），按顺序尝试：

1. 加大检索：`--gap-rounds 2 --max-sources 20`，换一个新的 `--output-dir` 重跑；
2. 把手头的条令、教材 PDF 放进 `data/library/` 并加 `--library data/library`；
3. 查看 `research_report.md` 开头的门槛明细和"人工获取清单"，手动下载 A/B 级资料后放入本地库再跑。

## 2. 写请求文件

```bash
cp examples/falcon_request.example.yaml $RUN/falcon_request.yaml
```

编辑 `$RUN/falcon_request.yaml`，只需改一行：

```yaml
crow_result: research_result.json      # 相对请求文件所在目录
```

其余保持示例即可：需求文本与第 1 步一致；`model_id: armor_platoon_v2`；`user_parameters` 留空（"纵队车距 50 米"已写在需求里，Falcon 会把它识别为用户给定值）。

## 3. 生成草稿

```bash
python -m afsim_research_agent.falcon draft $RUN/falcon_request.yaml --with-l3
echo "exit=$?"
```

- 模型包写到 `$RUN/models/armor_platoon_v2/`；目录已存在时会拒绝，换 `model_id` 或加 `--out $RUN/models/<新名字>`。
- `--with-l3` 会用未确认的数值试跑 L3 想定，结果只作参考。

| 退出码 | 含义 | 处理 |
| --- | --- | --- |
| 0 | 草稿通过 L1/L2 | 进入第 4 步 |
| 4 | 草稿已写出，但 3 轮修复后仍有错误 | 照常进入第 4 步（这本身就是评测结果） |
| 2 | Crow 门槛未通过 | 回到第 1 步 |
| 3 | 请求文件或 API key 问题 | 按提示修改 |
| 1 | 模型服务或网络异常 | 查看 `$RUN/models/armor_platoon_v2.failure.json`（只有调用阶段和耗时，不含模型输出），把它发给我 |

## 4. 记录草稿指标

```bash
export PKG=$RUN/models/armor_platoon_v2
python -m afsim_research_agent.falcon evaluate $PKG --reference examples/armor_platoon_terrain_formation
```

终端会打印指标，并写入 `$PKG/falcon_eval.json`。对照验收标准：

| 指标 | 期望 |
| --- | --- |
| ≤3 轮内通过 L1+L2 | 是 |
| 条款覆盖率 | 100%（超出范围的条款须有原因） |
| user_specified 被降级 | 0 次（大于 0 说明模型把无依据的数值写成了"用户给定"，代码已拦下，但需要改提示词） |
| 有证据的 basis 比例 | 记录即可（越高说明证据利用越好） |
| 主张丢弃率 | 记录即可（偏高时说明模型引文不逐字，或片段被截断） |
| 与参考包对比 | 记录角色、状态、协同模式、参数的异同，作为人工评估材料 |

然后阅读 `$PKG/falcon_review.md`：条款覆盖表、审查清单、冲突命题、代码强制调整。

## 5. 确认并正式构建

**正式评审**：逐项编辑 `$PKG/confirmations.yaml`，把 `decision` 改为 `confirm`、`reject` 或 `override`（仅参数，同时填写 `override: {value: …, unit: …}`）。

**只想先检查流程能否走通**（数值未经审核，结果不能当作战术结论）：

```bash
python - <<'PY'
import os, yaml
p = os.path.join(os.environ.get("PKG", ""), "confirmations.yaml")
d = yaml.safe_load(open(p, encoding="utf-8"))
for item in d["items"]:
    item["decision"] = "confirm"
open(p, "w", encoding="utf-8").write(yaml.safe_dump(d, allow_unicode=True, sort_keys=False))
print(len(d["items"]), "items set to confirm")
PY
```

（脚本使用第 4 步 `export` 的 `PKG` 变量。）

接着：

```bash
python -m afsim_research_agent.falcon confirm $PKG --by <你的姓名或标识>
python -m afsim_research_agent.falcon status  $PKG        # 应显示"可以正式构建"
python -m afsim_research_agent.ir build $PKG
python -m afsim_research_agent.falcon evaluate $PKG --reference examples/armor_platoon_terrain_formation
```

第二次 `evaluate` 会追加正式构建状态和每个 L3 想定的结果。`ir build` 生成的 `review.md` 里有 L3 的逐项测量值。

## 6. （可选）换模型对照

```bash
FALCON_CHAT_MODEL=<更强的模型名> python -m afsim_research_agent.falcon draft $RUN/falcon_request.yaml \
  --out $RUN/models/armor_platoon_v2_alt --with-l3
python -m afsim_research_agent.falcon evaluate $RUN/models/armor_platoon_v2_alt \
  --reference examples/armor_platoon_terrain_formation
```

模型名填你的百炼账号中可用的名称。同一份 Crow 结果可以反复生成，不需要重跑第 1 步。

## 7. 发回给我的文件

模型包目录中的：

- `falcon_eval.json`、`falcon_review.md`、`falcon_trace.json`
- `validation_report.json`、`behavior_spec.yaml`、`confirmations.yaml`
- 若第 5 步做了正式构建：`review.md`

再加上 `$RUN/research_report.md`（只需开头的门槛与覆盖部分）。

注意：

- `evidence_package.json` 含资料原文片段。用了本地资料库（`--library`）时，其中有你的本地资料摘录，请确认可以外发后再发；不确定就不发，上面的文件已足够分析。
- 不要发送 `.env`。
- 如果第 3 步退出码为 1，只发 `*.failure.json`。

## 8. 常见问题

| 现象 | 原因与处理 |
| --- | --- |
| `FAL004 stage '…' returned unusable output` | 模型连续两次输出不符合结构；记录是哪一段，换模型对照（第 6 步）或把 `falcon_trace.json` 发我调整提示词 |
| 调用超时 | `export FALCON_TIMEOUT_S=300` 后重跑 |
| 证据阶段很慢 | 片段较多时属正常；可用 `FALCON_EXTRACT_BATCH`（默认 6）调整每批片段数，`FALCON_MIN_SCORE`（默认 5）提高可过滤低相关片段 |
| `FAL002 output directory already exists` | Falcon 不覆盖已有模型包；换 `model_id` 或用 `--out` |
| `confirm` 提示"参数值已被手工修改" | 你在生成清单后改过该值；重新填写该项或直接在 `behavior_spec.yaml` 中修改 basis |
