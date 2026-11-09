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

## TPC-H SF 10 heavy-query run, cloud (2026-11-06)

**Where:** Claude Code cloud VM, 4 vCPU, 15 GB RAM. Load took about 3.5 min.
**Query times (warm):** Q5 about 5.3 s (answer INDIA, runner-up CHINA); Q10 about 4.3 s (answer Customer#001237537).
**Setup:** Haiku 5.5, 4 agents, sequential, `paraphrase`, no simulated delay, 3 reps per arm. Results are in `results/tpch-sf10/`.
**Key problem:** the cloud reserves `ANTHROPIC_API_KEY` for Claude Code's own login and doesn't pass it to commands. Fixed with the alias `AGENTSQL_ANTHROPIC_API_KEY`.

| task | arm | correct | follower wall s | follower LLM calls | follower LLM s | follower SQL s | reused/follower | DB s per run | makespan s |
|---|---|---|---|---|---|---|---|---|---|
| Q5 | direct | 12/12 | 10.9 ± 0.2 | 3.7 | 5.8 | 5.1 | 0 | 20.5 | 41.7 |
| Q5 | aware | 12/12 | **4.7 ± 0.1** | 3.0 | 4.7 | **0.0** | 1.00 | **5.1** | 23.0 |
| Q10 | direct | 12/12 | 9.1 ± 1.3 | 3.0 | 3.9 | 5.2 | 0 | 19.8 | 34.9 |
| Q10 | aware | 12/12 | **4.4 ± 0.1** | 3.0 | 4.4 | **0.0** | 1.00 | **4.3** | 23.3 |

**Findings:**
1. **Real queries, real savings.** Aware followers finished 57% (Q5) and 52% (Q10) faster. DB work per run fell 75–78%: only the leader's query executes. Every aware follower reused the cached query (9/9 per task), and all 48 answers were correct.
2. **The confound is resolved for Q10.** Direct and aware followers made the same number of LLM calls (3.0 vs 3.0), because direct followers ran their schema lookups alongside other calls in the same turns. Aware LLM time was even slightly higher (4.4 vs 3.9 s). So the whole 4.7 s per-follower saving is the cache hit (SQL 5.2 → 0 s). In Q5 about 1.1 s came from fewer turns and about 5.1 s from the hit.
3. **With heavy queries, SQL is about half of a follower's time** without the cache (5.1 of 10.9 s). Compare Aryan's BIRD measurement of about 3.6%. Whether caching matters is set by query cost.
4. **Overhead:** in Q10 the aware leader was 2.5 s slower (10.1 vs 7.6 s), from checking an empty cache. In Q5 there was no difference.

**Caveats:**
- Sequential is the best case: the leader finishes before followers start.
- n = 3 per arm, one model, two tasks with explicit wordings, one 4-vCPU VM.
- Tokens: Q5 aware used 16% fewer; Q10 aware used 7% more.

**Next:**
- Overlapping agents: staggered or parallel starts with heavy queries. Reuse will drop, so this is where single-flight (waiting on an in-flight query) matters.
- A retrieval-based catalog (top-k relevant entries) and the near-miss wrong-reuse test.

## Concurrent agents: design for the next cloud run (2026-11-06)

**Problem:** when agents overlap, a follower checks the cache before the leader's 5 s query has finished, finds nothing, and runs its own copy. The 2 s-delay staggered runs on the shop DB showed reuse falling to 0.33 per follower.

**Built:**
- **Single-flight in `SqlCache`** (exact mode): an identical cacheable query that's already executing is waited on, not re-run. Errors pass to the waiters and nothing is cached. Tested: 4 threads give 1 execution and 3 waits, failures don't hang, and direct mode has no single-flight.
- **`--live` (arm `aware-live`):** the catalog also lists queries that are RUNNING right now. Rerunning one verbatim waits for its result (via single-flight). The hint tells agents to re-check the catalog right before an expensive query, since the first check usually comes too early to help.
- **New metrics:**
  - `follower_waits`: waits on a running query;
  - `agent_wall`: mean time across all agents, the fairer measure under parallel starts;
  - `heavy_execs`: analytical queries that actually executed for ≥ 1 s.
- `cloud_run.sh concurrent <task>`: `parallel` + `staggered` (3 s gap) × `direct` / `aware` / `aware-live` × 3 reps.

**Design notes:**
- `aware-live` bundles two changes, RUNNING visibility and the re-check hint, to avoid another run. We can't separate their individual effects. The logs record when each catalog check happened and what it showed (`n_running`), which partly separates them.
- Single-flight is also active in the `aware` arm, but it only fires on identical SQL. Unaware or plain-aware agents rarely produce identical SQL, so the difference between arms comes from visibility.
- Mock dry run (simulated 1.5 s delay, identical mock SQL): direct ran 4 heavy executions, aware 1. That only checks the plumbing.

## TPC-H SF 10 concurrent agents, cloud (2026-11-06)

36 runs: Q5 and Q10 × `parallel` (all start at once) and `staggered` (3 s gap) × `direct` / `aware` / `aware-live` × 3 reps. Haiku 5.5, 4 agents, no simulated delay. **All 144 answers correct.** Results are in `results/tpch-sf10/concurrent-*`.

| task | schedule | arm | follower s | all-agent s | heavy runs/run | DB s/run | reused/follower | waits/follower |
|---|---|---|---|---|---|---|---|---|
| Q5 | parallel | direct | 20.2 | 19.7 | 4.0 | 57.2 | 0 | 0 |
| Q5 | parallel | aware | 19.9 | 18.6 | 4.0 | 49.6 | 0 | 0 |
| Q5 | parallel | aware-live | 19.8 | 19.6 | 4.0 | 57.4 | 0 | 0 |
| Q5 | staggered | direct | 13.4 | 12.5 | 4.0 | 30.7 | 0 | 0 |
| Q5 | staggered | aware | 8.1 | 8.2 | 2.3 | 12.7 | 0.56 | 0 |
| Q5 | staggered | aware-live | **7.5** | 8.8 | **2.0** | 14.2 | 0.67 | 0.56 |
| Q10 | parallel | direct | 18.1 | 18.3 | 5.7 | 57.2 | 0 | 0 |
| Q10 | parallel | aware | 17.7 | 17.7 | 4.0 | 46.9 | 0 | 0 |
| Q10 | parallel | aware-live | 16.7 | 16.6 | 4.0 | 48.7 | 0 | 0 |
| Q10 | staggered | direct | 9.1 | 8.9 | 4.0 | 20.5 | 0 | 0 |
| Q10 | staggered | aware | 8.7 | 9.3 | 3.0 | 16.8 | 0.33 | 0 |
| Q10 | staggered | aware-live | **4.1** | **5.3** | **1.0** | **4.5** | 1.00 | 0.33 |

**Findings:**
1. **Staggered starts: showing running queries works.**
   - Q10 aware-live: followers 9.1 → 4.1 s (−55%), heavy executions 4 → 1, DB time −78%. That matches the sequential result even though agents overlap. Plain `aware` barely helped there (0.33 reused per follower), because followers checked before the leader's query had *finished*.
   - Q5: aware-live −44% and aware −40%. The two are similar because Q5's leader is slower, so its result was often already complete when followers checked.
   - 6 of 12 aware-live catalog checks in staggered runs saw a RUNNING entry, and followers waited on it rather than recomputing.
2. **Parallel starts: no reuse in any arm.** Every agent checked the catalog once, about 0.8–1.3 s after start. The first heavy query only started at about 2.5–3.5 s, so there was never anything to see.
3. **The "re-check right before an expensive query" instruction was ignored.** Exactly 12 catalog checks per 12 agents in every arm and schedule: one each. The aware-live gains therefore come from RUNNING visibility plus single-flight waiting, not from re-checking. A prompt instruction alone doesn't make Haiku re-check.
4. **Contention is real:** 4 simultaneous 5 s queries on 4 vCPUs push follower SQL time to about 14 s, nearly 3× the sequential time.
5. **Cost:** aware arms use about 15–45% more input tokens.

**Implication / next design:** enforce the re-check in the system instead of asking for it. When an aware agent's `run_sql` would execute a new query, first return any cached or RUNNING entries it hasn't seen yet. The agent then reruns one verbatim or confirms its own. This targets the parallel case: the first heavy query starts about 0.5–1 s before the others.

## More TPC-H tasks for a larger batch (2026-11-08)

**Goal:** a bigger sample on the same architecture (sequential, parallel and staggered; direct, aware and aware-live). Nothing new in the design.

**Added tasks:** Q2, Q3, Q9, Q11, Q15 and Q18. Each is the standard TPC-H query with standard parameters, narrowed to a one-row answer (a name or key), with 4 wordings each.
- At SF 1 every answer matches the official TPC-H result: Q2 Supplier#000005359, Q3 order 2456423, Q15 Supplier#000008449, Q18 Customer#000128120 / 4722021.
- Every answer is unique (the runner-up differs).
- The answer check now ignores thousands separators, so an order key written as 2,456,423 still matches.

**SF 1 timings on the M2,** with expected cloud SF 10 times from the 12–26× ratio seen for Q5 and Q10:

| task | SF 1 | expected SF 10 |
|---|---|---|
| Q11 | 0.05 s | ~1 s |
| Q2 | 0.11 s | ~2 s |
| Q10 | 0.35 s | 4.3 s (measured) |
| Q15 | 0.22 s | ~4 s |
| Q5 | 0.2 s | 5.3 s (measured) |
| Q3 | 0.35 s | ~6 s |
| Q9 | 1.0 s | ~20 s |
| Q18 | 2.2 s | ~25–55 s, excluded from overlapping runs because of the 120 s timeout |

**Batch 2 plan** (new folder `results/tpch-sf10-b2/`, a fresh VM; Q5 and Q10 rerun as a reproducibility check against batch 1):
- sequential, direct vs aware, 5 reps × 7 tasks = 70 runs;
- concurrent, parallel + staggered × direct, aware and aware-live, 3 reps × 7 tasks = 126 runs.

The 7 tasks span about 1 s to 20 s per query, so the batch also shows **how the cache's benefit scales with query cost**.

## Batch 2 results (TPC-H SF 10, 7 tasks, cloud VM)

**Correctness:** 196 runs, 784 answers, all correct. No step failed. The cloud session used `~/.anthropic_key` because the environment variable wasn't set.

**Sequential:** on every task except Q2, all followers reused the leader's query. Follower time is about 3.5 s, three LLM calls, regardless of query cost.
- Follower speedup grows with query cost: Q11 17%, Q15 47%, Q10/Q5/Q3 62–67%, Q9 86%.
- DB work is down about 75% (Q9 66%, because its leader query alone is about 20 s of a 28 s total).
- Q5 and Q10 reproduce batch 1 within about 1 s.

**Q2 finding:** agents write Q2 as a CTE (`WITH ...`), and `is_cacheable` rejects those.
- Bypassed queries: 46/57 sequential, 80/105 concurrent. Every one starts with `WITH`.
- Reuse was 0.20 at best.
- So this is a cost of our conservative eligibility rule, not of agent behaviour. Next: accept read-only CTEs.
- The Q2 SQL errors (mostly "unterminated quoted identifier", 30 in all) didn't affect the final answers.

**Concurrent, staggered:** aware-live cuts heavy executions from about 4 to about 1, and DB time by 75–87%, on Q10, Q15, Q5, Q3 and Q9.
- Plain aware works only if the leader has finished by the time a follower looks.
- That was partly true on Q5, Q10 and Q15; it gave almost nothing on Q3 and Q9.

**Q2 outlier:** in the Q2 staggered aware-live run, rep 1 took 132 s because an uncached CTE query hit the 120 s statement timeout. The other reps took 16 s and 21 s.

**Concurrent, parallel:** about 0 reuse in every arm on every task (Q3 0.11). This is the same as batch 1: everyone checks at about 1 s, before any heavy query exists. That makes the enforced re-check (or a short wait before running a new heavy query) the top next step.
