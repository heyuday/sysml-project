# agent-sql-bench

**Can several LLM agents that query the same database share a SQL result cache, and does it help?**

Several agents answer analyst questions against one PostgreSQL database through a shared result cache. Every LLM call and SQL call is logged. We build it in short build → test → look iterations. [NOTES.md](NOTES.md) is the lab notebook.

CS 5470, Team 19.

## Main result: heavy queries (TPC-H, scale factor 10)

### Agents one after another

Setup:
- **Data:** TPC-H at scale factor 10 in Postgres 16, on a 4-vCPU / 15 GB cloud VM. The two tasks are classic TPC-H queries, each asked in 4 different wordings:
  - **Q5:** top Asian nation by local-supplier revenue, a 6-table join taking about 5.3 s.
  - **Q10:** the customer with the most revenue lost to returns, about 4.3 s.
- **Agents:** 4 Claude Haiku 5.5 agents, one after another. Agent 0 (the "leader") runs first, and agents 1–3 ("followers") can reuse its work.
- **Arms:** no cache vs. a shared cache with **cache-aware** agents. Aware agents get a `cached_queries` tool listing cached SQL with columns and row counts, and a hint to rerun a fitting query verbatim.
- 3 reps per arm. No simulated latency. Raw logs are in [`results/tpch-sf10/`](results/tpch-sf10/).

| task | arm | correct | follower time (s) | follower LLM calls | follower SQL time (s) | DB time per run (s) |
|---|---|---|---|---|---|---|
| Q5 | no cache | 12/12 | 10.9 ± 0.2 | 3.7 | 5.1 | 20.5 |
| Q5 | **cache-aware** | 12/12 | **4.7 ± 0.1** | 3.0 | **0.0** | **5.1** |
| Q10 | no cache | 12/12 | 9.1 ± 1.3 | 3.0 | 5.2 | 19.8 |
| Q10 | **cache-aware** | 12/12 | **4.4 ± 0.1** | 3.0 | **0.0** | **4.3** |

- **Followers finish 52–57% faster, and database work drops 75–78%.** Only the leader's heavy query executes. Every follower reused it, and every answer was correct.
- **The saving is the cache itself.** In Q10 both arms make exactly 3 LLM calls per follower, so the whole saving (about 4.7 s) is cached results replacing 4–5 s queries. In Q5, about 1 s comes from fewer turns and about 5 s from cache hits.
- **Query cost decides whether this matters.** Without a cache, SQL is about half of a follower's time here. On small benchmark databases such as BIRD it's a few percent, and caching barely helps there.
- **Limits:**
  - Sequential is the best case for reuse: the leader always finishes first.
  - n = 3, one model, two tasks.
  - The aware leader can be slightly slower (+2.5 s in Q10) because it checks an empty cache.

### Agents running at the same time

Same setup, but agents overlap: `parallel` (all start together) or `staggered` (3 s apart). The third arm, **`aware-live`**, also lists queries that are *still running*, and rerunning one verbatim waits for its result (single-flight). Mean follower time; "heavy runs" = analytical queries that actually executed ≥ 1 s per run; 3 reps; all 144 answers correct ([results](results/tpch-sf10/)):

| task | schedule | no cache | aware | aware-live | heavy runs (no cache → aware-live) |
|---|---|---|---|---|---|
| Q10 | staggered | 9.1 s | 8.7 s | **4.1 s** | 4 → **1** |
| Q5 | staggered | 13.4 s | 8.1 s | **7.5 s** | 4 → **2** |
| Q10 | parallel | 18.1 s | 17.7 s | 16.7 s | 5.7 → 4 |
| Q5 | parallel | 20.2 s | 19.9 s | 19.8 s | 4 → 4 |

- **Showing running queries is what makes overlap work.** In staggered runs, followers saw the leader's query RUNNING and waited for it instead of recomputing. On Q10 that's −55% follower time and −78% DB time, matching the sequential result. Plain `aware` barely helped there, because the leader's result wasn't finished yet when followers looked.
- **Simultaneous starts get no reuse in any arm.** Every agent checked the catalog once, about 1 s in. The first heavy query started at about 2.5–3.5 s, so there was nothing to see.
- **Agents ignored the instruction to re-check before their expensive query.** Every agent checked exactly once. The fix is to enforce it in the system rather than in the prompt (next runs, item 1).
- Overlap is costly without a cache: 4 simultaneous 5 s queries on 4 vCPUs took about 14 s each.

**Why awareness is needed:** on a small database, four agents given the same question in different words wrote seven different SQL statements (different aliases, join order, extra columns). So a plain exact-text cache got **0 hits** on the analytical queries. Cache-aware agents instead rerun a cached query verbatim. This held in every run where a cached query existed, across 24 runs on the small database ([results/sweep-paraphrase-v2](results/sweep-paraphrase-v2/), [first runs](results/first-real-runs/)).

## Next runs

1. **Enforced re-check for simultaneous starts.** Agents didn't follow the prompt to re-check before their expensive query. Instead, when an aware agent's `run_sql` would execute a new query, the system first shows it any cached or RUNNING entries it hasn't seen yet. The agent then reruns one verbatim or confirms its own. This targets the `parallel` case, where the first heavy query starts about 0.5–1 s before the others.
2. **A catalog that scales.** Listing every cached query costs more tokens as the cache grows. Instead, retrieve only the top-k entries relevant to the agent's question, and store the question behind each cached query. We'll measure tokens, reuse and accuracy as the catalog grows (10 → 100 → 1,000 entries).
3. **Wrong-reuse test.** Give agents near-miss questions: same tables, different filter (e.g. "revenue by region" vs. "revenue by region for one category"). Do they rerun a cached query that looks similar but is wrong? The research suggests similar-but-wrong context is a real failure mode, so we'll measure the wrong-reuse rate directly.
4. Later: invalidation when data changes (TTL vs. table versions), cache size limits and eviction, other models.

## How it works

- **Cache** ([`agentsql/cache.py`](agentsql/cache.py)):
  - stores results of read-only `SELECT`s, keyed on the exact SQL text (writes, volatile functions and locks bypass it);
  - is shared by all agents and thread-safe, with one Postgres connection per thread;
  - **single-flight:** identical in-flight queries execute once, and the others wait;
  - **catalog:** completed entries, plus running ones in live mode.
- **Agents** ([`agentsql/agent.py`](agentsql/agent.py)): a minimal tool loop with `list_tables`, `describe_table`, `run_sql` and `final_answer`, plus `cached_queries` when aware. Every call goes through the cache. An answer is correct if it contains a cell of the reference query's first row.
- **Backends** ([`agentsql/llm.py`](agentsql/llm.py)): Anthropic (default `claude-haiku-5-5`), any OpenAI-compatible API (OpenAI, Gemini, vLLM), and a scripted mock for tests.

## Quick start

You need Docker and [uv](https://docs.astral.sh/uv/).

```sh
docker compose up -d --wait      # Postgres 16 on 127.0.0.1:55440 with the small `shop` DB
uv sync
uv run pytest -q                 # 42 tests (TPC-H ones skip until it's loaded)

uv run python scripts/load_tpch.py --sf 1                     # TPC-H into DB `tpch` (SF 10 ≈ 15 GB)
uv run python run_agents.py --backend mock --dataset tpch --task q5_nation_revenue --scenario paraphrase --live
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env && set -a && source .env && set +a   # .env is gitignored
uv run python sweep.py --dataset tpch --task q10_returned_customer --delays 0 --schedules sequential --arms direct,aware
uv run python -m agentsql.aggregate results/tpch-sf10/q5_nation_revenue        # re-print a sweep's table
```

Main `run_agents.py` options:

| Flag | Values |
|---|---|
| `--dataset` / `--task` | `shop` or `tpch`; question for `same`/`paraphrase`. TPC-H tasks: `q2_min_cost_supplier`, `q3_shipping_priority`, `q5_nation_revenue`, `q9_green_profit`, `q10_returned_customer`, `q11_german_stock_part`, `q15_top_supplier`, `q18_large_order_customer` (each is the standard TPC-H query narrowed to a unique one-row answer; SF 1 answers match the official results) |
| `--scenario` | `same`, `paraphrase` (same question, reworded per agent), `related`, `mixed` |
| `--schedule` | `sequential`, `staggered --gap N`, `parallel` |
| `--cache` | `exact` or `direct` (no cache) |
| `--aware` / `--live` | cache-aware agents; `--live` adds RUNNING entries, waiting and the re-check hint |
| `--backend` / `--model` | `anthropic` (default), `openai`, `mock` |
| `--statement-timeout-s`, `--db-delay-ms` | Postgres timeout for agent SQL (default 120 s); simulated DB latency (shop-DB experiments only) |

### Running in the cloud (no Docker)

[`scripts/cloud_run.sh`](scripts/cloud_run.sh) runs the TPC-H experiments on a fresh Ubuntu VM, such as a Claude Code cloud session, in steps that each fit a command time limit:

```sh
bash scripts/cloud_run.sh setup                        # Postgres on :55440 + deps + tests
bash scripts/cloud_run.sh load                         # TPC-H SF 10 (~4 min on 4 vCPU)
bash scripts/cloud_run.sh sweep q5_nation_revenue      # sequential: direct vs aware
bash scripts/cloud_run.sh concurrent q5_nation_revenue # overlapping agents: direct vs aware vs aware-live
bash scripts/cloud_run.sh summary
```

The key goes in the environment as `AGENTSQL_ANTHROPIC_API_KEY`. Claude Code cloud sessions reserve `ANTHROPIC_API_KEY` for their own login and don't pass it to commands.

## Layout

| Path | What |
|---|---|
| [`agentsql/`](agentsql/) | Cache, agent loop, LLM backends, tasks, per-run and per-sweep analysis |
| [`run_agents.py`](run_agents.py), [`sweep.py`](sweep.py), [`demo.py`](demo.py) | Entry points: one run, a matrix of runs, and a cache demo |
| [`scripts/`](scripts/) | TPC-H loader, Docker-free Postgres setup, cloud run steps |
| [`tests/`](tests/) | Cache correctness, single-flight, concurrency, mock end-to-end runs |
| [`results/`](results/) | Logs and tables from real-model runs |
| [`db/seed.sql`](db/seed.sql), [`compose.yaml`](compose.yaml) | The small deterministic `shop` database |
| [`NOTES.md`](NOTES.md) | Lab notebook |

## Related work

- **Closest:**
  - [TVCACHE](https://arxiv.org/abs/2602.10986) (tool-value cache for agent RL training, including SQL)
  - [ToolCaching](https://arxiv.org/abs/2601.15335) (keep/evict policies for tool-call caches)
  - [semantic caching for multi-agent NL→SQL](https://arxiv.org/abs/2601.11687) (caches question → SQL translations)
- **Motivation:** [Agent-first data systems](https://arxiv.org/abs/2509.00997) (agents' queries are highly redundant).
- **Data:** [Text-to-Big SQL](https://arxiv.org/abs/2602.21480) (LLM agents on TPC-H).
- **Retrieving a few relevant entries instead of listing them all:** [RAG-MCP](https://arxiv.org/abs/2505.03275) and [DAIL-SQL](https://arxiv.org/abs/2308.15363).
- **Similar-but-wrong context:** [Shi et al. 2023](https://arxiv.org/abs/2302.00093) and [MeanCache](https://arxiv.org/abs/2403.02694).
- **None of these tests agents that can see a shared result cache and choose to reuse it.**

Teammates' related work:
- [dbkv](https://github.com/KilroyHere/dbkv) (GPU-serving side; measured the DB at about 3.6% of a BIRD agent's time).
- [`related-work/sysml-project/`](related-work/sysml-project/) is a separate SQL-caching experiment, copied unchanged from [MadBonzz/sysml-project](https://github.com/MadBonzz/sysml-project).
