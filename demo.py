"""Iteration 0 demo: run 10 queries twice, print hit/miss and latency per call."""

from agentsql import SqlCache

QUERIES = [
    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY 1",
    "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'orders'",
    "SELECT * FROM customers LIMIT 5",
    "SELECT count(*) FROM orders",
    "SELECT DISTINCT region FROM customers",
    "SELECT status, count(*) FROM orders GROUP BY status",
    "SELECT min(ordered_at), max(ordered_at) FROM orders",
    "SELECT p.category, sum(oi.quantity) FROM order_items oi JOIN products p ON p.id = oi.product_id GROUP BY 1 ORDER BY 2 DESC",
    "SELECT c.region, sum(p.price * oi.quantity) AS revenue FROM orders o JOIN customers c ON c.id = o.customer_id JOIN order_items oi ON oi.order_id = o.id JOIN products p ON p.id = oi.product_id GROUP BY 1 ORDER BY 2 DESC",
    "SELECT customer_id, count(*) FROM orders GROUP BY 1 ORDER BY 2 DESC LIMIT 10",
]


def main() -> None:
    with SqlCache(mode="exact") as cache:
        print(f"{'pass':<5}{'hit':<6}{'ms':>8}{'db_ms':>8}{'rows':>6}  sql")
        for p in (1, 2):
            for q in QUERIES:
                r, i = cache.run_sql(q)
                print(f"{p:<5}{str(i.hit):<6}{i.ms:8.2f}{i.db_ms:8.2f}{r.rowcount:6}  {q[:60]}")
        print("\nstats:", cache.stats.as_dict())
        miss = [i.ms for i in cache.log if not i.hit]
        hit = [i.ms for i in cache.log if i.hit]
        print(f"mean miss {sum(miss)/len(miss):.2f} ms | mean hit {sum(hit)/len(hit):.3f} ms")


if __name__ == "__main__":
    main()
