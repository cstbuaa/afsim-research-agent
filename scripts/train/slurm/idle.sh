#!/bin/bash
# idle cards per node right now (no compute process, < 500 MiB used): run before sbatch
for n in $(sinfo -h -p gpu -o "%N" | tr ',' ' '); do
  echo "== $n"
  if [ "$n" = "$(hostname)" ]; then
    nvidia-smi --query-gpu=index,memory.used,utilization.gpu --format=csv,noheader
  else
    timeout 20 ssh -o BatchMode=yes "$n" nvidia-smi --query-gpu=index,memory.used,utilization.gpu --format=csv,noheader 2>/dev/null || echo "(not reachable from here)"
  fi
done
squeue -p gpu -o "%.10i %.9u %.8T %.10M %b %N" | head -20
