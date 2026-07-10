"""Async unit tests for the data-access layer (`repository.py`), hitting a real
Postgres session directly — no HTTP. Complements the cross-surface suite by
pinning the repo functions in isolation: add+tags, query filters, search
snippet, update tag reconciliation, list_tags counts/descriptions, stats.

`asyncio_mode = auto` (pyproject) means these plain `async def` tests run without
a per-test decorator. Each gets a fresh NullPool engine so the async session is
bound to that test's own event loop.
"""

import pytest_asyncio

from agent_memory.server import repository as repo
from agent_memory.server.db import make_sessionmaker
from agent_memory.server.schemas import TagIn
from conftest import make_test_engine


@pytest_asyncio.fixture
async def session():
    """A committed-on-success AsyncSession on the (already-truncated) test DB. Uses
    the same sessionmaker the real server does — a hand-rolled one here previously
    drifted out of sync (autoflush=False) and masked a real repository bug."""
    engine = make_test_engine()
    sm = make_sessionmaker(engine)
    async with sm() as s:
        async with s.begin():
            yield s
    await engine.dispose()


def _tags(*specs):
    """Build TagIn objects from (name) or (name, description) tuples/strings."""
    out = []
    for spec in specs:
        if isinstance(spec, str):
            out.append(TagIn(name=spec))
        else:
            out.append(TagIn(name=spec[0], description=spec[1]))
    return out


async def test_add_returns_id_and_stores_row(session):
    mid = await repo.add(session, "hello world", "tester", "proj", _tags(), "note")
    assert mid == 1
    row = await repo.get(session, mid)
    assert row["content"] == "hello world"
    assert row["agent"] == "tester"
    assert row["project"] == "proj"
    assert row["type"] == "note"


async def test_add_wires_tags_alphabetically(session):
    mid = await repo.add(session, "x", "tester", None, _tags("shopping", "food"), None)
    row = await repo.get(session, mid)
    assert row["tags"] == ["food", "shopping"]   # ORDER BY name


async def test_add_reuses_existing_tag_case_insensitively(session):
    await repo.add(session, "a", "t", None, _tags("Auth"), None)
    await repo.add(session, "b", "t", None, _tags("auth"), None)
    tags = await repo.list_tags(session)
    names = [t["name"] for t in tags]
    assert names.count("Auth") + names.count("auth") == 1  # a single tag row
    assert tags[0]["count"] == 2


async def test_query_project_and_type_filters(session):
    await repo.add(session, "a", "t", "alpha", _tags(), "decision")
    await repo.add(session, "b", "t", "beta", _tags(), "note")
    assert len(await repo.query(session, project="alpha")) == 1
    assert len(await repo.query(session, mtype="decision")) == 1
    assert len(await repo.query(session, agent="t")) == 2


async def test_query_tag_filter_and_limit(session):
    await repo.add(session, "one", "t", None, _tags("x"), None)
    await repo.add(session, "two", "t", None, _tags("x"), None)
    await repo.add(session, "three", "t", None, _tags("y"), None)
    assert len(await repo.query(session, tag="x")) == 2
    assert len(await repo.query(session, limit=1)) == 1


async def test_query_since_days_window(session):
    await repo.add(session, "today", "t", None, _tags(), None)
    assert len(await repo.query(session, since_days=0)) == 1
    assert await repo.query(session, since_days=5) == []


async def test_search_matches_and_snippets(session):
    await repo.add(session, "the quick brown fox", "t", None, _tags(), None)
    hits = await repo.search(session, "brown")
    assert len(hits) == 1
    snip = hits[0]["snippet"]
    assert "brown" in snip and "→" in snip and "←" in snip
    assert await repo.search(session, "zzzznope") == []


async def test_update_content_and_reindex(session):
    mid = await repo.add(session, "findme orangutan", "t", None, _tags(), None)
    changes = await repo.update(session, mid, content="replaced penguin")
    assert "content" in changes
    await session.flush()
    assert await repo.search(session, "orangutan") == []
    assert len(await repo.search(session, "penguin")) == 1


async def test_update_tag_reconciliation(session):
    mid = await repo.add(session, "x", "t", None, _tags("keep", "drop"), None)
    await repo.update(session, mid, add_tags=_tags("new"), remove_tags=["drop"])
    row = await repo.get(session, mid)
    assert set(row["tags"]) == {"keep", "new"}


async def test_update_set_tags_replaces(session):
    mid = await repo.add(session, "x", "t", None, _tags("a", "b"), None)
    await repo.update(session, mid, set_tags=_tags("c"))
    row = await repo.get(session, mid)
    assert row["tags"] == ["c"]


async def test_update_not_found_returns_none(session):
    assert await repo.update(session, 999, content="y") is None


async def test_list_tags_counts_and_descriptions(session):
    await repo.add(session, "a", "t", None, _tags(("db", "the database")), None)
    await repo.add(session, "b", "t", None, _tags("db"), None)
    await repo.add(session, "c", "t", None, _tags("solo"), None)
    tags = {t["name"]: t for t in await repo.list_tags(session)}
    assert tags["db"]["count"] == 2
    assert tags["db"]["description"] == "the database"
    assert tags["solo"]["description"] == "solo"  # auto-defaulted to its own name


async def test_list_tags_excludes_zero_count(session):
    mid = await repo.add(session, "a", "t", None, _tags("orphan"), None)
    await repo.update(session, mid, set_tags=[])
    names = [t["name"] for t in await repo.list_tags(session)]
    assert "orphan" not in names


async def test_list_projects_counts(session):
    await repo.add(session, "a", "t", "alpha", _tags(), None)
    await repo.add(session, "b", "t", "alpha", _tags(), None)
    await repo.add(session, "c", "t", None, _tags(), None)  # null project excluded
    projects = dict((p["project"], p["count"]) for p in await repo.list_projects(session))
    assert projects == {"alpha": 2}


async def test_delete_and_get_many(session):
    m1 = await repo.add(session, "a", "t", None, _tags(), None)
    m2 = await repo.add(session, "b", "t", None, _tags(), None)
    rows = await repo.get_many(session, [m1, m2, 999])
    assert {r["id"] for r in rows} == {m1, m2}
    await repo.delete(session, [m1])
    assert await repo.get(session, m1) is None
    assert await repo.get(session, m2) is not None


async def test_stats_aggregates(session):
    await repo.add(session, "a", "clu", "alpha", _tags("t"), None)
    await repo.add(session, "b", "tron", "beta", _tags(), None)
    s = await repo.stats(session)
    assert s["total"] == 2
    assert s["agents"] == 2
    assert s["projects"] == 2
    assert s["tags"] == 1
    assert s["today"] == 2
    assert s["oldest"] is not None
    assert s["newest"] is not None
