#!/bin/bash
# Repair v2, each GPU step on fully idle cards (run_idle.sh):
#   0. prepare the v2 SFT files (CPU)
#   1. base model and the v1 adapter on the v2 test split (comparison)
#   2. QLoRA SFT from the base model on the v2 data (configs/repair_v2.yaml)
#   3. the v2 adapter on the v2 test split
# Run on the login node: nohup bash idle/pipeline_repair_v2.sh > logs/pipeline_repair_v2.log 2>&1 &
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
pred() {  # name out [lora]
  local name=$1 out=$2; shift 2
  [ -s "$out" ] || step "$name" 1 $V/bin/python predict.py --data $R/sft/repair_v2/test.jsonl \
    --model $M "$@" --max-model-len 8192 --max-tokens 1024 --gpu-memory-utilization 0.85 --out "$out"
}
echo "$(date '+%F %T') pipeline v2 start"
[ -s $R/sft/repair_v2/train.jsonl ] || $E/bin/python prepare.py --bundle $R/data/repair_v2 \
  --task repair --model $M --max-length 6144 --out $R/sft/repair_v2 || exit 1
pred repair_v2_base $R/preds/repair_v2test_base.jsonl || exit 2
pred repair_v2_v1 $R/preds/repair_v2test_v1.jsonl --lora $R/runs/repair-qwen3-8b-v1/final || exit 3
[ -f $R/runs/repair-qwen3-8b-v2/final/adapter_config.json ] || step repair_sft_v2 1 \
  $E/bin/python sft.py --config configs/repair_v2.yaml || exit 4
pred repair_v2_v2 $R/preds/repair_v2test_v2.jsonl --lora $R/runs/repair-qwen3-8b-v2/final || exit 5
echo "$(date '+%F %T') pipeline v2 done"
