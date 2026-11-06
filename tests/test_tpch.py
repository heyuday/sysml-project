"""Mock agents on TPC-H (skipped unless scripts/load_tpch.py has loaded a `tpch` DB)."""

import psycopg
import pytest

import run_agents
from agentsql.analyze import load, summarize
from agentsql.cache import TPCH_DSN
from agentsql.tasks import TPCH_TASKS


def _tpch_loaded() -> bool:
    try:
        with psycopg.connect(TPCH_DSN, autocommit=True) as c:
            return c.execute("SELECT count(*) FROM tpch_meta").fetchone()[0] > 0
    except psycopg.Error:
        return False


pytestmark = pytest.mark.skipif(not _tpch_loaded(), reason="tpch DB not loaded")


@pytest.mark.parametrize("task", list(TPCH_TASKS))
def test_mock_agents_answer_tpch_tasks(tmp_path, task):
    path = str(tmp_path / "run.jsonl")
    run_agents.main(["--backend", "mock", "--dataset", "tpch", "--task", task,
                     "--scenario", "paraphrase", "--agents", "3", "--aware",
                     "--log", path, "--quiet"])
    s = summarize(load(path))
    assert all(f["correct"] for f in s["finals"]), [f["answer"] for f in s["finals"]]
    assert s["by_tool"]["run_sql"]["repeat_cross_agent"] == 2  # followers rerun the cached query
