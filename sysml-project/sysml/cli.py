import argparse
import json
import os
import re
import sys
import platform
import hashlib
from importlib.metadata import version
from pathlib import Path

from . import db
from .common import ROOT, DSN, records, append, fresh, encoded, digest, fingerprint
from .evaluate import calibrate, normalization, live_replay, summarize
from .identity import CONTEXT, analyze
from .matrix import sweep
from .policies import Config
from .simulation import simulate
from .workloads import generate, bird_file, equivalence_pairs


def dump(path, items):
    path = fresh(path)
    for item in items:
        append(path, item)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def main():
    parser = argparse.ArgumentParser(
        description="Workload-aware result cache laboratory. Commands are explicit."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def command(name, *args):
        p = sub.add_parser(name)
        for key in args:
            p.add_argument(key)
        return p

    g = command("generate", "out")
    for name, typ, default in [
        ("n", int, 1000),
        ("seed", int, 1),
        ("clients", int, 8),
        ("write", float, 0.05),
        ("skew", float, 1.1),
        ("rate", float, 25),
        ("variation", float, 0),
    ]:
        g.add_argument("--" + name, type=typ, default=default)
    for name, choices, default in [
        ("write_column", ["mixed", "referenced", "unreferenced"], "mixed"),
        ("repetition", ["low", "medium", "high"], "medium"),
        ("locality", ["hot", "random"], "random"),
        ("pattern", ["stationary", "burst", "shift"], "stationary"),
        ("mix", ["cheap", "mixed", "expensive", "disagreement"], "mixed"),
    ]:
        g.add_argument("--" + name, choices=choices, default=default)
    command("bird-import", "bird_result", "out")
    command("pairs", "out")
    command("install-cdc")
    command("catalog", "out")
    p = command("clone-db", "target")
    p.add_argument("--template", default="chbenchmark")
    p = command("calibrate", "trace", "out")
    p.add_argument("--profile-refresh", action="store_true")
    p.add_argument("--oracle")
    p = command("bird-calibrate", "trace", "snapshot_map", "out")
    p = command("normalize", "trace", "catalog", "out")
    p.add_argument(
        "--policy",
        nargs="+",
        choices=["N0", "N1", "N2", "N3"],
        default=["N0", "N1", "N2"],
    )
    p.add_argument("--snapshot-env", action="append", default=[])
    p.add_argument("--scope")
    p.add_argument("--controlled", action="store_true")
    p = command("simulate", "trace", "out")
    p.add_argument("--config")
    p.add_argument("--warm", type=float, default=0.2)
    p = command("sweep", "trace", "out")
    p.add_argument("--track", choices=["rq1", "rq2a", "rq2b", "rq3"], required=True)
    p.add_argument("--split", choices=["tune", "test"], required=True)
    p.add_argument("--seed", type=int, required=True)
    p.add_argument(
        "--scenario", required=True, help="JSON file or human-readable scenario ID"
    )
    p.add_argument(
        "--config", help="Frozen base configuration chosen on the tuning partition"
    )
    p = command("serve")
    p.add_argument("--config")
    p.add_argument("--log", required=True)
    p.add_argument("--port", type=int, default=8650)
    p = command("live", "trace", "out")
    p.add_argument("--url", default="http://127.0.0.1:8650")
    p.add_argument("--clients", type=int, default=64)
    p = command("summarize", "out")
    p.add_argument("inputs", nargs="+")
    command("compare-live", "direct", "trial", "out")
    a = parser.parse_args()
    cfg = Config(**read_json(a.config)) if getattr(a, "config", None) else Config()
    if a.cmd == "generate":
        opts = {k: v for k, v in vars(a).items() if k not in {"cmd", "out"}}
        dump(a.out, generate(**opts))
        Path(a.out + ".scenario.json").write_bytes(encoded(opts))
    elif a.cmd == "bird-import":
        dump(a.out, bird_file(a.bird_result))
    elif a.cmd == "pairs":
        dump(a.out, equivalence_pairs())
    elif a.cmd == "clone-db":
        from psycopg import sql
        from psycopg.conninfo import make_conninfo

        if not re.fullmatch(r"sysml_[a-z0-9_]+", a.target) or not re.fullmatch(
            r"[a-z][a-z0-9_]*", a.template
        ):
            parser.error(
                "new database must start sysml_ and names must be simple identifiers"
            )
        with db.connect(make_conninfo(DSN, dbname="postgres")) as c:
            c.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                    sql.Identifier(a.target), sql.Identifier(a.template)
                )
            )
    elif a.cmd == "install-cdc":
        with db.connect(DSN) as c, c.transaction():
            c.execute((ROOT / "sql/install-cdc.sql").read_text())
    elif a.cmd == "catalog":
        with db.connect(DSN) as c:
            fresh(a.out).write_bytes(encoded(db.catalog(c, False)))
    elif a.cmd == "calibrate":
        calibrate(records(a.trace), a.out, profile=a.profile_refresh, oracle=a.oracle)
    elif a.cmd == "bird-calibrate":
        # Explicit mapping to operator-preserved per-scope database snapshots.
        # Each DSN is named by an environment variable so secrets stay out of traces.
        mapping = read_json(a.snapshot_map)
        out = fresh(a.out)
        catalogs = {}
        birth = None
        from contextlib import ExitStack

        with ExitStack() as stack:
            connections = {}
            for op in sorted(records(a.trace), key=lambda r: r.get("at") or 0):
                if op["kind"] != "read" or op.get("successful") is False:
                    continue
                if op.get("at") is None:
                    raise ValueError(
                        "ADK result files lack arrival times; use the timestamped BIRD capture wrapper"
                    )
                scope = op["scope"]
                if scope not in connections:
                    c = stack.enter_context(db.connect(os.environ[mapping[scope]]))
                    stack.enter_context(c.transaction())
                    db.begin(c)
                    connections[scope] = c
                    catalogs[scope] = db.catalog(c, False)
                c = connections[scope]
                ctx = {
                    **CONTEXT,
                    "database": op["database"],
                    "scope": scope,
                    "role": c.execute("select current_user").fetchone()[0],
                }
                ana = analyze(op["sql"], op["params"], ctx, "N0", catalogs[scope])
                value, cost = db.execute(c, op)
                if birth is None:
                    birth = op["at"]
                append(
                    out,
                    {
                        **op,
                        "at": op["at"] - birth,
                        "context": ctx,
                        "raw_id": digest([op["sql"], op["params"], ctx]),
                        "db_ms": cost,
                        "size": len(encoded(value)),
                        "digest": fingerprint(value, ana.ordered),
                    },
                )
        Path(a.out + ".catalog.json").write_bytes(encoded({"by_scope": catalogs}))
    elif a.cmd == "normalize":
        dsns = [os.environ[n] for n in a.snapshot_env]
        ops = [o for o in records(a.trace) if not a.scope or o.get("scope") == a.scope]
        scopes = {
            (o.get("database"), o.get("scope")) for o in ops if o.get("kind") == "read"
        }
        if len(scopes) > 1 and dsns:
            parser.error("select one --scope before providing matching snapshot DSNs")
        catalog = read_json(a.catalog)
        if "by_scope" in catalog:
            if not a.scope:
                parser.error("select one --scope for a per-scope BIRD catalog")
            catalog = catalog["by_scope"][a.scope]
        dump(a.out, normalization(ops, catalog, a.policy, dsns, a.controlled))
    elif a.cmd in {"simulate", "sweep"}:
        ops = list(records(a.trace))
        catalog = read_json(a.trace + ".catalog.json")
        if a.cmd == "simulate":
            dump(a.out, [simulate(ops, catalog, cfg, a.warm)])
        else:
            scenario = (
                read_json(a.scenario)
                if Path(a.scenario).is_file()
                else {"name": a.scenario}
            )
            dump(
                a.out, sweep(ops, catalog, a.track, a.seed, scenario, a.split, base=cfg)
            )
    elif a.cmd == "serve":
        import uvicorn
        from .live import app

        uvicorn.run(
            app(cfg, str(fresh(a.log))), host="127.0.0.1", port=a.port, workers=1
        )
    elif a.cmd == "live":
        live_replay(records(a.trace), a.url, a.out, a.clients)
    elif a.cmd == "summarize":
        dump(a.out, summarize(a.inputs))
    elif a.cmd == "compare-live":
        from .reporting import compare_live

        dump(
            a.out,
            [
                compare_live(
                    records(a.direct),
                    records(a.trial),
                    read_json(a.trial + ".stats.json"),
                )
            ],
        )
    if a.cmd in {
        "simulate",
        "sweep",
        "normalize",
        "calibrate",
        "live",
        "bird-calibrate",
    }:
        with open(a.trace, "rb") as stream:
            trace_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        info = {
            "arguments": vars(a),
            "input_sha256": trace_hash,
            "python": sys.version,
            "platform": platform.platform(),
            "processor": platform.processor(),
            "packages": {
                p: version(p)
                for p in ("psycopg", "redis", "sqlglot", "pglast", "sysml-project")
            },
            "benchbase_commit": "33c00473807ebd49304d114a6d769d2d2b2bbb34",
            "policy": vars(cfg),
            "n3_model": os.environ.get("SYSML_N3_MODEL"),
        }
        Path(a.out + ".manifest.json").write_bytes(encoded(info))


if __name__ == "__main__":
    main()
