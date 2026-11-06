"""Iteration 0: the smallest working SQL result cache.

One function matters: SqlCache.run_sql(sql) -> (Result, CallInfo).

Modes:
  direct - always execute against Postgres (baseline).
  exact  - cache results of eligible SELECTs, keyed on the exact SQL text.

No invalidation yet: in iteration 0 the cache is only correct while no one
writes to the tables it has cached. Writes bypass the cache but do not evict.

Thread safety: one connection per thread, and a lock around the in-memory
store only (see SqlCache). No single-flight yet (iteration 1).
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field

import psycopg

DEFAULT_DSN = os.environ.get(
    "AGENTSQL_DSN", "postgresql://bench:bench@127.0.0.1:55440/shop"
)
TPCH_DSN = os.environ.get(
    "AGENTSQL_TPCH_DSN", "postgresql://bench:bench@127.0.0.1:55440/tpch"
)
DSNS = {"shop": DEFAULT_DSN, "tpch": TPCH_DSN}

# Functions whose result can change between two executions of the same text.
_VOLATILE = re.compile(
    r"\b(random|now|clock_timestamp|statement_timestamp|transaction_timestamp|"
    r"timeofday|current_date|current_time|current_timestamp|localtime|"
    r"localtimestamp|nextval|setval|currval|gen_random_uuid|txid_current|"
    r"pg_sleep)\b",
    re.IGNORECASE,
)
_LOCKING = re.compile(r"\bfor\s+(update|share|no\s+key\s+update|key\s+share)\b", re.I)
_LEADING_COMMENTS = re.compile(r"^\s*(--[^\n]*\n|/\*.*?\*/\s*)*", re.S)


def is_cacheable(sql: str) -> tuple[bool, str]:
    """Conservative eligibility check. Returns (ok, reason)."""
    body = _LEADING_COMMENTS.sub("", sql).strip()
    if not re.match(r"select\b", body, re.I):
        return False, "not a plain SELECT"
    if ";" in body.rstrip(";"):
        return False, "multiple statements"
    if re.search(r"\binto\b", body, re.I):
        return False, "SELECT INTO"
    if _LOCKING.search(body):
        return False, "row locking"
    if _VOLATILE.search(body):
        return False, "volatile function"
    return True, "ok"


@dataclass(frozen=True)
class Result:
    columns: tuple[str, ...]
    rows: tuple[tuple, ...]
    rowcount: int  # rows returned, or rows affected for writes


@dataclass
class CallInfo:
    sql: str
    cacheable: bool
    reason: str
    hit: bool
    ms: float  # wall time of the run_sql call
    db_ms: float  # time spent executing in Postgres (0 on a hit or a coalesced wait)
    coalesced: bool = False  # waited for an identical in-flight execution instead of running it


@dataclass
class Stats:
    calls: int = 0
    hits: int = 0
    misses: int = 0
    bypass: int = 0
    coalesced: int = 0  # calls that waited on an identical in-flight query (single-flight)
    db_executions: int = 0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass(frozen=True)
class CatalogEntry:
    sql: str
    columns: tuple[str, ...]
    rowcount: int
    filled_at: float  # time.time() when stored, or when execution started if running
    hits: int
    running: bool = False  # still executing in another caller; rerunning it verbatim waits for it


class _Flight:
    """One in-flight execution that identical concurrent callers wait on."""

    def __init__(self):
        self.event = threading.Event()
        self.result: Result | None = None
        self.error: BaseException | None = None
        self.started = time.time()


class SqlCache:
    """Shared by many threads. Each thread gets its own Postgres connection;
    the lock guards only the in-memory state, so DB executions from different
    threads run concurrently.

    Single-flight (exact mode): if a cacheable query is already executing, an
    identical call waits for that execution's result instead of running it
    again. Errors are passed to the waiters and nothing is cached."""

    def __init__(self, dsn: str = DEFAULT_DSN, mode: str = "exact",
                 read_only: bool = False, db_delay_ms: float = 0.0,
                 statement_timeout_s: float = 0.0):
        if mode not in ("direct", "exact"):
            raise ValueError(f"unknown mode {mode!r}")
        self.mode = mode
        self.dsn = dsn
        self.db_delay_ms = db_delay_ms  # simulated extra DB latency, applied to real executions only
        opts = []
        if read_only:
            opts.append("-c default_transaction_read_only=on")
        if statement_timeout_s:  # protects runs from runaway agent SQL
            opts.append(f"-c statement_timeout={int(statement_timeout_s * 1000)}")
        self._conn_kwargs = {"options": " ".join(opts)} if opts else {}
        self._local = threading.local()
        self._conns: list[psycopg.Connection] = []
        self._lock = threading.Lock()
        self._store: dict[str, Result] = {}
        self._meta: dict[str, list] = {}  # sql -> [filled_at, hits]
        self._inflight: dict[str, _Flight] = {}
        self.stats = Stats()
        self.log: list[CallInfo] = []
        _ = self.conn  # connect eagerly so a bad DSN fails here

    @property
    def conn(self) -> psycopg.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = psycopg.connect(self.dsn, autocommit=True, **self._conn_kwargs)
            self._local.conn = c
            with self._lock:
                self._conns.append(c)
        return c

    # -- public API -------------------------------------------------------
    def run_sql(self, sql: str) -> tuple[Result, CallInfo]:
        t0 = time.perf_counter()
        cacheable, reason = is_cacheable(sql)
        use_cache = self.mode == "exact" and cacheable

        cached, flight, owner = None, None, False
        with self._lock:
            self.stats.calls += 1
            if use_cache:
                cached = self._store.get(sql)
                if cached is not None:
                    self.stats.hits += 1
                    self._meta[sql][1] += 1
                elif sql in self._inflight:
                    flight = self._inflight[sql]
                    self.stats.coalesced += 1
                else:
                    flight, owner = _Flight(), True
                    self._inflight[sql] = flight

        coalesced = False
        if cached is not None:
            result, hit, db_ms = cached, True, 0.0
        elif flight is not None and not owner:
            flight.event.wait()  # identical query already running: wait for it
            if flight.error is not None:
                raise flight.error
            result, hit, db_ms, coalesced = flight.result, False, 0.0, True
        else:
            try:
                result, db_ms = self._execute(sql)  # outside the lock
            except BaseException as e:
                if owner:
                    with self._lock:
                        self._inflight.pop(sql, None)
                    flight.error = e
                    flight.event.set()
                raise
            hit = False
            with self._lock:
                if use_cache:
                    self.stats.misses += 1
                    if sql not in self._store:
                        self._store[sql] = result
                        self._meta[sql] = [time.time(), 0]
                    self._inflight.pop(sql, None)
                else:
                    self.stats.bypass += 1
            if owner:
                flight.result = result
                flight.event.set()

        info = CallInfo(sql, cacheable, reason, hit,
                        (time.perf_counter() - t0) * 1000, db_ms, coalesced)
        with self._lock:
            self.log.append(info)
        return result, info

    def catalog(self, include_running: bool = False) -> list[CatalogEntry]:
        """What the cache holds, most recent first. With include_running, also
        queries currently executing (rerunning one verbatim waits for it)."""
        with self._lock:
            entries = [CatalogEntry(sql, r.columns, r.rowcount, *self._meta[sql])
                       for sql, r in self._store.items()]
            if include_running:
                entries += [CatalogEntry(sql, (), -1, f.started, 0, running=True)
                            for sql, f in self._inflight.items()]
        return sorted(entries, key=lambda e: e.filled_at, reverse=True)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._meta.clear()

    def close(self) -> None:
        with self._lock:
            for c in self._conns:
                c.close()
            self._conns.clear()
        self._local = threading.local()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- internals --------------------------------------------------------
    def _execute(self, sql: str) -> tuple[Result, float]:
        t0 = time.perf_counter()
        with self.conn.cursor() as cur:
            cur.execute(sql)
            if cur.description is not None:
                columns = tuple(d.name for d in cur.description)
                rows = tuple(tuple(r) for r in cur.fetchall())
                rowcount = len(rows)
            else:
                columns, rows, rowcount = (), (), cur.rowcount
        if self.db_delay_ms:
            time.sleep(self.db_delay_ms / 1000)
        with self._lock:
            self.stats.db_executions += 1
        return Result(columns, rows, rowcount), (time.perf_counter() - t0) * 1000
