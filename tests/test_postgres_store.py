"""Postgres-specific tests (Ticket F): engine selection + SQLite→Postgres migration.

Behavior parity (PostgresStore satisfies the same contract as SqliteStore) is covered
by the `pg` surface in the cross-surface suite (test_behaviors.py via conftest).
These pin the bits unique to Postgres. The migration test needs a live instance
(`AGENT_MEMORY_TEST_PG_DSN`, set by CI's postgres service or a local podman run).
"""

import os

import pytest

pytest.importorskip("psycopg")

import psycopg  # noqa: E402

from agent_memory.pg_store import PostgresStore, migrate_sqlite_to_postgres  # noqa: E402
from agent_memory.store import SqliteStore, get_store  # noqa: E402

PG_DSN = os.environ.get("AGENT_MEMORY_TEST_PG_DSN")
needs_pg = pytest.mark.skipif(not PG_DSN, reason="AGENT_MEMORY_TEST_PG_DSN not set")


# ── engine selection (no live server needed — __init__ does not connect) ────
def test_get_store_selects_postgres_for_dsn(monkeypatch):
    monkeypatch.delenv("AGENT_MEMORY_API", raising=False)
    monkeypatch.setenv("AGENT_MEMORY_DB", "postgresql://u:p@localhost:5432/db")
    assert isinstance(get_store(), PostgresStore)


def test_get_store_selects_sqlite_for_path(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_MEMORY_API", raising=False)
    monkeypatch.setenv("AGENT_MEMORY_DB", str(tmp_path / "memory.db"))
    assert isinstance(get_store(), SqliteStore)


# ── migration ───────────────────────────────────────────────────────────────
@pytest.fixture
def pg(tmp_path):
    store = PostgresStore(PG_DSN)
    store.initialize()
    with psycopg.connect(PG_DSN) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE memories, tags, memory_tags RESTART IDENTITY CASCADE")
    return store


@needs_pg
def test_migrate_sqlite_to_postgres_preserves_data(pg, tmp_path):
    # Seed a SQLite DB.
    sqlite_path = tmp_path / "source.db"
    src = SqliteStore(sqlite_path)
    src.initialize()
    m1 = src.add("the quick brown fox", "tester", "alpha", ["animal", "fast"], "note")
    m2 = src.add("a slow green turtle", "clu", "beta", ["animal"], "lesson")

    result = migrate_sqlite_to_postgres(str(sqlite_path), PG_DSN)
    assert result["memories"] == 2
    assert result["tags"] == 2  # distinct: animal, fast (turtle reuses animal)

    # Ids preserved.
    got1 = pg.get(m1)
    assert got1["content"] == "the quick brown fox"
    assert got1["project"] == "alpha"
    assert sorted(got1["tags"]) == ["animal", "fast"]
    assert pg.get(m2)["agent"] == "clu"

    # FTS works on the migrated rows.
    hits = pg.search("brown")
    assert len(hits) == 1
    assert "brown" in hits[0]["snippet"]

    # A fresh add continues after the migrated ids (sequence advanced).
    m3 = pg.add("new after migration", "tester", None, [], None)
    assert m3 > m2


@needs_pg
def test_migrate_is_idempotent(pg, tmp_path):
    sqlite_path = tmp_path / "source.db"
    src = SqliteStore(sqlite_path)
    src.initialize()
    src.add("only memory", "tester", None, ["solo"], None)

    migrate_sqlite_to_postgres(str(sqlite_path), PG_DSN)
    migrate_sqlite_to_postgres(str(sqlite_path), PG_DSN)  # again — must not duplicate
    assert pg.stats()["total"] == 1
    assert pg.stats()["tags"] == 1
