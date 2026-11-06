import pytest

from agentsql.cache import SqlCache, is_cacheable
from conftest import db_calls


def test_repeat_select_hits_and_matches(cache):
    sql = "SELECT region, count(*) FROM customers GROUP BY region ORDER BY region"
    r1, i1 = cache.run_sql(sql)
    r2, i2 = cache.run_sql(sql)
    assert (i1.hit, i2.hit) == (False, True)
    assert r1 == r2
    assert len(r1.rows) == 4 and r1.columns == ("region", "count")
    assert cache.stats.db_executions == 1


def test_different_select_misses(cache):
    _, a = cache.run_sql("SELECT count(*) FROM orders WHERE status = 'shipped'")
    _, b = cache.run_sql("SELECT count(*) FROM orders WHERE status = 'returned'")
    assert not a.hit and not b.hit
    assert cache.stats.misses == 2


def test_exact_text_means_formatting_variants_miss(cache):
    # Documents a deliberate limitation of exact-text keys (iteration 4 may fix).
    cache.run_sql("SELECT count(*) FROM orders")
    _, info = cache.run_sql("select count(*) from orders")
    assert not info.hit


def test_postgres_executes_once(admin, cache):
    admin.execute("SELECT pg_stat_statements_reset()")
    sql = "SELECT count(*) AS probe_exec_once FROM order_items WHERE quantity > 2"
    for _ in range(5):
        cache.run_sql(sql)
    assert db_calls(admin, "probe_exec_once") == 1
    assert cache.stats.hits == 4


def test_direct_mode_never_caches(admin):
    admin.execute("SELECT pg_stat_statements_reset()")
    with SqlCache(mode="direct") as c:
        for _ in range(3):
            _, info = c.run_sql("SELECT count(*) AS probe_direct FROM products")
            assert not info.hit
    assert db_calls(admin, "probe_direct") == 3


def test_cached_results_equal_direct(cache):
    queries = [
        "SELECT * FROM products ORDER BY id LIMIT 5",
        "SELECT c.region, sum(p.price * oi.quantity) AS revenue "
        "FROM orders o JOIN customers c ON c.id = o.customer_id "
        "JOIN order_items oi ON oi.order_id = o.id "
        "JOIN products p ON p.id = oi.product_id GROUP BY c.region ORDER BY c.region",
        "SELECT status, min(ordered_at), max(ordered_at) FROM orders GROUP BY status ORDER BY status",
    ]
    with SqlCache(mode="direct") as direct:
        for q in queries:
            cache.run_sql(q)  # fill
            cached, info = cache.run_sql(q)
            fresh, _ = direct.run_sql(q)
            assert info.hit
            assert cached == fresh


def test_writes_bypass_cache(cache):
    cache.run_sql("CREATE TEMP TABLE scratch (x int)")
    r, info = cache.run_sql("INSERT INTO scratch VALUES (1), (2)")
    assert not info.cacheable and not info.hit and r.rowcount == 2
    _, again = cache.run_sql("INSERT INTO scratch VALUES (1), (2)")
    assert not again.hit
    assert cache.stats.bypass == 3 and cache.stats.hits == 0


def test_known_gap_no_invalidation(cache):
    """Iteration 0 has NO invalidation: a write leaves a stale cached value.
    Iteration 2 must flip this test (second read should see 3 rows)."""
    cache.run_sql("CREATE TEMP TABLE scratch2 (x int)")
    cache.run_sql("INSERT INTO scratch2 VALUES (1), (2)")
    before, _ = cache.run_sql("SELECT count(*) FROM scratch2")
    cache.run_sql("INSERT INTO scratch2 VALUES (3)")
    after, info = cache.run_sql("SELECT count(*) FROM scratch2")
    assert info.hit and after.rows == before.rows == ((2,),)  # stale!


@pytest.mark.parametrize("sql, ok", [
    ("SELECT 1", True),
    ("  -- comment\n select * from orders", True),
    ("/* c */ SELECT * FROM orders;", True),
    ("WITH x AS (SELECT 1) SELECT * FROM x", False),  # conservative for now
    ("SELECT * FROM orders FOR UPDATE", False),
    ("SELECT random()", False),
    ("SELECT now()", False),
    ("SELECT * INTO t2 FROM orders", False),
    ("SELECT 1; DELETE FROM orders", False),
    ("INSERT INTO orders VALUES (1)", False),
    ("UPDATE orders SET status = 'x'", False),
    ("EXPLAIN SELECT 1", False),
])
def test_eligibility(sql, ok):
    assert is_cacheable(sql)[0] is ok
