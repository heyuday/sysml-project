"""Aggregate a sweep directory of run logs into per-arm means.

    uv run python -m agentsql.aggregate logs/sweep-<ts>

Per run, agent 0 is the "leader" (always first, starts against an empty cache)
and agents 1..N-1 are "followers": the ones a shared cache can help.
Writes runs.csv (one row per run) and summary.csv (one row per configuration).
"""

from __future__ import annotations

import csv
import glob
import os
import statistics as st
import sys
from collections import defaultdict

from agentsql.analyze import load


def run_metrics(events: list[dict]) -> dict:
    run = next(e for e in events if e["type"] == "run")
    finals = {e["agent"]: e for e in events if e["type"] == "final"}
    starts = {e["agent"]: e["t"] for e in events if e["type"] == "start"}
    per = defaultdict(lambda: dict(llm_s=0.0, llm_calls=0, sql_s=0.0, sql_calls=0,
                                   run_sql_hits=0, in_tok=0, out_tok=0))
    for e in events:
        if e["type"] == "llm":
            p = per[e["agent"]]
            p["llm_s"] += e["ms"] / 1000
            p["llm_calls"] += 1
            p["in_tok"] += e.get("in_tok", 0)
            p["out_tok"] += e.get("out_tok", 0)
        elif e["type"] == "tool":
            p = per[e["agent"]]
            p["sql_s"] += e.get("ms", 0) / 1000
            p["sql_calls"] += 1
            if e["tool"] == "run_sql" and e.get("hit"):
                p["run_sql_hits"] += 1

    followers = [a for a in finals if a != 0]

    def mean_of(key, agents):
        vals = [per[a][key] for a in agents]
        return sum(vals) / len(vals) if vals else 0.0

    end = max(f["t"] for f in finals.values())
    return dict(
        arm=("aware" if run.get("aware") else run["cache"]),
        delay=run.get("db_delay_ms", 0), schedule=run["schedule"],
        leader_wall=finals[0]["wall_s"] if 0 in finals else None,
        follower_wall=sum(finals[a]["wall_s"] for a in followers) / max(len(followers), 1),
        follower_llm_calls=mean_of("llm_calls", followers),
        follower_llm_s=mean_of("llm_s", followers),
        follower_sql_s=mean_of("sql_s", followers),
        follower_sql_calls=mean_of("sql_calls", followers),
        follower_reused=mean_of("run_sql_hits", followers),
        makespan=end - min(starts.values()),
        in_tok=sum(p["in_tok"] for p in per.values()),
        out_tok=sum(p["out_tok"] for p in per.values()),
        correct=sum(f["correct"] for f in finals.values()),
        n_agents=len(finals),
        errors=sum(1 for e in events if e["type"] == "llm_error"),
    )


METRICS = ["follower_wall", "follower_llm_calls", "follower_llm_s", "follower_sql_s",
           "follower_reused", "leader_wall", "makespan", "in_tok"]


def aggregate(folder: str) -> list[dict]:
    runs = []
    for path in sorted(glob.glob(os.path.join(folder, "*.jsonl"))):
        m = run_metrics(load(path))
        m["file"] = os.path.basename(path)
        runs.append(m)
    if runs:
        with open(os.path.join(folder, "runs.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(runs[0]))
            w.writeheader()
            w.writerows(runs)

    groups = defaultdict(list)
    for r in runs:
        groups[(r["delay"], r["schedule"], r["arm"])].append(r)
    order = {"direct": 0, "exact": 1, "aware": 2}
    rows = []
    for (delay, sched, arm), rs in sorted(groups.items(),
                                          key=lambda kv: (kv[0][0], kv[0][1], order.get(kv[0][2], 9))):
        row = dict(delay=delay, schedule=sched, arm=arm, n=len(rs),
                   correct=sum(r["correct"] for r in rs), answers=sum(r["n_agents"] for r in rs),
                   errors=sum(r["errors"] for r in rs))
        for k in METRICS:
            vals = [r[k] for r in rs if r[k] is not None]
            row[k] = st.mean(vals) if vals else None
            row[k + "_sd"] = st.stdev(vals) if len(vals) > 1 else 0.0
        rows.append(row)
    if rows:
        with open(os.path.join(folder, "summary.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    return rows


def report(rows: list[dict]) -> str:
    def ms(r, k, fmt="{:.1f}"):
        return f"{fmt.format(r[k])}±{fmt.format(r[k + '_sd'])}"

    head = (f"{'delay':>5} {'schedule':<10} {'arm':<6} {'n':>2} {'correct':>7}  "
            f"{'follower wall s':>15} {'LLM calls':>10} {'LLM s':>10} {'SQL s':>10} "
            f"{'reused':>7} {'leader s':>10} {'makespan s':>11} {'in tok':>13}")
    out = ["Followers = agents 1..N-1 (mean per agent); values are mean±sd across reps.", head]
    for r in rows:
        out.append(
            f"{int(r['delay']):>5} {r['schedule']:<10} {r['arm']:<6} {r['n']:>2} "
            f"{r['correct']:>3}/{r['answers']:<3}  {ms(r, 'follower_wall'):>15} "
            f"{ms(r, 'follower_llm_calls'):>10} {ms(r, 'follower_llm_s'):>10} "
            f"{ms(r, 'follower_sql_s'):>10} {r['follower_reused']:>7.2f} "
            f"{ms(r, 'leader_wall'):>10} {ms(r, 'makespan'):>11} "
            f"{ms(r, 'in_tok', '{:.0f}'):>13}"
            + (f"  LLM errors {r['errors']}" if r["errors"] else ""))
    return "\n".join(out)


if __name__ == "__main__":
    print(report(aggregate(sys.argv[1])))
