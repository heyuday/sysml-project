import psycopg
import pytest

from agentsql.cache import DEFAULT_DSN, SqlCache


@pytest.fixture(scope="session")
def admin():
    """Separate connection for inspecting Postgres (pg_stat_statements etc.)."""
    try:
        conn = psycopg.connect(DEFAULT_DSN, autocommit=True)
    except psycopg.OperationalError as e:
        pytest.exit(f"Postgres not reachable at {DEFAULT_DSN}: run `docker compose up -d`\n{e}")
    yield conn
    conn.close()


@pytest.fixture
def cache():
    c = SqlCache(mode="exact")
    yield c
    c.close()


def db_calls(admin, marker: str) -> int:
    """How many times Postgres actually executed statements containing `marker`."""
    row = admin.execute(
        "SELECT coalesce(sum(calls), 0) FROM pg_stat_statements "
        "WHERE query LIKE %s AND query NOT LIKE '%%pg_stat_statements%%'",
        (f"%{marker}%",),
    ).fetchone()
    return int(row[0])
