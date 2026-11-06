"""End-to-end harness tests with the scripted mock backend (no API key, no network)."""

import psycopg
import pytest

import run_agents
from agentsql.agent import format_result
from agentsql.analyze import load, summarize
from agentsql.cache import SqlCache
from agentsql.tasks import SCENARIOS, assign


def _run(tmp_path, *extra):
    path = str(tmp_path / "run.jsonl")
    run_agents.main(["--backend", "mock", "--agents", "4", "--log", path, "--quiet", *extra])
    return summarize(load(path))


def test_same_question_sequential_hits_after_first_agent(tmp_path):
    s = _run(tmp_path, "--scenario", "same", "--schedule", "sequential")
    assert len(s["finals"]) == 4 and all(f["correct"] for f in s["finals"])
    per_agent = s["n_tool"] // 4
    # Agent 0 misses everything; agents 1-3 hit everything (mock SQL is identical).
    assert s["hits"] == 3 * per_agent
    assert s["kinds"]["repeat_cross_agent"] == 3 * per_agent


def test_related_questions_share_only_exploration(tmp_path):
    s = _run(tmp_path, "--scenario", "related", "--schedule", "parallel")
    assert all(f["correct"] for f in s["finals"])
    assert s["by_tool"]["run_sql"].get("repeat_cross_agent", 0) == 0  # different questions
    assert s["by_tool"]["list_tables"]["repeat_cross_agent"] == 3      # same schema lookups


def test_direct_mode_has_no_hits_but_still_counts_repeats(tmp_path):
    s = _run(tmp_path, "--scenario", "same", "--cache", "direct")
    assert s["hits"] == 0 and s["kinds"]["repeat_cross_agent"] > 0


def test_staggered_concurrent_run_is_consistent(tmp_path):
    s = _run(tmp_path, "--scenario", "mixed", "--schedule", "staggered", "--gap", "0.01")
    assert all(f["correct"] for f in s["finals"])
    stats = next(e for e in load(tmp_path / "run.jsonl") if e["type"] == "cache_stats")
    outcomes = stats["hits"] + stats["misses"] + stats["bypass"] + stats["coalesced"]
    assert outcomes == stats["calls"] == s["n_tool"]


def test_agent_connection_is_read_only():
    with SqlCache(mode="exact", read_only=True) as c:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            c.run_sql("CREATE TABLE should_not_exist (x int)")


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_every_scenario_assigns_all_agents(scenario):
    plan = assign(scenario, 4)
    assert len(plan) == 4 and all(q for _, q in plan)


def test_format_result_truncates():
    text = format_result(("x",), tuple((i,) for i in range(60)))
    assert text.splitlines()[0] == "x" and "(showing 50 of 60 rows)" in text
