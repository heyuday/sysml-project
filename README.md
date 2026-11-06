# agent-sql-bench

**Can several LLM agents that query the same database share a SQL result cache, and does it help?**

This is a small, honest benchmark: a handful of agents answer analyst questions against one PostgreSQL database through a shared result cache. Every LLM call and SQL call is logged. We build it in short build → test → look iterations, getting correctness first and measuring performance second. [NOTES.md](NOTES.md) is the lab notebook, recording what each iteration built, found and changed.

CS 5470, Team 19.

## Findings so far

Setup: Claude Haiku 5.5 agents, 4 at a time, on a small `shop` database (4 tables, about 20k rows). Agent 0 runs first. Agents 1–3 ("followers") can reuse its work. Raw logs are in [`results/`](results/).

**1. A plain exact-text cache gets no hits on real analytical SQL.** Four agents answering the same question, reworded, wrote seven different SQL statements. They used different column aliases, join orders and operand order, and some added extra columns. Every hit was a fixed-text schema lookup. ([first-real-runs](results/first-real-runs/))

**2. Cache-aware agents reuse cached queries verbatim and stay correct.** With `--aware`, agents get a `cached_queries` tool, which lists cached SQL, its columns and row counts, plus a hint to rerun a fitting query exactly. Sweep of 24 runs, mean ± sd over 3 reps ([sweep-paraphrase-v2](results/sweep-paraphrase-v2/summary.csv)):

| simulated DB delay | schedule | arm | correct | follower time (s) | follower LLM calls | reused queries per follower |
|---|---|---|---|---|---|---|
| 0 | sequential | no cache | 12/12 | 6.3 ± 0.4 | 4.9 | 0 |
| 0 | sequential | **aware** | 12/12 | **4.3 ± 0.5** | **3.0** | 1.1 |
| 0 | staggered | no cache | 12/12 | 6.2 ± 0.5 | 4.8 | 0 |
| 0 | staggered | **aware** | 12/12 | **4.3 ± 0.8** | **3.0** | 1.0 |
| 2 s | staggered | aware | 12/12 | 8.0 ± 0.2 | 3.9 | 0.33 |

- Aware followers reused a cached query in every run where one existed. They took exactly 3 LLM calls each, against about 4.9 without the cache, and every answer was correct.
- With no simulated delay, follower time dropped 32%. All of that saving came from fewer LLM turns.
- When the leader is slow and followers start early (2 s delay, staggered), reuse falls to 0.33 per follower, because nothing is cached yet when they check.

**How far to trust this:**
- **Credible:** the behavior, meaning reuse counts, correctness and LLM-call counts.
- **Real but narrow:** the 0 ms timing. It's one easy question, Haiku only, and 3 reps.
- **Not credible as magnitudes:** the 2 s rows. The delay is simulated and also charges schema lookups, which are cheap on a real database.
- **Open confound:** the saved turns may come from *seeing working SQL* rather than from the cached *result*. Measuring the cache's own value needs genuinely heavy queries. That's the next step.

## Quick start

You need Docker and [uv](https://docs.astral.sh/uv/).

```sh
docker compose up -d --wait      # Postgres 16 on 127.0.0.1:55440, seeded from db/seed.sql
uv sync
uv run pytest -q                 # 37 tests (TPC-H ones skip until it's loaded); needs the DB running
uv run python demo.py            # cache basics: 10 queries run twice, hit/miss and latency

# agents (the mock backend needs no API key)
uv run python run_agents.py --backend mock --scenario paraphrase --aware
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env && set -a && source .env && set +a   # .env is gitignored
uv run python run_agents.py --scenario paraphrase --aware          # real run, Haiku 5.5 by default
uv run python sweep.py --reps 3                                    # direct vs aware matrix
uv run python -m agentsql.aggregate results/sweep-paraphrase-v2    # re-print a sweep's table
```

**TPC-H (heavy queries).** [`scripts/load_tpch.py`](scripts/load_tpch.py) generates TPC-H with `tpchgen-cli` (installed from PyPI) and loads it into a `tpch` database. It works one table at a time to limit disk use, then times the task queries:

```sh
uv run python scripts/load_tpch.py --sf 1      # SF 1 ≈ 1 GB; SF 10 ≈ 15 GB in Postgres
uv run python run_agents.py --dataset tpch --task q5_nation_revenue --scenario paraphrase --aware
```

TPC-H tasks: `q5_nation_revenue` (TPC-H Q5) and `q10_returned_customer` (Q10), each with 4 wordings. At SF 1 they reproduce the official TPC-H answers, INDONESIA and Customer#000057040. On an M2 MacBook at SF 1 the queries still take only 0.15–0.3 s, so the heavy run uses SF 10.

Main `run_agents.py` options:

| Flag | Values |
|---|---|
| `--dataset` | `shop` (tiny seed DB), `tpch` |
| `--task` | question for `same`/`paraphrase`, e.g. `q5_nation_revenue` |
| `--scenario` | `same`, `paraphrase` (same question, reworded per agent), `related` (different questions), `mixed` |
| `--schedule` | `sequential`, `staggered --gap N`, `parallel` |
| `--cache` | `exact`, `direct` (no cache) |
| `--aware` | give agents the `cached_queries` tool and reuse hint |
| `--db-delay-ms N` | simulated latency on real DB executions, never on hits |
| `--statement-timeout-s N` | Postgres timeout for agent SQL (default 120) |
| `--backend` | `anthropic` (default `claude-haiku-5-5`), `openai` (any OpenAI-compatible API: OpenAI, Gemini, vLLM), `mock` |

## Running in the cloud (no Docker)

[`scripts/cloud_run.sh`](scripts/cloud_run.sh) runs the TPC-H experiment on a fresh Ubuntu VM, such as a Claude Code cloud session. It runs in steps, so no single command runs too long:

```sh
bash scripts/cloud_run.sh setup                      # Postgres on :55440 + deps + tests (scripts/setup_cloud.sh)
bash scripts/cloud_run.sh load                       # TPC-H SF 10 (override with SF=...)
bash scripts/cloud_run.sh sweep q5_nation_revenue    # direct vs aware, sequential, 3 reps
bash scripts/cloud_run.sh sweep q10_returned_customer
bash scripts/cloud_run.sh summary
```

It needs `ANTHROPIC_API_KEY` in the environment, or the alias `AGENTSQL_ANTHROPIC_API_KEY` if the host strips the former. Results go to `results/tpch-sf10/`. Tested in an Ubuntu 24.04 container at SF 0.1 with the mock backend.

## Layout

| Path | What |
|---|---|
| [`agentsql/cache.py`](agentsql/cache.py) | The shared SQL result cache: exact-text keys, conservative eligibility, thread-safe, catalog |
| [`agentsql/agent.py`](agentsql/agent.py) | Minimal SQL agent loop and tools (`list_tables`, `describe_table`, `run_sql`, `final_answer`, `cached_queries`) |
| [`agentsql/llm.py`](agentsql/llm.py) | LLM backends behind one interface: Anthropic, OpenAI-compatible, mock |
| [`agentsql/tasks.py`](agentsql/tasks.py) | Questions (4 tasks × 4 wordings, unique answers) and scenarios |
| [`agentsql/analyze.py`](agentsql/analyze.py), [`agentsql/aggregate.py`](agentsql/aggregate.py) | Per-run report; per-sweep table and CSVs |
| [`run_agents.py`](run_agents.py), [`sweep.py`](sweep.py), [`demo.py`](demo.py) | Entry points |
| [`tests/`](tests/) | Cache correctness, concurrency, mock end-to-end agent runs |
| [`db/seed.sql`](db/seed.sql), [`compose.yaml`](compose.yaml) | The small deterministic `shop` database |
| [`scripts/`](scripts/) | TPC-H loader, Docker-free Postgres setup, cloud run steps |
| [`results/`](results/) | Logs and tables from real-model runs (committed) |
| [`NOTES.md`](NOTES.md) | Lab notebook, one section per iteration |
| `logs/` | Scratch output from local runs (gitignored) |

## Next

1. **Clean heavy-query run (ready, runs in the cloud):** TPC-H at scale factor 10, Q5 and Q10, no cache vs. aware, 3 reps each. No simulated delay.
2. **Scaling the aware catalog** (Shourya's ideas):
   - retrieve only the top-k relevant entries instead of listing everything;
   - store the question behind each cached query;
   - measure token cost as the catalog grows.
3. **Wrong-reuse test:** near-miss questions (same tables, different filter). Do agents reuse a similar but wrong cached query?
4. Later: merging identical in-flight queries, invalidation when data changes, cache size limits and eviction.

## Related work

- **Closest:**
  - [TVCACHE](https://arxiv.org/abs/2602.10986) (tool-value cache for agent RL training, including SQL)
  - [ToolCaching](https://arxiv.org/abs/2601.15335) (keep/evict policies for tool-call caches)
  - [semantic caching for multi-agent NL→SQL](https://arxiv.org/abs/2601.11687) (caches question → SQL translations)
- **Motivation:** [Agent-first data systems](https://arxiv.org/abs/2509.00997) (agents' queries are highly redundant).
- **Data:** [Text-to-Big SQL](https://arxiv.org/abs/2602.21480) (LLM agents on TPC-H, which our next step uses).
- **None of these tests agents that can see a shared result cache and choose to reuse it.**

Teammates' related work:
- **Aryan** ([dbkv](https://github.com/KilroyHere/dbkv)) found the database is about 3.6% of a BIRD agent's task time. Our results are consistent with that: on fast databases the gain is LLM turns, not database time.
- [`related-work/sysml-project/`](related-work/sysml-project/) is MadBonzz's separate SQL-caching experiment, copied unchanged from [MadBonzz/sysml-project](https://github.com/MadBonzz/sysml-project). It shares no code with this project.
