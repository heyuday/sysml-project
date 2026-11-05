import base64
import hashlib
import json
import os
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DSN = os.environ.get(
    "SYSML_DSN", "postgresql://bench:localbench@127.0.0.1:55432/chbenchmark"
)
REDIS = os.environ.get("SYSML_REDIS", "redis://127.0.0.1:56379/0")
_log_lock = threading.Lock()


def encoded(x):
    return json.dumps(
        x, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(x):
    return hashlib.sha256(encoded(x)).hexdigest()


def records(path):
    with open(path, encoding="utf-8-sig") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def append(path, record):
    if path:
        with _log_lock, open(path, "a", encoding="utf-8") as stream:
            stream.write(encoded(record).decode() + "\n")


def fresh(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=False)
    return path


def result(cur):
    # PostgreSQL's text representation preserves decimal precision and NULLs.
    pg = cur.pgresult
    columns = [
        (pg.fname(i).decode(), pg.ftype(i), pg.fmod(i)) for i in range(pg.nfields)
    ]
    rows = [
        [
            None if (v := pg.get_value(r, c)) is None else base64.b64encode(v).decode()
            for c in range(pg.nfields)
        ]
        for r in range(pg.ntuples)
    ]
    return {"columns": columns, "rows": rows}


def fingerprint(value, ordered=False):
    rows = value["rows"]
    return digest([value["columns"], rows if ordered else sorted(rows, key=encoded)])


def quantiles(xs):
    if not xs:
        return {}
    xs = sorted(xs)

    def at(p):
        pos = (len(xs) - 1) * p
        lo = int(pos)
        return xs[lo] + (xs[min(lo + 1, len(xs) - 1)] - xs[lo]) * (pos - lo)

    return {f"p{p}": at(p / 100) for p in (50, 95, 99)}


def ms(start):
    return (time.perf_counter() - start) * 1000
