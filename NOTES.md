# Iteration log

Run: `docker compose up -d --wait && uv run pytest -q && uv run python demo.py`
(Postgres 16 on 127.0.0.1:55440, db `shop`, user/pass `bench`/`bench`)

## Iteration 0 — smallest working cache (2026-11-06)

**Built:** `agentsql/cache.py` (`SqlCache.run_sql`, modes `direct`/`exact`, in-process dict keyed on exact SQL text, conservative `is_cacheable`), tiny deterministic seed DB (`db/seed.sql`: 500 customers, 100 products, 5k orders, 15k order_items), 20 pytest tests, `demo.py`.

**Results:** 20/20 tests pass. Postgres-side `pg_stat_statements` confirms one execution for 5 repeated calls. Cached results match direct execution. Demo: 10 misses averaging 2.2 ms, 10 hits averaging 0.008 ms.

**Observations / assumptions to revisit:**
- `information_schema` queries (3–4 ms) cost more than most data queries on this tiny DB. Schema exploration may be a real reuse target. The catch: catalog results won't be covered by table-version invalidation (they change on DDL, not DML).
- `WITH ...` (CTEs) is rejected as non-cacheable for now. Agents probably use CTEs a lot, so we'll check against real traces in iteration 3.
- `LIMIT` without `ORDER BY` is cached even though its result isn't formally deterministic. That's acceptable for now.
- Known gap, pinned by `test_known_gap_no_invalidation`: writes don't invalidate. Iteration 2 must flip that test.
- Hits return the shared immutable `Result`. That's safe because it's built from tuples.

**Next:** iteration 1. Concurrent fake agents, a thread-safe cache, single-flight coalescing.

## Iteration 3 (moved up) — real-agent harness, ready to run (2026-11-06)

We moved this ahead of iteration 1. The biggest open question is whether real agents repeat each other's queries at all. Concurrency engineering can wait until we know.

**Built:**
- `agentsql/agent.py`: a ReAct-style loop with `list_tables`, `describe_table`, `run_sql` and `final_answer`. All SQL goes through one shared `SqlCache`, and the connection is read-only.
- `agentsql/llm.py`: backends for `anthropic` (default `claude-haiku-5-5`, effort `medium`; changed from Opus 5.5 to keep runs cheap), `openai` (OpenAI-compatible: OpenAI, vLLM, SGLang) and `mock` (scripted, offline).
- `agentsql/tasks.py`: 4 questions × 4 wordings, each with a unique answer checked against the data, and scenarios `same`, `paraphrase`, `related`, `mixed`.
- `run_agents.py`: schedules `sequential`, `staggered --gap N`, `parallel`, and cache `exact` or `direct`. Writes a JSONL log of every LLM call and tool call.
- `agentsql/analyze.py`: classifies each call as first-seen, a repeat by the same agent, a repeat by another agent, or a near-duplicate (would need normalization).
- `SqlCache` now has a global lock, which is correct but serializes DB calls. That's fine at this scale; iteration 1 replaces it.

**Results so far, mock backend only:** 30/30 tests pass. A mock `mixed` staggered run had a 65% hit rate, all of it cross-agent, mostly `information_schema` lookups. The mock emits identical SQL for identical tasks, so this is an **upper bound**. Real models will vary their SQL text.

**Not yet verified:** the `anthropic` and `openai` backends have not made a real API call yet; we're waiting on a key.

**Run plan once a key is available:**
1. `same` sequential → maximum possible reuse with real SQL variation.
2. `paraphrase` sequential → does different wording still produce the same SQL?
3. `related` staggered → is the reuse only in schema exploration?
4. Run any of these with `--cache direct` as well, to confirm answers don't depend on caching.

## Cache-aware agents + simulated DB delay (2026-11-06)

**Why:** a cache only helps when agents send SQL that's already in it. Real models reword their SQL, so exact-text hits may be rare. The question: if agents can *see* what's cached, do they reuse those queries verbatim, and does that make them faster? The cache is still the mechanism. Awareness only steers agents toward it.

**Built:**
- `SqlCache.catalog()` lists what the cache holds.
- `--aware` gives agents a `cached_queries` tool plus a prompt hint: rerun cached SQL verbatim when it fits. The tool shows each query's SQL, columns and row count, but not its data, so every reuse shows up as a cache hit.
- `--db-delay-ms N` adds simulated latency to real DB executions only, never to hits. It's a stand-in for heavy queries until we load a big DB.
- The cache now uses one connection per thread and locks only the in-memory store, so concurrent misses run in parallel. There's still no single-flight: two simultaneous misses on the same query both execute.

**Results (mock only):** 35/35 tests pass. Mock `paraphrase` + aware + 300 ms delay: agent 0 took 2.07 s, agents 1–3 about 0.19 s each, with 75% hits. The mock writes identical SQL whether or not it's aware, so it can't show the awareness effect. That needs a real model.

**Real-model run plan:** 4 agents, sequential, `--db-delay-ms 2000`:
1. `paraphrase`, unaware: how often do reworded questions produce identical SQL on their own?
2. `paraphrase`, `--aware`: does the hit rate rise, and does wall time fall?
3. `related`, unaware vs `--aware`: the control. Different questions should *not* reuse each other's results. Check that accuracy holds and agents don't pick a wrong cached query.

Compare per agent: hits, SQL calls, LLM turns, wall time, correctness.

## First real-model runs: Haiku 5.5, 4 agents, sequential, 2 s simulated DB delay (2026-11-06)

One run per configuration, so treat these as directional (no repeats yet). Logs are in `results/first-real-runs/`. The four runs used about 91k input and 11k output tokens in total, roughly $0.015.

| Run | Agents 1–3 wall (s) | Agents 1–3 steps | Analytical SQL reused across agents | Tool calls | Input tokens | Correct |
|---|---|---|---|---|---|---|
| paraphrase, unaware | 9.3 / 16.7 / 10.8 | 5 / 6 / 5 | **0** | 27 | 24.0k | 4/4 |
| paraphrase, aware | **3.6 / 4.0 / 7.5** | 3 / 3 / 4 | **4** | 14 | 22.8k | 4/4 |
| related, unaware | 8.7 / 10.2 / 6.0 | 4 / 4 / 4 | 0 | 19 | 15.9k | 4/4 |
| related, aware | 9.0 / 7.3 / 6.7 | 4 / 4 / 4 | 0 | 21 | 28.2k | 4/4 |

**Findings:**
1. **Exact-text caching gets no hits on real analytical SQL, even for the same question.** Four agents produced seven different statements. Two computed identical results but differed in alias (`sales` vs `revenue`), join order and `price*quantity` operand order. Others added extra columns. Syntactic normalization wouldn't merge them either. Every exact hit in unaware runs was a fixed-text schema lookup.
2. **Cache-aware agents reuse verbatim and skip whole turns.** In the aware paraphrase run, agents 1–3 went straight to `cached_queries`, reran agent 0's SQL and answered. They skipped schema exploration, so the saving is mostly LLM turns rather than DB time. Wall time for agents 1–3 dropped about 2.5× and tool calls halved.
3. **No wrong reuse in the control.** With different questions, aware agents checked the cache, found nothing that fit and wrote their own SQL. All 4 were correct.
4. **Overhead:** agent 0 pays for checking an empty cache (26 s vs 22 s). When nothing can be reused, awareness costs about 75% more input tokens (related: 28k vs 16k).

**Caveats:**
- n = 1 per configuration.
- The schedule is sequential, the best case: agent 0 finishes before the others start.
- The 2 s delay is simulated, so it inflates the DB share.
- The DB is tiny, and the answers are trivially unique.

**Next:**
- Run 3 repeats per configuration to see the variance.
- Run with `--db-delay-ms 0` to isolate the turn savings from the DB savings.
- Try `staggered` and `parallel` schedules. Concurrent agents start with an empty cache, which is where single-flight matters.
- Run the `mixed` scenario: can agents pick the right cached query when distractors are present?

## Sweep v1 → v2: paraphrase scenario, 2 delays × 2 schedules × 3 reps (2026-11-06)

**Change:** v1 was stopped after 3 runs (set aside in `logs/sweep-paraphrase-v1-stopped/`). The `exact` (unaware) arm was dropped, because unaware agents reused 0 analytical queries in the first real runs; their only hits were fixed-text schema lookups. v2 compares `direct` vs `aware` only, 24 runs, in `results/sweep-paraphrase-v2/`.

**Design** (`sweep.py`, aggregated by `agentsql/aggregate.py`):
- **Arms:** `direct` (no cache), `exact` (plain cache, unaware agents), `aware` (cache + `cached_queries` tool and hint).
- **Delay:** 0 ms isolates turn savings; 2,000 ms adds DB savings.
- **Schedule:** `sequential` (leader done before followers start: best case for reuse) or `staggered` with a 5 s gap (followers start while the leader is still working).
- **Order:** arms are interleaved within each rep, so API-latency drift doesn't favor any one arm.
- **Metrics:** "leader" = agent 0; "followers" = agents 1–3. Reported as mean ± sd across reps: follower wall time, LLM calls, LLM seconds, SQL seconds, reused analytical queries (`run_sql` hits), leader wall time, makespan (first start to last finish), input tokens, correctness.

**Mock dry run (2 reps):** the output looks sensible. With staggered starts and short gaps, followers reuse nothing, because they start before the leader has filled the cache. Expect the same with real agents at a 5 s gap, since the leader takes about 20 s.

Output folder: `results/sweep-paraphrase-v2/` (`runs.csv`, `summary.csv`); v1 was stopped (see below).

### Sweep v2 results (24 runs, Haiku 5.5, ~519k in / 72k out tokens ≈ $0.09)

Followers = agents 1–3. Values are mean ± sd over 3 reps.

| delay | schedule | arm | correct | follower wall s | follower LLM calls | follower SQL s | reused/follower | makespan s |
|---|---|---|---|---|---|---|---|---|
| 0 | sequential | direct | 12/12 | 6.3±0.4 | 4.9±0.2 | 0.0 | 0 | 27.5±1.8 |
| 0 | sequential | aware | 12/12 | **4.3±0.5** | **3.0±0.0** | 0.0 | 1.11 | 20.4±1.6 |
| 0 | staggered | direct | 12/12 | 6.2±0.5 | 4.8±0.2 | 0.1 | 0 | 22.7±0.8 |
| 0 | staggered | aware | 12/12 | **4.3±0.8** | **3.0±0.0** | 0.0 | 1.00 | 20.0±1.2 |
| 2000 | sequential | direct | 12/12 | 19.3±0.6 | 4.8±0.2 | 13.0±0.4 | 0 | 80.0±3.6 |
| 2000 | sequential | aware | 12/12 | 5.4±0.5 | 3.0±0.0 | 0.0 | 1.00 | 35.2±1.3 |
| 2000 | staggered | direct | 12/12 | 19.8±1.4 | 4.8±0.2 | 13.7±1.0 | 0 | 37.8±1.2 |
| 2000 | staggered | aware | 12/12 | 8.0±0.2 | 3.9±0.2 | 2.9±0.4 | 0.33 | 21.7±3.6 |

**Credible (behavior counts, plus real timing at 0 ms):**
- Aware followers reused a cached analytical query in every 0 ms run and every sequential run. Every one of the 48 answers per arm was correct.
- Followers took exactly 3 LLM calls (sd 0) vs about 4.9 without the cache. Haiku follows the reuse hint consistently.
- At 0 ms (no simulation), follower wall time fell from 6.3 s to 4.3 s (−32%), entirely from fewer LLM turns.
- My prediction was wrong: staggered starts at 0 ms still reused fully. The leader's SQL lands in the cache about 4–5 s in, before followers check at 5–15 s.
- At 2 s delay and staggered, reuse fell to 0.33 per follower. The slow leader hadn't cached its query yet when agent 1 started. Timing overlap limits reuse, as expected.

**Not credible as magnitudes:** the 2 s rows. Aware followers still called `list_tables` (36×, all hits) and `describe_table` (25×, 18 hits). The simulated delay charges 2 s to schema lookups, which are cheap on a real DB, so the 2 s rows overstate the gain from skipping and hitting them.

**Confound (still open):** the 2-turn saving likely comes from seeing working SQL (shared knowledge), not from the cached result itself. Separating them needs the heavy-DB step, with an arm where agents see past SQL but reruns still execute.

**Next:** heavy queries on a real big DB (TPC-H in Postgres) instead of `--db-delay-ms`.

## Repo reorganized (2026-11-06)

This project now lives at the repo root. MadBonzz's original files moved, unchanged, to `related-work/sysml-project/`. Real-model results are committed under `results/`. `logs/` is local scratch and gitignored. `compose.yaml` pins the Docker project name `agent-sql-bench`, so the existing container keeps being used.

## TPC-H heavy-query setup, ready for the cloud (2026-11-06)

**Why:** the `--db-delay-ms` stand-in distorts timings, as noted above. It also charges cheap schema lookups. Real heavy queries replace it.

**Built:**
- `scripts/load_tpch.py`: generates with `tpchgen-cli` (from PyPI; DuckDB's TPC-H extension needs a download host that isn't in the cloud's default allowlist), then loads into DB `tpch` one table at a time, generating, COPYing and deleting each CSV. Adds primary keys, runs ANALYZE, and times the task queries, checking that each answer is unique.
- Tasks: `q5_nation_revenue` and `q10_returned_customer`, TPC-H Q5 and Q10 with standard parameters, narrowed to one-row answers, 4 wordings each. An answer is correct if it contains any cell of the reference row (name or key).
- Agent connections get `statement_timeout` (default 120 s).
- `scripts/setup_cloud.sh` sets up Postgres on :55440 without Docker. `scripts/cloud_run.sh` provides the `setup`, `load`, `sweep <task>` and `summary` steps.

**Checks:**
- SF 1 locally reproduces the official TPC-H answers (INDONESIA; Customer#000057040). But the queries take only 0.15–0.3 s on the M2 (parallel workers), which isn't heavy. Hence SF 10 for the real run.
- The whole cloud flow passes in a fresh Ubuntu 24.04 container at SF 0.1 with the mock backend: setup, tests, load, missing-key refusal, mock sweep, idempotent re-setup.

**Planned cloud run:** SF 10, sequential, 4 Haiku agents, `direct` vs `aware`, 3 reps per task. Results go to `results/tpch-sf10/`.
