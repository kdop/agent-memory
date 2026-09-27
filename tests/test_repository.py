"""Async unit tests for the data-access layer (`repository.py`), hitting a real
Postgres session directly — no HTTP. Complements the cross-surface suite by
pinning the repo functions in isolation: add+tags, query filters, search
snippet, update tag reconciliation, list_tags counts/descriptions, stats.

`asyncio_mode = auto` (pyproject) means these plain `async def` tests run without
a per-test decorator. Each gets a fresh NullPool engine so the async session is
bound to that test's own event loop.
"""

import pytest
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
    from datetime import datetime, timedelta

    from agent_memory.server.models import Memory

    mid = await repo.add(session, "today", "t", None, _tags(), None)
    assert len(await repo.query(session, since_days=0)) == 1
    # rolling window: today's entry stays visible at any N
    assert len(await repo.query(session, since_days=5)) == 1

    old_id = await repo.add(session, "ten days ago", "t", None, _tags(), None)
    old = await session.get(Memory, old_id)
    old.timestamp = datetime.now() - timedelta(days=10)
    await session.flush()
    assert [m["id"] for m in await repo.query(session, since_days=5)] == [mid]
    assert len(await repo.query(session, since_days=30)) == 2


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


# ── embedding on write ───────────────────────────────────────────────────────
async def _stored_vector(session, mid):
    """The two internal columns, read straight from the table: `_dump` leaves
    them out on purpose, so `repo.get` cannot show them."""
    from sqlalchemy import select

    from agent_memory.server.models import Memory

    stmt = select(Memory.embedding, Memory.embedding_model).where(Memory.id == mid)
    return (await session.execute(stmt)).one()


def _expected(text):
    from conftest import FakeEmbedder

    return FakeEmbedder().embed([text])[0]


async def test_add_stores_vector_and_model_name(session):
    from conftest import FakeEmbedder

    mid = await repo.add(session, "fox in the snow", "t", None, _tags(), None,
                         embedder=FakeEmbedder())
    vec, model = await _stored_vector(session, mid)
    assert model == "fake"
    # REAL is a 4-byte float, so the stored values come back a little rounded.
    assert vec == pytest.approx(_expected("fox in the snow"), abs=1e-6)


async def test_add_without_embedder_leaves_null(session):
    mid = await repo.add(session, "no model here", "t", None, _tags(), None)
    assert await _stored_vector(session, mid) == (None, None)


async def test_add_with_null_embedder_leaves_null(session):
    from agent_memory.server.embedding import NullEmbedder

    mid = await repo.add(session, "model is off", "t", None, _tags(), None,
                         embedder=NullEmbedder())
    assert await _stored_vector(session, mid) == (None, None)


async def test_update_content_replaces_vector(session):
    from conftest import FakeEmbedder

    emb = FakeEmbedder()
    mid = await repo.add(session, "first text", "t", None, _tags(), None, embedder=emb)
    await repo.update(session, mid, content="second text", embedder=emb)
    await session.flush()
    vec, model = await _stored_vector(session, mid)
    assert model == "fake"
    assert vec == pytest.approx(_expected("second text"), abs=1e-6)
    assert vec != pytest.approx(_expected("first text"), abs=1e-6)


async def test_update_tags_only_keeps_vector(session):
    from conftest import FakeEmbedder

    emb = FakeEmbedder()
    mid = await repo.add(session, "steady text", "t", None, _tags("a"), None, embedder=emb)
    await repo.update(session, mid, add_tags=_tags("b"), project="p", mtype="note",
                      embedder=emb)
    await session.flush()
    vec, model = await _stored_vector(session, mid)
    assert model == "fake"
    assert vec == pytest.approx(_expected("steady text"), abs=1e-6)


async def test_update_same_content_keeps_vector(session):
    from conftest import FakeEmbedder

    mid = await repo.add(session, "same text", "t", None, _tags(), None,
                         embedder=FakeEmbedder())
    # Same content is not a change, so the row keeps what it has, even when the
    # update runs without a model.
    changes = await repo.update(session, mid, content="same text")
    assert changes == []
    vec, model = await _stored_vector(session, mid)
    assert model == "fake"
    assert vec == pytest.approx(_expected("same text"), abs=1e-6)


async def test_update_content_without_model_clears_vector(session):
    from agent_memory.server.embedding import NullEmbedder
    from conftest import FakeEmbedder

    mid = await repo.add(session, "old text", "t", None, _tags(), None,
                         embedder=FakeEmbedder())
    await repo.update(session, mid, content="new text", embedder=NullEmbedder())
    await session.flush()
    assert await _stored_vector(session, mid) == (None, None)


# ── semantic search ──────────────────────────────────────────────────────────
async def _add_many(session, texts, embedder):
    return [await repo.add(session, t, "t", None, _tags(), None, embedder=embedder)
            for t in texts]


async def test_search_semantic_orders_by_cosine(session):
    from agent_memory.server.embedding import cosine
    from conftest import FakeEmbedder

    emb = FakeEmbedder()
    query = "the cat sat on the mat"
    await _add_many(session, ["a dog in the yard", "rain on the window", "coffee before code"], emb)
    exact = await repo.add(session, query, "t", None, _tags(), None, embedder=emb)

    hits = await repo.search_semantic(session, emb, query)
    assert len(hits) == 4
    # The memory that says the same thing as the query is the closest one.
    assert hits[0]["id"] == exact
    assert hits[0]["score"] == pytest.approx(1.0, abs=1e-6)
    assert hits[0]["snippet"] is None
    # The rest come in falling order of cosine, and the scores are the real cosines.
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True)
    qv = emb.embed([query])[0]
    for h in hits[1:]:
        assert h["score"] == pytest.approx(cosine(qv, emb.embed([h["content"]])[0]), abs=1e-6)


async def test_search_semantic_breaks_ties_by_id_descending(session):
    from conftest import FakeEmbedder

    emb = FakeEmbedder()
    # Same text, same vector, same score: the newer row wins.
    ids = await _add_many(session, ["twin", "twin"], emb)
    hits = await repo.search_semantic(session, emb, "twin")
    assert [h["id"] for h in hits] == sorted(ids, reverse=True)
    assert hits[0]["score"] == pytest.approx(hits[1]["score"])


async def test_search_semantic_applies_filters(session):
    from conftest import FakeEmbedder

    emb = FakeEmbedder()
    a = await repo.add(session, "alpha thing", "ann", "alpha", _tags("x"), None, embedder=emb)
    b = await repo.add(session, "beta thing", "bob", "beta", _tags("y"), None, embedder=emb)
    c = await repo.add(session, "alpha other", "bob", "alpha", _tags("y"), None, embedder=emb)

    async def ids(**filters):
        return {h["id"] for h in await repo.search_semantic(session, emb, "q", **filters)}

    assert await ids(project="alpha") == {a, c}
    assert await ids(agent="bob") == {b, c}
    assert await ids(tag="X") == {a}
    assert await ids(project="alpha", agent="bob") == {c}
    assert await ids(since="2999-01-01") == set()


async def test_search_semantic_skips_rows_without_a_vector(session):
    from conftest import FakeEmbedder

    emb = FakeEmbedder()
    with_vec = await repo.add(session, "has a vector", "t", None, _tags(), None, embedder=emb)
    await repo.add(session, "has no vector", "t", None, _tags(), None)
    hits = await repo.search_semantic(session, emb, "vector")
    assert [h["id"] for h in hits] == [with_vec]


async def test_search_semantic_applies_limit(session):
    from conftest import FakeEmbedder

    emb = FakeEmbedder()
    await _add_many(session, ["one", "two", "three", "four"], emb)
    everything = await repo.search_semantic(session, emb, "q")
    assert len(everything) == 4
    assert len(await repo.search_semantic(session, emb, "q", limit=0)) == 4
    assert len(await repo.search_semantic(session, emb, "q", limit=None)) == 4
    top2 = await repo.search_semantic(session, emb, "q", limit=2)
    assert [h["id"] for h in top2] == [h["id"] for h in everything[:2]]


async def test_search_keyword_carries_rank_as_score(session):
    await repo.add(session, "fox fox fox", "t", None, _tags(), None)
    await repo.add(session, "one fox among many other words here", "t", None, _tags(), None)
    hits = await repo.search(session, "fox")
    assert len(hits) == 2
    assert all(isinstance(h["score"], float) and h["score"] > 0 for h in hits)
    assert hits[0]["score"] >= hits[1]["score"]
    assert hits[0]["snippet"] is not None
    # Rows from a plain read carry no score.
    assert (await repo.get(session, hits[0]["id"]))["score"] is None


# ── reindex ──────────────────────────────────────────────────────────────────
async def _set_model(session, mid, model):
    """Pretend the row was embedded by another model, without touching the vector."""
    from sqlalchemy import update as sql_update

    from agent_memory.server.models import Memory

    await session.execute(sql_update(Memory).where(Memory.id == mid).values(embedding_model=model))
    session.expire_all()


class _CountingEmbedder:
    """FakeEmbedder that remembers how big each `embed` call was."""

    model_name = "fake"
    dim = 8

    def __init__(self):
        from conftest import FakeEmbedder

        self._inner = FakeEmbedder()
        self.calls = []

    def embed(self, texts):
        self.calls.append(len(texts))
        return self._inner.embed(texts)


async def test_reindex_fills_rows_without_vectors(session):
    from conftest import FakeEmbedder

    a = await repo.add(session, "no vector yet", "t", None, _tags(), None)
    b = await repo.add(session, "also none", "t", None, _tags(), None)
    assert await _stored_vector(session, a) == (None, None)

    assert await repo.reindex(session, FakeEmbedder()) == 2
    for mid, text in ((a, "no vector yet"), (b, "also none")):
        vec, model = await _stored_vector(session, mid)
        assert model == "fake"
        assert vec == pytest.approx(_expected(text), abs=1e-6)


async def test_reindex_replaces_vectors_from_another_model(session):
    from conftest import FakeEmbedder

    mid = await repo.add(session, "old model text", "t", None, _tags(), None,
                         embedder=FakeEmbedder())
    await _set_model(session, mid, "old-model")

    assert await repo.reindex(session, FakeEmbedder()) == 1
    vec, model = await _stored_vector(session, mid)
    assert model == "fake"
    assert vec == pytest.approx(_expected("old model text"), abs=1e-6)


async def test_reindex_leaves_current_model_rows_alone(session):
    emb = _CountingEmbedder()
    mid = await repo.add(session, "already done", "t", None, _tags(), None, embedder=emb)
    emb.calls.clear()

    assert await repo.reindex(session, emb) == 0
    assert emb.calls == []
    vec, model = await _stored_vector(session, mid)
    assert model == "fake"
    assert vec == pytest.approx(_expected("already done"), abs=1e-6)


async def test_reindex_touches_only_the_stale_rows(session):
    from conftest import FakeEmbedder

    done = await repo.add(session, "current", "t", None, _tags(), None, embedder=FakeEmbedder())
    missing = await repo.add(session, "missing", "t", None, _tags(), None)
    stale = await repo.add(session, "stale", "t", None, _tags(), None, embedder=FakeEmbedder())
    await _set_model(session, stale, "old-model")

    assert await repo.reindex(session, FakeEmbedder()) == 2
    for mid, text in ((done, "current"), (missing, "missing"), (stale, "stale")):
        vec, model = await _stored_vector(session, mid)
        assert model == "fake"
        assert vec == pytest.approx(_expected(text), abs=1e-6)
    # A second run finds nothing left to do.
    assert await repo.reindex(session, FakeEmbedder()) == 0


async def test_reindex_embeds_in_batches_in_id_order(session):
    for i in range(5):
        await repo.add(session, f"row {i}", "t", None, _tags(), None)
    emb = _CountingEmbedder()

    assert await repo.reindex(session, emb, batch=2) == 5
    assert emb.calls == [2, 2, 1]
    for mid in range(1, 6):
        vec, model = await _stored_vector(session, mid)
        assert model == "fake"
        assert vec == pytest.approx(_expected(f"row {mid - 1}"), abs=1e-6)


async def test_reindex_without_model_does_nothing(session):
    from agent_memory.server.embedding import NullEmbedder

    mid = await repo.add(session, "left alone", "t", None, _tags(), None)
    assert await repo.reindex(session, None) == 0
    assert await repo.reindex(session, NullEmbedder()) == 0
    assert await _stored_vector(session, mid) == (None, None)
