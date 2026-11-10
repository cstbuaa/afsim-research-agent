# 本地小模型微调（试点数据包 pilot_fp_v1）

上级文档：`design/small-model-training-v1.md`（训练方案）。本目录在 **Linux 训练服务器**（2×RTX 4090）上运行；判分在 **Windows 电脑**上运行，因为检查器需要能力集，而能力集不拷到服务器。

```
服务器：prepare.py → sft.py → predict.py ──预测 jsonl──▶ Windows：python -m afsim_research_agent.training.score
```

## 1. 环境

训练和推理分成两个虚拟环境：vLLM 会锁定自己的 torch 版本。

```bash
# 训练
python -m venv ~/venv-train && source ~/venv-train/bin/activate
pip install torch --index-url https://download.pytorch.org/whl/cu124   # 按服务器的 CUDA 版本选
pip install "transformers>=4.51" "trl>=0.20" "peft>=0.15" "datasets>=3.0" "accelerate>=1.6" \
    "bitsandbytes>=0.45" "liger-kernel>=0.5" tensorboard pyyaml
# 推理
python -m venv ~/venv-vllm && source ~/venv-vllm/bin/activate
pip install "vllm>=0.9"
```

模型权重：`Qwen/Qwen3-8B`（Hugging Face）。服务器访问不了 Hugging Face 时，用 ModelScope 下载到本地目录，再把配置里的 `model` 改成该目录。

## 2. 数据

```bash
mkdir -p ~/afsim-train/data && cd ~/afsim-train/data
unzip pilot_fp_v1.zip && cd pilot_fp_v1 && bash verify.sh      # 每个文件都应显示 OK
```

## 3. 转成训练文件（每个任务一次）

```bash
source ~/venv-train/bin/activate
cd <仓库>/scripts/train
B=~/afsim-train/data/pilot_fp_v1
python prepare.py --bundle $B --task repair       --max-length 4096  --out ~/afsim-train/sft/repair
python prepare.py --bundle $B --task structure    --max-length 6144  --out ~/afsim-train/sft/structure
python prepare.py --bundle $B --task control      --max-length 14336 --out ~/afsim-train/sft/control
python prepare.py --bundle $B --task verification --max-length 6144  --out ~/afsim-train/sft/verification
```

- 用 Qwen3 的对话格式、关闭思考（`enable_thinking=False`），与 vLLM 给 Falcon 提供服务时一致。
- 输入超过 `--max-length` 的样本直接丢掉，不截断提示，数量记在 `stats.json` 的 `too_long`。先看一眼 `stats.json`：丢弃太多就调大长度，或者换更大显存的方案。
- 列表风格的需求文字和事实原文太像，默认训练集只保留一半（`--list-keep 0.5`）。

## 4. 基线（训练前，用基座模型）

```bash
source ~/venv-vllm/bin/activate
CUDA_VISIBLE_DEVICES=1 python predict.py --data ~/afsim-train/sft/repair/test.jsonl \
    --max-model-len 8192 --out ~/afsim-train/preds/repair_test_base.jsonl
```

## 5. 训练

```bash
source ~/venv-train/bin/activate
CUDA_VISIBLE_DEVICES=0 python sft.py --config configs/repair.yaml
# 两张卡数据并行（把 grad_accum 减半，保持有效批量 32）：
accelerate launch --num_processes 2 sft.py --config configs/repair.yaml --set grad_accum=16
tensorboard --logdir ~/afsim-train/runs     # 看 loss 和 eval_loss
```

| 配置 | 任务 | 最长 token | 轮数 | 说明 |
| --- | --- | --- | --- | --- |
| `configs/repair.yaml` | 修复（T1） | 8192 | 2 | 先做这个：数据最多，效果最好判断 |
| `configs/structure.yaml` | 结构稿 | 12288 | 3 | |
| `configs/control.yaml` | 控制稿（T2） | 24576 | 3 | 提示中位约 3.2 万字符；单卡 24 GB 依赖 4-bit 基座和 Liger 融合损失 |
| `configs/verification.yaml` | 验证块（T3） | 12288 | 3 | |

共同设置：Qwen3-8B 4-bit 基座（QLoRA），LoRA r=32、α=64、全部线性层，学习率 1e-4（余弦），有效批量 32，bf16，梯度检查点，只在答案部分计算损失。保存 eval_loss 最好的检查点到 `final/`。任意参数可以用 `--set 键=值` 覆盖。

显存不够时依次尝试：`--set max_length=…` 调小（同时用 prepare.py 重新生成）、`--set lora_r=16`、换 `Qwen/Qwen3-4B`。

## 6. 预测与判分

```bash
source ~/venv-vllm/bin/activate
CUDA_VISIBLE_DEVICES=1 python predict.py --data ~/afsim-train/sft/repair/test.jsonl \
    --lora ~/afsim-train/runs/repair-qwen3-8b-v1/final --max-model-len 8192 \
    --out ~/afsim-train/preds/repair_test_v1.jsonl
```

把 `preds/*.jsonl` 拷回 Windows 电脑，在仓库根目录：

```powershell
python -m afsim_research_agent.training.score --bundle <解压后的 pilot_fp_v1> `
    --pred repair_test_v1.jsonl --task repair --out repair_test_v1_score.json
```

| 任务 | 判分方式 | 主要指标 |
| --- | --- | --- |
| repair | 按样本 id 重建注入后的稿件（第二步先放入标准诊断），把模型的动作交给修复循环自己的工具执行 | 动作对不对（diagnose / edit）、被循环接受、稿件全部检查通过（solved）；按错误类型和阶段分别统计 |
| structure / control | 预测的块替换标准稿件里的对应块，其余块和标准测试、想定保持不变 | 块齐全、L1 通过、全部检查通过 |
| verification | 预测的验证块放到标准稿件上运行 | 块能解析、正确稿件全部通过 |

修复样本的标准答案是“改回原文”，不是唯一正确的修法，所以只用检查器判定，不逐字比对。

## 7. 部署给 Falcon（训练效果确认之后）

```bash
source ~/venv-vllm/bin/activate
CUDA_VISIBLE_DEVICES=1 vllm serve Qwen/Qwen3-8B --max-model-len 24576 \
    --enable-lora --max-lora-rank 64 \
    --lora-modules local-repair-v1=~/afsim-train/runs/repair-qwen3-8b-v1/final \
    --host 127.0.0.1 --port 8000
```

- 只监听本机，Windows 电脑通过 SSH 隧道访问：`ssh -L 8000:127.0.0.1:8000 <服务器>`。
- 请求时要带 `chat_template_kwargs: {"enable_thinking": false}`，与训练时的格式一致。
- Falcon 按环节切换模型的配置（训练方案 §7 的 `FALCON_TASK_MODELS`）还没实现，接入时再做。

## 8. 回传

每次训练把这些发给 Claude（不含数据和权重）：
- `runs/<名称>/run_config.json`
- `final/eval.json`
- `prepare.py` 输出的 `stats.json`
- Windows 上判分的 `*_score.json` 里的 `report` 部分
