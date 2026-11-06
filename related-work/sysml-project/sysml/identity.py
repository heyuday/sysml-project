"""Conservative PostgreSQL identities. Only the original SQL is executed."""

import json
import os
import re
from dataclasses import dataclass, field

import httpx
import sqlglot
from pglast import parser, scan

from .common import digest, encoded

# Closed grammar for a controlled schema with pg_catalog operators and scalar types.
NODES = set(
    """SelectStmt ResTarget ColumnRef String A_Star A_Const Integer Float Boolean
RangeVar Alias A_Expr BoolExpr NullTest BooleanTest ParamRef FuncCall SortBy JoinExpr
RangeSubselect SubLink List TypeCast TypeName CommonTableExpr WithClause CaseExpr CaseWhen
CoalesceExpr MinMaxExpr SQLValueFunction""".split()
) - {"SQLValueFunction"}
FUNCS = {
    "count",
    "min",
    "max",
    "sum",
    "avg",
    "abs",
    "lower",
    "upper",
    "length",
    "round",
}
OPS = {"=", "<>", "<", ">", "<=", ">=", "+", "-", "*", "/", "~~", "!~~", "||"}
TYPES = {
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
}
INTS = {"int2", "int4", "int8"}
CONTEXT = {
    "role": "bench",
    "search_path": "pg_catalog,public",
    "timezone": "UTC",
    "schema_epoch": 1,
}


def nodes(obj):
    if isinstance(obj, list):
        for part in obj:
            yield from nodes(part)
    if isinstance(obj, dict):
        for key, val in obj.items():
            if key[0].isupper() and isinstance(val, dict):
                yield key, val
            yield from nodes(val)


def clean(obj):
    if isinstance(obj, list):
        return [clean(v) for v in obj]
    if isinstance(obj, dict):
        return {
            k: clean(v)
            for k, v in obj.items()
            if k not in {"location", "stmt_location", "stmt_len"}
        }
    return obj


def tree(sql):
    stmts = json.loads(parser.parse_sql_json(sql))["stmts"]
    if len(stmts) != 1 or "SelectStmt" not in stmts[0]["stmt"]:
        raise ValueError("not a single SELECT")
    return clean(stmts[0]["stmt"])


def strings(xs):
    return [x.get("String", {}).get("sval", "") for x in xs]


def int_test(expr, params, columns):
    a = expr.get("A_Expr", {})
    names = strings(a.get("name", []))
    col = strings(a.get("lexpr", {}).get("ColumnRef", {}).get("fields", []))
    if (
        len(col) != 1
        or columns.get(col[0]) not in INTS
        or names not in ([x] for x in ("=", "<", ">", "<=", ">="))
    ):
        return None
    rhs = a.get("rexpr", {})
    if "ParamRef" in rhs:
        pos = rhs["ParamRef"]["number"] - 1
        v = params[pos] if 0 <= pos < len(params) else None
    else:
        atom = rhs.get("A_Const", {})
        v = atom["ival"].get("ival", 0) if "ival" in atom else None
    return (col[0], names[0], v) if type(v) is int else None


@dataclass
class Analysis:
    key: str | None = None
    tables: set = field(default_factory=set)
    columns: set | None = None
    predicate: tuple | None = None
    shape: str = "unsupported"
    reason: str = ""
    ordered: bool = False
    proposal: str = ""


def proof(ast, params, catalog):
    # Reorder only a top-level WHERE conjunction of total built-in integer comparisons.
    import copy

    ast = copy.deepcopy(ast)
    s = ast["SelectStmt"]
    frm = s.get("fromClause", [])
    if len(frm) == 1 and "RangeVar" in frm[0]:
        cols = catalog.get(frm[0]["RangeVar"]["relname"], {})
        b = s.get("whereClause", {}).get("BoolExpr", {})
        if b.get("boolop") == "AND_EXPR" and all(
            int_test(x, params, cols) for x in b["args"]
        ):
            b["args"].sort(key=encoded)
    return ast


def lexical(sql):
    if not sql.isascii():
        return sql
    toks = []
    for t in scan(sql):
        value = sql[t.start : t.end + 1]
        toks.append(value.upper() if t.kind.endswith("KEYWORD") else value)
    return toks


def propose(sql):
    url = os.environ.get("SYSML_N3_URL")
    model = os.environ.get("SYSML_N3_MODEL")
    if not url or not model:
        raise RuntimeError("N3 requires SYSML_N3_URL and SYSML_N3_MODEL")
    headers = {}
    if os.environ.get("SYSML_N3_KEY"):
        headers["Authorization"] = "Bearer " + os.environ["SYSML_N3_KEY"]
    r = httpx.post(
        url.rstrip("/") + "/chat/completions",
        headers=headers,
        timeout=90,
        json={
            "model": model,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": "Return only one equivalent PostgreSQL SELECT. Canonicalize formatting and sort simple integer WHERE conjunctions consistently. Preserve output column names, types, ordering and parameter positions.",
                },
                {"role": "user", "content": sql},
            ],
        },
    )
    r.raise_for_status()
    return (
        r.json()["choices"][0]["message"]["content"]
        .strip()
        .removeprefix("```sql")
        .removesuffix("```")
        .strip()
    )


def analyze(sql, params, context, policy, catalog):
    a = Analysis()
    try:
        ast = tree(sql)
        s = ast["SelectStmt"]
        ns = list(nodes(ast))
        a.ordered = bool(s.get("sortClause"))
        literals = [v.get("sval", "") for kind, v in ns if kind == "String"]
        literals += [
            v.get("sval", {}).get("sval", "") for kind, v in ns if kind == "A_Const"
        ]
        if any(
            re.search(
                r"\b(now|today|tomorrow|yesterday|current_time|current_date)\b", v, re.I
            )
            for v in [*literals, *params]
            if isinstance(v, str)
        ):
            raise ValueError("relative temporal input")
        for kind, v in ns:
            if kind == "ColumnRef" and set(strings(v.get("fields", []))) & {
                "ctid",
                "xmin",
                "xmax",
                "cmin",
                "cmax",
                "tableoid",
            }:
                raise ValueError("physical/system column")
            if kind == "RangeVar" and v.get("alias", {}).get("colnames"):
                raise ValueError("renamed relation columns")
            if kind == "TypeCast":
                typ = v.get("typeName", {})
                names = strings(typ.get("names", []))
                if (
                    not names
                    or names[-1] not in TYPES
                    or names[:-1] not in ([], ["pg_catalog"])
                    or typ.get("arrayBounds")
                ):
                    raise ValueError("unmodeled cast target")
                if names[-1] in {"date", "timestamp"}:
                    literal = (
                        v.get("arg", {})
                        .get("A_Const", {})
                        .get("sval", {})
                        .get("sval", "")
                    )
                    if not re.fullmatch(
                        r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)?",
                        literal,
                    ):
                        raise ValueError("nonliteral temporal cast")
            if kind not in NODES:
                raise ValueError("unsupported " + kind)
            if kind == "SelectStmt" and any(
                v.get(k)
                for k in (
                    "lockingClause",
                    "intoClause",
                    "limitCount",
                    "limitOffset",
                    "distinctClause",
                    "windowClause",
                )
            ):
                raise ValueError("unsupported locking/limit/distinct/window")
            if kind == "SelectStmt" and v.get("op", "SETOP_NONE") != "SETOP_NONE":
                raise ValueError("set operation")
            if kind == "FuncCall" and (
                strings(v["funcname"]) not in ([f] for f in FUNCS) or v.get("over")
            ):
                raise ValueError("unsupported function")
            if kind == "TypeName" and strings(v.get("names", []))[-1:] not in (
                [t] for t in TYPES
            ):
                raise ValueError("unsupported cast")
            if (
                kind == "TypeName"
                and len(strings(v.get("names", []))) > 1
                and strings(v["names"])[:-1] != ["pg_catalog"]
            ):
                raise ValueError("non-builtin cast")
            if kind == "A_Expr" and strings(v.get("name", [])) not in (
                [op] for op in OPS
            ):
                raise ValueError("unsupported operator")
            if kind == "RangeVar":
                if (
                    v.get("schemaname", "public") != "public"
                    or v.get("catalogname")
                    or v["relname"] not in catalog
                ):
                    raise ValueError("untracked relation")
                a.tables.add(v["relname"])
        if not a.tables:
            raise ValueError("no tracked relation")
        simple = (
            len(a.tables) == 1
            and sum(k == "SelectStmt" for k, _ in ns) == 1
            and sum(k == "RangeVar" for k, _ in ns) == 1
        )
        if simple:
            cols = catalog[next(iter(a.tables))]
            refs = [strings(v["fields"]) for k, v in ns if k == "ColumnRef"]
            if all(x and x[-1] in cols for x in refs):
                a.columns = {x[-1] for x in refs}
            a.predicate = int_test(s.get("whereClause", {}), params, cols)
        a.shape = (
            "join"
            if len(a.tables) > 1 or any(k == "JoinExpr" for k, _ in ns)
            else "aggregate"
            if any(
                k == "FuncCall"
                and strings(v["funcname"])[-1] in {"sum", "avg", "min", "max", "count"}
                for k, v in ns
            )
            else "point"
            if a.predicate and a.predicate[1] == "="
            else "range"
            if a.predicate
            else "other"
        )
        if policy == "N0":
            identity = sql
        elif policy == "N1":
            identity = [lexical(sql), ast]
        elif policy in ("N2", "N3"):
            # PG signature guards against SQLGlot dialect/serialization collisions.
            def canonical(text, parsed):
                return [
                    sqlglot.parse_one(text, read="postgres").sql(
                        dialect="postgres", normalize=True, comments=False
                    ),
                    parsed,
                ]

            identity = canonical(sql, ast)
            if policy == "N3":
                try:
                    candidate = propose(sql)
                    alt = tree(candidate)
                    if proof(ast, params, catalog) == proof(alt, params, catalog):
                        identity = canonical(candidate, alt)
                        a.proposal = "verified"
                    else:
                        a.proposal = "rejected"
                except (
                    ValueError,
                    KeyError,
                    IndexError,
                    AttributeError,
                    httpx.HTTPError,
                    parser.ParseError,
                    sqlglot.errors.SqlglotError,
                ):
                    a.proposal = "rejected"
        else:
            raise ValueError("unknown normalizer")
        a.key = digest([policy, context, params, identity])
    except (
        ValueError,
        KeyError,
        IndexError,
        parser.ParseError,
        sqlglot.errors.SqlglotError,
    ) as e:
        a.reason = str(e)
    return a
