"""Vector work runs off the event loop, and tags keep their vectors (issue #69).

Before this change `embedder.embed` ran on the event loop in every path that
needs a vector (add, update, the searches by meaning, the duplicate check, the
neighbours and tags for a review, reindex), and `tags_for_review` embedded
every tag in use again on every review. A `/health` call sat for a second
while the catch-up ran. Now every call to the model goes through
`asyncio.to_thread`, and each tag stores the vector of its `name: description`
once, when it is created or its description changes; reindex fills the rest.

The tests here run the repository functions on a session, and the routes on
an in-process app, with a fake embedder that records the thread each call ran
on, or one that sleeps so the loop can be timed meanwhile.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy import update as sql_update

from agent_memory.server import repository as repo
from agent_memory.server.db import make_sessionmaker
from agent_memory.server.embedding import NullEmbedder, cosine
from agent_memory.server.models import Memory, Tag
from agent_memory.server.review import Verdict
from agent_memory.server.schemas import TagIn
from conftest import FakeEmbedder, make_test_engine
from test_review import _App

APPROVE = Verdict("approve", None, "Fine.", None, None)

# How long a request may take while the model is busy in a thread.
RESPONSIVE = 0.1


class ThreadEmbedder(FakeEmbedder):
    """A `FakeEmbedder` that records the thread each `embed` call ran on and
    the texts it was given, in order."""

    def __init__(self):
        self.calls: list[tuple[int, list[str]]] = []

    def embed(self, texts):
        self.calls.append((threading.get_ident(), list(texts)))
        return super().embed(texts)

    @property
    def texts(self) -> list[list[str]]:
        return [texts for _, texts in self.calls]


class SlowEmbedder(FakeEmbedder):
    """A `FakeEmbedder` whose every call takes `delay` seconds, the way the
    real model takes its time. Records when each call ran, as
    `(start, end)`, and sets `started` when the first one begins."""

    def __init__(self, delay: float = 0.3):
        self.delay = delay
        self.windows: list[tuple[float, float]] = []
        self.started = threading.Event()

    def embed(self, texts):
        start = time.perf_counter()
        self.started.set()
        time.sleep(self.delay)
        vectors = super().embed(texts)
        self.windows.append((start, time.perf_counter()))
        return vectors


@pytest_asyncio.fixture
async def session():
    """A session on the (already truncated) test DB."""
    engine = make_test_engine()
    async with make_sessionmaker(engine)() as s, s.begin():
        yield s
    await engine.dispose()


async def _tag(session, name) -> Tag:
    return (await session.execute(
        select(Tag).where(func.lower(Tag.name) == name.lower()))).scalar_one()


def _vector(text) -> list[float]:
    return FakeEmbedder().embed([text])[0]


def _same(got, expected) -> bool:
    """True when the stored vector `got` is `expected`, within float error."""
    if got is None or len(got) != len(expected):
        return False
    return all(abs(a - b) < 1e-6 for a, b in zip(got, expected))


async def _clear_vectors(session) -> None:
    await session.execute(sql_update(Memory).values(embedding=None, embedding_model=None))
    await session.execute(sql_update(Tag).values(embedding=None, embedding_model=None))
    session.expire_all()


# ── every embed call runs off the event loop ─────────────────────────────────
async def test_every_embed_call_runs_off_the_event_loop(session):
    emb = ThreadEmbedder()
    loop_thread = threading.get_ident()
    seen = 0

    def check(step: str) -> None:
        """The calls made since the last check: at least one, none on the loop."""
        nonlocal seen
        new = emb.calls[seen:]
        assert new, f"{step} made no embed call"
        assert all(thread != loop_thread for thread, _ in new), f"{step} embedded on the loop"
        seen = len(emb.calls)

    mid = await repo.add(session, "the cat sat on the mat", "t", "alpha",
                         [TagIn(name="pets", description="animals at home")], "note",
                         embedder=emb)
    check("add")
    await repo.set_review(session, mid, APPROVE, "t")

    await repo.update(session, mid, content="the cat sat on the rug", embedder=emb)
    check("update of the content")
    await repo.update(session, mid, add_tags=[TagIn(name="home", description="house things")],
                      embedder=emb)
    check("update with a new tag")
    await repo.set_review(session, mid, APPROVE, "t")

    await repo.search_semantic(session, emb, "a cat")
    check("search_semantic")
    await repo.search_hybrid(session, emb, "a cat")
    check("search_hybrid")
    assert await repo.find_duplicate(session, emb, "the cat sat on the rug", "alpha") is not None
    check("find_duplicate")
    assert await repo.neighbours_for(session, emb, "a cat", "alpha")
    check("neighbours_for")
    assert sorted(await repo.tags_for_review(session, emb, "a cat")) == ["home", "pets"]
    check("tags_for_review")

    await _clear_vectors(session)
    assert await repo.reindex(session, emb, batch=1) == {"updated": 1, "tags": 2}
    check("reindex")
    # Three batches of one, each its own call.
    assert emb.texts[-3:] == [["the cat sat on the rug"], ["pets: animals at home"],
                              ["home: house things"]]


# ── the loop stays responsive while the model works ──────────────────────────
async def _wait_for_the_model(emb: SlowEmbedder) -> None:
    """Wait until the model is busy in its thread. The loop must be free to
    run this at all: a call on the loop would keep it from ever returning."""
    deadline = time.perf_counter() + 5
    while not emb.started.is_set():
        assert time.perf_counter() < deadline, "the model never started"
        await asyncio.sleep(0.005)


async def _time_health_calls(a, n=5) -> list[tuple[float, float]]:
    """`n` calls to `/health`, one after another, each as `(start, end)`."""
    windows = []
    for _ in range(n):
        start = time.perf_counter()
        resp = await a.client.get("/health")
        windows.append((start, time.perf_counter()))
        assert resp.status_code == 200
    return windows


def _inside(inner, outer) -> bool:
    return outer[0] <= inner[0] and inner[1] <= outer[1]


def _assert_answered_meanwhile(health, embeds) -> None:
    """Every health call answered fast, and at least one of them ran while
    the model was busy, so the timing measured something."""
    for start, end in health:
        assert end - start < RESPONSIVE, f"/health took {end - start:.3f} s"
    assert any(_inside(h, e) for h in health for e in embeds), \
        "no health call fell inside an embed call"


async def test_health_answers_while_a_reindex_embeds():
    emb = SlowEmbedder(0.3)
    async with _App(embedder=emb) as a:
        async with make_sessionmaker(a.engine)() as s, s.begin():
            for i in range(10):
                # No embedder: the rows are stored without a vector.
                await repo.add(s, f"row {i} of ten", "t", "alpha", [], None)
        reindex = asyncio.create_task(a.client.post("/admin/reindex"))
        await _wait_for_the_model(emb)
        health = await _time_health_calls(a)
        resp = await reindex
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"updated": 10, "tags": 0}
    _assert_answered_meanwhile(health, emb.windows)


async def test_health_answers_while_an_add_embeds():
    emb = SlowEmbedder(0.3)
    async with _App(embedder=emb) as a:
        add = asyncio.create_task(a.client.post("/memories", json={
            "content": "Chose Postgres for the store, because several agents write at once.",
            "project": "alpha", "agent": "tester", "tags": []}))
        await _wait_for_the_model(emb)
        health = await _time_health_calls(a)
        resp = await add
        assert resp.status_code == 201, resp.text
    # The duplicate check and the row: two calls, both in a thread.
    assert len(emb.windows) == 2
    _assert_answered_meanwhile(health, emb.windows)


# ── tag vectors ──────────────────────────────────────────────────────────────
async def test_tag_vector_is_stored_on_create_and_replaced_on_a_new_description(session):
    emb = ThreadEmbedder()
    await repo.add(session, "carrier one", "t", "alpha",
                   [TagIn(name="db", description="the database layer")], None, embedder=emb)
    tag = await _tag(session, "db")
    assert tag.embedding_model == "fake"
    assert _same(tag.embedding, _vector("db: the database layer"))
    assert ["db: the database layer"] in emb.texts

    # The same tag again, with no description: nothing changes, no call.
    emb.calls.clear()
    await repo.add(session, "carrier two", "t", "alpha", [TagIn(name="db")], None, embedder=emb)
    assert _same(tag.embedding, _vector("db: the database layer"))
    assert emb.texts == [["carrier two"]]

    # A new description: the vector follows it.
    await repo.add(session, "carrier three", "t", "alpha",
                   [TagIn(name="DB", description="where the rows live")], None, embedder=emb)
    assert tag.description == "where the rows live"
    assert _same(tag.embedding, _vector("db: where the rows live"))
    assert ["db: where the rows live"] in emb.texts

    # A tag with no description is embedded as `name: name`.
    await repo.add(session, "carrier four", "t", "alpha", [TagIn(name="ui")], None, embedder=emb)
    ui = await _tag(session, "ui")
    assert _same(ui.embedding, _vector("ui: ui"))


async def test_rename_keeps_the_vector_and_a_new_description_replaces_it(session):
    emb = ThreadEmbedder()
    await repo.add(session, "carrier", "t", "alpha",
                   [TagIn(name="db", description="the database layer")], None, embedder=emb)
    before = _vector("db: the database layer")
    emb.calls.clear()

    # A rename alone: the vector stays, and the model is not called.
    result = await repo.patch_tag(session, "db", new_name="database", embedder=emb)
    assert result["name"] == "database"
    tag = await _tag(session, "database")
    assert _same(tag.embedding, before)
    assert emb.calls == []

    # The same description again: still nothing.
    await repo.patch_tag(session, "database", description="the database layer", embedder=emb)
    assert _same(tag.embedding, before)
    assert emb.calls == []

    # A new description: a new vector, made from the current name.
    await repo.patch_tag(session, "database", description="where the rows live", embedder=emb)
    assert _same(tag.embedding, _vector("database: where the rows live"))
    assert emb.texts == [["database: where the rows live"]]

    # Merging into a tag that does not exist yet creates it with a vector.
    merged = await repo.merge_tags(session, ["database"], "store", "the store", embedder=emb)
    assert merged["target"] == "store"
    store = await _tag(session, "store")
    assert _same(store.embedding, _vector("store: the store"))


async def test_tag_routes_store_and_keep_vectors_the_same_way():
    emb = ThreadEmbedder()
    async with _App(embedder=emb) as a:
        await a.client.post("/memories", json={
            "content": "carrier", "project": "alpha", "agent": "tester",
            "tags": [{"name": "db", "description": "the database layer"}]})
        emb.calls.clear()

        resp = await a.client.patch("/tags/db", json={"name": "database"})
        assert resp.status_code == 200, resp.text
        assert emb.calls == []

        resp = await a.client.patch("/tags/database", json={"description": "where the rows live"})
        assert resp.status_code == 200, resp.text
        assert emb.texts == [["database: where the rows live"]]

        async with make_sessionmaker(a.engine)() as s:
            tag = await _tag(s, "database")
            assert _same(tag.embedding, _vector("database: where the rows live"))
            assert tag.embedding_model == "fake"


async def test_without_a_model_tags_have_no_vector_and_reindex_fills_them(session):
    await repo.add(session, "carrier one", "t", "alpha",
                   [TagIn(name="db", description="the database layer")], None,
                   embedder=NullEmbedder())
    await repo.add(session, "carrier two", "t", "alpha", [TagIn(name="ui")], None)
    db, ui = await _tag(session, "db"), await _tag(session, "ui")
    assert (db.embedding, db.embedding_model) == (None, None)
    assert (ui.embedding, ui.embedding_model) == (None, None)

    # Reindex counts the tags next to the memories. Batches of one here, so
    # each row is its own call.
    emb = ThreadEmbedder()
    assert await repo.reindex(session, emb, batch=1) == {"updated": 2, "tags": 2}
    assert emb.texts == [["carrier one"], ["carrier two"],
                         ["db: the database layer"], ["ui: ui"]]
    assert _same(db.embedding, _vector("db: the database layer")) and db.embedding_model == "fake"
    assert _same(ui.embedding, _vector("ui: ui")) and ui.embedding_model == "fake"

    # A vector from another model is replaced; one from this model is left alone.
    await session.execute(sql_update(Tag).where(Tag.id == db.id).values(embedding_model="old"))
    session.expire_all()
    emb.calls.clear()
    assert await repo.reindex(session, emb) == {"updated": 0, "tags": 1}
    assert emb.texts == [["db: the database layer"]]
    assert await repo.reindex(session, emb) == {"updated": 0, "tags": 0}


# ── the tags offered to the review ───────────────────────────────────────────
TAGS = [("db", "the database layer"), ("ui", "the dashboard"), ("net", "the network")]
ENTRY = "Chose Postgres for the store."


def _by_meaning(content, tags) -> list[str]:
    vec = _vector(content)
    scored = [(cosine(vec, _vector(f"{name}: {desc}")), name) for name, desc in tags]
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [name for _, name in scored]


async def _seed_tags(session, emb):
    for n, (name, desc) in enumerate(TAGS):
        await repo.add(session, f"carrier {n}", "t", "alpha",
                       [TagIn(name=name, description=desc)], None, embedder=emb)


async def test_tags_for_review_embeds_only_the_entry_and_ranks_by_stored_vectors(session):
    emb = ThreadEmbedder()
    await _seed_tags(session, emb)
    emb.calls.clear()

    assert await repo.tags_for_review(session, emb, ENTRY) == _by_meaning(ENTRY, TAGS)
    # Exactly one call, for the entry; no tag text is embedded again.
    assert emb.texts == [[ENTRY]]

    # The stored vector decides, not the tag's text: give `net` the entry's
    # own vector and it comes first.
    await session.execute(sql_update(Tag).where(Tag.name == "net").values(embedding=_vector(ENTRY)))
    assert (await repo.tags_for_review(session, emb, ENTRY))[0] == "net"

    # The limit still applies.
    assert len(await repo.tags_for_review(session, emb, ENTRY, limit=2)) == 2


async def test_tags_without_a_vector_from_this_model_are_not_offered(session):
    emb = ThreadEmbedder()
    await _seed_tags(session, emb)
    await session.execute(sql_update(Tag).where(Tag.name == "db")
                          .values(embedding=None, embedding_model=None))
    await session.execute(sql_update(Tag).where(Tag.name == "ui").values(embedding_model="old"))

    assert await repo.tags_for_review(session, emb, ENTRY) == ["net"]
    # Without a model the most used tags are offered, vectors or not.
    assert await repo.tags_for_review(session, NullEmbedder(), ENTRY) == ["db", "net", "ui"]
    assert await repo.tags_for_review(session, None, ENTRY) == ["db", "net", "ui"]
    # Reindex fills the two, and they are offered again.
    assert await repo.reindex(session, emb) == {"updated": 0, "tags": 2}
    assert await repo.tags_for_review(session, emb, ENTRY) == _by_meaning(ENTRY, TAGS)
