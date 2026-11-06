"""Single-flight (identical in-flight queries wait instead of re-running) and live catalog."""

import threading
import time

import psycopg
import pytest

import run_agents
from agentsql.agent import format_catalog
from agentsql.analyze import load
from agentsql.cache import SqlCache


def _parallel(cache, sqls):
    out = [None] * len(sqls)

    def go(i, q):
        try:
            out[i] = cache.run_sql(q)
        except Exception as e:  # noqa: BLE001
            out[i] = e

    ts = [threading.Thread(target=go, args=(i, q)) for i, q in enumerate(sqls)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in ts), "a caller hung"
    return out


def test_identical_concurrent_queries_execute_once():
    with SqlCache(mode="exact", db_delay_ms=300) as c:
        res = _parallel(c, ["SELECT count(*) AS n FROM orders"] * 4)
        assert c.stats.db_executions == 1
        assert c.stats.misses == 1 and c.stats.coalesced == 3
        assert sum(info.coalesced for _, info in res) == 3
        assert len({r for r, _ in res}) == 1  # identical results
        _, again = c.run_sql("SELECT count(*) AS n FROM orders")
        assert again.hit  # stored after the flight landed


def test_failed_flight_propagates_error_and_caches_nothing():
    with SqlCache(mode="exact", db_delay_ms=200) as c:
        res = _parallel(c, ["SELECT no_such_column FROM orders"] * 3)
        assert all(isinstance(r, psycopg.Error) for r in res)
        assert c.catalog(include_running=True) == []
        with pytest.raises(psycopg.Error):  # retry runs again, it isn't stuck
            c.run_sql("SELECT no_such_column FROM orders")


def test_direct_mode_has_no_single_flight():
    with SqlCache(mode="direct", db_delay_ms=200) as c:
        _parallel(c, ["SELECT 1 AS a"] * 3)
        assert c.stats.db_executions == 3 and c.stats.coalesced == 0


def test_catalog_shows_running_queries():
    with SqlCache(mode="exact", db_delay_ms=600) as c:
        t = threading.Thread(target=c.run_sql, args=("SELECT count(*) AS slow FROM order_items",))
        t.start()
        time.sleep(0.2)
        running = c.catalog(include_running=True)
        assert [e.running for e in running] == [True]
        assert c.catalog() == []  # default view: completed results only
        assert "[RUNNING" in format_catalog(running)
        t.join()
        assert [e.running for e in c.catalog(include_running=True)] == [False]


def test_live_mock_agents_in_parallel(tmp_path):
    path = str(tmp_path / "run.jsonl")
    run_agents.main(["--backend", "mock", "--agents", "4", "--scenario", "same", "--live",
                     "--schedule", "parallel", "--db-delay-ms", "300", "--log", path, "--quiet"])
    events = load(path)
    assert all(e["correct"] for e in events if e["type"] == "final")
    stats = next(e for e in events if e["type"] == "cache_stats")
    # identical SQL from 4 simultaneous agents: one execution per distinct statement
    distinct = len({e["sql"] for e in events if e["type"] == "tool"})
    assert stats["db_executions"] == distinct
    assert sum(e["type"] == "catalog" for e in events) == 8  # 2 checks per agent
