"""Run several LLM agents against the shared cache and log every call.

Examples:
  uv run python run_agents.py --backend mock --scenario same            # no API key needed
  uv run python run_agents.py --scenario paraphrase --schedule staggered --gap 15
  uv run python run_agents.py --backend openai --model Qwen/Qwen3-8B --scenario related
  uv run python run_agents.py --scenario paraphrase --aware --db-delay-ms 2000

Schedules:
  sequential - agent i starts after agent i-1 finishes (max reuse, no overlap in time)
  staggered  - agent i starts i*gap seconds after the first; agents then run concurrently
  parallel   - all agents start at once
"""

from __future__ import annotations

import argparse
import os
import threading
import time
from datetime import datetime

import psycopg

from agentsql.agent import Agent, EventLog
from agentsql.analyze import load, report, summarize
from agentsql.cache import DSNS, SqlCache
from agentsql.llm import make_backend
from agentsql.tasks import DATASETS, SCENARIOS, assign


def expected_answers(tasks, dsn) -> dict[str, list[str]]:
    """Run each reference query directly (outside the cache). Any cell of its
    first row counts as a correct answer (e.g. a customer's name or key)."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        out = {}
        for t in tasks:
            row = conn.execute(t.reference_sql).fetchone()
            out[t.id] = [str(v).strip() for v in row if v is not None]
        return out


def main(argv=None) -> str:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", choices=list(DATASETS), default="shop")
    ap.add_argument("--task", default=None, help="question for same/paraphrase (default: dataset's first)")
    ap.add_argument("--agents", type=int, default=4)
    ap.add_argument("--scenario", choices=SCENARIOS, default="same")
    ap.add_argument("--schedule", choices=("sequential", "staggered", "parallel"), default="sequential")
    ap.add_argument("--gap", type=float, default=10.0, help="seconds between starts (staggered)")
    ap.add_argument("--cache", choices=("exact", "direct"), default="exact")
    ap.add_argument("--backend", choices=("anthropic", "openai", "mock"), default="anthropic")
    ap.add_argument("--model", default=None, help="default: claude-haiku-5-5 for anthropic")
    ap.add_argument("--effort", default="medium", help="anthropic effort: low|medium|high|xhigh|max")
    ap.add_argument("--aware", action="store_true",
                    help="give agents a cached_queries tool and a hint to reuse cached SQL")
    ap.add_argument("--db-delay-ms", type=float, default=0.0,
                    help="simulated extra latency on every real DB execution (not on hits)")
    ap.add_argument("--statement-timeout-s", type=float, default=120.0,
                    help="Postgres statement_timeout for agent queries (0 = none)")
    ap.add_argument("--max-steps", type=int, default=20)
    ap.add_argument("--log", default=None)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    dsn = DSNS[args.dataset]
    plan = assign(args.scenario, args.agents, args.dataset, args.task)
    expected = expected_answers({t for t, _ in plan}, dsn)
    backend = make_backend(args.backend, args.model, args.effort)
    model = getattr(backend, "model", None)

    os.makedirs("logs", exist_ok=True)
    path = args.log or datetime.now().strftime(
        f"logs/%Y%m%d-%H%M%S-{args.dataset}-{args.scenario}-{args.schedule}-{args.backend}.jsonl")
    log = EventLog(path)
    log.write(type="run", dataset=args.dataset, task=plan[0][0].id, scenario=args.scenario, schedule=args.schedule, gap=args.gap,
              n_agents=args.agents, cache=args.cache, backend=args.backend, model=model,
              effort=args.effort if args.backend == "anthropic" else None,
              aware=args.aware, db_delay_ms=args.db_delay_ms)

    # one cache shared by all agents
    cache = SqlCache(dsn=dsn, mode=args.cache, read_only=True, db_delay_ms=args.db_delay_ms,
                     statement_timeout_s=args.statement_timeout_s)
    agents = [Agent(i, t, q, expected[t.id], backend, cache, log, args.max_steps, args.aware)
              for i, (t, q) in enumerate(plan)]

    if args.schedule == "sequential":
        for a in agents:
            a.run()
    else:
        gap = args.gap if args.schedule == "staggered" else 0.0
        threads = []
        for i, a in enumerate(agents):
            if i and gap:
                time.sleep(gap)
            th = threading.Thread(target=a.run, name=f"agent-{i}")
            th.start()
            threads.append(th)
        for th in threads:
            th.join()

    log.write(type="cache_stats", **cache.stats.as_dict())
    log.close()
    cache.close()
    if not args.quiet:
        print(report(summarize(load(path))))
        print(f"\nlog: {path}")
    return path


if __name__ == "__main__":
    main()
