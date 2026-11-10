#!/bin/bash
#SBATCH --job-name=afsim-predict
#SBATCH --partition=gpu
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --gres=gpu:rtx4090:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=48G
#SBATCH --output=/data/caoshutong/projects/afsim_train/logs/%x-%j.out
#SBATCH --error=/data/caoshutong/projects/afsim_train/logs/%x-%j.err
# usage: sbatch --export=ALL,DATA=sft/repair/test.jsonl,OUT=preds/repair_test_base.jsonl,LEN=8192[,LORA=runs/.../final] slurm/predict.slurm
set -euo pipefail
R=/data/caoshutong/projects/afsim_train
V=/data/caoshutong/.conda/envs/afsim-vllm
export HF_HOME=/data/caoshutong/.cache/huggingface HF_HUB_OFFLINE=1
cd $R
echo "job $SLURM_JOB_ID on $(hostname), GPUs $CUDA_VISIBLE_DEVICES"
nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv
# only fully idle cards: no compute process and almost no memory in use on any card this
# job was given; otherwise stop at once without touching the card (resubmit later)
busy=$(nvidia-smi --query-gpu=index,memory.used --format=csv,noheader,nounits | awk -F', ' '$2 > 500 {print $1}')
apps=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader | wc -l)
if [ -n "$busy" ] || [ "$apps" -gt 0 ]; then
  echo "GPU in use by others (cards: ${busy:-none}, processes: $apps): not starting"; exit 3
fi
ARGS=(--data "$DATA" --model "${MODEL:-/data/caoshutong/models/Qwen3-8B}" --max-model-len "${LEN:-8192}" --out "$OUT" --gpu-memory-utilization "${MEMFRAC:-0.85}")
[ -n "${LORA:-}" ] && ARGS+=(--lora "$LORA")
$V/bin/python scripts/predict.py "${ARGS[@]}"
