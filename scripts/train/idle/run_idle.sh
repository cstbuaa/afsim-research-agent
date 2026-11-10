#!/bin/bash
# Run one command on fully idle cards of this node (server usage guide §6.1):
#   - a card is idle when it holds < 500 MiB and no compute process, and no Slurm job on
#     this node has it allocated;
#   - wait (polling every POLL seconds) until NGPU such cards exist, re-check right before
#     the start, then run with CUDA_VISIBLE_DEVICES set to them;
#   - while it runs, a compute process that is not ours on one of those cards stops our
#     command (SIGTERM, then SIGKILL): we never share a card.
# usage: run_idle.sh NAME NGPU -- command args...
# exit: the command's exit code; 75 when stopped because someone else took the card.
set -uo pipefail
NAME=$1; NGPU=$2; shift 3
POLL=${POLL:-300}
LOG=/data/caoshutong/projects/afsim_train/logs
mkdir -p $LOG
say() { echo "[$(date '+%F %T')] $NAME: $*" | tee -a $LOG/idle.log; }

slurm_busy() {  # card indexes allocated to Slurm jobs on this node
  for j in $(squeue -h -w "$(hostname)" -t R -o %i 2>/dev/null); do
    scontrol show job -d "$j" 2>/dev/null | grep -o 'IDX:[0-9,-]*' | cut -d: -f2 | tr ',' '\n' |
      awk -F- '{if (NF==2) for (i=$1;i<=$2;i++) print i; else print $1}'
  done
}

idle_cards() {
  local used apps
  apps=$(nvidia-smi --query-compute-apps=gpu_uuid --format=csv,noheader | sort -u)
  used=$(slurm_busy | sort -u)
  nvidia-smi --query-gpu=index,uuid,memory.used --format=csv,noheader,nounits |
    while IFS=', ' read -r idx uuid mem; do
      [ "$mem" -lt 500 ] || continue
      grep -q "$uuid" <<<"$apps" && continue
      grep -qx "$idx" <<<"$used" && continue
      echo "$idx"
    done
}

ours() {  # all pids of our process tree
  local p=$1
  echo "$p"
  for c in $(pgrep -P "$p"); do ours "$c"; done
}

while true; do
  mapfile -t free < <(idle_cards)
  if [ "${#free[@]}" -ge "$NGPU" ]; then
    pick=$(printf '%s\n' "${free[@]:0:$NGPU}" | paste -sd,)
    sleep 20  # a card that just looked idle must still be idle 20 s later
    mapfile -t again < <(idle_cards)
    ok=1
    for c in ${pick//,/ }; do printf '%s\n' "${again[@]}" | grep -qx "$c" || ok=0; done
    [ $ok -eq 1 ] && break
  fi
  say "waiting: idle cards [${free[*]:-none}], need $NGPU"
  sleep "$POLL"
done

uuids=$(nvidia-smi --query-gpu=index,uuid --format=csv,noheader | awk -F', ' -v p=",$pick," 'index(p, ","$1",") {print $2}')
say "start on cards $pick: $*"
CUDA_VISIBLE_DEVICES=$pick "$@" &
pid=$!
echo "$pid $pick" > "$LOG/$NAME.pid"
while kill -0 "$pid" 2>/dev/null; do
  sleep 30
  mine=$(ours "$pid" | sort -u)
  other=$(nvidia-smi --query-compute-apps=gpu_uuid,pid --format=csv,noheader |
    while IFS=', ' read -r u p; do
      grep -q "$u" <<<"$uuids" || continue
      grep -qx "$p" <<<"$mine" || echo "$p"
    done)
  if [ -n "$other" ]; then
    say "another process ($other) on cards $pick: stopping ours"
    kill -TERM -- $(ours "$pid") 2>/dev/null; sleep 30; kill -KILL -- $(ours "$pid") 2>/dev/null
    rm -f "$LOG/$NAME.pid"
    exit 75
  fi
done
wait "$pid"; rc=$?
say "finished, exit $rc"
rm -f "$LOG/$NAME.pid"
exit $rc
