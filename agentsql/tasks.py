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
