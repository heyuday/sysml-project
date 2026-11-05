from collections import defaultdict
import time
import psycopg
from psycopg import RawCursor

from .common import ms, result


def connect(dsn):
    return psycopg.connect(
        dsn,
        autocommit=True,
        options="-c timezone=UTC -c search_path=pg_catalog,public -c datestyle=ISO,YMD -c max_parallel_workers_per_gather=0 -c statement_timeout=120000",
    )


def begin(c, read=True):
    c.execute(
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
        + (" READ ONLY" if read else "")
    )


def catalog(c, tracked=True):
    info = defaultdict(dict)
    blocked = set()
    for table, col, typ, namespace in c.execute(
        """
      SELECT cl.relname,a.attname,t.typname,tn.nspname FROM pg_class cl JOIN pg_namespace n ON n.oid=cl.relnamespace
      JOIN pg_attribute a ON a.attrelid=cl.oid JOIN pg_type t ON t.oid=a.atttypid JOIN pg_namespace tn ON tn.oid=t.typnamespace
      WHERE n.nspname='public' AND cl.relkind='r' AND NOT cl.relrowsecurity AND a.attnum>0 AND NOT a.attisdropped
      AND NOT EXISTS(SELECT FROM pg_inherits WHERE inhparent=cl.oid OR inhrelid=cl.oid)
      """
        + ("AND cl.relname IN (SELECT rel FROM sysml.tracked)" if tracked else "")
    ):
        info[table][col] = typ
        if namespace != "pg_catalog" or typ not in {
            "int2",
            "int4",
            "int8",
            "numeric",
            "text",
            "varchar",
            "bpchar",
            "bool",
            "date",
            "timestamp",
            "timestamptz",
            "float4",
            "float8",
            "jsonb",
            "json",
            "uuid",
            "bytea",
            "time",
            "interval",
        }:
            blocked.add(table)
    return {t: cols for t, cols in info.items() if t not in blocked}


def execute(c, op):
    with RawCursor(c) as cur:
        start = time.perf_counter()
        cur.execute(op["sql"], op.get("params", []))
        elapsed = ms(start)
        return result(cur), elapsed


def changes(c, lo, hi):
    sql = """SELECT v,tx,rel,op,cols,before_image,after_image,extract(epoch FROM emitted)::float8
           FROM sysml.events WHERE v>%s AND v<=%s ORDER BY v,seq"""
    return [
        dict(version=v, tx=tx, table=t, op=op, columns=cols, old=o, new=n, emitted=ts)
        for v, tx, t, op, cols, o, n, ts in c.execute(sql, (lo, hi))
    ]


def watermark(c):
    return c.execute("SELECT v FROM sysml.clock WHERE singleton").fetchone()[0]


def write(c, op):
    # Trusted experiment runner only; operations have one parameterized DML statement.
    from pglast import parser
    import json

    stmts = json.loads(parser.parse_sql_json(op["sql"]))["stmts"]
    if len(stmts) != 1 or not (
        set(stmts[0]["stmt"]) & {"InsertStmt", "UpdateStmt", "DeleteStmt"}
    ):
        raise ValueError("write requires a single DML statement")
    with c.transaction(), RawCursor(c) as cur:
        # Acquire fence first to return exactly this transaction's before/after range.
        lo = c.execute("SELECT v FROM sysml.clock FOR UPDATE").fetchone()[0]
        cur.execute(op["sql"], op.get("params", []))
        count = cur.rowcount
        hi = watermark(c)
        events = changes(c, lo, hi)
    return {
        "before": lo,
        "version": hi,
        "changes": events,
        "rows_changed": count,
        "committed": True,
    }
