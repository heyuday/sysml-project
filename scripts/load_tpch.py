"""Generate TPC-H data with tpchgen-cli (from PyPI) and load it into Postgres.

    uv run python scripts/load_tpch.py --sf 1           # create/load DB `tpch`, then time the tasks
    uv run python scripts/load_tpch.py --sf 1 --force   # drop and reload
    uv run python scripts/load_tpch.py --time-only      # just time the task queries

Idempotent: if the DB already holds the requested scale factor, loading is skipped.
Uses AGENTSQL_TPCH_DSN (default postgresql://bench:bench@127.0.0.1:55440/tpch);
the role needs CREATEDB (the docker/cloud setups use a superuser).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agentsql.cache import TPCH_DSN  # noqa: E402
from agentsql.tasks import TPCH_TASKS  # noqa: E402

DDL = {
    "region": "r_regionkey integer, r_name text, r_comment text",
    "nation": "n_nationkey integer, n_name text, n_regionkey integer, n_comment text",
    "part": ("p_partkey integer, p_name text, p_mfgr text, p_brand text, p_type text, "
             "p_size integer, p_container text, p_retailprice numeric(15,2), p_comment text"),
    "supplier": ("s_suppkey integer, s_name text, s_address text, s_nationkey integer, "
                 "s_phone text, s_acctbal numeric(15,2), s_comment text"),
    "partsupp": ("ps_partkey integer, ps_suppkey integer, ps_availqty integer, "
                 "ps_supplycost numeric(15,2), ps_comment text"),
    "customer": ("c_custkey integer, c_name text, c_address text, c_nationkey integer, "
                 "c_phone text, c_acctbal numeric(15,2), c_mktsegment text, c_comment text"),
    "orders": ("o_orderkey bigint, o_custkey integer, o_orderstatus text, o_totalprice numeric(15,2), "
               "o_orderdate date, o_orderpriority text, o_clerk text, o_shippriority integer, "
               "o_comment text"),
    "lineitem": ("l_orderkey bigint, l_partkey integer, l_suppkey integer, l_linenumber integer, "
                 "l_quantity numeric(15,2), l_extendedprice numeric(15,2), l_discount numeric(15,2), "
                 "l_tax numeric(15,2), l_returnflag text, l_linestatus text, l_shipdate date, "
                 "l_commitdate date, l_receiptdate date, l_shipinstruct text, l_shipmode text, "
                 "l_comment text"),
}
PKS = {
    "region": "r_regionkey", "nation": "n_nationkey", "part": "p_partkey",
    "supplier": "s_suppkey", "partsupp": "ps_partkey, ps_suppkey", "customer": "c_custkey",
    "orders": "o_orderkey", "lineitem": "l_orderkey, l_linenumber",
}


def admin_dsn(dsn: str) -> tuple[str, str]:
    params = conninfo_to_dict(dsn)
    db = params.get("dbname", "tpch")
    params["dbname"] = "postgres"
    return make_conninfo(**params), db


def loaded_sf(dsn: str) -> float | None:
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            row = c.execute("SELECT sf FROM tpch_meta").fetchone()
            return float(row[0]) if row else None
    except psycopg.Error:
        return None


def ensure_db(dsn: str, force: bool) -> None:
    admin, db = admin_dsn(dsn)
    with psycopg.connect(admin, autocommit=True) as c:
        exists = c.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db,)).fetchone()
        if exists and force:
            c.execute(f'DROP DATABASE "{db}" WITH (FORCE)')
            exists = None
        if not exists:
            c.execute(f'CREATE DATABASE "{db}"')


def generate(sf: float, table: str, out_dir: str) -> str:
    exe = os.path.join(os.path.dirname(sys.executable), "tpchgen-cli")
    if not os.path.exists(exe):
        exe = "tpchgen-cli"
    subprocess.run([exe, "csv", "-s", str(sf), f"--tables={table}", f"--output-dir={out_dir}"],
                   check=True)
    return os.path.join(out_dir, f"{table}.csv")


def load(dsn: str, sf: float, tmp_dir: str) -> None:
    """One table at a time: generate its CSV, COPY it, delete it. Keeps peak
    disk use to roughly one table's CSV (lineitem is ~7.5 GB at SF 10)."""
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS tpch_meta")
        for table in DDL:
            c.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        for table, cols in DDL.items():
            t0 = time.perf_counter()
            path = generate(sf, table, tmp_dir)
            t_gen = time.perf_counter() - t0
            c.execute(f"CREATE TABLE {table} ({cols})")
            with c.cursor() as cur, open(path, "rb") as f:
                with cur.copy(f"COPY {table} FROM STDIN (FORMAT csv, HEADER true)") as cp:
                    while chunk := f.read(1 << 20):
                        cp.write(chunk)
            os.remove(path)
            n = c.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            print(f"  {table:<9} {n:>12,} rows  gen {t_gen:6.1f}s  total {time.perf_counter() - t0:6.1f}s",
                  flush=True)
        for table, pk in PKS.items():
            c.execute(f"ALTER TABLE {table} ADD PRIMARY KEY ({pk})")
        print("adding primary keys + ANALYZE ...", flush=True)
        c.execute("ANALYZE")
        c.execute("CREATE TABLE tpch_meta (sf numeric)")
        c.execute("INSERT INTO tpch_meta VALUES (%s)", (sf,))
    print("primary keys added, ANALYZE done")


def time_tasks(dsn: str) -> None:
    print("\nreference query timings (run 1 = cold-ish, runs 2-3 = warm):")
    with psycopg.connect(dsn, autocommit=True) as c:
        for t in TPCH_TASKS.values():
            times, row = [], None
            for _ in range(3):
                t0 = time.perf_counter()
                row = c.execute(t.reference_sql).fetchone()
                times.append(time.perf_counter() - t0)
            # runner-up check: the answer should be unique
            sql2 = t.reference_sql.replace("LIMIT 1", "LIMIT 2")
            top2 = c.execute(sql2).fetchall()
            print(f"  {t.id:<22} answer={row}  times={[round(x, 2) for x in times]}s  "
                  f"runner-up={top2[1] if len(top2) > 1 else None}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sf", type=float, default=1.0)
    ap.add_argument("--dsn", default=TPCH_DSN)
    ap.add_argument("--force", action="store_true", help="drop and reload even if loaded")
    ap.add_argument("--time-only", action="store_true")
    args = ap.parse_args()

    if not args.time_only:
        current = loaded_sf(args.dsn)
        if current == args.sf and not args.force:
            print(f"tpch already loaded at SF {current}; skipping load (use --force to reload)")
        else:
            ensure_db(args.dsn, args.force or current is not None)
            with tempfile.TemporaryDirectory(prefix="tpch-") as d:
                load(args.dsn, args.sf, d)
    time_tasks(args.dsn)


if __name__ == "__main__":
    main()
