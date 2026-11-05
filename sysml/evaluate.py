import random
import statistics
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path

import httpx
import psycopg
from . import db
from .common import (
    DSN,
    encoded,
    digest,
    fingerprint,
    append,
    fresh,
    ms,
    quantiles,
    records,
)
from .identity import analyze, CONTEXT
from .policies import Cache, Config
from .workloads import equivalence_pairs


def calibrate(ops, out, dsn=DSN, profile=False, oracle=None):
    previous = {}
    with db.connect(dsn) as c:
        cat = db.catalog(c)
        if not cat:
            raise ValueError("CDC catalog is empty")
        name, role = c.execute("select current_database(),current_user").fetchone()

        def direct(op):
            a = analyze(op["sql"], op.get("params", []), op["context"], "N0", cat)
            with c.transaction():
                db.begin(c)
                v = db.watermark(c)
                value, cost = db.execute(c, op)
            return {
                "version": v,
                "db_ms": cost,
                "size": len(encoded(value)),
                "digest": fingerprint(value, a.ordered),
            }

        out = fresh(out)
        Path(str(out) + ".catalog.json").write_bytes(encoded(cat))
        oracle_file = fresh(oracle) if oracle else None
        for op in ops:
            if op["database"] != "chbenchmark":
                raise ValueError("CH calibration requires a dedicated CH clone")
            if op["kind"] == "read":
                ctx = {
                    **CONTEXT,
                    "role": role,
                    "database": op["database"],
                    "scope": op["scope"],
                }
                rid = digest([op["sql"], op.get("params", []), ctx])
                op = {**op, "context": ctx, "raw_id": rid}
                observed = direct(op)
                a = analyze(op["sql"], op.get("params", []), ctx, "N0", cat)
                if a.key:
                    previous[rid] = (op, a)
                append(out, {**op, **observed})
            elif op["kind"] == "write":
                before = (
                    {k: direct(q) for k, (q, _) in previous.items()}
                    if oracle_file
                    else {}
                )
                start = time.perf_counter()
                commit = db.write(c, op)
                elapsed = ms(start)
                post = (
                    {k: direct(q) for k, (q, _) in previous.items()}
                    if profile or oracle_file
                    else {}
                )
                if before:
                    if any(
                        x["version"] != commit["version"] for x in post.values()
                    ) or any(x["version"] != commit["before"] for x in before.values()):
                        raise RuntimeError("external writer during controlled oracle")
                    actual = {
                        k for k in before if before[k]["digest"] != post[k]["digest"]
                    }
                    for policy in ("I0", "I1", "I2", "I3", "I4"):
                        cache = Cache(Config(invalid=policy))
                        for k, (q, a) in previous.items():
                            cache.access(a, q, 0)
                            cache.publish(a.key, 1, 1, 0)
                        d = time.perf_counter()
                        selected, _ = cache.changes(commit["changes"], 1)
                        write_decision = ms(d)
                        revision_check_ms = 0.0
                        if policy == "I4":
                            check_started = time.perf_counter()
                            selected = {
                                a.key
                                for _, a in previous.values()
                                if not cache.valid(a.key)
                            }
                            revision_check_ms = ms(check_started)
                        guessed = {
                            k for k, (_, a) in previous.items() if a.key in selected
                        }
                        shapes = {}
                        for k, (_, a) in previous.items():
                            s = shapes.setdefault(a.shape, Counter())
                            s["queries"] += 1
                            s["changed"] += k in actual
                            s["missed"] += k in actual - guessed
                            s["unnecessary"] += k in guessed - actual
                        append(
                            oracle_file,
                            {
                                "write": op["id"],
                                "policy": policy,
                                "actual": sorted(actual),
                                "selected": sorted(guessed),
                                "missed": sorted(actual - guessed),
                                "unnecessary": sorted(guessed - actual),
                                "precision": len(guessed & actual) / len(guessed)
                                if guessed
                                else None,
                                "decision_ms": write_decision + revision_check_ms,
                                "write_decision_ms": write_decision,
                                "oracle_revision_check_ms": revision_check_ms,
                                "oracle_revision_checks": len(previous) if policy == "I4" else 0,
                                "examined": cache.stats["examined"],
                                "by_shape": shapes,
                            },
                        )
                append(out, {**op, **commit, "db_ms": elapsed, "post": post})
            else:
                raise ValueError("CH calibration accepts reads/writes only")


def normalization(ops, cat, policies, dsns=(), controlled=False):
    # DSNs must be independent snapshots of the logical database and session scope.
    ops = list(ops)
    for policy in policies:
        groups = defaultdict(dict)
        raw_seen = set()
        raw_hits = gained = eligible = 0
        timings = []
        reasons = Counter()
        proposals = Counter()
        primary = Counter()
        for op in ops:
            if op.get("kind") != "read" or op.get("successful") is False:
                continue
            ctx = op.get(
                "context", {**CONTEXT, "database": op["database"], "scope": op["scope"]}
            )
            params = op.get("params", [])
            start = time.perf_counter()
            a = analyze(op["sql"], params, ctx, policy, cat)
            timings.append(ms(start))
            if not a.key:
                reasons[a.reason] += 1
                continue
            eligible += 1
            rid = digest([op["sql"], params, ctx])
            if rid in raw_seen:
                raw_hits += 1
            elif a.key in groups:
                gained += 1
                if op.get("db_ms") is not None:
                    primary["additional_avoided_db_ms"] += op["db_ms"]
                    primary["additional_hits_with_cost"] += 1
            raw_seen.add(rid)
            groups[a.key][rid] = (op, a)
            if a.proposal:
                proposals[a.proposal] += 1
        collisions = []
        checked = 0
        pair_results = []

        def observe(c, op, a):
            try:
                with c.transaction():
                    value, _ = db.execute(c, op)
                    return ("result", fingerprint(value, a.ordered))
            except psycopg.Error as exc:
                return ("error", exc.sqlstate)

        with ExitStack() as stack:
            conns = [stack.enter_context(db.connect(dsn)) for dsn in dsns]
            for c in conns:
                stack.enter_context(c.transaction())
                db.begin(c)
            for k, g in groups.items():
                if len(g) < 2:
                    continue
                for snap, c in enumerate(conns):
                    values = set()
                    for op, a in g.values():
                        values.add(observe(c, op, a))
                    checked += 1
                    if len(values) > 1:
                        collisions.append({"key": k, "snapshot": snap})
            if controlled:
                for pair in equivalence_pairs():
                    sides = [pair["left"], pair["right"]]
                    analyses = [
                        analyze(v["sql"], v["params"], v["context"], policy, cat)
                        for v in sides
                    ]
                    merged = bool(
                        analyses[0].key and analyses[0].key == analyses[1].key
                    )
                    findings = []
                    for c in conns:
                        if sides[0]["context"] != sides[1]["context"]:
                            continue
                        values = [observe(c, v, a) for v, a in zip(sides, analyses)]
                        findings.append(
                            {
                                "same_observation": values[0] == values[1],
                                "status": [v[0] for v in values],
                            }
                        )
                    pair_results.append(
                        {
                            "construct": pair["construct"],
                            "merged": merged,
                            "expected": pair["equivalent"],
                            "false_equivalence": merged and not pair["equivalent"],
                            "snapshots": findings,
                        }
                    )
        yield {
            "policy": policy,
            "requests": len(timings),
            "eligible": eligible,
            "coverage": eligible / max(1, len(timings)),
            "raw_distinct_eligible": len(raw_seen),
            "keys": len(groups),
            "additional_hits": gained,
            "raw_hits": raw_hits,
            "additional_hit_rate": gained / max(1, len(timings)),
            "normal_ms": quantiles(timings),
            "total_normal_ms": sum(timings),
            "unsupported": dict(reasons),
            "proposals": dict(proposals),
            "cost_observations": primary,
            "snapshot_group_checks": checked,
            "collisions": collisions,
            "controlled_pairs": pair_results,
        }


def live_replay(ops, base, out, clients):
    out = fresh(out)
    ops = list(ops)
    if not ops:
        raise ValueError("empty trace")
    lanes = defaultdict(list)
    for op in ops:
        lanes[op["client"]].append(op)
    if len(lanes) > clients:
        raise ValueError("client limit smaller than number of trace clients")
    first = ops[0]["at"]
    def lane(client, items, start):
        for op in items:
            due = start + op["at"] - first
            time.sleep(max(0, due - time.perf_counter()))
            started = time.perf_counter()
            response = client.post("/" + op["kind"], json=op)
            response.raise_for_status()
            body = response.json()
            finished = time.perf_counter()
            append(out, {
                **body.get("metrics", body),
                "client_wait_ms": 1000 * (finished - started),
                "arrival_queue_ms": 1000 * (started - due),
                "schedule_to_completion_ms": 1000 * (finished - due),
            })

    with ExitStack() as stack, ThreadPoolExecutor(clients) as pool:
        connections = []
        for _ in lanes:
            client = stack.enter_context(httpx.Client(base_url=base, timeout=240, trust_env=False))
            client.get("/stats").raise_for_status()
            connections.append(client)
        # Connection setup is outside the trace. perf_counter uses the high-resolution
        # performance counter on Windows Python versions whose monotonic clock is coarse.
        start = time.perf_counter()
        jobs = [pool.submit(lane, c, ops, start) for c, ops in zip(connections, lanes.values())]
        for job in jobs:
            job.result()
    with httpx.Client(base_url=base, timeout=240, trust_env=False) as client:
        response = client.get("/stats")
        response.raise_for_status()
        Path(str(out) + ".stats.json").write_bytes(encoded(response.json()))


def summarize(paths):
    groups = defaultdict(dict)
    for path in paths:
        for r in records(path):
            if "seed" not in r:
                raise ValueError(
                    "aggregate seeded matrix results, not individual request logs"
                )
            comparable = dict(r["config"])
            scenario = {k: v for k, v in r["scenario"].items() if k != "seed"}
            if "capacity_fraction" in scenario:
                comparable["capacity"] = scenario["capacity_fraction"]
            key = digest([comparable, scenario, r["split"], r["track"]])
            if r["seed"] in groups[key]:
                raise ValueError("duplicate seed within experimental cell")
            groups[key][r["seed"]] = r
    rng = random.Random(23)
    for xs in groups.values():
        runs = list(xs.values())
        first = runs[0]
        vals = [r["phases"]["steady"]["avoided_db_ms"] for r in runs]
        boot = (
            [statistics.mean(rng.choices(vals, k=len(vals))) for _ in range(2000)]
            if len(vals) > 1
            else []
        )
        boot.sort()
        yield {k: first[k] for k in ("track", "split", "config", "scenario")} | {
            "seeds": sorted(xs),
            "mean_avoided_db_ms": statistics.mean(vals),
            "bootstrap_95pct": [boot[49], boot[1949]] if boot else None,
        }
