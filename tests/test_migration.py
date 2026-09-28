"""Alembic migration tests.

`alembic upgrade head` builds a working schema on an empty Postgres, and a
row can be written through the repository against it. The other tests walk
one revision each, up and down.

A test that stops at an older revision cannot go through the repository for
a table the models have since grown: the ORM would ask for a column that does
not exist yet. Those tests check the migrated schema with plain SQL instead.

The test DB carries the models' schema (made once by the session `_schema`
fixture), so each test here starts from an empty `public` schema (the
`clean_slate` fixture) and puts the models' schema back when it ends, so the
rest of the session is unaffected. Alembic runs in process, in a thread,
since its env.py starts an event loop of its own.
"""

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from agent_memory.server import repository as repo
from agent_memory.server.db import make_sessionmaker
from agent_memory.server.schemas import TagIn
from conftest import PG_DSN, create_schema, make_test_engine

REPO_ROOT = Path(__file__).resolve().parent.parent

BASELINE = "310017671d9e"
EMBEDDING_COLUMNS = "62fcc84d6c84"
MEMORY_REVIEWS = "2906ffedb42f"
REVIEW_TAGS = "7c3e1a9d5b20"
REVIEW_STATUS = "b8e2f4a6c9d1"
SUPERSEDES = "d3f9a7c2e6b4"
TAG_EMBEDDING = "e5a1c7d9f2b3"
REVIEW_HISTORY = "f4c2a8e6d1b9"


async def _alembic(action: str, revision: str) -> None:
    """`alembic upgrade|downgrade <revision>` against the test DB."""
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    await asyncio.to_thread(getattr(command, action), cfg, revision)


@pytest.fixture
async def clean_slate(monkeypatch):
    """An empty `public` schema for the test; the models' schema after it."""
    monkeypatch.setenv("AGENT_MEMORY_DB", PG_DSN)
    await _exec("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    yield
    await _exec("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    await create_schema()


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


async def test_alembic_upgrade_head_builds_working_schema(clean_slate):
    # Clean slate: wipe everything the session fixture created.
    await _alembic("upgrade", "head")

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


async def test_embedding_columns_revision_upgrades_and_downgrades(clean_slate):
    # Clean slate, then stop at the baseline: the columns must not exist yet.
    await _alembic("upgrade", BASELINE)
    assert await _columns("memories", "embedding") == {}

    # Upgrade one step: both columns appear, nullable, as real[] and text.
    await _alembic("upgrade", EMBEDDING_COLUMNS)
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
    await _alembic("downgrade", BASELINE)
    assert await _columns("memories", "embedding") == {}
    assert await _table_exists("memories")


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


async def test_memory_reviews_revision_upgrades_and_downgrades(clean_slate):
    # Clean slate, then stop one step short: the table must not exist yet.
    await _alembic("upgrade", EMBEDDING_COLUMNS)
    assert not await _table_exists("memory_reviews")

    # Upgrade one step: the table appears with every column as designed.
    await _alembic("upgrade", MEMORY_REVIEWS)
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
    await _alembic("downgrade", EMBEDDING_COLUMNS)
    assert not await _table_exists("memory_reviews")
    assert await _table_exists("memories")


async def test_review_tags_revision_upgrades_and_downgrades(clean_slate):
    # Clean slate, then stop one step short: the column must not exist yet.
    await _alembic("upgrade", MEMORY_REVIEWS)
    assert await _columns("memory_reviews", "tags") == {}

    # Upgrade one step: the column appears, nullable, as text[].
    await _alembic("upgrade", REVIEW_TAGS)
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
    await _alembic("downgrade", MEMORY_REVIEWS)
    assert await _columns("memory_reviews", "tags") == {}
    assert await _scalar("SELECT count(*) FROM memory_reviews") == 2


async def _constraint_exists(name: str) -> bool:
    return await _scalar("SELECT count(*) FROM pg_constraint WHERE conname = :n", n=name) == 1


async def _statuses() -> list[str]:
    return await _column_values("SELECT review_status FROM memories ORDER BY id")


async def test_review_status_revision_fills_existing_rows_and_downgrades(clean_slate):
    # Clean slate, then stop one step short: the column must not exist yet.
    await _alembic("upgrade", REVIEW_TAGS)
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
    await _alembic("upgrade", REVIEW_STATUS)
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
    await _alembic("downgrade", REVIEW_TAGS)
    assert await _columns("memories", "review_status") == {}
    assert not await _constraint_exists("ck_memories_review_status")
    assert await _scalar("SELECT count(*) FROM pg_indexes WHERE indexname = "
                         "'ix_memories_review_status'") == 0
    assert await _scalar("SELECT count(*) FROM memories") == 5
    assert await _scalar("SELECT count(*) FROM memory_reviews") == 4

    # Upgrade again: the fill runs again from the review rows, `bare`
    # now among the approved ones.
    await _alembic("upgrade", REVIEW_STATUS)
    assert await _statuses() == ["verified", "flagged", "flagged", "verified", "unverified"]


async def _index_exists(name: str) -> bool:
    return await _scalar("SELECT count(*) FROM pg_indexes WHERE indexname = :n", n=name) == 1


async def test_supersedes_revision_upgrades_and_downgrades(clean_slate):
    # Clean slate, then stop one step short: the column must not exist yet.
    await _alembic("upgrade", REVIEW_STATUS)
    assert await _columns("memories", "supersedes") == {}
    assert not await _constraint_exists("fk_memories_supersedes")
    old = await _insert_memory("Chose SQLite because one agent writes at a time.")

    # Upgrade one step: the column appears, nullable, as bigint, with its
    # foreign key onto memories itself and its index; existing rows get
    # NULL.
    await _alembic("upgrade", SUPERSEDES)
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
    await _alembic("downgrade", REVIEW_STATUS)
    assert await _columns("memories", "supersedes") == {}
    assert not await _constraint_exists("fk_memories_supersedes")
    assert not await _index_exists("ix_memories_supersedes")
    assert await _scalar("SELECT count(*) FROM memories") == 2
    assert await _scalar("SELECT count(*) FROM memory_reviews") == 1


async def test_tag_embedding_revision_upgrades_and_downgrades(clean_slate):
    # Clean slate, then stop one step short: the columns must not exist yet.
    await _alembic("upgrade", SUPERSEDES)
    assert await _columns("tags", "embedding") == {}
    old = await _scalar("INSERT INTO tags (name, description) "
                        "VALUES ('db', 'the database layer') RETURNING id")

    # Upgrade one step: both columns appear, nullable, as real[] and text,
    # and the existing tag gets NULL.
    await _alembic("upgrade", TAG_EMBEDDING)
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
    await _alembic("downgrade", SUPERSEDES)
    assert await _columns("tags", "embedding") == {}
    assert await _scalar("SELECT count(*) FROM tags") == 2
    assert await _scalar("SELECT count(*) FROM memories") == 1


async def _primary_key() -> list[str]:
    return await _column_values(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "WHERE i.indrelid = 'memory_reviews'::regclass AND i.indisprimary ORDER BY a.attnum")


async def test_review_history_revision_upgrades_and_downgrades(clean_slate):
    # One step short: one review row per memory, keyed by it.
    await _alembic("upgrade", TAG_EMBEDDING)
    assert await _columns("memory_reviews", "id") == {}
    assert await _primary_key() == ["memory_id"]
    mid = await _insert_memory("Chose Postgres.")
    other = await _insert_memory("Something else.")
    await _exec(
        "INSERT INTO memory_reviews (memory_id, verdict, rule, reason, model, created_at) "
        f"VALUES ({mid}, 'reject', 2, 'a diary line', 'm', now() - interval '30 minutes'), "
        f"({other}, 'approve', NULL, 'fine', 'm', now() - interval '25 minutes')")
    with pytest.raises(IntegrityError, match="memory_reviews_pkey"):
        await _exec("INSERT INTO memory_reviews (memory_id, verdict, reason, model) "
                    f"VALUES ({mid}, 'approve', 'fine', 'm')")

    # Upgrade one step: `id` is the key, the rows are kept and have one, the
    # index is there, and a memory can now hold several rows.
    await _alembic("upgrade", REVIEW_HISTORY)
    assert await _columns("memory_reviews", "id") == {"id": ("int8", False)}
    assert await _primary_key() == ["id"]
    assert await _index_exists("ix_memory_reviews_memory_id_created_at")
    assert await _scalar("SELECT count(DISTINCT id) FROM memory_reviews") == 2
    await _exec(
        "INSERT INTO memory_reviews (memory_id, verdict, rule, reason, rewrite, model, created_at) "
        f"VALUES ({mid}, 'rewrite', 3, 'say why', 'Chose Postgres, because of X.', 'm', "
        "now() - interval '20 minutes'); "
        "INSERT INTO memory_reviews (memory_id, verdict, reason, model, created_at) "
        f"VALUES ({mid}, 'approve', 'fine now', 'm', now() - interval '10 minutes')")

    # The repository at head sees the three rows and shows the newest; deleting
    # a memory takes every row with it.
    eng = make_test_engine()
    try:
        async with make_sessionmaker(eng)() as s, s.begin():
            assert (await repo.get(s, mid))["review"]["verdict"] == "approve"
            assert [r["verdict"] for r in await repo.reviews(s, mid)] == [
                "approve", "rewrite", "reject"]
            await repo.delete(s, [other])
    finally:
        await eng.dispose()
    assert await _scalar("SELECT count(*) FROM memory_reviews") == 3

    # Downgrade one step: the newest row of the memory stays, the older ones
    # go, `memory_id` is the key again and refuses a second row.
    await _alembic("downgrade", TAG_EMBEDDING)
    assert await _columns("memory_reviews", "id") == {}
    assert not await _index_exists("ix_memory_reviews_memory_id_created_at")
    assert await _primary_key() == ["memory_id"]
    assert await _column_values("SELECT reason FROM memory_reviews") == ["fine now"]
    with pytest.raises(IntegrityError, match="memory_reviews_pkey"):
        await _exec("INSERT INTO memory_reviews (memory_id, verdict, reason, model) "
                    f"VALUES ({mid}, 'reject', 'again', 'm')")

    # Up again: the row kept gets an id and the history grows from it.
    await _alembic("upgrade", REVIEW_HISTORY)
    await _exec("INSERT INTO memory_reviews (memory_id, verdict, reason, model) "
                f"VALUES ({mid}, 'reject', 'again', 'm')")
    assert await _column_values("SELECT verdict FROM memory_reviews ORDER BY id") == [
        "approve", "reject"]
