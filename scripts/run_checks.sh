#!/usr/bin/env bash
# Run the full check sequence and keep logs under data/eval/logs/.
#   bash scripts/run_checks.sh            # unit tests, ruff, live smoke, baseline evaluation
#   bash scripts/run_checks.sh quick      # unit tests and ruff only
#   bash scripts/run_checks.sh eval armor-terrain-formations,behavior-trees
#   bash scripts/run_checks.sh ir           # behavior IR: build the example package (L1/L2/L3)
set -u
cd "$(dirname "$0")/.."
unset DASHSCOPE_API_KEY OPENALEX_API_KEY SEMANTIC_SCHOLAR_API_KEY CORE_API_KEY TAVILY_API_KEY
export LITELLM_LOCAL_MODEL_COST_MAP=True
logs=data/eval/logs
mkdir -p "$logs"
mode="${1:-all}"

if [[ "$mode" == "all" || "$mode" == "quick" ]]; then
  echo "== 1/4 unit tests"; pytest -q 2>&1 | tee "$logs/1-pytest.log"
  echo "== 2/4 ruff"; ruff check src tests 2>&1 | tee "$logs/2-ruff.log"
fi
if [[ "$mode" == "all" || "$mode" == "quick" || "$mode" == "ir" ]]; then
  echo "== IR example build"
  python -m afsim_research_agent.ir build examples/armor_platoon_terrain_formation 2>&1 \
    | tee "$logs/ir-build.log"
  echo "== IR co-simulation (reference engine)"
  python -m afsim_research_agent.ir cosim examples/armor_platoon_terrain_formation \
    --engine reference 2>&1 | tee "$logs/ir-cosim.log"
fi
if [[ "$mode" == "all" ]]; then
  echo "== 3/4 live smoke tests"; pytest -m live -s tests/live 2>&1 | tee "$logs/3-live.log"
fi
if [[ "$mode" == "all" || "$mode" == "eval" ]]; then
  cases=()
  [[ -n "${2:-}" ]] && cases=(--cases "$2")
  echo "== 4/4 retrieval evaluation"
  python -m afsim_research_agent.evaluation.retrieval run "${cases[@]}" 2>&1 \
    | tee "$logs/4-eval.log"
fi
echo "Logs: $logs"
