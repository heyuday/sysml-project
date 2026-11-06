# agent-sql-bench

A small benchmark for caching the results of SQL queries made by LLM agents that share a PostgreSQL database. We build it in short iterations, getting correctness first and measuring performance later. [NOTES.md](NOTES.md) logs what each iteration built and found.

## Status

Iteration 0 is done: an exact-text result cache for single-threaded use, with no invalidation yet.

- `SqlCache.run_sql(sql)` in [agentsql/cache.py](agentsql/cache.py) has two modes:
  - `direct`: always runs the query on Postgres.
  - `exact`: caches eligible `SELECT` results, keyed on the exact SQL text.
- Writes, row locks, volatile functions (`now()`, `random()`), CTEs and multi-statement input are never cached.
- Known limitation: writes don't invalidate cached results yet, so stale reads are possible. [tests/test_cache.py](tests/test_cache.py) pins this down on purpose.

## Run

You need Docker and [uv](https://docs.astral.sh/uv/).

```sh
docker compose up -d --wait   # Postgres 16 on 127.0.0.1:55440, seeded from db/seed.sql
uv sync
uv run pytest -q              # tests expect the database to be running
uv run python demo.py         # runs 10 queries twice and prints hit/miss and latency
```

The seed database is small and fixed: 500 customers, 100 products, 5,000 orders and 15,000 order items. Set `AGENTSQL_DSN` to point at a different database. The credentials in `compose.yaml` are for the local container only.

## Next

1. Concurrent clients and merging identical in-flight queries.
2. Invalidation when data changes (TTL vs. per-table version counters).
3. Real LLM agents on the tiny database, to check which queries actually repeat.
4. Query normalization, if the agent logs show it's needed.
5. Measurement at larger scale.
