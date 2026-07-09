"""Alembic migration smoke test.

Proves `alembic upgrade head` builds a working schema from scratch on an empty
Postgres, then that a row can be inserted through the repository against it.

The test DB already carries the models' schema (created once by the session
`_schema` fixture), so this test works on a *clean slate*: it drops and recreates
the `public` schema, runs the migrations there, verifies, and — crucially —
restores the models' schema in a `finally` so the rest of the session is
unaffected. The NullPool engines everywhere mean no connection outlives the drop.
"""

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent_memory.server import repository as repo
from agent_memory.server.models import Base
from agent_memory.server.schemas import TagIn
from conftest import PG_DSN, make_test_engine

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"


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


async def test_alembic_upgrade_head_builds_working_schema():
    # Clean slate: wipe everything the session fixture created.
    await _exec("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    try:
        env = {
            **os.environ,
            "AGENT_MEMORY_DB": PG_DSN,
            "PYTHONPATH": str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", ""),
        }
        proc = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 0, f"alembic failed:\n{proc.stdout}\n{proc.stderr}"

        # The migration produced the real tables.
        assert await _table_exists("memories")
        assert await _table_exists("tags")
        assert await _table_exists("memory_tags")

        # And the schema actually works: add a row through the repo, read it back
        # (exercises the generated tsvector + tag wiring the migration must build).
        eng = make_test_engine()
        sm = async_sessionmaker(eng, expire_on_commit=False, autoflush=False)
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
