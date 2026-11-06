#!/usr/bin/env bash
# The TPC-H heavy-query experiment, in steps that each fit a cloud command time limit.
#
#   bash scripts/cloud_run.sh setup              # Postgres + deps + tests        (~2 min)
#   bash scripts/cloud_run.sh load               # TPC-H at $SF (default 10)      (~10 min)
#   bash scripts/cloud_run.sh sweep <task>       # direct vs aware, $REPS reps    (~10-20 min)
#       tasks: q5_nation_revenue, q10_returned_customer
#   bash scripts/cloud_run.sh summary            # print both tables
#
# Results land in results/tpch-sf$SF/ (load timings, machine info, per-task sweep logs + CSVs).
set -euo pipefail
cd "$(dirname "$0")/.."

SF=${SF:-10}
REPS=${REPS:-3}
OUT=results/tpch-sf${SF%.*}
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
      --reps "$REPS" --out "$OUT/$task" 2>&1 | tee "$OUT/$task.out"
    ;;
  summary)
    for d in "$OUT"/*/; do echo "== $d"; uv run python -m agentsql.aggregate "$d"; echo; done
    ;;
  *)
    sed -n '2,12p' "$0"; exit 1
    ;;
esac
