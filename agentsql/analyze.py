"""Summarize an agent-run JSONL log.

    uv run python -m agentsql.analyze logs/<run>.jsonl

Repetition is computed from the log itself, independent of cache mode, so a
`direct` run still shows how many calls an exact cache *would* have served.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict


def load(path) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def light_norm(sql: str) -> str:
    """Rough stand-in for lexical normalization: case, whitespace, trailing ';'.
    Lowercases string literals too, so it can over-merge; it's an estimate only."""
    return re.sub(r"\s+", " ", sql.strip().rstrip(";").strip()).lower()


def summarize(events: list[dict]) -> dict:
    run = next((e for e in events if e["type"] == "run"), {})
    tools = sorted((e for e in events if e["type"] == "tool"), key=lambda e: e["t_start"])
    llms = [e for e in events if e["type"] == "llm"]
    finals = [e for e in events if e["type"] == "final"]

    # Classify every SQL call by the first time its text appeared.
    first_exact: dict[str, int] = {}
    first_norm: dict[str, int] = {}
    kinds = Counter()
    by_tool = defaultdict(Counter)
    repeat_db_ms = 0.0
    texts = Counter()
    for e in tools:
        sql, a = e["sql"], e["agent"]
        n = light_norm(sql)
        texts[sql] += 1
        if sql in first_exact:
            k = "repeat_same_agent" if first_exact[sql] == a else "repeat_cross_agent"
            repeat_db_ms += e.get("db_ms", 0.0) or 0.0
        elif n in first_norm:
            k = "near_dup"  # would need normalization to hit
        else:
            k = "first"
        first_exact.setdefault(sql, a)
        first_norm.setdefault(n, a)
        kinds[k] += 1
        by_tool[e["tool"]][k] += 1

    hits = sum(1 for e in tools if e.get("hit"))
    errors = sum(1 for e in tools if e.get("error"))
    noncache = Counter(e.get("reason") for e in tools
                       if not e.get("error") and e.get("cacheable") is False)
    return dict(
        run=run, n_tool=len(tools), n_llm=len(llms), hits=hits, errors=errors,
        kinds=kinds, by_tool={k: dict(v) for k, v in by_tool.items()},
        noncacheable=dict(noncache), repeat_db_ms=repeat_db_ms,
        llm_s=sum(e["ms"] for e in llms) / 1000,
        tool_s=sum(e.get("ms", 0) for e in tools) / 1000,
        db_s=sum(e.get("db_ms", 0) or 0 for e in tools) / 1000,
        in_tok=sum(e.get("in_tok", 0) for e in llms),
        out_tok=sum(e.get("out_tok", 0) for e in llms),
        finals=finals, top=texts.most_common(5),
        llm_errors=[e for e in events if e["type"] == "llm_error"],
        catalog_calls=sum(1 for e in events if e["type"] == "catalog"),
    )


def report(s: dict) -> str:
    r, k, n = s["run"], s["kinds"], max(s["n_tool"], 1)
    out = [
        f"run: scenario={r.get('scenario')} schedule={r.get('schedule')} agents={r.get('n_agents')} "
        f"cache={r.get('cache')} backend={r.get('backend')} model={r.get('model')} "
        f"aware={r.get('aware')} db_delay_ms={r.get('db_delay_ms')}",
        "",
        "agent  task                 steps  correct  wall_s  answer",
    ]
    for f in sorted(s["finals"], key=lambda f: f["agent"]):
        ans = (f["answer"] or "").replace("\n", " ")[:50]
        out.append(f"{f['agent']:<6} {f['task']:<20} {f['steps']:<6} {str(f['correct']):<8} "
                   f"{f['wall_s']:<7} {ans}")
    out += [
        "",
        f"tool calls: {s['n_tool']}  (errors {s['errors']}, cache hits {s['hits']} = {s['hits']/n:.0%})",
        f"  first seen          {k['first']:4}",
        f"  repeat, same agent  {k['repeat_same_agent']:4}",
        f"  repeat, other agent {k['repeat_cross_agent']:4}   <- what a shared cache buys",
        f"  near-duplicate      {k['near_dup']:4}   <- only normalization would catch these",
        f"  exact-repeat rate   {(k['repeat_same_agent'] + k['repeat_cross_agent'])/n:.0%}",
        f"by tool: {s['by_tool']}",
        f"cached_queries (catalog) calls: {s['catalog_calls']}",
        f"not cacheable: {s['noncacheable'] or 'none'}",
        "",
        f"time: LLM {s['llm_s']:.1f}s over {s['n_llm']} calls | tools {s['tool_s']:.3f}s | "
        f"DB {s['db_s']:.3f}s | DB time on exact repeats {s['repeat_db_ms']/1000:.3f}s "
        f"(direct mode: what a cache would have saved; exact mode: ~0, they were hits)",
        f"tokens: in {s['in_tok']}  out {s['out_tok']}",
        "",
        "most repeated statements:",
    ]
    out += [f"  {c}x  {q[:100]}" for q, c in s["top"]]
    for e in s["llm_errors"]:
        out.append(f"LLM ERROR agent {e['agent']}: {e['error'][:200]}")
    return "\n".join(out)


if __name__ == "__main__":
    print(report(summarize(load(sys.argv[1]))))
