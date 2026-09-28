"""API-surface characterization — HTTP-specific concerns tested with a raw
`httpx.Client` against the live server: the open /health probe, bearer auth
(401), fail-closed 503 when the app has no token, request validation (422), the
and the search route not being shadowed by the id route.

Cross-surface behavior lives in test_behaviors.py (run against the API via
`ApiDriver`); this file pins the wire contract itself.
"""

import httpx
import pytest

from conftest import TOKEN, make_test_engine, verify


@pytest.fixture
def client(live_server):
    url, _ = live_server
    with httpx.Client(base_url=url, timeout=30) as c:
        yield c


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


# ── auth ────────────────────────────────────────────────────────────────────
def test_health_needs_no_auth(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    # The shared server runs without a review model, so no poll: `off`.
    assert resp.json() == {"status": "ok", "review_model": "off"}


def test_protected_route_without_token_401(client):
    assert client.get("/stats").status_code == 401


def test_protected_route_bad_token_401(client):
    assert client.get("/stats", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_protected_route_good_token_200(client):
    assert client.get("/stats", headers=_auth()).status_code == 200


async def test_app_with_empty_token_fails_closed_503():
    # A separate app instance configured with an empty token must refuse to serve
    # data (503) even with a bearer header — fail closed. Uses httpx's in-process
    # ASGITransport (async-only) so it needs no second live server. Auth runs before
    # any DB access, so the empty-token 503 comes back without touching Postgres.
    from agent_memory.server.app import create_app
    from agent_memory.server.db import make_sessionmaker

    engine = make_test_engine()
    app = create_app(sessionmaker=make_sessionmaker(engine), token="")
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            resp = await c.get("/stats", headers=_auth())
            assert resp.status_code == 503
    finally:
        await engine.dispose()


# ── validation ──────────────────────────────────────────────────────────────
def test_empty_tag_name_422(client):
    resp = client.post(
        "/memories",
        json={"content": "x", "agent": "t", "tags": [{"name": ""}]},
        headers=_auth(),
    )
    assert resp.status_code == 422


def test_blank_tag_name_422(client):
    resp = client.post(
        "/memories",
        json={"content": "x", "agent": "t", "tags": [{"name": "   "}]},
        headers=_auth(),
    )
    assert resp.status_code == 422


def test_disallowed_memory_type_422(client):
    resp = client.post(
        "/memories",
        json={"content": "x", "agent": "t", "type": "code"},
        headers=_auth(),
    )
    assert resp.status_code == 422


def test_allowed_memory_type_200(client):
    resp = client.post(
        "/memories",
        json={"content": "x", "agent": "t", "type": "preference"},
        headers=_auth(),
    )
    assert resp.status_code == 201


def test_update_disallowed_memory_type_422(client):
    mid = client.post(
        "/memories", json={"content": "x", "agent": "t"}, headers=_auth(),
    ).json()["id"]
    resp = client.patch(
        f"/memories/{mid}", json={"type": "reference"}, headers=_auth(),
    )
    assert resp.status_code == 422


# ── routing ─────────────────────────────────────────────────────────────────
def test_search_route_not_shadowed_by_id_route(client):
    # /memories/search must win over /memories/{mid:int}; a search must not 422.
    client.post("/memories", json={"content": "alpha beta", "agent": "t"}, headers=_auth())
    resp = client.get("/memories/search", params={"q": "beta"}, headers=_auth())
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_add_then_read_round_trips_over_http(client):
    mid = client.post(
        "/memories",
        json={"content": "over the wire", "tags": [{"name": "net"}], "agent": "tester"},
        headers=_auth(),
    ).json()["id"]
    got = client.get(f"/memories/{mid}", headers=_auth()).json()
    assert got["content"] == "over the wire"
    assert got["tags"] == ["net"]


# ── warnings on add ──────────────────────────────────────────────────────────
def test_add_returns_warnings_and_still_stores(client):
    resp = client.post(
        "/memories",
        json={"content": "tiny", "type": "decision", "agent": "tester"},
        headers=_auth(),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["warnings"] == ["short", "no-project", "no-reasoning"]
    # Stored regardless of the warnings.
    assert client.get(f"/memories/{body['id']}", headers=_auth()).status_code == 200


def test_add_clean_entry_has_empty_warnings(client):
    resp = client.post(
        "/memories",
        json={"content": "Run the build on every push because the nightly was too slow.",
              "project": "ci", "type": "decision", "agent": "tester"},
        headers=_auth(),
    )
    assert resp.status_code == 201
    assert resp.json()["warnings"] == []


# ── embedding on write ──────────────────────────────────────────────────────
async def _stored_vector(mid):
    """Read the two internal columns straight from the table; no route returns them."""
    from sqlalchemy import select

    from agent_memory.server.models import Memory

    engine = make_test_engine()
    try:
        async with engine.connect() as conn:
            stmt = select(Memory.embedding, Memory.embedding_model).where(Memory.id == mid)
            return (await conn.execute(stmt)).one()
    finally:
        await engine.dispose()


async def test_routes_embed_on_add_and_content_update(live_server):
    # The live server runs with FakeEmbedder, so POST must store a vector and
    # PATCH of the content must replace it, while PATCH of tags keeps it.
    from conftest import FakeEmbedder

    url, _ = live_server
    async with httpx.AsyncClient(base_url=url, timeout=30) as c:
        body = (await c.post("/memories", json={"content": "sent over http", "agent": "t"},
                             headers=_auth())).json()
        mid = body["id"]
        assert "embedding" not in body
        vec, model = await _stored_vector(mid)
        assert model == "fake"
        assert vec == pytest.approx(FakeEmbedder().embed(["sent over http"])[0], abs=1e-6)

        await c.patch(f"/memories/{mid}", json={"add_tags": [{"name": "x"}]}, headers=_auth())
        assert (await _stored_vector(mid))[0] == pytest.approx(vec, abs=1e-6)

        await c.patch(f"/memories/{mid}", json={"content": "changed over http"}, headers=_auth())
        vec2, model2 = await _stored_vector(mid)
        assert model2 == "fake"
        assert vec2 == pytest.approx(FakeEmbedder().embed(["changed over http"])[0], abs=1e-6)

        got = (await c.get(f"/memories/{mid}", headers=_auth())).json()
        assert "embedding" not in got and "embedding_model" not in got


# ── duplicate refusal on add ─────────────────────────────────────────────────
def _post(client, content, project=None, **params):
    return client.post("/memories", params=params, headers=_auth(),
                       json={"content": content, "project": project, "agent": "tester"})


def test_add_identical_content_409_with_existing_id(client):
    first = _post(client, "dup me", "proj")
    assert first.status_code == 201
    existing = first.json()["id"]
    verify(existing)   # only a verified memory counts as reference

    again = _post(client, "dup me", "proj")
    assert again.status_code == 409
    detail = again.json()["detail"]
    assert detail["reason"] == "duplicate"
    assert detail["existing_id"] == existing
    assert detail["score"] == pytest.approx(1.0, abs=1e-5)
    assert set(again.json()) == {"detail"}
    assert set(detail) == {"reason", "existing_id", "score"}
    # Nothing was inserted.
    assert len(client.get("/memories", headers=_auth()).json()) == 1


def test_add_same_content_in_another_project_is_stored(client):
    assert _post(client, "dup me", "alpha").status_code == 201
    verify(1)
    assert _post(client, "dup me", "beta").status_code == 201
    assert _post(client, "dup me", None).status_code == 201


def test_add_same_content_with_no_project_twice_409(client):
    assert _post(client, "dup me", None).status_code == 201
    verify(1)
    assert _post(client, "dup me", None).status_code == 409


def test_add_force_stores_the_duplicate(client):
    assert _post(client, "dup me", "proj").status_code == 201
    verify(1)
    forced = _post(client, "dup me", "proj", force="true")
    assert forced.status_code == 201
    assert forced.json()["id"] == 2
    assert len(client.get("/memories", headers=_auth()).json()) == 2


def test_add_force_false_still_checks(client):
    assert _post(client, "dup me", "proj").status_code == 201
    verify(1)
    assert _post(client, "dup me", "proj", force="false").status_code == 409


async def test_add_without_model_never_refuses():
    # A separate in-process app with a NullEmbedder: no vectors, so the same
    # content twice is stored twice.
    from agent_memory.server.app import create_app
    from agent_memory.server.db import make_sessionmaker
    from agent_memory.server.embedding import NullEmbedder

    engine = make_test_engine()
    app = create_app(sessionmaker=make_sessionmaker(engine), token=TOKEN,
                     embedder=NullEmbedder())
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            body = {"content": "dup me", "project": "proj", "agent": "tester"}
            first = await c.post("/memories", json=body, headers=_auth())
            again = await c.post("/memories", json=body, headers=_auth())
            assert first.status_code == 201
            assert again.status_code == 201
            assert again.json()["id"] != first.json()["id"]
    finally:
        await engine.dispose()


# ── search modes ────────────────────────────────────────────────────────────
def _seed_search(client):
    """Four memories; the live server embeds each one with FakeEmbedder."""
    ids = {}
    for key, content in [("cat", "the cat sat on the mat"), ("dog", "a dog in the yard"),
                         ("rain", "rain on the window"), ("code", "coffee before code")]:
        ids[key] = client.post("/memories", json={"content": content, "agent": "t"},
                               headers=_auth()).json()["id"]
    return ids


def test_search_default_mode_is_keyword_with_score(client):
    ids = _seed_search(client)
    resp = client.get("/memories/search", params={"q": "cat"}, headers=_auth())
    assert resp.status_code == 200
    hits = resp.json()
    assert [h["id"] for h in hits] == [ids["cat"]]
    assert hits[0]["score"] > 0
    assert "cat" in hits[0]["snippet"]
    same = client.get("/memories/search", params={"q": "cat", "mode": "keyword"},
                      headers=_auth()).json()
    assert same == hits


def test_search_semantic_mode_over_http(client):
    ids = _seed_search(client)
    query = "the cat sat on the mat"
    resp = client.get("/memories/search", params={"q": query, "mode": "semantic"},
                      headers=_auth())
    assert resp.status_code == 200
    hits = resp.json()
    assert len(hits) == 4
    assert hits[0]["id"] == ids["cat"]
    assert hits[0]["score"] == pytest.approx(1.0, abs=1e-6)
    assert hits[0]["snippet"] is None
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True)

    limited = client.get("/memories/search", params={"q": query, "mode": "semantic", "limit": 2},
                         headers=_auth()).json()
    assert [h["id"] for h in limited] == [h["id"] for h in hits[:2]]


def test_search_hybrid_mode_over_http(client):
    ids = _seed_search(client)
    query = "the cat sat on the mat"
    resp = client.get("/memories/search", params={"q": query, "mode": "hybrid"},
                      headers=_auth())
    assert resp.status_code == 200
    assert "X-Search-Fallback" not in resp.headers
    hits = resp.json()
    assert len(hits) == 4
    # The cat memory is rank 1 by words and rank 1 by meaning: 1/61 + 1/61.
    assert hits[0]["id"] == ids["cat"]
    assert hits[0]["score"] == pytest.approx(2 / 61)
    assert "cat" in hits[0]["snippet"]
    # The others were found by meaning only: no words to highlight.
    assert all(h["snippet"] is None for h in hits[1:])
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True)

    limited = client.get("/memories/search", params={"q": query, "mode": "hybrid", "limit": 2},
                         headers=_auth()).json()
    assert [h["id"] for h in limited] == [h["id"] for h in hits[:2]]


@pytest.mark.parametrize("embedder", ["null", "none"])
async def test_search_hybrid_without_model_falls_back_to_keyword(embedder):
    # No model means no semantic half to fuse. Hybrid then serves the keyword
    # results and says so in a header, so the caller can tell.
    from agent_memory.server.app import create_app
    from agent_memory.server.db import make_sessionmaker
    from agent_memory.server.embedding import NullEmbedder

    engine = make_test_engine()
    kwargs = {"embedder": NullEmbedder("fastembed is not installed")} if embedder == "null" else {}
    app = create_app(sessionmaker=make_sessionmaker(engine), token=TOKEN, **kwargs)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            for content in ["the cat sat on the mat", "a cat in the yard", "coffee before code"]:
                await c.post("/memories", json={"content": content, "agent": "t"}, headers=_auth())
            hybrid = await c.get("/memories/search", params={"q": "cat", "mode": "hybrid"},
                                 headers=_auth())
            keyword = await c.get("/memories/search", params={"q": "cat"}, headers=_auth())
            assert hybrid.status_code == 200
            assert hybrid.headers["X-Search-Fallback"] == "keyword"
            assert len(hybrid.json()) == 2
            assert hybrid.json() == keyword.json()
            assert "X-Search-Fallback" not in keyword.headers
    finally:
        await engine.dispose()


def test_search_unknown_mode_422(client):
    resp = client.get("/memories/search", params={"q": "x", "mode": "fuzzy"}, headers=_auth())
    assert resp.status_code == 422


@pytest.mark.parametrize("embedder", ["null", "none"])
async def test_search_semantic_without_model_400(embedder):
    # An app whose embedder is a NullEmbedder (model off or not installed), or that
    # has none at all, must refuse semantic search with a message that says why.
    # Keyword search keeps working on the same app.
    from agent_memory.server.app import create_app
    from agent_memory.server.db import make_sessionmaker
    from agent_memory.server.embedding import NullEmbedder

    engine = make_test_engine()
    kwargs = {"embedder": NullEmbedder("fastembed is not installed")} if embedder == "null" else {}
    app = create_app(sessionmaker=make_sessionmaker(engine), token=TOKEN, **kwargs)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            resp = await c.get("/memories/search", params={"q": "x", "mode": "semantic"},
                               headers=_auth())
            assert resp.status_code == 400
            detail = resp.json()["detail"]
            assert detail.startswith("semantic search is not available")
            if embedder == "null":
                assert "fastembed is not installed" in detail
            else:
                assert "off or not installed" in detail
            ok = await c.get("/memories/search", params={"q": "x"}, headers=_auth())
            assert ok.status_code == 200
    finally:
        await engine.dispose()


# ── POST /admin/reindex ─────────────────────────────────────────────────────
async def _clear_vectors():
    """Drop every stored vector, as if the rows were written before the model
    existed. Goes straight to the table; no route can do this."""
    from sqlalchemy import update

    from agent_memory.server.models import Memory

    engine = make_test_engine()
    try:
        async with engine.begin() as conn:
            await conn.execute(update(Memory).values(embedding=None, embedding_model=None))
    finally:
        await engine.dispose()


def test_reindex_needs_token(client):
    assert client.post("/admin/reindex").status_code == 401


async def test_reindex_returns_the_count_and_fills_vectors(live_server):
    from conftest import FakeEmbedder

    url, _ = live_server
    async with httpx.AsyncClient(base_url=url, timeout=30) as c:
        ids = []
        for text in ("first", "second", "third"):
            body = (await c.post("/memories", json={"content": text, "agent": "t"},
                                 headers=_auth())).json()
            ids.append(body["id"])
        await _clear_vectors()
        assert (await _stored_vector(ids[0])) == (None, None)

        resp = await c.post("/admin/reindex", headers=_auth())
        assert resp.status_code == 200
        assert resp.json() == {"updated": 3}
        for mid, text in zip(ids, ("first", "second", "third")):
            vec, model = await _stored_vector(mid)
            assert model == "fake"
            assert vec == pytest.approx(FakeEmbedder().embed([text])[0], abs=1e-6)

        # Nothing left to do the second time.
        resp = await c.post("/admin/reindex", headers=_auth())
        assert resp.json() == {"updated": 0}


async def test_reindex_503_when_the_server_has_no_model():
    from agent_memory.server.app import create_app
    from agent_memory.server.db import make_sessionmaker
    from agent_memory.server.embedding import NullEmbedder

    engine = make_test_engine()
    app = create_app(sessionmaker=make_sessionmaker(engine), token=TOKEN,
                     embedder=NullEmbedder("embedding is off"))
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            resp = await c.post("/admin/reindex", headers=_auth())
            assert resp.status_code == 503
            assert "embedding is off" in resp.json()["detail"]
    finally:
        await engine.dispose()
