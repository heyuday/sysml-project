#!/usr/bin/env bash
# The TPC-H heavy-query experiment, in steps that each fit a cloud command time limit.
#
#   bash scripts/cloud_run.sh setup              # Postgres + deps + tests        (~2 min)
#   bash scripts/cloud_run.sh load               # TPC-H at $SF (default 10)      (~10 min)
#   bash scripts/cloud_run.sh sweep <task>       # direct vs aware, $REPS reps    (~10-20 min)
#       tasks (by expected cost at SF 10): q11_german_stock_part (~1 s), q2_min_cost_supplier,
#       q10_returned_customer, q15_top_supplier, q5_nation_revenue, q3_shipping_priority,
#       q9_green_profit (~20 s); q18_large_order_customer exists but is too slow for concurrent runs
#   bash scripts/cloud_run.sh concurrent <task>  # agents overlap: parallel + staggered (3 s gap),
#                                                #   direct vs aware vs aware-live, $REPS reps (~15 min)
#   bash scripts/cloud_run.sh summary            # print every sweep table
#
# Env overrides: SF, REPS, OUT, MODEL, ARMS (comma-separated, replaces the step's default arms).
# Results land in results/tpch-sf$SF/ (load timings, machine info, per-task sweep logs + CSVs).
set -euo pipefail
cd "$(dirname "$0")/.."

SF=${SF:-10}
REPS=${REPS:-3}
OUT=${OUT:-results/tpch-sf${SF%.*}}   # override for a new batch, e.g. OUT=results/tpch-sf10-b2
MODEL=${MODEL:-}                       # e.g. MODEL=claude-opus-5-5 (default: the backend's, Haiku 5.5)
MODEL_ARGS=(); [ -n "$MODEL" ] && MODEL_ARGS=(--model "$MODEL")
mkdir -p "$OUT"

case "${1:-}" in
  setup)
    bash scripts/setup_cloud.sh
    uv run pytest -q
    ;;
  load)
    { echo "date: $(date -u +%FT%TZ)"; echo "cpus: $(nproc)"; free -g | head -2; df -h / | tail -1; } > "$OUT/machine.txt"
    uv run python scripts/load_tpch.py --sf "$SF" 2>&1 | tee "$OUT/load.txt"
    uv run pytest -q tests/test_tpch.py
    ;;
  sweep)
    task=${2:?usage: cloud_run.sh sweep <task>}
    if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${AGENTSQL_ANTHROPIC_API_KEY:-}" ]; then
      echo "no API key: set ANTHROPIC_API_KEY or AGENTSQL_ANTHROPIC_API_KEY (cloud: environment settings)" >&2
      exit 1
    fi
    uv run python sweep.py --dataset tpch --task "$task" --delays 0 --schedules sequential \
      --arms "${ARMS:-direct,aware}" "${MODEL_ARGS[@]}" --reps "$REPS" --out "$OUT/$task" 2>&1 | tee "$OUT/$task.out"
    ;;
  concurrent)
    task=${2:?usage: cloud_run.sh concurrent <task>}
    if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${AGENTSQL_ANTHROPIC_API_KEY:-}" ]; then
      echo "no API key: set ANTHROPIC_API_KEY or AGENTSQL_ANTHROPIC_API_KEY (cloud: environment settings)" >&2
      exit 1
    fi
    uv run python sweep.py --dataset tpch --task "$task" --delays 0 \
      --schedules parallel,staggered --gap 3 --arms "${ARMS:-direct,aware,aware-live}" \
      "${MODEL_ARGS[@]}" --reps "$REPS" --out "$OUT/concurrent-$task" 2>&1 | tee "$OUT/concurrent-$task.out"
    ;;
  summary)
    for d in "$OUT"/*/; do echo "== $d"; uv run python -m agentsql.aggregate "$d"; echo; done
    ;;
  *)
    sed -n '2,17p' "$0"; exit 1
    ;;
esac
