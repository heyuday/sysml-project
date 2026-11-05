"""One broker per experiment, concurrent clients, one Redis result namespace."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from itertools import groupby
import json
import threading
import time
import uuid
from dataclasses import asdict

from fastapi import FastAPI, HTTPException
from psycopg_pool import ConnectionPool
from redis import Redis

from . import db
from .common import DSN, REDIS, append, encoded, ms, fingerprint
from .identity import analyze, CONTEXT
from .policies import Cache


class Broker:
    def __init__(self, cfg, dsn=DSN, redis_url=REDIS, log=None):
        self.log = log
        self.pool = ConnectionPool(
            dsn,
            min_size=1,
            max_size=80,
            timeout=120,
            kwargs={
                "autocommit": True,
                "options": "-c timezone=UTC -c search_path=pg_catalog,public -c datestyle=ISO,YMD -c max_parallel_workers_per_gather=0 -c statement_timeout=120000",
            },
        )
        with self.pool.connection() as c:
            self.catalog = db.catalog(c)
            self.pos = db.watermark(c)
            name, role = c.execute("SELECT current_database(),current_user").fetchone()
        self.context = {**CONTEXT, "database": name, "role": role}
        self.redis = Redis.from_url(redis_url)
        self.prefix = "sysml:" + uuid.uuid4().hex + ":"
        self.cache = Cache(cfg, lambda k: self.redis.delete(self.prefix + k))
        self.lock = threading.RLock()
        self.pending = {}
        self.workers = ThreadPoolExecutor(4, thread_name_prefix="refresh")
        self.jobs = set()
        self.done = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self.poll, daemon=True)
        self.thread.start()

    def sync(self, c, v):
        if v <= self.pos:
            return
        events = db.changes(c, self.pos, v)
        for _, batch in groupby(events, key=lambda e: e["tx"]):
            group = list(batch)
            start = time.perf_counter()
            _, keys = self.cache.changes(group, time.monotonic())
            for k in keys:
                job = self.workers.submit(self.refresh, k, self.cache.epoch, start)
                self.jobs.add(job)
                job.add_done_callback(self.finished)
            append(
                self.log,
                {
                    "type": "cdc",
                    "version": group[-1]["version"],
                    "selection_ms": ms(start),
                    "capture_to_apply_ms": 1000 * (time.time() - group[-1]["emitted"]),
                },
            )
        self.pos = v

    def finished(self, job):
        with self.lock:
            self.jobs.discard(job)

    def poll(self):
        while not self.done.wait(0.05):
            try:
                with self.pool.connection() as c, self.lock:
                    self.sync(c, db.watermark(c))
            except Exception as e:
                self.error = repr(e)
                self.done.set()

    def query(self, op, refresh=None):
        start = time.perf_counter()
        t0 = time.perf_counter()
        a = (
            self.cache.hist[refresh[0]].analysis
            if refresh
            else analyze(
                op["sql"],
                op.get("params", []),
                self.context,
                self.cache.cfg.normal,
                self.catalog,
            )
        )
        norm = ms(t0)
        spans = {
            "redis_get_ms": 0.0,
            "redis_set_ms": 0.0,
            "watermark_ms": 0.0,
            "cdc_catchup_ms": 0.0,
            "singleflight_wait_ms": 0.0,
        }
        k = None if self.cache.cfg.direct else a.key
        requested = False
        while True:
            owned = False
            wait = None
            try:
                with self.pool.connection() as c:
                    with c.transaction():
                        db.begin(c)
                        tick = time.perf_counter()
                        v = db.watermark(c)
                        spans["watermark_ms"] += ms(tick)
                        with self.lock:
                            if self.error:
                                raise RuntimeError(self.error)
                            if v > self.pos and not refresh:
                                self.cache.stats["reads_ahead_of_cdc"] += 1
                            tick = time.perf_counter()
                            self.sync(c, v)
                            spans["cdc_catchup_ms"] += ms(tick)
                            if refresh and (
                                self.cache.epoch != refresh[1]
                                or self.pos != v
                                or k in self.pending
                                or self.cache.valid(k)
                            ):
                                return None
                            if k and not requested and not refresh:
                                self.cache.access(
                                    a,
                                    {"sql": op["sql"], "params": op.get("params", [])},
                                    time.monotonic(),
                                )
                                requested = True
                            if k and self.pos == v and self.cache.valid(k):
                                tick = time.perf_counter()
                                raw = self.redis.get(self.prefix + k)
                                spans["redis_get_ms"] += ms(tick)
                                if raw is not None:
                                    cached = json.loads(raw)
                                    if self.cache.cfg.audit_hits:
                                        exact, audit_cost = db.execute(c, op)
                                        self.cache.stats["audit_db_ms"] += audit_cost
                                        if fingerprint(
                                            cached, a.ordered
                                        ) != fingerprint(exact, a.ordered):
                                            self.cache.stats["stale_hits"] += 1
                                            raise RuntimeError(
                                                "cached result differs from same-snapshot execution"
                                            )
                                    self.cache.hit(k)
                                    return self.response(
                                        op, a, cached, v, True, 0, start, norm, spans
                                    )
                                self.cache.remove(k, "redis_absent")
                            if k and self.pos == v:
                                wait = self.pending.get(k)
                                if wait is None:
                                    self.pending[k] = threading.Event()
                                    owned = True
                        if wait is None:
                            value, cost = db.execute(c, op)
                            raw = encoded(value)
                    if wait is None:
                        if owned:
                            with self.lock:
                                self.sync(c, db.watermark(c))
                                if self.pos == v and self.cache.publish(
                                    k, len(raw), cost, time.monotonic(), bool(refresh)
                                ):
                                    try:
                                        tick = time.perf_counter()
                                        self.redis.set(self.prefix + k, raw)
                                        spans["redis_set_ms"] += ms(tick)
                                    except Exception:
                                        self.cache.remove(k, "storage_error")
                                        raise
                                elif refresh:
                                    self.cache.stats["refresh_discarded"] += 1
                                    self.cache.stats["refresh_wasted_db_ms"] += cost
                        if refresh:
                            with self.lock:
                                self.cache.stats["refresh_runs"] += 1
                                self.cache.stats["refresh_db_ms"] += cost
                            append(
                                self.log,
                                {
                                    "type": "refresh",
                                    "key": k,
                                    "db_ms": cost,
                                    "delay_ms": ms(refresh[2]),
                                    "version": v,
                                },
                            )
                            return None
                        return self.response(
                            op, a, value, v, False, cost, start, norm, spans
                        )
            finally:
                if owned:
                    with self.lock:
                        self.pending.pop(k).set()
            tick = time.perf_counter()
            if not wait.wait(180):
                raise TimeoutError("single-flight owner timed out")
            spans["singleflight_wait_ms"] += ms(tick)

    def response(self, op, a, value, v, hit, cost, start, norm, spans):
        r = {
            "type": "read",
            "id": op.get("id"),
            "client": op.get("client"),
            "sql": op["sql"],
            "params": op.get("params", []),
            "database": self.context["database"],
            "arrival": op.get("at"),
            "state_scope": op.get("scope"),
            "key": a.key,
            "shape": a.shape,
            "eligible": bool(a.key),
            "reason": a.reason,
            "proposal": a.proposal,
            "hit": hit,
            "version": v,
            "db_ms": cost,
            "size": len(encoded(value)),
            "digest": fingerprint(value, a.ordered),
            "normal_ms": norm,
            **spans,
            "wait_ms": ms(start),
        }
        append(self.log, r)
        return {"result": value, "metrics": r}

    def refresh(self, k, epoch, start):
        try:
            self.query(self.cache.hist[k].request, (k, epoch, start))
        except Exception as e:
            append(self.log, {"type": "refresh_error", "key": k, "error": repr(e)})
            with self.lock:
                self.cache.stats["refresh_errors"] += 1

    def close(self):
        self.done.set()
        self.thread.join()
        self.workers.shutdown()
        with self.lock:
            for k in list(self.cache.entries):
                self.redis.delete(self.prefix + k)
        self.pool.close()
        self.redis.close()


def app(cfg, log):
    @asynccontextmanager
    async def lifespan(api):
        api.state.b = Broker(cfg, log=log)
        try:
            yield
        finally:
            api.state.b.close()

    api = FastAPI(lifespan=lifespan)

    @api.post("/read")
    def read(op: dict):
        # Refuse multiple statements even on the bypass path.
        from .identity import tree

        try:
            tree(op["sql"])
        except Exception as e:
            raise HTTPException(400, "single SELECT required") from e
        return api.state.b.query(op)

    @api.post("/write")
    def write(op: dict):
        b = api.state.b
        with b.pool.connection() as c:
            start = time.perf_counter()
            r = db.write(c, op)
            elapsed = ms(start)
            with b.lock:
                b.sync(c, db.watermark(c))
        record = {**op, **r, "type": "write", "db_ms": elapsed, "wait_ms": ms(start)}
        append(log, record)
        return record

    @api.get("/stats")
    def stats():
        from .simulation import memory

        b = api.state.b
        # After the last replay client joins, wait for outstanding refresh accounting.
        from concurrent.futures import wait

        with b.lock:
            jobs = list(b.jobs)
        wait(jobs)
        with b.lock:
            redis_bytes = sum(
                b.redis.memory_usage(b.prefix + k) or 0 for k in b.cache.entries
            )
            return {
                **b.cache.stats,
                "value_bytes": b.cache.used,
                "peak_value_bytes": b.cache.peak,
                "metadata_bytes_estimate": memory(b.cache),
                "redis_allocated_value_keys": redis_bytes,
                "version": b.pos,
                "cdc_error": b.error,
                "config": asdict(b.cache.cfg),
                "pending_unused_refreshes": sum(
                    e.proactive is not None for e in b.cache.entries.values()
                ),
            }

    return api
