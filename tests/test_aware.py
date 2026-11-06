"""Cache catalog, simulated DB delay, concurrent misses, and cache-aware agents (mock)."""

import threading
import time

import run_agents
from agentsql.agent import format_catalog
from agentsql.analyze import load
from agentsql.cache import SqlCache


def test_catalog_lists_entries_with_hits():
    with SqlCache(mode="exact") as c:
        c.run_sql("SELECT count(*) AS n FROM orders")
        c.run_sql("SELECT count(*) AS n FROM orders")
        c.run_sql("SELECT region FROM customers GROUP BY region")
        cat = c.catalog()
        assert [e.sql for e in cat] == ["SELECT region FROM customers GROUP BY region",
                                        "SELECT count(*) AS n FROM orders"]
        assert cat[1].hits == 1 and cat[1].columns == ("n",) and cat[1].rowcount == 1


def test_catalog_text_hides_schema_lookups():
    with SqlCache(mode="exact") as c:
        c.run_sql("SELECT table_name FROM information_schema.tables")
        assert format_catalog(c.catalog()) == "(no cached queries yet)"
        c.run_sql("SELECT count(*) FROM orders")
        assert "SELECT count(*) FROM orders" in format_catalog(c.catalog())


def test_db_delay_applies_to_misses_not_hits():
    with SqlCache(mode="exact", db_delay_ms=150) as c:
        _, miss = c.run_sql("SELECT 1 AS a")
        _, hit = c.run_sql("SELECT 1 AS a")
        assert miss.ms >= 150 and hit.ms < 5


def test_concurrent_misses_are_not_serialized():
    with SqlCache(mode="exact", db_delay_ms=300) as c:
        threads = [threading.Thread(target=c.run_sql, args=(f"SELECT {i} AS x",)) for i in range(4)]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert time.perf_counter() - t0 < 0.9  # serialized would be >= 1.2 s
        assert c.stats.db_executions == 4 and len(c._conns) == 5  # main + 4 threads


def test_aware_mock_agents_see_earlier_results(tmp_path):
    path = str(tmp_path / "run.jsonl")
    run_agents.main(["--backend", "mock", "--agents", "3", "--scenario", "same", "--aware",
                     "--log", path, "--quiet"])
    events = load(path)
    seen = [e["n_entries"] for e in events if e["type"] == "catalog"]
    assert seen == [0, 1, 1]  # agent 0 finds nothing; later agents see the cached answer query
    assert all(e["correct"] for e in events if e["type"] == "final")
