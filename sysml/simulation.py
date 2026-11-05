"""Discrete-event fixed-order replay with calibrated direct execution costs (milliseconds)."""

from collections import Counter
from dataclasses import asdict
import heapq
import itertools
import sys
import time

from .common import ms, quantiles
from .identity import analyze
from .policies import Cache


def memory(cache):
    seen = set()

    def size(x):
        if id(x) in seen:
            return 0
        seen.add(id(x))
        total = sys.getsizeof(x)
        if isinstance(x, dict):
            total += sum(size(k) + size(v) for k, v in x.items())
        elif isinstance(x, (list, set, tuple)):
            total += sum(map(size, x))
        elif hasattr(x, "__dict__") and not callable(x):
            total += size(vars(x))
        return total

    return size({k: v for k, v in vars(cache).items() if k != "delete"})


def simulate(ops, catalog, cfg, warm=0.2, details=None, write_details=None):
    payload = {}
    cache = Cache(cfg, lambda k: payload.pop(k, None))
    pending = {}
    queue = []
    serial = itertools.count()
    lanes = [0.0] * 4
    stats = {p: Counter() for p in ("cold", "steady")}
    times = {p: [] for p in stats}
    norm_times = []
    raw = set()
    keys = set()
    split = int(len(ops) * warm)

    def clean_pending(k, token):
        if pending.get(k, (None, None))[1] == token:
            del pending[k]

    def finish(t):
        while queue and queue[0][0] <= t:
            end, token, k, epoch, op, cost, refresh, start = heapq.heappop(queue)
            clean_pending(k, token)
            if epoch != cache.epoch:
                if refresh:
                    cache.stats["refresh_discarded"] += 1
                    cache.stats["refresh_wasted_db_ms"] += cost
                continue
            if cache.publish(k, op["size"], cost, end, refresh):
                payload[k] = (op["digest"], op)
            if refresh:
                cache.stats["refresh_delay_ms_sum"] += 1000 * (end - start)

    def schedule(k, op, start, cost, refresh):
        token = next(serial)
        begin = start
        if refresh:
            lane = min(range(len(lanes)), key=lanes.__getitem__)
            begin = max(begin, lanes[lane])
            lanes[lane] = begin + cost / 1000
        end = begin + cost / 1000
        heapq.heappush(queue, (end, token, k, cache.epoch, op, cost, refresh, start))
        pending[k] = (end, token)
        return end

    for i, op in enumerate(ops):
        if op.get("at") is None or op.get("db_ms") is None:
            raise ValueError("timed replay requires calibrated costs and arrival times")
        t = op["at"]
        if i and t < ops[i - 1]["at"]:
            raise ValueError("arrival schedule must be ordered")
        finish(t)
        phase = "cold" if i < split else "steady"
        s = stats[phase]
        if op["kind"] == "write":
            invalidated, refresh = cache.changes(op["changes"], t)
            if write_details:
                write_details({"id":op["id"], "invalidated":sorted(invalidated), "refresh":list(refresh)})
            if op["changes"]:
                pending.clear()
            s["write_db_ms"] += op["db_ms"]
            s["writes"] += 1
            for k in refresh:
                # Subsequent contents may change after a write. Calibration records post-write
                # size/digest/cost for each optional eager refresh, separately from agent calls.
                original = cache.hist[k].request
                stamp = original["raw_id"]
                new = op.get("post", {}).get(stamp)
                if not new:
                    raise ValueError(
                        "refresh simulation requires --profile-refresh calibration"
                    )
                new = {**original, **new}
                schedule(k, new, t, new["db_ms"], True)
                s["refresh_db_ms"] += new["db_ms"]
                cache.stats["refresh_runs"] += 1
            continue
        start = time.perf_counter()
        schema = catalog["by_scope"][op["scope"]] if "by_scope" in catalog else catalog
        a = analyze(op["sql"], op.get("params", []), op["context"], cfg.normal, schema)
        if cfg.direct:
            a.key = None
        norm = ms(start)
        norm_times.append(norm)
        cost = op["db_ms"]
        raw.add(op["raw_id"])
        s["reads"] += 1
        s["baseline_db_ms"] += cost
        s["demand_bytes"] += op["size"]
        delay = 0
        hit = False
        if a.key:
            keys.add(a.key)
            cache.access(a, op, t)
        if a.key and cache.valid(a.key):
            hit = True
            cache.hit(a.key)
            s["hits"] += 1
            s["hit_bytes"] += op["size"]
            s["stale_hits"] += payload[a.key][0] != op["digest"]
        elif a.key and a.key in pending:
            s["coalesced"] += 1
            delay = max(0, (pending[a.key][0] - t) * 1000)
        else:
            s["misses"] += 1
            s["demand_db_ms"] += cost
            if not a.key:
                s["bypasses"] += 1
            delay = cost
            if a.key:
                schedule(a.key, op, t, cost, False)
        s["normal_ms"] += norm
        if op.get("version", 0) > 0:
            s["post_write_reads"] += 1
            s["post_write_hits"] += hit
        s["on_demand_recomputations"] += bool(
            a.key and not hit and cache.hist[a.key].invalidated and delay == cost
        )
        times[phase].append(delay + norm)
        if details:
            details(
                {
                    "id": op["id"],
                    "phase": phase,
                    "key": a.key,
                    "hit": hit,
                    "modeled_wait_ms": delay + norm,
                    "normal_ms": norm,
                }
            )
    finish(float("inf"))
    summary = {}
    for phase, s in stats.items():
        summary[phase] = {
            **s,
            "modeled_tool_wait_ms": sum(times[phase]),
            "latency_ms": quantiles(times[phase]),
            "hit_rate": s["hits"] / max(1, s["reads"]),
            "byte_hit_rate": s["hit_bytes"] / max(1, s["demand_bytes"]),
            "cost_weighted_miss_ratio": s["demand_db_ms"]
            / max(1e-12, s["baseline_db_ms"]),
            "avoided_db_ms": s["baseline_db_ms"]
            - s["demand_db_ms"]
            - s["refresh_db_ms"],
        }
    return {
        "mode": "offline-calibrated",
        "config": asdict(cfg),
        "phases": summary,
        "policy": dict(cache.stats),
        "peak_value_bytes": cache.peak,
        "end_value_bytes": cache.used,
        "metadata_bytes_estimate": memory(cache),
        "pending_unused_refreshes": sum(
            e.proactive is not None for e in cache.entries.values()
        ),
        "normalization_ms": quantiles(norm_times),
        "distinct_raw": len(raw),
        "distinct_eligible_keys": len(keys),
    }
