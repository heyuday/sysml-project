"""Analyst questions per dataset, and scenarios that assign them to agents.

Datasets:
  shop - the tiny seed DB (db/seed.sql); queries take milliseconds
  tpch - TPC-H loaded by scripts/load_tpch.py; queries take seconds at SF 1

Each task has several wordings of the same question. The reference SQL's first
row is the expected answer: an agent is correct if its final answer contains
any cell of that row (case-insensitive), e.g. a customer's name or its key.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Task:
    id: str
    wordings: tuple[str, ...]
    reference_sql: str  # first cell of the first row is the expected answer
    tables: tuple[str, ...]  # tables the reference SQL touches (used by the mock agent)


SHOP_TASKS = {
    "region_revenue": Task(
        "region_revenue",
        (
            "Which customer region generated the most revenue? Revenue is product price times quantity, summed over order items.",
            "Across all orders, which region brought in the highest sales (unit price x quantity)?",
            "Find the top region by total revenue, where revenue = sum of price * quantity over order items.",
            "Rank the customer regions by revenue (price times quantity) and tell me which one is first.",
        ),
        "SELECT c.region FROM orders o JOIN customers c ON c.id = o.customer_id "
        "JOIN order_items oi ON oi.order_id = o.id JOIN products p ON p.id = oi.product_id "
        "GROUP BY c.region ORDER BY sum(p.price * oi.quantity) DESC LIMIT 1",
        ("customers", "orders", "order_items", "products"),
    ),
    "category_units": Task(
        "category_units",
        (
            "Which product category has sold the most units in total?",
            "By total quantity ordered, what is the best-selling product category?",
            "Sum the quantities sold per product category. Which category is highest?",
            "Which category of products moves the most units across all orders?",
        ),
        "SELECT p.category FROM order_items oi JOIN products p ON p.id = oi.product_id "
        "GROUP BY p.category ORDER BY sum(oi.quantity) DESC LIMIT 1",
        ("order_items", "products"),
    ),
    "top_games_product": Task(
        "top_games_product",
        (
            "In the 'games' category, which product has the highest revenue (price times quantity)? Give the product name.",
            "What is the name of the top-earning games product, measured by price x quantity sold?",
            "Among products in the games category, which one generated the most revenue? Answer with its name.",
            "Find the games-category product with the largest total sales (unit price times quantity). What is it called?",
        ),
        "SELECT p.name FROM order_items oi JOIN products p ON p.id = oi.product_id "
        "WHERE p.category = 'games' GROUP BY p.name ORDER BY sum(p.price * oi.quantity) DESC LIMIT 1",
        ("order_items", "products"),
    ),
    "returns_region": Task(
        "returns_region",
        (
            "Which customer region has the most orders with status 'returned'?",
            "Count returned orders per customer region. Which region has the most?",
            "Where do most returned orders come from, by customer region?",
            "Which region's customers returned the most orders?",
        ),
        "SELECT c.region FROM orders o JOIN customers c ON c.id = o.customer_id "
        "WHERE o.status = 'returned' GROUP BY c.region ORDER BY count(*) DESC LIMIT 1",
        ("customers", "orders"),
    ),
}

# Classic TPC-H queries (Q5, Q10) with their standard parameters, narrowed to a
# single-row answer. Wordings spell out every condition, since the answer
# depends on them.
TPCH_TASKS = {
    "q5_nation_revenue": Task(
        "q5_nation_revenue",
        (
            "Among nations in the ASIA region, which nation had the highest local revenue in 1994? "
            "Count only line items where the customer and the supplier are in the same nation, for "
            "orders placed in 1994; revenue is extendedprice * (1 - discount).",
            "For orders placed during 1994, consider line items whose customer and supplier come from "
            "the same nation, restricted to nations in the ASIA region. Which nation has the largest "
            "total of extended price times (1 minus discount)?",
            "Which ASIA-region nation earned the most revenue (sum of l_extendedprice * (1 - l_discount)) "
            "from 1994 orders, counting only items supplied by a supplier located in the customer's own nation?",
            "Rank ASIA's nations by 1994 revenue from local suppliers (customer nation = supplier nation), "
            "with revenue = extended price after discount, using the order date for the year. Which is first?",
        ),
        "SELECT n_name FROM customer, orders, lineitem, supplier, nation, region "
        "WHERE c_custkey = o_custkey AND l_orderkey = o_orderkey AND l_suppkey = s_suppkey "
        "AND c_nationkey = s_nationkey AND s_nationkey = n_nationkey AND n_regionkey = r_regionkey "
        "AND r_name = 'ASIA' AND o_orderdate >= date '1994-01-01' AND o_orderdate < date '1995-01-01' "
        "GROUP BY n_name ORDER BY sum(l_extendedprice * (1 - l_discount)) DESC LIMIT 1",
        ("customer", "orders", "lineitem", "supplier", "nation", "region"),
    ),
    "q10_returned_customer": Task(
        "q10_returned_customer",
        (
            "Which customer lost us the most revenue through returned items on orders placed in the "
            "fourth quarter of 1993 (Oct 1 to Dec 31)? Returned items have return flag 'R'; lost revenue "
            "is extendedprice * (1 - discount). Give the customer's name.",
            "For orders placed between 1993-10-01 and 1993-12-31, sum extended price times (1 - discount) "
            "over line items with returnflag = 'R', per customer. Which customer has the largest total? Name them.",
            "Find the customer with the highest revenue from returned line items (l_returnflag = 'R', revenue = "
            "l_extendedprice * (1 - l_discount)) among orders dated in Q4 1993. What is the customer's name?",
            "Looking at Q4 1993 orders (by order date), whose returned items (flag R) account for the most "
            "discounted extended price? Answer with the customer name.",
        ),
        "SELECT c_name, c_custkey FROM customer, orders, lineitem "
        "WHERE c_custkey = o_custkey AND l_orderkey = o_orderkey "
        "AND o_orderdate >= date '1993-10-01' AND o_orderdate < date '1994-01-01' "
        "AND l_returnflag = 'R' GROUP BY c_custkey, c_name "
        "ORDER BY sum(l_extendedprice * (1 - l_discount)) DESC LIMIT 1",
        ("customer", "orders", "lineitem"),
    ),
    "q3_shipping_priority": Task(
        "q3_shipping_priority",
        (
            "Among orders from customers in the BUILDING market segment that were placed before 1995-03-15 "
            "and still had line items shipping after 1995-03-15, which order has the highest revenue from "
            "those later-shipping items (extendedprice * (1 - discount))? Give the order key.",
            "For BUILDING-segment customers, look at orders dated before March 15, 1995 whose line items "
            "ship after that date. Summing l_extendedprice * (1 - l_discount) over those line items, which "
            "order key comes out on top?",
            "Which order (by order key) has the most unshipped revenue as of 1995-03-15? Only count orders "
            "placed before that date by customers in market segment BUILDING, and only line items with a "
            "ship date after it; revenue = extended price after discount.",
            "Find the top order by potential revenue at 1995-03-15: BUILDING-segment customers, order date "
            "before that day, line items with ship date after it, revenue = sum(l_extendedprice * (1 - l_discount)). "
            "Answer with the order key.",
        ),
        "SELECT l_orderkey FROM customer, orders, lineitem "
        "WHERE c_mktsegment = 'BUILDING' AND c_custkey = o_custkey AND l_orderkey = o_orderkey "
        "AND o_orderdate < date '1995-03-15' AND l_shipdate > date '1995-03-15' "
        "GROUP BY l_orderkey, o_orderdate, o_shippriority "
        "ORDER BY sum(l_extendedprice * (1 - l_discount)) DESC, o_orderdate LIMIT 1",
        ("customer", "orders", "lineitem"),
    ),
    "q9_green_profit": Task(
        "q9_green_profit",
        (
            "For parts whose name contains 'green', which supplier nation made the most total profit in 1998? "
            "Profit per line item is extendedprice * (1 - discount) minus supplycost * quantity, using the "
            "partsupp supply cost for that part and supplier; the year is the order date's year.",
            "Considering only parts with 'green' in their name and orders placed in 1998, which nation's "
            "suppliers earned the largest profit, where profit = l_extendedprice * (1 - l_discount) - "
            "ps_supplycost * l_quantity (partsupp matched on part and supplier)?",
            "Which nation (of the supplier) had the highest 1998 profit on 'green' parts? Use order year 1998, "
            "p_name containing 'green', and profit = discounted extended price minus supply cost times quantity.",
            "Rank supplier nations by total 1998 profit from line items of parts named like '%green%'. "
            "Profit = l_extendedprice * (1 - l_discount) - ps_supplycost * l_quantity. Which nation is first?",
        ),
        "SELECT n_name FROM part, supplier, lineitem, partsupp, orders, nation "
        "WHERE s_suppkey = l_suppkey AND ps_suppkey = l_suppkey AND ps_partkey = l_partkey "
        "AND p_partkey = l_partkey AND o_orderkey = l_orderkey AND s_nationkey = n_nationkey "
        "AND p_name LIKE '%green%' AND o_orderdate >= date '1998-01-01' AND o_orderdate < date '1999-01-01' "
        "GROUP BY n_name ORDER BY sum(l_extendedprice * (1 - l_discount) - ps_supplycost * l_quantity) DESC LIMIT 1",
        ("part", "supplier", "lineitem", "partsupp", "orders", "nation"),
    ),
    "q11_german_stock_part": Task(
        "q11_german_stock_part",
        (
            "Across suppliers located in GERMANY, which part has the largest total stock value, where stock "
            "value is supplycost * availqty from partsupp? Give the part key.",
            "Sum ps_supplycost * ps_availqty per part over partsupp rows from German suppliers. Which part key "
            "has the highest total?",
            "Which part (by part key) represents the most inventory value held by suppliers in GERMANY? "
            "Inventory value = supply cost times available quantity.",
            "For suppliers whose nation is GERMANY, compute each part's total value of available stock "
            "(ps_supplycost * ps_availqty). Report the part key of the most valuable one.",
        ),
        "SELECT ps_partkey FROM partsupp, supplier, nation "
        "WHERE ps_suppkey = s_suppkey AND s_nationkey = n_nationkey AND n_name = 'GERMANY' "
        "GROUP BY ps_partkey ORDER BY sum(ps_supplycost * ps_availqty) DESC LIMIT 1",
        ("partsupp", "supplier", "nation"),
    ),
    "q18_large_order_customer": Task(
        "q18_large_order_customer",
        (
            "Among orders whose line items add up to a total quantity above 300, which order has the highest "
            "total price (o_totalprice)? Give the name of the customer who placed it.",
            "Consider only 'large' orders: total l_quantity across the order's line items greater than 300. "
            "Which customer placed the large order with the biggest o_totalprice? Name the customer.",
            "Find the most expensive order (by o_totalprice) among those with more than 300 units in total "
            "(sum of l_quantity). Who is the customer?",
            "Which customer owns the priciest order among orders whose summed line item quantity exceeds 300? "
            "Answer with the customer name.",
        ),
        "SELECT c_name, o_orderkey FROM customer, orders, lineitem "
        "WHERE o_orderkey IN (SELECT l_orderkey FROM lineitem GROUP BY l_orderkey HAVING sum(l_quantity) > 300) "
        "AND c_custkey = o_custkey AND o_orderkey = l_orderkey "
        "GROUP BY c_name, c_custkey, o_orderkey, o_orderdate, o_totalprice "
        "ORDER BY o_totalprice DESC, o_orderdate LIMIT 1",
        ("customer", "orders", "lineitem"),
    ),
    "q2_min_cost_supplier": Task(
        "q2_min_cost_supplier",
        (
            "Look at parts of size 15 whose type ends in 'BRASS'. For each such part, find the suppliers in the "
            "EUROPE region that offer it at the minimum supply cost available in EUROPE. Among all those "
            "minimum-cost offers, which supplier has the highest account balance? Give the supplier name.",
            "For size-15 parts with p_type like '%BRASS', consider only European suppliers (region EUROPE) whose "
            "ps_supplycost equals the lowest European supply cost for that part. Which of those suppliers has the "
            "largest s_acctbal? Name it.",
            "Which European supplier, offering a size-15 BRASS part at that part's cheapest supply cost within the "
            "EUROPE region, has the biggest account balance? Answer with the supplier's name.",
            "Find the minimum-cost European supplier for each part of size 15 and type ending in BRASS (minimum "
            "taken over suppliers in region EUROPE). Of these suppliers, which has the top account balance?",
        ),
        "SELECT s_name FROM part, supplier, partsupp, nation, region "
        "WHERE p_partkey = ps_partkey AND s_suppkey = ps_suppkey AND p_size = 15 AND p_type LIKE '%BRASS' "
        "AND s_nationkey = n_nationkey AND n_regionkey = r_regionkey AND r_name = 'EUROPE' "
        "AND ps_supplycost = (SELECT min(ps_supplycost) FROM partsupp, supplier, nation, region "
        "WHERE p_partkey = ps_partkey AND s_suppkey = ps_suppkey AND s_nationkey = n_nationkey "
        "AND n_regionkey = r_regionkey AND r_name = 'EUROPE') "
        "ORDER BY s_acctbal DESC, n_name, s_name, p_partkey LIMIT 1",
        ("part", "supplier", "partsupp", "nation", "region"),
    ),
    "q15_top_supplier": Task(
        "q15_top_supplier",
        (
            "Which supplier generated the most revenue from line items shipped in the first quarter of 1996 "
            "(ship date from 1996-01-01 up to but not including 1996-04-01)? Revenue is extendedprice * "
            "(1 - discount). Give the supplier's name.",
            "Sum l_extendedprice * (1 - l_discount) per supplier over line items with l_shipdate in "
            "[1996-01-01, 1996-04-01). Which supplier has the largest total? Name it.",
            "Who was the top supplier by discounted revenue for items shipped during Q1 1996? Answer with the "
            "supplier name.",
            "Rank suppliers by total revenue (extended price after discount) on line items shipped between "
            "January 1 and March 31, 1996. Which supplier is first?",
        ),
        "SELECT s_name, s_suppkey FROM supplier, (SELECT l_suppkey AS supplier_no, "
        "sum(l_extendedprice * (1 - l_discount)) AS total_revenue FROM lineitem "
        "WHERE l_shipdate >= date '1996-01-01' AND l_shipdate < date '1996-04-01' GROUP BY l_suppkey) r "
        "WHERE s_suppkey = supplier_no ORDER BY total_revenue DESC LIMIT 1",
        ("supplier", "lineitem"),
    ),
}

DATASETS = {"shop": SHOP_TASKS, "tpch": TPCH_TASKS}
TASKS = SHOP_TASKS  # backwards-compatible name


def assign(scenario: str, n_agents: int, dataset: str = "shop",
           task_id: str | None = None) -> list[tuple[Task, str]]:
    """Return one (task, wording) per agent.

    same       - every agent gets the identical question text
    paraphrase - same underlying question, a different wording per agent
    related    - different questions over the same DB (shared exploration only)
    mixed      - pairs of agents share a question, worded differently

    `task_id` picks the question for same/paraphrase (default: the dataset's first).
    """
    tasks = DATASETS[dataset]
    order = list(tasks)
    main = tasks[task_id or order[0]]
    out = []
    for i in range(n_agents):
        if scenario == "same":
            t, w = main, 0
        elif scenario == "paraphrase":
            t, w = main, i
        elif scenario == "related":
            t, w = tasks[order[i % len(order)]], 0
        elif scenario == "mixed":
            t, w = tasks[order[(i // 2) % len(order)]], i % 2
        else:
            raise ValueError(f"unknown scenario {scenario!r}")
        out.append((t, t.wordings[w % len(t.wordings)]))
    return out


SCENARIOS = ("same", "paraphrase", "related", "mixed")
