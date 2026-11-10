#!/bin/bash
# Repair task, first round, each step on fully idle cards (run_idle.sh):
#   1. baseline: Qwen3-8B without LoRA on the repair test split
#   2. QLoRA SFT (configs/repair.yaml)
#   3. the trained adapter on the repair test split
# A step stopped because someone took its card (exit 75) waits for idle cards again.
# Run on the login node: nohup bash idle/pipeline_repair.sh > logs/pipeline_repair.log 2>&1 &
set -uo pipefail
R=/data/caoshutong/projects/afsim_train
E=/data/caoshutong/.conda/envs/afsim-train
V=/data/caoshutong/.conda/envs/afsim-vllm
M=/data/caoshutong/models/Qwen3-8B
export HF_HOME=/data/caoshutong/.cache/huggingface HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
cd $R/scripts
mkdir -p $R/preds
step() {  # name ngpu cmd...
  local name=$1 n=$2; shift 2
  while true; do
    bash idle/run_idle.sh "$name" "$n" -- "$@"; rc=$?
    [ $rc -eq 75 ] && { echo "$(date '+%F %T') $name stopped (card taken), waiting again"; continue; }
    return $rc
  done
}
echo "$(date '+%F %T') pipeline start"
[ -s $R/preds/repair_test_base.jsonl ] || step repair_base 1 \
  $V/bin/python predict.py --data $R/sft/repair/test.jsonl --model $M --max-model-len 4096 \
    --max-tokens 1024 --gpu-memory-utilization 0.85 --out $R/preds/repair_test_base.jsonl || exit 1
[ -f $R/runs/repair-qwen3-8b-v1/final/adapter_config.json ] || step repair_sft 1 \
  $E/bin/python sft.py --config configs/repair.yaml || exit 2
[ -s $R/preds/repair_test_v1.jsonl ] || step repair_v1 1 \
  $V/bin/python predict.py --data $R/sft/repair/test.jsonl --model $M \
    --lora $R/runs/repair-qwen3-8b-v1/final --max-model-len 4096 --max-tokens 1024 \
    --gpu-memory-utilization 0.85 --out $R/preds/repair_test_v1.jsonl || exit 3
echo "$(date '+%F %T') pipeline done"
