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
