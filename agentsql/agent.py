"""A minimal SQL analyst agent. Every tool call goes through the shared SqlCache
and is logged, together with every LLM call, to a JSONL event log."""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass

import psycopg

from agentsql.cache import SqlCache
from agentsql.llm import ToolSpec
from agentsql.tasks import Task

SYSTEM = (
    "You are a data analyst with read-only access to a PostgreSQL database. "
    "You don't know the schema in advance: use the tools to explore it, run SQL "
    "to compute the answer, then call final_answer with a short, direct answer. "
    "Only SELECT queries are allowed."
)

AWARE_HINT = (
    " Other analysts share this database and a result cache. Before writing new "
    "SQL, call cached_queries to see queries that were already run. If one of them "
    "returns the data you need, run that exact SQL text with run_sql: it is served "
    "instantly from the cache. Write new SQL only when none of them fits."
)

_OBJ = {"type": "object", "additionalProperties": False}
TOOLS = [
    ToolSpec("list_tables", "List the tables in the database.",
             {**_OBJ, "properties": {}, "required": []}),
    ToolSpec("describe_table", "Show the columns and types of one table.",
             {**_OBJ, "properties": {"table": {"type": "string"}}, "required": ["table"]}),
    ToolSpec("run_sql", "Run one read-only SQL query and return up to 50 rows.",
             {**_OBJ, "properties": {"sql": {"type": "string"}}, "required": ["sql"]}),
    ToolSpec("final_answer", "Submit the final answer to the question. Ends the task.",
             {**_OBJ, "properties": {"answer": {"type": "string"}}, "required": ["answer"]}),
]

LIVE_HINT = (
    " The list also shows queries other analysts are running right now, marked "
    "RUNNING: running one of those verbatim waits for its result instead of "
    "recomputing it. Others may start a query while you explore, so call "
    "cached_queries again right before you run an expensive query."
)

CACHED_QUERIES_TOOL = ToolSpec(
    "cached_queries",
    "List SQL queries whose results are already cached (most recent first), with "
    "their output columns and row counts. Running one of them verbatim is instant.",
    {**_OBJ, "properties": {}, "required": []},
)
MAX_CATALOG = 30

LIST_TABLES_SQL = (
    "SELECT table_name FROM information_schema.tables "
    "WHERE table_schema = 'public' ORDER BY table_name"
)
DESCRIBE_SQL = (
    "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
    "WHERE table_schema = 'public' AND table_name = '{table}' ORDER BY ordinal_position"
)
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_ROWS = 50


class EventLog:
    """Thread-safe JSONL writer. Every event gets `t`, seconds since the log opened."""

    def __init__(self, path):
        self._f = open(path, "w")
        self._lock = threading.Lock()
        self.t0 = time.perf_counter()

    def now(self) -> float:
        return time.perf_counter() - self.t0

    def write(self, **event):
        event.setdefault("t", round(self.now(), 4))
        with self._lock:
            self._f.write(json.dumps(event, default=str) + "\n")
            self._f.flush()

    def close(self):
        self._f.close()


def format_result(columns, rows) -> str:
    lines = ["\t".join(columns)]
    lines += ["\t".join("NULL" if v is None else str(v) for v in r) for r in rows[:MAX_ROWS]]
    if len(rows) > MAX_ROWS:
        lines.append(f"(showing {MAX_ROWS} of {len(rows)} rows)")
    elif not rows:
        lines.append("(0 rows)")
    return "\n".join(lines)


@dataclass
class AgentResult:
    agent_id: int
    task_id: str
    answer: str | None
    correct: bool
    steps: int
    wall_s: float


def format_catalog(entries) -> str:
    """Numbered list of cached (and, in live mode, running) analytical queries.
    Schema lookups are left out: the schema tools already hit the cache."""
    rows = [e for e in entries if "information_schema" not in e.sql][:MAX_CATALOG]
    if not rows:
        return "(no cached queries yet)"
    now = time.time()

    def head(e):
        if e.running:
            return (f"[RUNNING, started {now - e.filled_at:.0f}s ago by another analyst; "
                    f"run it verbatim to wait for its result]")
        return f"[{now - e.filled_at:.0f}s ago, {e.rowcount} rows, columns: {', '.join(e.columns)}]"

    return "\n".join(f"{i}. {head(e)}\n   {e.sql}" for i, e in enumerate(rows, 1))


class Agent:
    def __init__(self, agent_id: int, task: Task, question: str, expected,
                 backend, cache: SqlCache, log: EventLog, max_steps: int = 20,
                 aware: bool = False, live: bool = False):
        self.id, self.task, self.question = agent_id, task, question
        # acceptable answers: any cell of the reference query's first row
        self.expected = [expected] if isinstance(expected, str) else list(expected)
        self.backend, self.cache, self.log, self.max_steps = backend, cache, log, max_steps
        self.aware = aware or live
        self.live = live  # catalog shows running queries + re-check hint

    # -- tools ------------------------------------------------------------
    def _tool(self, step: int, name: str, args: dict) -> tuple[str, bool]:
        if name == "cached_queries" and self.aware:
            entries = self.cache.catalog(include_running=self.live)
            text = format_catalog(entries)
            self.log.write(type="catalog", agent=self.id, task=self.task.id, step=step,
                           n_entries=text.count("\n   "), n_running=text.count("[RUNNING"))
            return text, False
        if name == "list_tables":
            sql = LIST_TABLES_SQL
        elif name == "describe_table":
            table = str(args.get("table", ""))
            if not _IDENT.match(table):
                return f"invalid table name {table!r}", True
            sql = DESCRIBE_SQL.format(table=table)
        elif name == "run_sql":
            sql = str(args.get("sql", ""))
        else:
            return f"unknown tool {name!r}", True

        t_start = self.log.now()
        ev = dict(type="tool", agent=self.id, task=self.task.id, step=step,
                  tool=name, sql=sql, t_start=round(t_start, 4))
        try:
            result, info = self.cache.run_sql(sql)
        except psycopg.Error as e:
            msg = (getattr(e.diag, "message_primary", None) or str(e)).strip()
            self.log.write(**ev, error=msg, hit=False,
                           ms=round((self.log.now() - t_start) * 1000, 3))
            return f"SQL error: {msg}", True
        self.log.write(**ev, hit=info.hit, coalesced=info.coalesced,
                       cacheable=info.cacheable, reason=info.reason,
                       ms=round(info.ms, 3), db_ms=round(info.db_ms, 3),
                       rows=result.rowcount)
        return format_result(result.columns, result.rows), False

    # -- loop -------------------------------------------------------------
    def run(self) -> AgentResult:
        t0 = time.perf_counter()
        self.log.write(type="start", agent=self.id, task=self.task.id, question=self.question)
        system = SYSTEM + (AWARE_HINT if self.aware else "") + (LIVE_HINT if self.live else "")
        tools = TOOLS + ([CACHED_QUERIES_TOOL] if self.aware else [])
        conv = self.backend.new_conversation(system, self.question, tools, task=self.task)
        answer, step = None, 0
        for step in range(1, self.max_steps + 1):
            t_llm = self.log.now()
            try:
                turn = conv.step()
            except Exception as e:  # API errors end this agent, not the whole run
                self.log.write(type="llm_error", agent=self.id, step=step,
                               error=f"{type(e).__name__}: {e}")
                break
            self.log.write(type="llm", agent=self.id, step=step,
                           ms=round((self.log.now() - t_llm) * 1000, 1),
                           stop=turn.stop_reason, in_tok=turn.input_tokens,
                           out_tok=turn.output_tokens, n_calls=len(turn.tool_calls),
                           text=turn.text[:500])
            if not turn.tool_calls:  # model replied in plain text: take it as the answer
                answer = turn.text.strip() or None
                break
            results = []
            for call in turn.tool_calls:
                if call.name == "final_answer":
                    answer = str(call.input.get("answer", ""))
                    results.append((call.id, "Answer recorded.", False))
                else:
                    text, err = self._tool(step, call.name, call.input)
                    results.append((call.id, text, err))
            if answer is not None:
                break
            conv.add_tool_results(results)

        norm = (answer or "").lower().replace(",", "")
        correct = answer is not None and any(e.lower().replace(",", "") in norm for e in self.expected)
        wall = time.perf_counter() - t0
        self.log.write(type="final", agent=self.id, task=self.task.id, answer=answer,
                       expected=self.expected, correct=correct, steps=step,
                       wall_s=round(wall, 3))
        return AgentResult(self.id, self.task.id, answer, correct, step, wall)
