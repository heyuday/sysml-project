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
    db_ms: float  # time spent executing in Postgres (0 on a hit)


@dataclass
class Stats:
    calls: int = 0
    hits: int = 0
    misses: int = 0
    bypass: int = 0
    db_executions: int = 0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass(frozen=True)
class CatalogEntry:
    sql: str
    columns: tuple[str, ...]
    rowcount: int
    filled_at: float  # time.time() when the result was stored
    hits: int


class SqlCache:
    """Shared by many threads. Each thread gets its own Postgres connection;
    the lock guards only the in-memory store and counters, so DB executions
    from different threads run concurrently. Two threads that miss on the same
    key at the same time both execute it (no single-flight yet)."""

    def __init__(self, dsn: str = DEFAULT_DSN, mode: str = "exact",
                 read_only: bool = False, db_delay_ms: float = 0.0):
        if mode not in ("direct", "exact"):
            raise ValueError(f"unknown mode {mode!r}")
        self.mode = mode
        self.dsn = dsn
        self.db_delay_ms = db_delay_ms  # simulated extra DB latency, applied to real executions only
        self._conn_kwargs = (
            {"options": "-c default_transaction_read_only=on"} if read_only else {})
        self._local = threading.local()
        self._conns: list[psycopg.Connection] = []
        self._lock = threading.Lock()
        self._store: dict[str, Result] = {}
        self._meta: dict[str, list] = {}  # sql -> [filled_at, hits]
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

        with self._lock:
            self.stats.calls += 1
            cached = self._store.get(sql) if use_cache else None
            if cached is not None:
                self.stats.hits += 1
                self._meta[sql][1] += 1

        if cached is not None:
            result, hit, db_ms = cached, True, 0.0
        else:
            result, db_ms = self._execute(sql)  # outside the lock
            hit = False
            with self._lock:
                if use_cache:
                    self.stats.misses += 1
                    if sql not in self._store:
                        self._store[sql] = result
                        self._meta[sql] = [time.time(), 0]
                else:
                    self.stats.bypass += 1

        info = CallInfo(sql, cacheable, reason, hit,
                        (time.perf_counter() - t0) * 1000, db_ms)
        with self._lock:
            self.log.append(info)
        return result, info

    def catalog(self) -> list[CatalogEntry]:
        """What the cache currently holds, most recently filled first."""
        with self._lock:
            entries = [CatalogEntry(sql, r.columns, r.rowcount, *self._meta[sql])
                       for sql, r in self._store.items()]
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
