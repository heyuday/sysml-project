"""Analyst questions about the seed `shop` DB, and scenarios that assign them to agents.

Each task has several wordings of the same question. The expected answer is a
single name (unique in the seed data, checked 2026-11-06), so grading is a
case-insensitive substring check on the agent's final answer.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Task:
    id: str
    wordings: tuple[str, ...]
    reference_sql: str  # first cell of the first row is the expected answer
    tables: tuple[str, ...]  # tables the reference SQL touches (used by the mock agent)


TASKS = {
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

_ORDER = list(TASKS)


def assign(scenario: str, n_agents: int) -> list[tuple[Task, str]]:
    """Return one (task, wording) per agent.

    same       - every agent gets the identical question text
    paraphrase - same underlying question, a different wording per agent
    related    - different questions over the same DB (shared exploration only)
    mixed      - pairs of agents share a question, worded differently
    """
    out = []
    for i in range(n_agents):
        if scenario == "same":
            t, w = TASKS["region_revenue"], 0
        elif scenario == "paraphrase":
            t, w = TASKS["region_revenue"], i
        elif scenario == "related":
            t, w = TASKS[_ORDER[i % len(_ORDER)]], 0
        elif scenario == "mixed":
            t, w = TASKS[_ORDER[(i // 2) % len(_ORDER)]], i % 2
        else:
            raise ValueError(f"unknown scenario {scenario!r}")
        out.append((t, t.wordings[w % len(t.wordings)]))
    return out


SCENARIOS = ("same", "paraphrase", "related", "mixed")
