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
