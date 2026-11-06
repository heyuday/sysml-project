"""Iteration 0: the smallest working SQL result cache.

One function matters: SqlCache.run_sql(sql) -> (Result, CallInfo).

Modes:
  direct - always execute against Postgres (baseline).
  exact  - cache results of eligible SELECTs, keyed on the exact SQL text.

No invalidation yet: in iteration 0 the cache is only correct while no one
writes to the tables it has cached. Writes bypass the cache but do not evict.
"""

from __future__ import annotations

import os
import re
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


class SqlCache:
    def __init__(self, dsn: str = DEFAULT_DSN, mode: str = "exact"):
        if mode not in ("direct", "exact"):
            raise ValueError(f"unknown mode {mode!r}")
        self.mode = mode
        self.conn = psycopg.connect(dsn, autocommit=True)
        self._store: dict[str, Result] = {}
        self.stats = Stats()
        self.log: list[CallInfo] = []

    # -- public API -------------------------------------------------------
    def run_sql(self, sql: str) -> tuple[Result, CallInfo]:
        t0 = time.perf_counter()
        self.stats.calls += 1
        cacheable, reason = is_cacheable(sql)
        use_cache = self.mode == "exact" and cacheable

        if use_cache and sql in self._store:
            self.stats.hits += 1
            result, hit, db_ms = self._store[sql], True, 0.0
        else:
            result, db_ms = self._execute(sql)
            hit = False
            if use_cache:
                self.stats.misses += 1
                self._store[sql] = result
            else:
                self.stats.bypass += 1

        info = CallInfo(sql, cacheable, reason, hit,
                        (time.perf_counter() - t0) * 1000, db_ms)
        self.log.append(info)
        return result, info

    def clear(self) -> None:
        self._store.clear()

    def close(self) -> None:
        self.conn.close()

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
        self.stats.db_executions += 1
        return Result(columns, rows, rowcount), (time.perf_counter() - t0) * 1000
