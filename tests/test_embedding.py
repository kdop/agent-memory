"""Unit tests for the embedding module: the interface, `cosine`, `make_embedder`
and the app wiring. Everything but the last test runs on `FakeEmbedder`; the
real model runs once, under the `embed` marker, only when fastembed is
installed."""

import logging
import math
import time

import pytest

from agent_memory.server import embedding
from agent_memory.server.embedding import (
    EmbeddingUnavailable,
    NullEmbedder,
    cosine,
    make_embedder,
    normalize,
)
from conftest import FakeEmbedder


def _length(vec):
    return math.sqrt(sum(x * x for x in vec))


# ── the interface, on the fake ───────────────────────────────────────────────
def test_fake_embedder_shape_and_unit_length():
    emb = FakeEmbedder()
    assert emb.model_name == "fake"
    vecs = emb.embed(["one", "two", "three"])
    assert len(vecs) == 3
    for v in vecs:
        assert len(v) == emb.dim == 8
        assert _length(v) == pytest.approx(1.0)


def test_fake_embedder_is_deterministic_and_text_dependent():
    a1, b = FakeEmbedder().embed(["same text", "other text"])
    a2 = FakeEmbedder().embed(["same text"])[0]
    assert a1 == a2
    assert a1 != b


def test_embed_empty_list_gives_empty_list():
    assert FakeEmbedder().embed([]) == []


def test_null_embedder_has_no_model_and_refuses_to_embed():
    null = NullEmbedder()
    assert null.model_name is None
    with pytest.raises(EmbeddingUnavailable):
        null.embed(["anything"])


# ── cosine ───────────────────────────────────────────────────────────────────
def test_cosine_of_same_vector_is_one():
    v = FakeEmbedder().embed(["hello"])[0]
    assert cosine(v, v) == pytest.approx(1.0)


def test_cosine_of_opposite_vectors_is_minus_one():
    v = [0.6, 0.8]
    assert cosine(v, [-x for x in v]) == pytest.approx(-1.0)


def test_cosine_of_orthogonal_vectors_is_zero():
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_ignores_scale():
    a = [1.0, 2.0, 3.0]
    assert cosine(a, [10.0, 20.0, 30.0]) == pytest.approx(1.0)


def test_cosine_zero_vector_is_zero_not_error():
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_cosine_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        cosine([1.0, 2.0], [1.0])


def test_cosine_is_symmetric_and_bounded_on_fake_vectors():
    a, b = FakeEmbedder().embed(["alpha", "beta"])
    assert cosine(a, b) == pytest.approx(cosine(b, a))
    assert -1.0 <= cosine(a, b) <= 1.0


def test_normalize_gives_unit_length_and_keeps_zero():
    assert _length(normalize([3.0, 4.0])) == pytest.approx(1.0)
    assert normalize([0.0, 0.0]) == [0.0, 0.0]


# ── make_embedder and the environment ────────────────────────────────────────
def test_make_embedder_off_returns_null_and_logs_once(monkeypatch, caplog):
    monkeypatch.setenv("AGENT_MEMORY_EMBED_MODEL", "off")
    with caplog.at_level(logging.INFO, logger="agent_memory.server.embedding"):
        emb = make_embedder()
    assert isinstance(emb, NullEmbedder)
    assert emb.model_name is None
    assert len(caplog.records) == 1
    assert "off" in caplog.records[0].getMessage()


def test_make_embedder_off_is_case_and_space_tolerant(monkeypatch):
    monkeypatch.setenv("AGENT_MEMORY_EMBED_MODEL", " OFF ")
    assert isinstance(make_embedder(), NullEmbedder)


def test_make_embedder_without_fastembed_returns_null(monkeypatch, caplog):
    monkeypatch.delenv("AGENT_MEMORY_EMBED_MODEL", raising=False)
    # Make `import fastembed` fail even when it is installed.
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kw):
        if name == "fastembed" or name.startswith("fastembed."):
            raise ImportError("no fastembed here")
        return real_import(name, *args, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with caplog.at_level(logging.INFO, logger="agent_memory.server.embedding"):
        emb = make_embedder()
    assert isinstance(emb, NullEmbedder)
    assert len(caplog.records) == 1
    assert "not installed" in caplog.records[0].getMessage()


def test_cache_dir_env_wins_over_repo_default(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_MEMORY_EMBED_CACHE", str(tmp_path / "models"))
    assert embedding.default_cache_dir() == tmp_path / "models"
    monkeypatch.delenv("AGENT_MEMORY_EMBED_CACHE")
    assert embedding.default_cache_dir() == embedding.repo_root() / ".cache" / "fastembed"
    assert (embedding.repo_root() / "pyproject.toml").is_file()


# ── app wiring ───────────────────────────────────────────────────────────────
def test_create_app_stores_the_given_embedder():
    from agent_memory.server.app import create_app

    emb = FakeEmbedder()
    app = create_app(token="t", embedder=emb)
    assert app.state.embedder is emb


async def test_create_app_builds_embedder_from_env_at_startup(monkeypatch):
    from agent_memory.server.app import create_app

    from conftest import make_test_engine
    from agent_memory.server.db import make_sessionmaker

    monkeypatch.setenv("AGENT_MEMORY_EMBED_MODEL", "off")
    engine = make_test_engine()
    app = create_app(sessionmaker=make_sessionmaker(engine), token="t")
    try:
        async with app.router.lifespan_context(app):
            assert isinstance(app.state.embedder, NullEmbedder)
    finally:
        await engine.dispose()


# ── reindex at startup ───────────────────────────────────────────────────────
async def _plant_rows_without_vectors(engine, *texts):
    from agent_memory.server import repository as repo
    from agent_memory.server.db import make_sessionmaker

    async with make_sessionmaker(engine)() as session, session.begin():
        return [await repo.add(session, t, "t", None, [], None) for t in texts]


async def _stored_models(engine):
    from sqlalchemy import select

    from agent_memory.server.models import Memory

    async with engine.connect() as conn:
        stmt = select(Memory.id, Memory.embedding_model).order_by(Memory.id)
        return dict((await conn.execute(stmt)).all())


async def test_lifespan_reindexes_once_at_startup(caplog):
    from agent_memory.server.app import create_app
    from agent_memory.server.db import make_sessionmaker
    from conftest import make_test_engine

    engine = make_test_engine()
    try:
        ids = await _plant_rows_without_vectors(engine, "before the model", "also before")
        app = create_app(sessionmaker=make_sessionmaker(engine), token="t",
                         embedder=FakeEmbedder())
        with caplog.at_level(logging.INFO, logger="agent_memory.server.app"):
            async with app.router.lifespan_context(app):
                pass
        assert await _stored_models(engine) == {ids[0]: "fake", ids[1]: "fake"}
        assert any("2 memories" in r.getMessage() for r in caplog.records)
    finally:
        await engine.dispose()


async def test_lifespan_skips_reindex_without_a_model(monkeypatch):
    from agent_memory.server.app import create_app
    from agent_memory.server.db import make_sessionmaker
    from conftest import make_test_engine

    monkeypatch.setenv("AGENT_MEMORY_EMBED_MODEL", "off")
    engine = make_test_engine()
    try:
        ids = await _plant_rows_without_vectors(engine, "stays bare")
        app = create_app(sessionmaker=make_sessionmaker(engine), token="t")
        async with app.router.lifespan_context(app):
            pass
        assert await _stored_models(engine) == {ids[0]: None}
    finally:
        await engine.dispose()


async def test_lifespan_survives_a_failed_reindex(caplog):
    from agent_memory.server.app import create_app
    from agent_memory.server.db import make_sessionmaker
    from conftest import make_test_engine

    class BrokenEmbedder(FakeEmbedder):
        def embed(self, texts):
            raise RuntimeError("model blew up")

    engine = make_test_engine()
    try:
        ids = await _plant_rows_without_vectors(engine, "never embedded")
        app = create_app(sessionmaker=make_sessionmaker(engine), token="t",
                         embedder=BrokenEmbedder())
        with caplog.at_level(logging.ERROR, logger="agent_memory.server.app"):
            async with app.router.lifespan_context(app):
                # The server is up: the failure was logged, not raised.
                assert app.state.embedder.model_name == "fake"
        assert await _stored_models(engine) == {ids[0]: None}
        assert any("reindex at startup failed" in r.getMessage() for r in caplog.records)
    finally:
        await engine.dispose()


# ── the real model, once ─────────────────────────────────────────────────────
@pytest.mark.embed
def test_real_model_embeds_unit_vectors(monkeypatch):
    pytest.importorskip("fastembed")
    monkeypatch.delenv("AGENT_MEMORY_EMBED_MODEL", raising=False)

    t0 = time.perf_counter()
    emb = make_embedder()
    load_s = time.perf_counter() - t0
    assert emb.model_name == embedding.DEFAULT_MODEL
    assert emb.dim == 384

    texts = [f"memory number {i}: a short note about topic {i % 7}" for i in range(100)]
    t0 = time.perf_counter()
    vecs = emb.embed(texts)
    embed_s = time.perf_counter() - t0
    print(f"\n[embed] model load {load_s:.2f}s; 100 texts {embed_s:.2f}s")

    assert len(vecs) == 100
    for v in vecs:
        assert len(v) == 384
        assert _length(v) == pytest.approx(1.0, abs=1e-5)

    # Close in meaning scores higher than far in meaning.
    cat, kitten, tax = emb.embed(["a cat sat on the mat", "a kitten sleeps on a rug",
                                  "quarterly tax filing deadline"])
    assert cosine(cat, kitten) > cosine(cat, tax)
    assert emb.embed([]) == []
