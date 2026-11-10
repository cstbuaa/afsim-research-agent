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
ARGS=(--data "$DATA" --model "${MODEL:-/data/caoshutong/models/Qwen3-8B}" --max-model-len "${LEN:-8192}" --out "$OUT" --gpu-memory-utilization "${MEMFRAC:-0.85}")
[ -n "${LORA:-}" ] && ARGS+=(--lora "$LORA")
$V/bin/python scripts/predict.py "${ARGS[@]}"
