#!/bin/bash
#SBATCH --job-name=afsim-sft
#SBATCH --partition=gpu
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --gres=gpu:rtx4090:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --output=/data/caoshutong/projects/afsim_train/logs/%x-%j.out
#SBATCH --error=/data/caoshutong/projects/afsim_train/logs/%x-%j.err
# usage: sbatch --export=ALL,CONFIG=configs/repair.yaml,EXTRA="--set epochs=1" slurm/sft.slurm
# two cards: sbatch --gres=gpu:rtx4090:2 --export=ALL,CONFIG=...,NPROC=2,EXTRA="--set grad_accum=16" slurm/sft.slurm
set -euo pipefail
R=/data/caoshutong/projects/afsim_train
E=/data/caoshutong/.conda/envs/afsim-train
export HF_HOME=/data/caoshutong/.cache/huggingface HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
cd $R/scripts
echo "job $SLURM_JOB_ID on $(hostname), GPUs $CUDA_VISIBLE_DEVICES"
nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv
# only fully idle cards: no compute process and almost no memory in use on any card this
# job was given; otherwise stop at once without touching the card (resubmit later)
busy=$(nvidia-smi --query-gpu=index,memory.used --format=csv,noheader,nounits | awk -F', ' '$2 > 500 {print $1}')
apps=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader | wc -l)
if [ -n "$busy" ] || [ "$apps" -gt 0 ]; then
  echo "GPU in use by others (cards: ${busy:-none}, processes: $apps): not starting"; exit 3
fi
if [ "${NPROC:-1}" -gt 1 ]; then
  $E/bin/accelerate launch --num_processes "$NPROC" sft.py --config "$CONFIG" ${EXTRA:-}
else
  $E/bin/python sft.py --config "$CONFIG" ${EXTRA:-}
fi
