"""Trace sources; no cache hints or future events are exposed to a policy."""

import json
import math
import random
import re
from pathlib import Path
from hashlib import sha256
from .common import ROOT

BENCH = ROOT / "vendor/benchbase"
PROC = BENCH / "src/main/java/com/oltpbenchmark/benchmarks/chbenchmark/queries"


def ch_query(number):
    src = (PROC / f"Q{number}.java").read_text(encoding="utf8")
    body = re.search(r"new SQLStmt\s*\((.*?)\);", src, re.S)
    if not body:
        raise ValueError(f"unrecognized BenchBase Q{number} template")
    literals = re.findall(r'"(?:\\.|[^"\\])*"', body.group(1))
    if not literals or re.sub(r'"(?:\\.|[^"\\])*"|\s|\+', "", body.group(1)):
        raise ValueError(f"BenchBase Q{number} requires a dialect adapter")
    return "".join(map(json.loads, literals))


def generate(
    n,
    seed,
    clients,
    write,
    skew,
    repetition,
    locality,
    pattern,
    mix,
    rate,
    variation=0,
    write_column="mixed",
):
    if clients < 1 or n < 1 or not 0 <= write <= 1 or rate <= 0 or skew < 0:
        raise ValueError("invalid workload parameters")
    rng = random.Random(seed)
    write_count = round(n * write)
    boundaries = list(range(0, n, 40)) if pattern == "burst" else []
    reserved = set(rng.sample(boundaries, min(write_count, len(boundaries))))
    writes = reserved | set(
        rng.sample(
            [i for i in range(n) if i not in reserved], write_count - len(reserved)
        )
    )
    domain = {"low": 10000, "medium": 1000, "high": 50}[repetition]
    keys = list(range(1, domain + 1))
    weights = [1 / (k**skew) for k in keys]
    import itertools, bisect

    cum = list(itertools.accumulate(weights))
    sqls = {i: ch_query(i) for i in (1, 6, 14)}
    at = 0.0
    previous = 0.0
    for i in range(n):
        at += rng.expovariate(rate)
        phase = i >= n // 2

        def popular():
            k = keys[bisect.bisect_left(cum, rng.random() * cum[-1])]
            return (
                1
                + (k - 1 + (domain // 2 if pattern == "shift" and phase else 0))
                % domain
            )

        k = popular()
        burst = pattern == "burst" and i % 40 < clients
        if burst:
            k = 1 + (i // 40) % domain
        base = {
            "id": str(i),
            "client": i % clients,
            "at": at if not burst else math.floor(at * 10) / 10,
            "database": "chbenchmark",
            "scope": "shared",
            "source": "controlled-ch",
            "params": [],
        }
        # Preserve monotone arrival times, including tied burst arrivals.
        at = max(at, 0)
        base["at"] = max(base["at"], previous)
        previous = base["at"]
        if i in writes:
            k = k if locality == "hot" else rng.randint(1, 100000)
            column = {
                "referenced": "s_quantity",
                "unreferenced": "s_ytd",
                "mixed": rng.choice(["s_quantity", "s_ytd"]),
            }[write_column]
            if write_column == "mixed" and not burst and rng.random() < 0.20:
                yield {
                    **base,
                    "kind": "write",
                    "template": "controlled-orderline-update",
                    "sql": "UPDATE order_line SET ol_amount=ol_amount+0.01 WHERE ol_w_id=1 AND ol_d_id=1 AND ol_o_id=$1",
                    "params": [1 + (k - 1) % 3000],
                }
                continue
            yield {
                **base,
                "kind": "write",
                "template": "controlled-stock-update",
                "sql": f"UPDATE stock SET {column}={column}+1 WHERE s_w_id=1 AND s_i_id=$1",
                "params": [k],
            }
        else:
            p = rng.random()
            analytical = {
                "cheap": 0.02,
                "mixed": 0.20,
                "expensive": 0.75,
                "disagreement": 0.08,
            }[mix]
            if mix == "disagreement" and p < analytical:
                sql = (
                    "SELECT sum(ol_amount) AS total FROM order_line WHERE ol_i_id >= $1"
                )
                params = [rng.randint(1, 100000)]
                label = "controlled-expensive-long-tail"
            elif mix == "disagreement":
                sql = "SELECT i_price FROM item WHERE i_id=$1"
                params = [1 + k % 5]
                label = "controlled-cheap-hot"
            elif p < analytical:
                q = rng.choice([1, 6, 14])
                sql = sqls[q]
                params = []
                label = f"BenchBase-Q{q}"
            elif p < analytical + (1 - analytical) * 0.12:
                sql = "SELECT sum(s_quantity) AS units FROM stock WHERE s_i_id >= $1"
                params = [k]
                label = "controlled-range"
            elif p < analytical + (1 - analytical) * 0.22:
                sql = "SELECT i_price,s_quantity FROM item JOIN stock ON i_id=s_i_id WHERE i_id=$1 AND s_w_id=1"
                params = [k]
                label = "controlled-join"
            else:
                sql = "SELECT s_quantity FROM stock WHERE s_i_id=$1"
                params = [k]
                label = "controlled-point"
            if rng.random() < variation:
                sql = sql.replace("SELECT ", "select\n  ", 1)
            yield {
                **base,
                "kind": "read",
                "template": label,
                "sql": sql,
                "params": params,
            }


def bird_file(path):
    content = Path(path).read_bytes()
    run_id = sha256(content).hexdigest()[:16]
    raw = json.loads(content.decode("utf-8-sig"))
    tasks = raw.get("results", raw.get("samples"))
    if not isinstance(tasks, list):
        raise ValueError("Expected BIRD runner results or bundled example samples")
    for r in tasks:
        task = r.get("instance_id") or r.get("task_id")
        epoch = 0
        for i, call in enumerate(r.get("tool_trajectory", [])):
            tool = call.get("tool")
            if tool not in {"execute_sql", "submit_sql"}:
                continue
            sql = call.get("args", {}).get("sql")
            if isinstance(sql, str):
                yield {
                    "id": f"{task}-{i}",
                    "client": task,
                    "order": i,
                    "at": None,
                    "database": r.get("database"),
                    "scope": f"{run_id}/{task}/{epoch}",
                    "kind": "read" if tool == "execute_sql" else "submission",
                    "successful": not str(call.get("result", ""))
                    .lower()
                    .startswith(("sql error", "error calling")),
                    "sql": sql,
                    "params": [],
                    "source": "bird-actual",
                    "tool": tool,
                    "db_ms": None,
                    "size": None,
                }
            if tool == "submit_sql":
                epoch += 1


def equivalence_pairs():
    base = "SELECT s_quantity AS qty FROM stock WHERE s_i_id=42"
    pairs = [
        ("keyword whitespace", base, base.replace("SELECT ", "select\n  "), True),
        ("unquoted name", base, base.replace("s_quantity", "S_QUANTITY"), True),
        ("parentheses", base, base.replace("s_i_id=42", "(s_i_id=42)"), True),
        (
            "predicate conjunction",
            "SELECT s_quantity FROM stock WHERE s_i_id=42 AND s_w_id=1",
            "SELECT s_quantity FROM stock WHERE s_w_id=1 AND s_i_id=42",
            True,
        ),
        ("primary-key non-null", base, base + " AND s_i_id IS NOT NULL", True),
        ("literal", base, base.replace("42", "43"), False),
        (
            "string",
            "SELECT i_name FROM item WHERE i_name='A'",
            "SELECT i_name FROM item WHERE i_name='a'",
            False,
        ),
        ("quoted identifier", base, base.replace("s_quantity", '"S_QUANTITY"'), False),
        ("output alias", base, base.replace(" AS qty", " AS quantity"), False),
        ("operator", base, base.replace("=42", ">42"), False),
        (
            "type metadata",
            base,
            base.replace("s_quantity AS", "s_quantity::bigint AS"),
            False,
        ),
        ("ordering", base + " ORDER BY 1 ASC", base + " ORDER BY 1 DESC", False),
        ("limit", base + " LIMIT 1", base + " LIMIT 2", False),
        (
            "postgres-specific",
            "SELECT DISTINCT ON (i_id) i_id FROM item",
            "SELECT DISTINCT i_id FROM item",
            True,
        ),
        (
            "bound parameter",
            "SELECT i_name FROM item WHERE i_id=$1",
            "SELECT i_name FROM item WHERE i_id=$1",
            False,
        ),
        ("session scope", base, base, False),
    ]
    for label, left, right, same in pairs:

        def side(sql, second):
            return {
                "sql": sql,
                "params": [42 + second] if label == "bound parameter" else [],
                "context": {
                    "database": "chbenchmark",
                    "scope": "other"
                    if second and label == "session scope"
                    else "shared",
                },
            }

        yield {
            "construct": label,
            "left": side(left, False),
            "right": side(right, True),
            "equivalent": same,
        }
