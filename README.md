# agent-sql-bench

**Can several LLM agents that query the same database share a SQL result cache, and does it help?**

Several agents answer analyst questions against one PostgreSQL database through a shared result cache. Every LLM call and SQL call is logged. We build it in short build → test → look iterations. [NOTES.md](NOTES.md) is the lab notebook.

CS 5470, Team 19.

## Main result: heavy queries (TPC-H, scale factor 10)

Setup:
- **Data:** TPC-H at scale factor 10 in Postgres 16, on a 4-vCPU / 15 GB cloud VM.
- **Tasks:** 7 classic TPC-H queries, each narrowed to a one-row answer and asked in 4 different wordings. Single-query cost ranges from about 0.8 s (Q11) to about 20 s (Q9).
- **Agents:** 4 Claude Haiku 5.5 agents. Agent 0 is the "leader"; agents 1–3 are "followers" that can reuse its work.
- **Arms:** no cache, or a shared cache with **cache-aware** agents. Aware agents get a `cached_queries` tool that lists cached SQL with its columns and row counts, plus a hint to rerun a fitting query verbatim.
- **Batch 2** ([`results/tpch-sf10-b2/`](results/tpch-sf10-b2/)): 5 reps sequential and 3 reps concurrent, per task and arm. **All 784 answers are correct.** Batch 1 ([`results/tpch-sf10/`](results/tpch-sf10/), Q5 and Q10 only) gave the same numbers.

### Agents one after another

Follower time is the mean per follower. DB time is the total database execution time per run.

| task | follower s, no cache | follower s, cache-aware | faster | reuse | DB s / run | less DB |
|---|---|---|---|---|---|---|
| Q11 (0.8 s query) | 4.4 | **3.6** | 17% | 1.00 | 3.0 → **0.8** | 74% |
| Q2 | 8.6 | **8.3** | 3% | 0.20 | 15.4 → **13.4** | 13% |
| Q10 | 9.7 | **3.7** | 62% | 1.00 | 23.6 → **5.3** | 78% |
| Q15 | 7.5 | **3.9** | 47% | 1.00 | 14.6 → **3.7** | 75% |
| Q5 | 10.6 | **3.9** | 63% | 1.00 | 23.6 → **6.1** | 74% |
| Q3 | 10.1 | **3.3** | 67% | 1.00 | 24.4 → **6.1** | 75% |
| Q9 (20 s query) | 26.5 | **3.7** | 86% | 1.00 | 82.4 → **27.8** | 66% |

- **Whenever a follower can see a cached result, it reuses it.** On 6 of 7 tasks every follower reused the leader's query. Follower time drops to about 3.5 s, the cost of three LLM calls, no matter how heavy the query is. DB work falls by about 75%.
- **The saving grows with query cost:** 17% on a 0.8 s query, 86% on a 20 s query. On Q9 a whole 4-agent run went from 104 s to 45 s.
- **Q2 is the exception, and the cause is our own cache rule.** Agents usually write Q2 with a CTE (`WITH ...`). Our cache conservatively refuses anything that isn't a plain `SELECT`, so 46 of 57 Q2 queries bypassed it. A follower can't reuse a result that was never stored. Accepting read-only CTEs is a cheap fix (next runs, item 2).
- **Limits:**
  - Sequential is the best case for reuse, because the leader always finishes first.
  - One model.
  - The aware leader is sometimes slightly slower, because it checks an empty cache first.

### Agents running at the same time

Same setup, but the agents overlap:
- **`parallel`:** all agents start together.
- **`staggered`:** agents start 3 s apart.

The third arm, **`aware-live`**, also lists queries that are *still running*. Rerunning one of them verbatim waits for its result (single-flight) instead of executing again.

Columns: follower time is the mean in seconds. "Heavy runs" counts analytical queries per run that actually executed and took ≥ 1 s.

| task | schedule | no cache | aware | aware-live | reuse (live) | heavy runs (no cache → live) | DB s / run (no cache → live) |
|---|---|---|---|---|---|---|---|
| Q10 | staggered | 11.1 | 8.5 | **4.4** | 1.00 | 4.3 → **1.0** | 27 → **5** |
| Q15 | staggered | 7.6 | 4.8 | **3.4** | 1.00 | 4.0 → **1.0** | 17 → **3** |
| Q5 | staggered | 17.5 | 10.5 | **6.5** | 0.89 | 4.0 → **1.3** | 44 → **10** |
| Q3 | staggered | 16.9 | 16.6 | **4.9** | 1.00 | 4.0 → **1.0** | 45 → **6** |
| Q9 | staggered | 44.5 | 43.8 | **19.1** | 0.67 | 4.3 → **2.0** | 148 → **37** |
| Q11 | staggered | 4.4 | 4.0 | 3.4 | 1.00 | (all < 1 s) | 3 → 1 |
| Q2 | staggered | 9.2 | 9.6 | 20.8* | 0.33 | 2.7 → 2.3 | 15 → 13 |
| all 7 | parallel | | | about equal in every arm | ≈ 0 (Q3: 0.11) | no change | no change |

\* One rep hit the 120 s statement timeout on an uncached CTE query. The other two reps took 16 s and 21 s.

- **With staggered starts, showing running queries is what makes reuse work.** Followers see the leader's query RUNNING, rerun it verbatim and wait. The database runs the heavy query **once instead of about 4 times**, which saves 75–87% of DB time on Q10, Q15, Q5, Q3 and Q9.
- **Plain `aware` helps only when the leader happens to finish before a follower looks.** It got some reuse on Q5, Q10 and Q15. On Q3 and Q9 it got almost none, because the leader's query was still running.
- **Simultaneous starts get no reuse in any arm, on any task.** Every agent checked the catalog once, about 1 s in, before anyone's heavy query had started. Agents also ignored the prompt to re-check before an expensive query. The fix has to be enforced by the system (next runs, item 1).
- Overlap is costly without a cache. On 4 vCPUs, 4 simultaneous copies of a heavy query each run 2–3.5× slower.

**Why awareness is needed:** on a small database, four agents given the same question in different words wrote seven different SQL statements (different aliases, join order, extra columns). So a plain exact-text cache got **0 hits** on the analytical queries. Cache-aware agents instead rerun a cached query verbatim. This held in every run where a cached query existed, across 24 runs on the small database ([results/sweep-paraphrase-v2](results/sweep-paraphrase-v2/), [first runs](results/first-real-runs/)).

## Next runs

1. **Enforced re-check for simultaneous starts.** Agents didn't follow the prompt to re-check before their expensive query. Instead, when an aware agent's `run_sql` would execute a new query, the system first shows it any cached or RUNNING entries it hasn't seen yet. The agent then reruns one verbatim or confirms its own. This targets the `parallel` case, where the first heavy query starts about 0.5–1 s before the others.
2. **Cache read-only CTEs.** Accept `WITH` queries whose bodies are plain SELECTs. This should recover Q2, where agents write CTEs and about 80% of their queries bypassed the cache.
3. **A catalog that scales.** Listing every cached query costs more tokens as the cache grows. Instead, retrieve only the top-k entries relevant to the agent's question, and store the question behind each cached query. We'll measure tokens, reuse and accuracy as the catalog grows (10 → 100 → 1,000 entries).
4. **Wrong-reuse test.** Give agents near-miss questions: same tables, different filter (e.g. "revenue by region" vs. "revenue by region for one category"). Do they rerun a cached query that looks similar but is wrong? The research suggests similar-but-wrong context is a real failure mode, so we'll measure the wrong-reuse rate directly.
5. Later: invalidation when data changes (TTL vs. table versions), cache size limits and eviction, other models.

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
uv run pytest -q                 # 48 tests (TPC-H ones skip until it's loaded)

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
