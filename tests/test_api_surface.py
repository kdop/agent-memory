"""API-surface characterization — HTTP-specific concerns tested with a raw
`httpx.Client` against the live server: the open /health probe, bearer auth
(401), fail-closed 503 when the app has no token, request validation (422), the
and the search route not being shadowed by the id route.

Cross-surface behavior lives in test_behaviors.py (run against the API via
`ApiDriver`); this file pins the wire contract itself.
"""

import httpx
import pytest

from conftest import TOKEN, make_test_engine


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
    assert resp.json() == {"status": "ok"}


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
    assert _post(client, "dup me", "beta").status_code == 201
    assert _post(client, "dup me", None).status_code == 201


def test_add_same_content_with_no_project_twice_409(client):
    assert _post(client, "dup me", None).status_code == 201
    assert _post(client, "dup me", None).status_code == 409


def test_add_force_stores_the_duplicate(client):
    assert _post(client, "dup me", "proj").status_code == 201
    forced = _post(client, "dup me", "proj", force="true")
    assert forced.status_code == 201
    assert forced.json()["id"] == 2
    assert len(client.get("/memories", headers=_auth()).json()) == 2


def test_add_force_false_still_checks(client):
    assert _post(client, "dup me", "proj").status_code == 201
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
