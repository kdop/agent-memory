"""Alembic migration tests.

Proves `alembic upgrade head` builds a working schema from scratch on an empty
Postgres, then that a row can be inserted through the repository against it.
The other tests walk one revision each up and down on its own.

A test that stops at an older revision cannot go through the repository for a
table the models have since grown: the ORM would ask for a column that does not
exist yet. Those tests check the migrated schema with plain SQL instead.

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

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from agent_memory.server import repository as repo
from agent_memory.server.db import make_sessionmaker
from agent_memory.server.models import Base
from agent_memory.server.schemas import TagIn
from conftest import PG_DSN, make_test_engine

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"

BASELINE = "310017671d9e"
EMBEDDING_COLUMNS = "62fcc84d6c84"
MEMORY_REVIEWS = "2906ffedb42f"
REVIEW_TAGS = "7c3e1a9d5b20"
REVIEW_STATUS = "b8e2f4a6c9d1"
SUPERSEDES = "d3f9a7c2e6b4"
TAG_EMBEDDING = "e5a1c7d9f2b3"
REVIEW_HISTORY = "f4c2a8e6d1b9"  # walked in tests/test_review_history.py


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
                    assert row["review_status"] == "unverified"
                    assert row["supersedes"] is None and row["superseded_by"] is None
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

        # A vector can be written into the migrated table and read back. Plain
        # SQL: the models have since grown columns this revision does not have
        # (see the module docstring).
        mid = await _scalar("INSERT INTO memories (agent, content) VALUES ('tester', 'has a vector') "
                            "RETURNING id")
        assert await _scalar("SELECT embedding FROM memories WHERE id = :m", m=mid) is None
        assert await _scalar("SELECT embedding_model FROM memories WHERE id = :m", m=mid) is None
        await _exec(f"UPDATE memories SET embedding = ARRAY[0.5, -1.25, 2.0]::real[], "
                    f"embedding_model = 'test-model' WHERE id = {mid}")
        assert list(await _scalar("SELECT embedding FROM memories WHERE id = :m", m=mid)) == [0.5, -1.25, 2.0]
        assert await _scalar("SELECT embedding_model FROM memories WHERE id = :m", m=mid) == "test-model"

        # Downgrade one step: both columns are gone, the rest of the table stays.
        _alembic("downgrade", BASELINE)
        assert await _columns("memories", "embedding") == {}
        assert await _table_exists("memories")
    finally:
        await _exec("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        await _create_all()


async def _scalar(sql: str, **params):
    eng = make_test_engine()
    try:
        async with eng.begin() as conn:
            return (await conn.execute(text(sql), params)).scalar()
    finally:
        await eng.dispose()


async def _column_values(sql: str, **params) -> list:
    """The first column of every row `sql` returns, as a list."""
    eng = make_test_engine()
    try:
        async with eng.begin() as conn:
            return list((await conn.execute(text(sql), params)).scalars().all())
    finally:
        await eng.dispose()


async def _insert_memory(content: str, **columns) -> int:
    """Insert a memory with plain SQL (for a revision the ORM has outgrown)
    and return its id. `columns` are extra column values, bound as parameters."""
    names = ", ".join(["agent", "project", "content", *columns])
    values = ", ".join(["'tester'", "'proj'", ":content", *(f":{c}" for c in columns)])
    return await _scalar(f"INSERT INTO memories ({names}) VALUES ({values}) RETURNING id",
                         content=content, **columns)


async def test_memory_reviews_revision_upgrades_and_downgrades():
    # Clean slate, then stop one step short: the table must not exist yet.
    await _exec("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    try:
        _alembic("upgrade", EMBEDDING_COLUMNS)
        assert not await _table_exists("memory_reviews")

        # Upgrade one step: the table appears with every column as designed.
        _alembic("upgrade", MEMORY_REVIEWS)
        assert await _columns("memory_reviews", "") == {
            "memory_id": ("int8", False),
            "verdict": ("text", False),
            "rule": ("int4", True),
            "reason": ("text", False),
            "rewrite": ("text", True),
            "duplicate_of": ("int8", True),
            "model": ("text", False),
            "created_at": ("timestamptz", False),
        }

        # A verdict can be written into the migrated table; deleting the memory
        # takes its review with it, and deleting the memory a verdict points at
        # clears `duplicate_of`. Plain SQL: the models have since grown columns
        # this revision does not have (see the module docstring).
        first = await _insert_memory("the original")
        second = await _insert_memory("the same again")
        await _exec(
            "INSERT INTO memory_reviews (memory_id, verdict, reason, duplicate_of, model) "
            f"VALUES ({second}, 'reject', 'says the same as #{first}', {first}, 'test-model')")
        assert await _scalar("SELECT duplicate_of FROM memory_reviews WHERE memory_id = :m",
                             m=second) == first
        await _exec(f"DELETE FROM memories WHERE id = {first}")
        assert await _scalar("SELECT duplicate_of FROM memory_reviews WHERE memory_id = :m",
                             m=second) is None
        await _exec(f"DELETE FROM memories WHERE id = {second}")
        assert await _scalar("SELECT count(*) FROM memory_reviews") == 0

        # Downgrade one step: the table is gone, the memories table stays.
        _alembic("downgrade", EMBEDDING_COLUMNS)
        assert not await _table_exists("memory_reviews")
        assert await _table_exists("memories")
    finally:
        await _exec("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        await _create_all()


async def test_review_tags_revision_upgrades_and_downgrades():
    # Clean slate, then stop one step short: the column must not exist yet.
    await _exec("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    try:
        _alembic("upgrade", MEMORY_REVIEWS)
        assert await _columns("memory_reviews", "tags") == {}

        # Upgrade one step: the column appears, nullable, as text[].
        _alembic("upgrade", REVIEW_TAGS)
        assert await _columns("memory_reviews", "tags") == {"tags": ("_text", True)}

        # A verdict with tags goes into the migrated table and comes back; one
        # without tags leaves the column NULL. Plain SQL: the models have
        # since grown a column this revision does not have (see the module
        # docstring).
        first = await _insert_memory("the original")
        second = await _insert_memory("the same again")
        await _exec(
            "INSERT INTO memory_reviews (memory_id, verdict, rule, reason, rewrite, tags, model) "
            f"VALUES ({first}, 'rewrite', 3, 'say why', 'the original, because of X', "
            "ARRAY['db', 'search']::text[], 'test-model'); "
            "INSERT INTO memory_reviews (memory_id, verdict, reason, duplicate_of, model) "
            f"VALUES ({second}, 'reject', 'says the same as #1', {first}, 'test-model')")
        assert await _column_values("SELECT tags FROM memory_reviews ORDER BY memory_id") == [
            ["db", "search"], None]

        # Downgrade one step: the column is gone, the table and its rows stay.
        _alembic("downgrade", MEMORY_REVIEWS)
        assert await _columns("memory_reviews", "tags") == {}
        assert await _scalar("SELECT count(*) FROM memory_reviews") == 2
    finally:
        await _exec("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        await _create_all()


async def _constraint_exists(name: str) -> bool:
    return await _scalar("SELECT count(*) FROM pg_constraint WHERE conname = :n", n=name) == 1


async def _statuses() -> list[str]:
    return await _column_values("SELECT review_status FROM memories ORDER BY id")


async def test_review_status_revision_fills_existing_rows_and_downgrades():
    # Clean slate, then stop one step short: the column must not exist yet.
    await _exec("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    try:
        _alembic("upgrade", REVIEW_TAGS)
        assert await _columns("memories", "review_status") == {}
        assert not await _constraint_exists("ck_memories_review_status")

        # Four memories written before the column existed: one approved, one
        # rejected, one with a rewrite suggested, one never reviewed.
        approved = await _insert_memory("Chose Postgres because several agents write at once.")
        rejected = await _insert_memory("Spent the afternoon tidying.")
        rewritten = await _insert_memory("Chose Postgres.")
        bare = await _insert_memory("Not reviewed yet.")
        await _exec(
            "INSERT INTO memory_reviews (memory_id, verdict, reason, model) VALUES "
            f"({approved}, 'approve', 'fine', 'm'), "
            f"({rejected}, 'reject', 'a diary line', 'm'), "
            f"({rewritten}, 'rewrite', 'say why', 'm')")

        # Upgrade one step: the column appears, NOT NULL with the default,
        # the CHECK and the index are there, and existing rows are filled
        # from their review row.
        _alembic("upgrade", REVIEW_STATUS)
        assert await _columns("memories", "review_status") == {"review_status": ("text", False)}
        assert await _scalar(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name = 'memories' AND column_name = 'review_status'"
        ) == "'unverified'::text"
        assert await _constraint_exists("ck_memories_review_status")
        assert await _scalar("SELECT count(*) FROM pg_indexes WHERE indexname = "
                             "'ix_memories_review_status'") == 1
        assert await _statuses() == ["verified", "flagged", "flagged", "unverified"]

        # A row inserted without a status gets the default; a value outside
        # the three is refused by the CHECK.
        new = await _insert_memory("written after the upgrade")
        assert await _scalar("SELECT review_status FROM memories WHERE id = :m", m=new) == "unverified"
        with pytest.raises(IntegrityError, match="ck_memories_review_status"):
            await _insert_memory("a made-up status", review_status="maybe")
        assert await _scalar("SELECT count(*) FROM memories") == 5

        # A verdict stored now sets the status, the way `set_review` does.
        # Plain SQL: the models have since grown a column this revision does
        # not have (see the module docstring).
        await _exec(
            "INSERT INTO memory_reviews (memory_id, verdict, reason, model) VALUES "
            f"({bare}, 'approve', 'fine', 'm'); "
            f"UPDATE memories SET review_status = 'verified' WHERE id = {bare}")
        assert await _scalar("SELECT review_status FROM memories WHERE id = :m", m=bare) == "verified"
        assert await _scalar("SELECT review_status FROM memories WHERE id = :m", m=new) == "unverified"

        # Downgrade one step: the column, the CHECK and the index are gone;
        # the memories and their reviews stay.
        _alembic("downgrade", REVIEW_TAGS)
        assert await _columns("memories", "review_status") == {}
        assert not await _constraint_exists("ck_memories_review_status")
        assert await _scalar("SELECT count(*) FROM pg_indexes WHERE indexname = "
                             "'ix_memories_review_status'") == 0
        assert await _scalar("SELECT count(*) FROM memories") == 5
        assert await _scalar("SELECT count(*) FROM memory_reviews") == 4

        # Upgrade again: the fill runs again from the review rows, `bare`
        # now among the approved ones.
        _alembic("upgrade", REVIEW_STATUS)
        assert await _statuses() == ["verified", "flagged", "flagged", "verified", "unverified"]
    finally:
        await _exec("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        await _create_all()


async def _index_exists(name: str) -> bool:
    return await _scalar("SELECT count(*) FROM pg_indexes WHERE indexname = :n", n=name) == 1


async def test_supersedes_revision_upgrades_and_downgrades():
    # Clean slate, then stop one step short: the column must not exist yet.
    await _exec("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    try:
        _alembic("upgrade", REVIEW_STATUS)
        assert await _columns("memories", "supersedes") == {}
        assert not await _constraint_exists("fk_memories_supersedes")
        old = await _insert_memory("Chose SQLite because one agent writes at a time.")

        # Upgrade one step: the column appears, nullable, as bigint, with its
        # foreign key onto memories itself and its index; existing rows get
        # NULL.
        _alembic("upgrade", SUPERSEDES)
        assert await _columns("memories", "supersedes") == {"supersedes": ("int8", True)}
        assert await _constraint_exists("fk_memories_supersedes")
        assert await _index_exists("ix_memories_supersedes")
        assert await _scalar("SELECT supersedes FROM memories WHERE id = :m", m=old) is None

        # A link can be written and read back; one to a memory that does not
        # exist is refused; deleting the old memory clears the link on the
        # new one and keeps the new one.
        new = await _insert_memory("Moved to Postgres because several agents write at once.",
                                   supersedes=old)
        assert await _scalar("SELECT supersedes FROM memories WHERE id = :m", m=new) == old
        with pytest.raises(IntegrityError, match="fk_memories_supersedes"):
            await _insert_memory("points nowhere", supersedes=999)
        await _exec(f"DELETE FROM memories WHERE id = {old}")
        assert await _scalar("SELECT supersedes FROM memories WHERE id = :m", m=new) is None
        assert await _scalar("SELECT count(*) FROM memories") == 1

        # A verdict that names an older memory sets the link, the way
        # `set_review` does. Plain SQL: the models have since grown columns
        # this revision does not have (see the module docstring).
        again = await _insert_memory("the old one, again")
        await _exec(
            "INSERT INTO memory_reviews (memory_id, verdict, reason, model) VALUES "
            f"({new}, 'approve', 'fine', 'm'); "
            f"UPDATE memories SET supersedes = {again} WHERE id = {new}")
        assert await _scalar("SELECT supersedes FROM memories WHERE id = :m", m=new) == again
        assert await _column_values("SELECT id FROM memories WHERE supersedes = :m", m=again) == [new]

        # Downgrade one step: the index, the foreign key and the column are
        # gone; the memories and their reviews stay.
        _alembic("downgrade", REVIEW_STATUS)
        assert await _columns("memories", "supersedes") == {}
        assert not await _constraint_exists("fk_memories_supersedes")
        assert not await _index_exists("ix_memories_supersedes")
        assert await _scalar("SELECT count(*) FROM memories") == 2
        assert await _scalar("SELECT count(*) FROM memory_reviews") == 1
    finally:
        await _exec("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        await _create_all()


async def test_tag_embedding_revision_upgrades_and_downgrades():
    # Clean slate, then stop one step short: the columns must not exist yet.
    await _exec("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    try:
        _alembic("upgrade", SUPERSEDES)
        assert await _columns("tags", "embedding") == {}
        old = await _scalar("INSERT INTO tags (name, description) "
                            "VALUES ('db', 'the database layer') RETURNING id")

        # Upgrade one step: both columns appear, nullable, as real[] and text,
        # and the existing tag gets NULL.
        _alembic("upgrade", TAG_EMBEDDING)
        assert await _columns("tags", "embedding") == {
            "embedding": ("_float4", True),
            "embedding_model": ("text", True),
        }
        assert await _scalar("SELECT embedding FROM tags WHERE id = :t", t=old) is None
        assert await _scalar("SELECT embedding_model FROM tags WHERE id = :t", t=old) is None
        await _exec(f"UPDATE tags SET embedding = ARRAY[0.5, -1.25, 2.0]::real[], "
                    f"embedding_model = 'test-model' WHERE id = {old}")
        assert list(await _scalar("SELECT embedding FROM tags WHERE id = :t", t=old)) == [0.5, -1.25, 2.0]
        assert await _scalar("SELECT embedding_model FROM tags WHERE id = :t", t=old) == "test-model"

        # The repository sees the columns at this revision (it is head): a tag
        # made through it stores a vector, and reindex replaces the one from
        # the other model.
        from conftest import FakeEmbedder

        eng = make_test_engine()
        sm = make_sessionmaker(eng)
        try:
            async with sm() as s, s.begin():
                await repo.add(s, "a memory with a tag", "tester", "proj",
                               [TagIn(name="ui", description="the dashboard")], "note",
                               embedder=FakeEmbedder())
                assert await repo.reindex(s, FakeEmbedder()) == {"updated": 0, "tags": 1}
        finally:
            await eng.dispose()
        assert await _column_values("SELECT embedding_model FROM tags ORDER BY id") == ["fake", "fake"]
        assert await _scalar("SELECT embedding FROM tags WHERE id = :t", t=old) != [0.5, -1.25, 2.0]

        # Downgrade one step: both columns are gone, the tags stay.
        _alembic("downgrade", SUPERSEDES)
        assert await _columns("tags", "embedding") == {}
        assert await _scalar("SELECT count(*) FROM tags") == 2
        assert await _scalar("SELECT count(*) FROM memories") == 1
    finally:
        await _exec("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        await _create_all()
