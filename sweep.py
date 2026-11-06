"""Run a matrix of agent experiments and aggregate them.

    uv run python sweep.py --reps 3                       # default matrix (see below)
    uv run python sweep.py --backend mock --reps 1        # dry run, no API key
    uv run python -m agentsql.aggregate logs/sweep-<ts>   # re-print a finished sweep

Arms:
  direct - no cache (baseline)
  exact  - shared exact-text cache, agents unaware of it
  aware  - shared exact-text cache + cached_queries tool and reuse hint

Order: for each rep, for each (delay, schedule), run every arm back to back.
Interleaving keeps API-latency drift from favouring one arm.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime

import run_agents
from agentsql.aggregate import aggregate, report

ARMS = {
    "direct": ["--cache", "direct"],
    "exact": ["--cache", "exact"],
    "aware": ["--cache", "exact", "--aware"],
}


def main(argv=None) -> str:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", default="paraphrase")
    ap.add_argument("--agents", type=int, default=4)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--delays", default="0,2000", help="comma-separated --db-delay-ms values")
    ap.add_argument("--schedules", default="sequential,staggered")
    ap.add_argument("--gap", type=float, default=5.0, help="seconds between starts when staggered")
    ap.add_argument("--arms", default="direct,aware", help=f"any of {list(ARMS)}")
    ap.add_argument("--backend", default="anthropic")
    ap.add_argument("--model", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    out = args.out or datetime.now().strftime("logs/sweep-%Y%m%d-%H%M%S")
    os.makedirs(out, exist_ok=True)
    delays = [float(d) for d in args.delays.split(",")]
    schedules = args.schedules.split(",")
    arms = args.arms.split(",")
    json.dump(vars(args), open(os.path.join(out, "manifest.json"), "w"), indent=1)

    total = args.reps * len(delays) * len(schedules) * len(arms)
    n = 0
    for rep in range(args.reps):
        for delay in delays:
            for sched in schedules:
                for arm in arms:
                    n += 1
                    name = f"{arm}-d{int(delay)}-{sched}-r{rep}"
                    argv_run = ["--scenario", args.scenario, "--agents", str(args.agents),
                                "--schedule", sched, "--gap", str(args.gap),
                                "--db-delay-ms", str(delay), "--backend", args.backend,
                                "--log", os.path.join(out, name + ".jsonl"), "--quiet",
                                *ARMS[arm]]
                    if args.model:
                        argv_run += ["--model", args.model]
                    print(f"[{n}/{total}] {name}", flush=True)
                    run_agents.main(argv_run)

    print()
    print(report(aggregate(out)))
    return out


if __name__ == "__main__":
    main()
