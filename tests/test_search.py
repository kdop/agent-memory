"""Search and the vectors behind it.

Keyword search (Postgres full text, a snippet with the matches marked), search
by meaning (cosine over stored vectors) and hybrid (reciprocal rank fusion of
the two); the embedding module; the vectors stored for memories and tags and
`reindex`, which fills the missing ones; every call to the model running off
the event loop; and the keyword baseline on the retrieval set.

The searches by meaning read only `id` and `embedding` first, score and sort
in Python, and then load the winners whole. A `QueryLog` on the engine lets a
test check what each statement selects. Results are checked against a plain
cosine over every row, computed in the test.
"""

from __future__ import annotations

import asyncio
import builtins
import logging
import math
import threading
import time

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy import update as sql_update

from agent_memory.server import embedding
from agent_memory.server import repository as repo
from agent_memory.server.app import create_app
from agent_memory.server.db import make_sessionmaker
from agent_memory.server.embedding import (
    EmbeddingUnavailable,
    NullEmbedder,
    cosine,
    make_embedder,
    normalize,
)
from agent_memory.server.models import Memory, Tag
from agent_memory.server.review import NEIGHBOUR_COUNT, Verdict
from agent_memory.server.schemas import TagIn
from conftest import (
    APPROVE,
    App,
    FakeEmbedder,
    QueryLog,
    add_rows,
    load_retrieval_set,
    make_test_engine,
    same_vector,
    stored_vector,
    vector,
)
from drivers import recall_at

# The first step of every search by meaning selects these two columns only.
ID_AND_VECTOR = ["memories.id", "memories.embedding"]
QUERY = "the cat sat on the mat"


def _rrf(*ranks):
    return sum(1.0 / (repo.RRF_K + r) for r in ranks)


def _pairs(hits):
    return [(h["id"], h["score"]) for h in hits]


def _assert_same(got, expected):
    assert [mid for mid, _ in got] == [mid for mid, _ in expected]
    for (_, a), (_, b) in zip(got, expected):
        assert a == pytest.approx(b, abs=1e-6)


@pytest_asyncio.fixture
async def db():
    """A session on the test DB and the log of every statement it sends."""
    engine = make_test_engine()
    log = QueryLog(engine)
    async with make_sessionmaker(engine)() as s, s.begin():
        yield s, log
    await engine.dispose()


# Twelve memories in two projects, with tags, some verified, one superseded.
# FakeEmbedder vectors come from a hash of the text, so every pair scores a
# different cosine; only the same text scores 1.0, and the first text is
# there twice, once per project, for a tie.
TEXTS = [
    ("alpha", "the cat sat on the mat"),
    ("alpha", "a dog in the yard"),
    ("alpha", "rain on the window"),
    ("alpha", "coffee before code"),
    ("alpha", "the moon over the hill"),
    ("alpha", "bread in the oven"),
    ("alpha", "a long walk by the river"),
    ("alpha", "snow on the road"),
    ("beta", "the cat sat on the mat"),
    ("beta", "wind in the trees"),
    ("beta", "a light in the window"),
    (None, "the last one, in no project"),
]


async def _seed(session, emb):
    """Store the set. Returns `{id: (project, content)}`. The first eight rows
    are verified; the second (a dog in the yard) is superseded by the third."""
    rows = {}
    for n, (project, content) in enumerate(TEXTS):
        mid = await repo.add(session, content, "t", project, [TagIn(name=f"tag{n % 3}")],
                             "note", embedder=emb)
        rows[mid] = (project, content)
    ids = list(rows)
    for mid in ids[:8]:
        await repo.set_review(session, mid, APPROVE, "t")
    await repo.set_review(session, ids[2], Verdict("approve", None, "Reverses #2.", None, None,
                                                   [], supersedes=ids[1]), "t")
    await session.flush()
    return rows


def _expected(emb, text, rows, *, project=None, current=False, exclude=()):
    """What a search by meaning must return: the cosine of `text` against
    every row's content, best first, ties by id newest first."""
    query_vec = emb.embed([text])[0]
    superseded = {list(rows)[1]} if current else set()
    scored = [(mid, cosine(query_vec, emb.embed([content])[0]))
              for mid, (proj, content) in rows.items()
              if (project is None or proj == project)
              and mid not in superseded and mid not in exclude]
    scored.sort(key=lambda pair: (-pair[1], -pair[0]))
    return scored


async def _expected_hybrid(session, emb, text, rows, limit=None, **filters):
    """The fusion, computed here from the keyword list and the plain cosine
    order: `[(id, fused score, snippet or None)]`."""
    by_words = await repo.search(session, text, limit=0, **filters)
    by_meaning = _expected(emb, text, rows, **filters)
    fused: dict[int, float] = {}
    for ids in ([h["id"] for h in by_words], [mid for mid, _ in by_meaning]):
        for rank, mid in enumerate(ids, start=1):
            fused[mid] = fused.get(mid, 0.0) + _rrf(rank)
    snippets = {h["id"]: h["snippet"] for h in by_words}
    order = sorted(fused, key=lambda mid: (-fused[mid], -mid))[:limit or None]
    return [(mid, fused[mid], snippets.get(mid)) for mid in order]


# ── keyword ──────────────────────────────────────────────────────────────────
async def test_keyword_search_ranks_and_marks_the_words(session):
    one = await repo.add(session, "one fox among many other words here", "t", None, [], None)
    three = await repo.add(session, "fox fox fox", "t", None, [], None)
    hits = await repo.search(session, "fox")
    assert [h["id"] for h in hits] == [three, one]
    assert all(isinstance(h["score"], float) and h["score"] > 0 for h in hits)
    assert "→fox←" in hits[0]["snippet"]
    assert await repo.search(session, "zzzznope") == []
    # A plain read carries no score.
    assert (await repo.get(session, one))["score"] is None


# ── by meaning: the results ──────────────────────────────────────────────────
async def test_semantic_results_and_scores_match_cosine_over_all_rows(db):
    session, _ = db
    emb = FakeEmbedder()
    rows = await _seed(session, emb)
    everything = _expected(emb, QUERY, rows)

    hits = await repo.search_semantic(session, emb, QUERY)
    _assert_same(_pairs(hits), everything)
    assert len(hits) == len(rows)
    assert hits[0]["score"] == pytest.approx(1.0)
    # The winners come back whole: tags, review and the supersedes links.
    by_id = {h["id"]: h for h in hits}
    ids = list(rows)
    assert by_id[ids[0]]["tags"] == ["tag0"]
    assert by_id[ids[0]]["review"]["verdict"] == "approve"
    assert by_id[ids[0]]["review_status"] == "verified"
    assert by_id[ids[8]]["review_status"] == "unverified"
    assert by_id[ids[1]]["superseded_by"] == ids[2]
    assert by_id[ids[2]]["supersedes"] == ids[1]
    assert all(h["snippet"] is None for h in hits)

    _assert_same(_pairs(await repo.search_semantic(session, emb, QUERY, limit=3)),
                 everything[:3])
    for limit in (0, None):
        _assert_same(_pairs(await repo.search_semantic(session, emb, QUERY, limit=limit)),
                     everything)
    current = await repo.search_semantic(session, emb, QUERY, current=True)
    _assert_same(_pairs(current), _expected(emb, QUERY, rows, current=True))


@pytest.mark.parametrize("search", [repo.search_semantic, repo.search_hybrid])
async def test_search_by_meaning_applies_the_filters(session, search):
    emb = FakeEmbedder()
    a = await repo.add(session, "alpha thing", "ann", "alpha", [TagIn(name="x")], None,
                       embedder=emb)
    b = await repo.add(session, "beta thing", "bob", "beta", [TagIn(name="y")], None,
                       embedder=emb)
    c = await repo.add(session, "alpha other", "bob", "alpha", [TagIn(name="y")], None,
                       embedder=emb)
    # Written while the model was off: no vector, so never found by meaning.
    await repo.add(session, "no vector", "bob", "alpha", [], None)

    async def ids(**filters):
        return {h["id"] for h in await search(session, emb, "q", **filters)}

    assert await ids() == {a, b, c}
    assert await ids(project="alpha") == {a, c}
    assert await ids(agent="bob") == {b, c}
    assert await ids(tag="X") == {a}
    assert await ids(project="alpha", agent="bob") == {c}
    assert await ids(since="2999-01-01") == set()


async def test_hybrid_matches_the_fusion_of_both_lists(db):
    session, _ = db
    emb = FakeEmbedder()
    rows = await _seed(session, emb)
    query = "cat on the mat"  # hits by words in two rows, by meaning in all
    expected = await _expected_hybrid(session, emb, query, rows)
    assert 0 < sum(1 for _, _, snip in expected if snip) < len(expected)

    hits = await repo.search_hybrid(session, emb, query)
    _assert_same(_pairs(hits), [(mid, score) for mid, score, _ in expected])
    # The snippet comes from the keyword hit; the rest have none.
    assert [h["snippet"] for h in hits] == [snip for _, _, snip in expected]
    assert "→cat←" in hits[0]["snippet"]
    for h in hits:
        assert h["tags"] and h["content"] == rows[h["id"]][1]

    for kwargs in ({"limit": 4}, {"current": True}):
        got = await repo.search_hybrid(session, emb, query, **kwargs)
        want = await _expected_hybrid(session, emb, query, rows, **kwargs)
        _assert_same(_pairs(got), [(mid, score) for mid, score, _ in want])
    for limit in (0, None):
        assert len(await repo.search_hybrid(session, emb, query, limit=limit)) == len(rows)


async def test_hybrid_puts_a_hit_in_both_lists_first(session):
    assert repo.RRF_K == 60
    emb = FakeEmbedder()
    for text in ("a dog in the yard", "rain on the window", "coffee before code"):
        await repo.add(session, text, "t", None, [], None, embedder=emb)
    both = await repo.add(session, QUERY, "t", None, [], None, embedder=emb)
    hits = await repo.search_hybrid(session, emb, QUERY)
    # First in both lists: two rank-1 contributions. The rest hit by meaning only.
    assert hits[0]["id"] == both and hits[0]["score"] == pytest.approx(_rrf(1, 1))
    assert all(h["score"] < _rrf(1, 1) for h in hits[1:])


async def test_hybrid_keeps_a_keyword_hit_without_a_vector(session):
    emb = FakeEmbedder()
    no_vec = await repo.add(session, QUERY, "t", None, [], None)
    with_vec = await repo.add(session, "a dog in the yard", "t", None, [], None, embedder=emb)
    hits = await repo.search_hybrid(session, emb, QUERY)
    # Rank 1 in one list each: equal scores, the newer id first.
    assert [h["id"] for h in hits] == [with_vec, no_vec]
    assert [h["score"] for h in hits] == pytest.approx([_rrf(1), _rrf(1)])


# ── by meaning: the queries it sends ─────────────────────────────────────────
async def test_semantic_reads_id_and_vector_then_loads_only_the_winners(db):
    session, log = db
    emb = FakeEmbedder()
    rows = await _seed(session, emb)
    log.clear()

    hits = await repo.search_semantic(session, emb, QUERY, project="alpha", current=True,
                                      limit=3)
    assert len(hits) == 3
    first = log.statements[0]
    assert log.columns(first) == ID_AND_VECTOR
    # The filters ride on that first query, so no candidate outside them is read.
    for clause in ("memories.project = ", "memories.embedding IS NOT NULL",
                   "memories.embedding_model = ", "NOT (EXISTS"):
        assert clause in first
    # One row load, naming only the three winners, not the twelve rows.
    (sql, params), = log.row_loads()
    assert sorted(params) == sorted(h["id"] for h in hits) and len(params) == 3 < len(rows)
    # Candidates, row load, then the loads of tags, reviews and superseding
    # rows: five statements, whatever the table size.
    assert len(log.statements) == 5

    # No candidate: the first query is the only one.
    log.clear()
    assert await repo.search_semantic(session, emb, QUERY, project="nowhere") == []
    assert len(log.statements) == 1 and log.row_loads() == []


async def test_find_duplicate_runs_one_query_on_id_and_vector(db):
    session, log = db
    emb = FakeEmbedder()
    rows = await _seed(session, emb)
    verified_alpha = dict(list(rows.items())[:8])
    for text, found in ((QUERY, True), ("nothing like this", False)):
        log.clear()
        result = await repo.find_duplicate(session, emb, text, "alpha")
        if found:
            assert result == pytest.approx(_expected(emb, QUERY, verified_alpha)[0])
            assert result[0] == list(rows)[0]
        else:
            assert result is None
        assert len(log.statements) == 1
        assert log.columns(log.statements[0]) == ID_AND_VECTOR


async def test_hybrid_scores_on_ids_and_loads_only_the_rows_words_missed(db):
    session, log = db
    emb = FakeEmbedder()
    rows = await _seed(session, emb)
    query = "cat on the mat"
    by_words = {h["id"] for h in await repo.search(session, query, limit=0)}
    log.clear()

    hits = await repo.search_hybrid(session, emb, query, limit=4)
    assert len(hits) == 4
    # Statement one is the keyword search; the meaning half is one two-column select.
    assert "plainto_tsquery" in log.statements[0]
    assert sum(log.columns(sql) == ID_AND_VECTOR for sql in log.statements) == 1
    # Then one row load, for the winners the keyword list did not carry.
    wanted = [h["id"] for h in hits if h["id"] not in by_words]
    (_, params), = log.row_loads()
    assert sorted(params) == sorted(wanted) and len(wanted) < len(rows)


async def test_neighbours_are_the_five_best_verified_in_the_project(db):
    session, log = db
    emb = FakeEmbedder()
    rows = await _seed(session, emb)
    verified_alpha = dict(list(rows.items())[:8])
    text = "a cat on a mat"
    log.clear()

    neighbours = await repo.neighbours_for(session, emb, text, "alpha")
    assert len(neighbours) == NEIGHBOUR_COUNT == 5
    _assert_same(_pairs(neighbours), _expected(emb, text, verified_alpha)[:5])
    assert all(n["review_status"] == "verified" and n["project"] == "alpha" and n["tags"]
               for n in neighbours)
    assert log.columns(log.statements[0]) == ID_AND_VECTOR
    (_, params), = log.row_loads()
    assert sorted(params) == sorted(n["id"] for n in neighbours)
    assert await repo.neighbours_for(session, NullEmbedder(), text, "alpha") == []

    # For a stored memory, the memory itself is left out.
    mine = list(rows)[0]
    log.clear()
    memory, neighbours = await repo.review_input(session, mine)
    assert memory["id"] == mine
    others = dict(list(rows.items())[1:8])
    _assert_same(_pairs(neighbours), _expected(emb, rows[mine][1], others)[:5])
    two_column = [sql for sql in log.statements if log.columns(sql) == ID_AND_VECTOR]
    assert len(two_column) == 1 and "memories.id != " in two_column[0]
    (_, params), = log.row_loads()
    assert len(params) == 5
    assert await repo.review_input(session, 999) is None


@pytest.mark.embed
async def test_real_model_results_match_a_full_scan(retrieval_set):
    """With the real model on the retrieval set, both search modes return
    what a plain cosine over every stored vector returns, id for id and
    score for score."""
    pytest.importorskip("fastembed")
    from agent_memory.server.embedding import FastEmbedEmbedder

    emb = FastEmbedEmbedder()
    engine = make_test_engine()
    try:
        async with make_sessionmaker(engine)() as session, session.begin():
            await repo.reindex(session, emb)
            stored = {mid: list(vec) for mid, vec in
                      await session.execute(select(Memory.id, Memory.embedding))}
            for q in retrieval_set:
                query_vec = emb.embed([q["question"]])[0]
                expected = sorted(((mid, cosine(query_vec, vec)) for mid, vec in stored.items()),
                                  key=lambda pair: (-pair[1], -pair[0]))
                hits = await repo.search_semantic(session, emb, q["question"], limit=5)
                _assert_same(_pairs(hits), expected[:5])

                by_words = await repo.search(session, q["question"], limit=0)
                fused: dict[int, float] = {}
                for ids in ([h["id"] for h in by_words], [mid for mid, _ in expected]):
                    for rank, mid in enumerate(ids, start=1):
                        fused[mid] = fused.get(mid, 0.0) + _rrf(rank)
                order = sorted(fused, key=lambda mid: (-fused[mid], -mid))[:5]
                hybrid = await repo.search_hybrid(session, emb, q["question"], limit=5)
                _assert_same(_pairs(hybrid), [(mid, fused[mid]) for mid in order])
    finally:
        await engine.dispose()


# ── the search route ─────────────────────────────────────────────────────────
async def test_the_search_route_serves_each_mode():
    async with App() as a:
        for content in (QUERY, "a dog in the yard", "rain on the window", "coffee before code"):
            await a.add(content, agent="t")

        async def search(**params):
            resp = await a.client.get("/memories/search", params=params)
            assert resp.status_code == 200, resp.text
            return resp

        keyword = (await search(q="cat")).json()
        assert [h["id"] for h in keyword] == [1]
        assert keyword[0]["score"] > 0 and "→cat←" in keyword[0]["snippet"]
        assert (await search(q="cat", mode="keyword")).json() == keyword

        semantic = (await search(q=QUERY, mode="semantic")).json()
        assert len(semantic) == 4 and semantic[0]["id"] == 1
        assert semantic[0]["score"] == pytest.approx(1.0, abs=1e-6)
        assert semantic[0]["snippet"] is None
        scores = [h["score"] for h in semantic]
        assert scores == sorted(scores, reverse=True)

        resp = await search(q=QUERY, mode="hybrid")
        hybrid = resp.json()
        assert "X-Search-Fallback" not in resp.headers
        # Rank 1 by words and rank 1 by meaning: 1/61 + 1/61.
        assert hybrid[0]["id"] == 1 and hybrid[0]["score"] == pytest.approx(2 / 61)
        assert all(h["snippet"] is None for h in hybrid[1:])
        for mode, full in (("semantic", semantic), ("hybrid", hybrid)):
            limited = (await search(q=QUERY, mode=mode, limit=2)).json()
            assert [h["id"] for h in limited] == [h["id"] for h in full[:2]]
        for mode in ("semantic", "hybrid"):
            assert len((await search(q=QUERY, mode=mode, limit=0)).json()) == 4

        assert (await a.client.get("/memories/search",
                                   params={"q": "x", "mode": "fuzzy"})).status_code == 422
        assert (await a.client.get("/memories/search",
                                   params={"q": "x", "current": "maybe"})).status_code == 422


@pytest.mark.parametrize("embedder, reason", [
    (NullEmbedder("fastembed is not installed"), "fastembed is not installed"),
    (None, "off or not installed"),
])
async def test_without_a_model_hybrid_falls_back_and_semantic_is_refused(embedder, reason):
    async with App(embedder=embedder) as a:
        for content in (QUERY, "a cat in the yard", "coffee before code"):
            await a.add(content)
        hybrid = await a.client.get("/memories/search", params={"q": "cat", "mode": "hybrid"})
        keyword = await a.client.get("/memories/search", params={"q": "cat"})
        # Hybrid serves its keyword half and says so in a header.
        assert hybrid.headers["X-Search-Fallback"] == "keyword"
        assert len(hybrid.json()) == 2 and hybrid.json() == keyword.json()
        assert "X-Search-Fallback" not in keyword.headers

        resp = await a.client.get("/memories/search", params={"q": "x", "mode": "semantic"})
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail.startswith("semantic search is not available") and reason in detail


# ── the embedding module ─────────────────────────────────────────────────────
def test_null_embedder_has_no_model_and_refuses_to_embed():
    with pytest.raises(NotImplementedError):
        embedding.Embedder().embed(["x"])
    null = NullEmbedder()
    assert null.model_name is None
    with pytest.raises(EmbeddingUnavailable):
        null.embed(["anything"])


@pytest.mark.parametrize("a, b, expected", [
    ([0.6, 0.8], [0.6, 0.8], 1.0),
    ([0.6, 0.8], [-0.6, -0.8], -1.0),
    ([1.0, 0.0], [0.0, 1.0], 0.0),
    ([1.0, 2.0, 3.0], [10.0, 20.0, 30.0], 1.0),     # scale does not matter
    ([0.0, 0.0], [1.0, 1.0], 0.0),                  # a zero vector is 0, not an error
])
def test_cosine(a, b, expected):
    assert cosine(a, b) == pytest.approx(expected)
    assert cosine(b, a) == pytest.approx(expected)


def test_cosine_rejects_mismatched_lengths_and_normalize_gives_unit_length():
    with pytest.raises(ValueError):
        cosine([1.0, 2.0], [1.0])
    assert math.hypot(*normalize([3.0, 4.0])) == pytest.approx(1.0)
    assert normalize([0.0, 0.0]) == [0.0, 0.0]


@pytest.mark.parametrize("value", ["off", " OFF "])
def test_make_embedder_off_returns_null_and_logs_once(monkeypatch, caplog, value):
    monkeypatch.setenv("AGENT_MEMORY_EMBED_MODEL", value)
    with caplog.at_level(logging.INFO, logger="agent_memory.server.embedding"):
        emb = make_embedder()
    assert isinstance(emb, NullEmbedder) and emb.model_name is None
    assert len(caplog.records) == 1 and "off" in caplog.records[0].getMessage()


def test_make_embedder_without_fastembed_returns_null(monkeypatch, caplog):
    monkeypatch.delenv("AGENT_MEMORY_EMBED_MODEL", raising=False)
    real_import = builtins.__import__

    def fake_import(name, *args, **kw):
        if name == "fastembed" or name.startswith("fastembed."):
            raise ImportError("no fastembed here")
        return real_import(name, *args, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with caplog.at_level(logging.INFO, logger="agent_memory.server.embedding"):
        emb = make_embedder()
    assert isinstance(emb, NullEmbedder)
    assert len(caplog.records) == 1 and "not installed" in caplog.records[0].getMessage()


def test_cache_dir_env_wins_over_repo_default(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_MEMORY_EMBED_CACHE", str(tmp_path / "models"))
    assert embedding.default_cache_dir() == tmp_path / "models"
    monkeypatch.delenv("AGENT_MEMORY_EMBED_CACHE")
    assert embedding.default_cache_dir() == embedding.repo_root() / ".cache" / "fastembed"
    assert (embedding.repo_root() / "pyproject.toml").is_file()


@pytest.mark.embed
def test_real_model_embeds_unit_vectors(monkeypatch):
    pytest.importorskip("fastembed")
    monkeypatch.delenv("AGENT_MEMORY_EMBED_MODEL", raising=False)
    emb = make_embedder()
    assert emb.model_name == embedding.DEFAULT_MODEL and emb.dim == 384
    vecs = emb.embed([f"memory number {i}: a short note about topic {i % 7}" for i in range(100)])
    assert len(vecs) == 100
    assert all(len(v) == 384 and math.hypot(*v) == pytest.approx(1.0, abs=1e-5) for v in vecs)
    # Close in meaning scores higher than far in meaning.
    cat, kitten, tax = emb.embed(["a cat sat on the mat", "a kitten sleeps on a rug",
                                  "quarterly tax filing deadline"])
    assert cosine(cat, kitten) > cosine(cat, tax)
    assert emb.embed([]) == []


# ── the app's embedder and the reindex at startup ────────────────────────────
async def _stored_models():
    engine = make_test_engine()
    try:
        async with engine.connect() as conn:
            stmt = select(Memory.id, Memory.embedding_model).order_by(Memory.id)
            return dict((await conn.execute(stmt)).all())
    finally:
        await engine.dispose()


async def _lifespan(caplog, level=logging.INFO, **kwargs):
    """Build an app over the test DB and run its lifespan once."""
    engine = make_test_engine()
    app = create_app(sessionmaker=make_sessionmaker(engine), token="t", review_poll=0,
                     **kwargs)
    try:
        with caplog.at_level(level, logger="agent_memory.server.app"):
            async with app.router.lifespan_context(app):
                pass
    finally:
        await engine.dispose()
    return app


async def test_the_app_uses_the_given_embedder_or_builds_one_at_startup(monkeypatch, caplog):
    emb = FakeEmbedder()
    assert create_app(token="t", embedder=emb).state.embedder is emb
    monkeypatch.setenv("AGENT_MEMORY_EMBED_MODEL", "off")
    ids = await add_rows("stays bare", embedder=NullEmbedder())
    app = await _lifespan(caplog)
    assert isinstance(app.state.embedder, NullEmbedder)
    # No model: nothing to reindex.
    assert await _stored_models() == {ids[0]: None}


async def test_startup_reindexes_once_and_survives_a_failure(caplog):
    ids = await add_rows("before the model", "also before", embedder=NullEmbedder())

    class Broken(FakeEmbedder):
        def embed(self, texts):
            raise RuntimeError("model blew up")

    app = await _lifespan(caplog, level=logging.ERROR, embedder=Broken())
    # The server came up: the failure was logged, not raised.
    assert app.state.embedder.model_name == "fake"
    assert await _stored_models() == {ids[0]: None, ids[1]: None}
    assert any("reindex at startup failed" in r.getMessage() for r in caplog.records)

    caplog.clear()
    await _lifespan(caplog, embedder=FakeEmbedder())
    assert await _stored_models() == {ids[0]: "fake", ids[1]: "fake"}
    assert any("2 memories" in r.getMessage() for r in caplog.records)


# ── reindex ──────────────────────────────────────────────────────────────────
class CountingEmbedder(FakeEmbedder):
    """FakeEmbedder that records the thread and the texts of each call."""

    def __init__(self):
        self.calls: list[tuple[int, list[str]]] = []

    def embed(self, texts):
        self.calls.append((threading.get_ident(), list(texts)))
        return super().embed(texts)

    @property
    def texts(self) -> list[list[str]]:
        return [texts for _, texts in self.calls]


async def _set_model(session, mid, model):
    """Pretend the row was embedded by another model, without touching the vector."""
    await session.execute(sql_update(Memory).where(Memory.id == mid).values(embedding_model=model))
    session.expire_all()


async def test_reindex_fills_missing_and_stale_vectors_in_batches(session):
    emb = CountingEmbedder()
    done = await repo.add(session, "current", "t", None, [], None, embedder=emb)
    missing = [await repo.add(session, f"row {i}", "t", None, [], None) for i in range(4)]
    stale = await repo.add(session, "stale", "t", None, [], None, embedder=emb)
    await _set_model(session, stale, "old-model")
    emb.calls.clear()

    assert await repo.reindex(session, emb, batch=2) == {"updated": 5, "tags": 0}
    # In id order, two at a time; the row from the current model is left alone.
    assert emb.texts == [["row 0", "row 1"], ["row 2", "row 3"], ["stale"]]
    for mid, text in [(done, "current"), (stale, "stale")] + [
            (m, f"row {i}") for i, m in enumerate(missing)]:
        vec, model = await stored_vector(mid, session=session)
        assert model == "fake" and same_vector(vec, text)
    # Nothing left the second time, and no call to the model.
    emb.calls.clear()
    assert await repo.reindex(session, emb) == {"updated": 0, "tags": 0}
    assert emb.calls == []
    # Without a model there is nothing to do.
    await _set_model(session, done, "old-model")
    assert await repo.reindex(session, None) == {"updated": 0, "tags": 0}
    assert await repo.reindex(session, NullEmbedder()) == {"updated": 0, "tags": 0}


async def test_the_reindex_route_counts_what_it_filled():
    ids = await add_rows("first", "second", "third", embedder=NullEmbedder())
    async with App() as a:
        resp = await a.client.post("/admin/reindex")
        assert resp.status_code == 200 and resp.json() == {"updated": 3, "tags": 0}
        for mid, text in zip(ids, ("first", "second", "third")):
            assert same_vector((await stored_vector(mid))[0], text)
        assert (await a.client.post("/admin/reindex")).json() == {"updated": 0, "tags": 0}
    async with App(embedder=NullEmbedder("embedding is off")) as a:
        resp = await a.client.post("/admin/reindex")
        assert resp.status_code == 503 and "embedding is off" in resp.json()["detail"]


# ── every call to the model runs off the event loop ──────────────────────────
async def test_every_embed_call_runs_off_the_event_loop(session):
    emb = CountingEmbedder()
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
    await session.execute(sql_update(Memory).values(embedding=None, embedding_model=None))
    await session.execute(sql_update(Tag).values(embedding=None, embedding_model=None))
    session.expire_all()
    assert await repo.reindex(session, emb, batch=1) == {"updated": 1, "tags": 2}
    check("reindex")
    assert emb.texts[-3:] == [["the cat sat on the rug"], ["pets: animals at home"],
                              ["home: house things"]]


class SlowEmbedder(FakeEmbedder):
    """A FakeEmbedder whose every call takes `delay` seconds, the way the real
    model takes its time. Records each call as `(start, end)` and sets
    `started` when the first one begins."""

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


@pytest.mark.parametrize("work", ["reindex", "add"])
async def test_health_answers_while_the_model_works(work):
    """A request that calls the model in a thread leaves the loop free: five
    health calls made meanwhile each answer in under 0.1 s, and at least one
    falls inside a call to the model, so the timing measured something."""
    emb = SlowEmbedder(0.3)
    if work == "reindex":
        await add_rows(*[f"row {i} of ten" for i in range(10)], embedder=NullEmbedder())
    async with App(embedder=emb) as a:
        if work == "reindex":
            request = asyncio.create_task(a.client.post("/admin/reindex"))
        else:
            request = asyncio.create_task(a.post(
                "Chose Postgres for the store, because several agents write at once."))
        deadline = time.perf_counter() + 5
        while not emb.started.is_set():
            assert time.perf_counter() < deadline, "the model never started"
            await asyncio.sleep(0.005)
        health = []
        for _ in range(5):
            start = time.perf_counter()
            assert (await a.client.get("/health")).status_code == 200
            health.append((start, time.perf_counter()))
        resp = await request
        assert resp.status_code in (200, 201), resp.text
    if work == "reindex":
        assert resp.json() == {"updated": 10, "tags": 0}
    else:
        # The duplicate check and the row: two calls, both in a thread.
        assert len(emb.windows) == 2
    for start, end in health:
        assert end - start < 0.1, f"/health took {end - start:.3f} s"
    assert any(e[0] <= h[0] and h[1] <= e[1] for h in health for e in emb.windows), \
        "no health call fell inside an embed call"


# ── tag vectors ──────────────────────────────────────────────────────────────
async def _tag(session, name) -> Tag:
    return (await session.execute(
        select(Tag).where(func.lower(Tag.name) == name.lower()))).scalar_one()


async def test_a_tag_vector_is_made_on_create_and_on_a_new_description_only(session):
    emb = CountingEmbedder()
    await repo.add(session, "carrier one", "t", "alpha",
                   [TagIn(name="db", description="the database layer")], None, embedder=emb)
    tag = await _tag(session, "db")
    assert tag.embedding_model == "fake"
    assert same_vector(tag.embedding, "db: the database layer")

    # The same tag again with no description: nothing changes, no call.
    emb.calls.clear()
    await repo.add(session, "carrier two", "t", "alpha", [TagIn(name="db")], None, embedder=emb)
    assert emb.texts == [["carrier two"]]
    # A new description: the vector follows it.
    await repo.add(session, "carrier three", "t", "alpha",
                   [TagIn(name="DB", description="where the rows live")], None, embedder=emb)
    assert same_vector(tag.embedding, "db: where the rows live")
    # A tag with no description is embedded as `name: name`.
    await repo.add(session, "carrier four", "t", "alpha", [TagIn(name="ui")], None, embedder=emb)
    assert same_vector((await _tag(session, "ui")).embedding, "ui: ui")

    # A rename alone keeps the vector; so does the same description again.
    emb.calls.clear()
    assert (await repo.patch_tag(session, "db", new_name="database", embedder=emb))["name"] == \
        "database"
    await repo.patch_tag(session, "database", description="where the rows live", embedder=emb)
    assert emb.calls == [] and same_vector(tag.embedding, "db: where the rows live")
    # A new description: a new vector, from the current name.
    await repo.patch_tag(session, "database", description="the store", embedder=emb)
    assert emb.texts == [["database: the store"]]
    # Merging into a tag that does not exist yet creates it with a vector.
    await repo.merge_tags(session, ["database"], "store", "the store", embedder=emb)
    assert same_vector((await _tag(session, "store")).embedding, "store: the store")


async def test_the_tag_route_keeps_the_vector_on_a_rename():
    emb = CountingEmbedder()
    async with App(embedder=emb) as a:
        await a.add("carrier", tags=[{"name": "db", "description": "the database layer"}])
        emb.calls.clear()
        assert (await a.client.patch("/tags/db", json={"name": "database"})).status_code == 200
        assert emb.calls == []
        await a.client.patch("/tags/database", json={"description": "where the rows live"})
        assert emb.texts == [["database: where the rows live"]]
        vec, model = await stored_vector(1, table="tags")
        assert model == "fake" and same_vector(vec, "database: where the rows live")


async def test_without_a_model_tags_have_no_vector_and_reindex_fills_them(session):
    await repo.add(session, "carrier one", "t", "alpha",
                   [TagIn(name="db", description="the database layer")], None,
                   embedder=NullEmbedder())
    await repo.add(session, "carrier two", "t", "alpha", [TagIn(name="ui")], None)
    db, ui = await _tag(session, "db"), await _tag(session, "ui")
    assert (db.embedding, db.embedding_model, ui.embedding) == (None, None, None)

    emb = CountingEmbedder()
    assert await repo.reindex(session, emb, batch=1) == {"updated": 2, "tags": 2}
    assert emb.texts == [["carrier one"], ["carrier two"], ["db: the database layer"], ["ui: ui"]]
    assert same_vector(db.embedding, "db: the database layer") and db.embedding_model == "fake"
    # A vector from another model is replaced; one from this model is left alone.
    await session.execute(sql_update(Tag).where(Tag.id == db.id).values(embedding_model="old"))
    session.expire_all()
    emb.calls.clear()
    assert await repo.reindex(session, emb) == {"updated": 0, "tags": 1}
    assert emb.texts == [["db: the database layer"]]


# ── the tags offered to the review ───────────────────────────────────────────
TAGS = [("db", "the database layer"), ("ui", "the dashboard"), ("net", "the network")]
ENTRY = "Chose Postgres for the store."


def _by_meaning(content, tags) -> list[str]:
    vec = vector(content)
    scored = [(cosine(vec, vector(f"{name}: {desc}")), name) for name, desc in tags]
    return [name for _, name in sorted(scored, key=lambda pair: (-pair[0], pair[1]))]


async def test_tags_for_review_embeds_only_the_entry_and_ranks_by_stored_vectors(session):
    emb = CountingEmbedder()
    for n, (name, desc) in enumerate(TAGS):
        await repo.add(session, f"carrier {n}", "t", "alpha",
                       [TagIn(name=name, description=desc)], None, embedder=emb)
    emb.calls.clear()

    assert await repo.tags_for_review(session, emb, ENTRY) == _by_meaning(ENTRY, TAGS)
    assert emb.texts == [[ENTRY]]
    assert len(await repo.tags_for_review(session, emb, ENTRY, limit=2)) == 2
    # The stored vector decides, not the tag's text.
    await session.execute(sql_update(Tag).where(Tag.name == "net").values(embedding=vector(ENTRY)))
    assert (await repo.tags_for_review(session, emb, ENTRY))[0] == "net"

    # Tags without a vector from this model are not offered.
    await session.execute(sql_update(Tag).where(Tag.name == "db")
                          .values(embedding=None, embedding_model=None))
    await session.execute(sql_update(Tag).where(Tag.name == "ui").values(embedding_model="old"))
    assert await repo.tags_for_review(session, emb, ENTRY) == ["net"]
    # Without a model the most used tags are offered, vectors or not.
    assert await repo.tags_for_review(session, NullEmbedder(), ENTRY) == ["db", "net", "ui"]
    assert await repo.tags_for_review(session, None, ENTRY) == ["db", "net", "ui"]


# ── the retrieval set and the keyword baseline ───────────────────────────────
def test_retrieval_set_is_well_formed_and_recall_at_counts():
    memories, questions = load_retrieval_set()
    assert 25 <= len(memories) <= 35 and len(questions) == 20
    assert sum(q["paraphrase"] for q in questions) >= 8
    assert len({m["project"] for m in memories}) >= 3
    assert {m["type"] for m in memories} == {"decision", "lesson", "preference", "note"}
    assert all(m["content"].strip() and all(t["name"] for t in m["tags"]) for m in memories)

    assert recall_at(3, [1, 2, 3], {1}) == 1.0
    assert recall_at(3, [9, 8, 7, 1], {1}) == 0.0
    assert recall_at(3, [1, 5, 2], {1, 2, 3, 4}) == 0.5
    assert recall_at(3, [{"id": 4}], {4}) == 1.0
    with pytest.raises(ValueError):
        recall_at(3, [1], set())


async def test_retrieval_keyword_baseline(retrieval_set, record_property):
    """Every question through keyword search, recall@3 recorded. It measures;
    it never fails on the score."""
    scores = {True: [], False: []}
    engine = make_test_engine()
    try:
        async with make_sessionmaker(engine)() as session:
            for q in retrieval_set:
                hits = await repo.search(session, q["question"], limit=3)
                scores[q["paraphrase"]].append(recall_at(3, hits, q["expected_ids"]))
    finally:
        await engine.dispose()
    every = scores[True] + scores[False]
    for name, values in (("", every), (" same-words", scores[False]),
                         (" paraphrased", scores[True])):
        record_property(f"recall@3 keyword{name}", f"{sum(values) / len(values):.2f}")
    assert 0.0 <= sum(every) / len(every) <= 1.0
