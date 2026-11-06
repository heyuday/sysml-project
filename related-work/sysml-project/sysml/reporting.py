"""Live SQL-level comparison with a separately recorded direct replay of the same schedule."""

from collections import Counter
from .common import quantiles


def compare_live(reference, trial, broker_stats, warm=0.2):
    base = {r["id"]: r for r in reference}
    test = {r["id"]: r for r in trial}
    if set(base) != set(test) or not base:
        raise ValueError("runs must contain the same nonempty request ID set")
    ordered = sorted(
        test, key=lambda k: (test[k].get("arrival", test[k].get("at", 0)), str(k))
    )
    report = {}
    split = int(len(ordered) * warm)
    for name, keys in [("cold", ordered[:split]), ("steady", ordered[split:])]:
        counters = Counter()
        latency = []
        for k in keys:
            a, b = base[k], test[k]
            if (
                a["sql"] != b["sql"]
                or a.get("params", []) != b.get("params", [])
                or a["type"] != b["type"]
            ):
                raise ValueError("request ID reused for different work")
            counters["direct_wait_ms"] += a["schedule_to_completion_ms"]
            counters["tool_wait_ms"] += b["schedule_to_completion_ms"]
            latency.append(b["schedule_to_completion_ms"])
            if b["type"] != "read":
                counters["writes"] += 1
                counters["write_ms"] += b["db_ms"]
                continue
            if a["hit"]:
                raise ValueError("reference must use direct mode")
            counters["reads"] += 1
            counters["hits"] += b["hit"]
            counters["bytes"] += b["size"]
            counters["hit_bytes"] += b["size"] * b["hit"]
            counters["normal_ms"] += b["normal_ms"]
            counters["direct_db_ms"] += a["db_ms"]
            counters["demand_db_ms"] += b["db_ms"]
            if b["digest"] != a["digest"]:
                counters["different_results_between_schedules"] += 1
        report[name] = {
            **counters,
            "latency_ms": quantiles(latency),
            "hit_rate": counters["hits"] / max(1, counters["reads"]),
            "byte_hit_rate": counters["hit_bytes"] / max(1, counters["bytes"]),
            "wait_saved_ms": counters["direct_wait_ms"] - counters["tool_wait_ms"],
            "cost_weighted_miss_ratio": counters["demand_db_ms"]
            / max(1e-12, counters["direct_db_ms"]),
        }
    saved = sum(
        x.get("direct_db_ms", 0) - x.get("demand_db_ms", 0) for x in report.values()
    )
    return {
        "mode": "live-postgres-redis",
        "phases": report,
        "broker": broker_stats,
        "avoided_read_db_ms_after_refresh": saved
        - broker_stats.get("refresh_db_ms", 0),
    }
