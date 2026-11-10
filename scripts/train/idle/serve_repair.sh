#!/bin/bash
# OpenAI-compatible server for the real-sheet repair test (training/realeval.py on Windows,
# reached through an SSH tunnel): the base model plus the v1 / v2 adapters, on one fully
# idle card, listening on 127.0.0.1 only. Stop: kill the pid in logs/serve_repair.pid.
# Run on the login node: nohup bash idle/serve_repair.sh > logs/serve_repair.log 2>&1 < /dev/null &
set -uo pipefail
R=/data/caoshutong/projects/afsim_train
V=/data/caoshutong/.conda/envs/afsim-vllm
M=/data/caoshutong/models/Qwen3-8B
export HF_HOME=/data/caoshutong/.cache/huggingface HF_HUB_OFFLINE=1
cd $R/scripts
mods=()
for v in v1 v2; do
  [ -f $R/runs/repair-qwen3-8b-$v/final/adapter_config.json ] &&
    mods+=("repair-$v=$R/runs/repair-qwen3-8b-$v/final")
done
exec bash idle/run_idle.sh serve_repair 1 -- $V/bin/vllm serve $M --served-model-name base \
  --max-model-len 16384 --gpu-memory-utilization 0.85 --enable-lora --max-lora-rank 32 \
  --max-loras 2 --lora-modules "${mods[@]}" --host 127.0.0.1 --port ${PORT:-8000}
