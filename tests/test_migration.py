"""Alembic migration tests.

Proves `alembic upgrade head` builds a working schema from scratch on an empty
Postgres, then that a row can be inserted through the repository against it. A
second test walks the `embedding_columns` revision up and down on its own.

The test DB already carries the models' schema (created once by the session
`_schema` fixture), so these tests work on a *clean slate*: they drop and recreate
the `public` schema, run the migrations there, verify, and — crucially —
restore the models' schema in a `finally` so the rest of the session is
unaffected. The NullPool engines everywhere mean no connection outlives the drop.
"""

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text

from agent_memory.server import repository as repo
from agent_memory.server.db import make_sessionmaker
from agent_memory.server.models import Base, Memory
from agent_memory.server.schemas import TagIn
from conftest import PG_DSN, make_test_engine

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"

BASELINE = "310017671d9e"
EMBEDDING_COLUMNS = "62fcc84d6c84"


def _alembic(*args: str) -> None:
    """Run the alembic CLI against the test DB and fail loudly if it does not exit 0."""
    env = {
        **os.environ,
        "AGENT_MEMORY_DB": PG_DSN,
        "PYTHONPATH": str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"alembic {' '.join(args)} failed:\n{proc.stdout}\n{proc.stderr}"


async def _exec(sql: str):
    eng = make_test_engine()
    try:
        async with eng.begin() as conn:
            for stmt in sql.split(";"):
                if stmt.strip():
                    await conn.execute(text(stmt))
    finally:
        await eng.dispose()


async def _table_exists(name: str) -> bool:
    eng = make_test_engine()
    try:
        async with eng.connect() as conn:
            got = await conn.execute(text("SELECT to_regclass(:n)"), {"n": name})
            return got.scalar() is not None
    finally:
        await eng.dispose()


async def _create_all():
    eng = make_test_engine()
    try:
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await eng.dispose()


async def _columns(table: str, prefix: str) -> dict[str, tuple[str, bool]]:
    """Columns of `table` whose name starts with `prefix`, as
    {name: (postgres type name, nullable)} — e.g. {"embedding": ("_float4", True)}."""
    eng = make_test_engine()
    try:
        async with eng.connect() as conn:
            got = await conn.execute(
                text(
                    "SELECT column_name, udt_name, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name LIKE :p"
                ),
                {"t": table, "p": prefix + "%"},
            )
            return {name: (udt, nullable == "YES") for name, udt, nullable in got.all()}
    finally:
        await eng.dispose()


async def test_alembic_upgrade_head_builds_working_schema():
    # Clean slate: wipe everything the session fixture created.
    await _exec("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    try:
        _alembic("upgrade", "head")

        # The migration produced the real tables.
        assert await _table_exists("memories")
        assert await _table_exists("tags")
        assert await _table_exists("memory_tags")

        # And the schema actually works: add a row through the repo, read it back
        # (exercises the generated tsvector + tag wiring the migration must build).
        eng = make_test_engine()
        sm = make_sessionmaker(eng)
        try:
            async with sm() as s:
                async with s.begin():
                    mid = await repo.add(s, "migrated in", "tester", "proj",
                                         [TagIn(name="net")], "note")
                    row = await repo.get(s, mid)
                    assert row["content"] == "migrated in"
                    assert row["tags"] == ["net"]
                    hits = await repo.search(s, "migrated")
                    assert len(hits) == 1
        finally:
            await eng.dispose()
    finally:
        # Restore the models' schema for the remaining tests in the session.
        await _exec("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        await _create_all()


async def test_embedding_columns_revision_upgrades_and_downgrades():
    # Clean slate, then stop at the baseline: the columns must not exist yet.
    await _exec("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    try:
        _alembic("upgrade", BASELINE)
        assert await _columns("memories", "embedding") == {}

        # Upgrade one step: both columns appear, nullable, as real[] and text.
        _alembic("upgrade", EMBEDDING_COLUMNS)
        assert await _columns("memories", "embedding") == {
            "embedding": ("_float4", True),
            "embedding_model": ("text", True),
        }

        # The model can write a vector into the migrated table and read it back,
        # and the API-facing dump keeps both columns out of sight.
        eng = make_test_engine()
        sm = make_sessionmaker(eng)
        try:
            async with sm() as s:
                async with s.begin():
                    mid = await repo.add(s, "has a vector", "tester", "proj", [], "note")
                    m = await s.get(Memory, mid)
                    assert m.embedding is None and m.embedding_model is None
                    m.embedding = [0.5, -1.25, 2.0]
                    m.embedding_model = "test-model"
                async with s.begin():
                    m = await s.get(Memory, mid)
                    assert m.embedding == [0.5, -1.25, 2.0]
                    assert m.embedding_model == "test-model"
                    row = await repo.get(s, mid)
                    assert "embedding" not in row and "embedding_model" not in row
        finally:
            await eng.dispose()

        # Downgrade one step: both columns are gone, the rest of the table stays.
        _alembic("downgrade", BASELINE)
        assert await _columns("memories", "embedding") == {}
        assert await _table_exists("memories")
    finally:
        await _exec("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        await _create_all()
