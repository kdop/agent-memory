"""The memories and tags: the repository and the HTTP routes, in process.

The routes run on an in-process app (httpx's ASGI transport) over the test
database; the repository runs on a session. What only one surface shows
(the CLI's output, the MCP result shapes, the client's errors) is in
test_surfaces.py; search is in test_search.py and the review in
test_review.py.
"""

from datetime import datetime, timedelta

import httpx
import pytest

from agent_memory.server import repository as repo
from agent_memory.server.app import create_app
from agent_memory.server.embedding import NullEmbedder
from agent_memory.server.models import Memory
from agent_memory.server.schemas import TagIn
from conftest import AUTH, App, FakeEmbedder, make_test_engine, same_vector, stored_vector


def _tags(*specs):
    """TagIn objects from names or (name, description) pairs."""
    return [TagIn(name=s) if isinstance(s, str) else TagIn(name=s[0], description=s[1])
            for s in specs]


# ── auth and validation ──────────────────────────────────────────────────────
async def test_health_is_open_and_every_other_route_needs_the_token():
    async with App() as a:
        bare = httpx.AsyncClient(transport=httpx.ASGITransport(app=a.app),
                                 base_url="http://testserver")
        async with bare:
            health = await bare.get("/health")
            assert health.status_code == 200
            assert health.json() == {"status": "ok", "review_model": "off"}
            for method, path in (("GET", "/stats"), ("GET", "/memories"), ("GET", "/tags"),
                                 ("POST", "/memories"),
                                 ("POST", "/admin/reindex"), ("GET", "/memories/1/reviews"),
                                 ("GET", "/memories/flagged"), ("POST", "/admin/review"),
                                 ("POST", "/admin/review/1")):
                assert (await bare.request(method, path)).status_code == 401, path
            wrong = await bare.get("/stats", headers={"Authorization": "Bearer wrong"})
            assert wrong.status_code == 401
        assert (await a.client.get("/stats")).status_code == 200


async def test_an_app_with_an_empty_token_fails_closed():
    # Auth runs before any database access: 503 even with a bearer header.
    engine = make_test_engine()
    app = create_app(token="", embedder=FakeEmbedder())
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://testserver") as c:
            assert (await c.get("/stats", headers=AUTH)).status_code == 503
    finally:
        await engine.dispose()


@pytest.mark.parametrize("body", [
    {"content": "x", "agent": "t", "tags": [{"name": ""}]},
    {"content": "x", "agent": "t", "tags": [{"name": "   "}]},
    {"content": "x", "agent": "t", "type": "code"},
])
async def test_a_bad_add_is_422(body):
    async with App() as a:
        assert (await a.client.post("/memories", json=body)).status_code == 422


async def test_the_allowed_types_and_a_bad_type_on_update():
    async with App() as a:
        for t in ("decision", "lesson", "note", "preference"):
            await a.add("x", type=t)
        resp = await a.client.patch("/memories/1", json={"type": "reference"})
        assert resp.status_code == 422


# ── add, get, update, delete ─────────────────────────────────────────────────
async def test_add_then_read_back():
    async with App() as a:
        assert await a.add("first", agent="clu") == 1
        mid = await a.add("remember the milk", project="home", type="note",
                          tags=["shopping", "food"])
        assert mid == 2
        got = await a.get(mid)
        assert (got["content"], got["agent"], got["project"], got["type"]) == (
            "remember the milk", "tester", "home", "note")
        assert got["tags"] == ["food", "shopping"]          # alphabetical
        assert got["review_status"] == "unverified" and got["review"] is None
        assert got["score"] is None and got["snippet"] is None
        # The vector is internal: no route returns it.
        assert "embedding" not in got and "embedding_model" not in got
        assert (await a.get(1))["agent"] == "clu"
        assert (await a.client.get("/memories/999")).status_code == 404


async def test_update_reports_each_change():
    async with App() as a:
        mid = await a.add("old text", tags=["keep", "drop"])

        async def patch(body, mid=mid):
            resp = await a.client.patch(f"/memories/{mid}", json=body)
            return resp.status_code, resp.json()

        assert await patch({"content": "new text"}) == (200, {"changes": ["content"]})
        assert await patch({"content": "new text"}) == (200, {"changes": []})
        assert await patch({}) == (200, {"changes": []})
        assert await patch({"add_tags": [{"name": "new"}], "remove_tags": ["drop"]}) == (
            200, {"changes": ["+tags: new", "-tags: drop"]})
        assert (await a.get(mid))["tags"] == ["keep", "new"]
        assert await patch({"project": "beta", "type": "note"}) == (
            200, {"changes": ["project → beta", "type → note"]})
        assert await patch({"set_tags": [{"name": "c"}]}) == (
            200, {"changes": ["tags set to: c"]})
        # An empty string clears a field.
        assert await patch({"set_tags": [], "project": "", "type": ""}) == (
            200, {"changes": ["project → None", "type → None", "tags set to: (none)"]})
        row = await a.get(mid)
        assert (row["content"], row["project"], row["type"], row["tags"]) == (
            "new text", None, None, [])
        status, body = await patch({"content": "y"}, mid=999)
        assert status == 404 and "#999" in body["detail"]


async def test_delete_and_the_bulk_read():
    async with App() as a:
        for text in ("keepme aardvark", "deleteme aardvark"):
            await a.add(text, type="note")
        bulk = (await a.client.get("/memories/bulk", params={"ids": [1, 2, 999]})).json()
        assert {r["id"] for r in bulk} == {1, 2}
        assert set(bulk[0]) == {"id", "agent", "project", "type", "content"}
        resp = await a.client.request("DELETE", "/memories", params={"ids": [2, 999]})
        assert resp.json() == {"deleted": 1, "missing": [999]}
        assert (await a.client.get("/memories/2")).status_code == 404
        hits = (await a.client.get("/memories/search", params={"q": "aardvark"})).json()
        assert [h["id"] for h in hits] == [1]


async def test_update_changes_what_keyword_search_finds(session):
    mid = await repo.add(session, "findme original orangutan", "t", None, [], None)
    assert await repo.update(session, mid, content="replaced penguin") == ["content"]
    await session.flush()
    assert await repo.search(session, "orangutan") == []
    assert [h["id"] for h in await repo.search(session, "penguin")] == [mid]
    assert await repo.update(session, 999, content="y") is None
    assert await repo.get_many(session, []) == []


# ── the vector stored on write ───────────────────────────────────────────────
async def test_add_and_update_keep_the_vector_in_step_with_the_content(session):
    emb = FakeEmbedder()
    mid = await repo.add(session, "first text", "t", None, _tags("a"), None, embedder=emb)
    vec, model = await stored_vector(mid, session=session)
    assert model == "fake" and same_vector(vec, "first text")

    # Tags, project and type alone, or the same text again, keep the vector.
    await repo.update(session, mid, add_tags=_tags("b"), project="p", mtype="note",
                      embedder=emb)
    assert await repo.update(session, mid, content="first text") == []
    assert same_vector((await stored_vector(mid, session=session))[0], "first text")

    await repo.update(session, mid, content="second text", embedder=emb)
    assert same_vector((await stored_vector(mid, session=session))[0], "second text")

    # New text without a model clears it: a vector of the old text would be wrong.
    await repo.update(session, mid, content="third text", embedder=NullEmbedder())
    assert await stored_vector(mid, session=session) == (None, None)


@pytest.mark.parametrize("embedder", [None, NullEmbedder()])
async def test_add_without_a_model_stores_no_vector(session, embedder):
    mid = await repo.add(session, "no model here", "t", None, [], None, embedder=embedder)
    assert await stored_vector(mid, session=session) == (None, None)


async def test_the_routes_embed_on_add_and_on_new_content():
    async with App() as a:
        mid = await a.add("sent over http")
        vec, model = await stored_vector(mid)
        assert model == "fake" and same_vector(vec, "sent over http")
        await a.client.patch(f"/memories/{mid}", json={"add_tags": [{"name": "x"}]})
        assert same_vector((await stored_vector(mid))[0], "sent over http")
        await a.client.patch(f"/memories/{mid}", json={"content": "changed over http"})
        assert same_vector((await stored_vector(mid))[0], "changed over http")


# ── the timeline and its filters ─────────────────────────────────────────────
async def test_query_filters_in_the_repository(session):
    await repo.add(session, "one", "ann", "alpha", _tags("x"), "decision")
    await repo.add(session, "two", "bob", "beta", _tags("X"), "note")
    await repo.add(session, "three", "bob", "alpha", _tags("y"), None)

    async def contents(**filters):
        # One transaction: every row has the same timestamp, so compare sets.
        return {m["content"] for m in await repo.query(session, **filters)}

    assert await contents() == {"one", "two", "three"}
    assert await contents(project="alpha") == {"one", "three"}
    assert await contents(agent="bob") == {"two", "three"}
    assert await contents(tag="x") == {"one", "two"}         # any case
    assert await contents(mtype="decision") == {"one"}
    assert len(await contents(limit=1)) == 1
    assert await contents(since="2999-01-01") == set()
    assert await contents(until="2000-01-01") == set()
    assert await contents(status="unverified", current=True) == {"one", "two", "three"}
    with pytest.raises(ValueError):
        await repo.query(session, status="maybe")


async def test_since_days_is_a_rolling_window(session):
    today = await repo.add(session, "today", "t", None, [], None)
    old_id = await repo.add(session, "ten days ago", "t", None, [], None)
    (await session.get(Memory, old_id)).timestamp = datetime.now() - timedelta(days=10)
    await session.flush()
    assert [m["id"] for m in await repo.query(session, since_days=0)] == [today]
    assert [m["id"] for m in await repo.query(session, since_days=5)] == [today]
    assert len(await repo.query(session, since_days=30)) == 2
    rows, total = await repo.list_memories(session, since_days=5)
    assert [m["id"] for m in rows] == [today] and total == 1


async def test_the_list_route_filters_pages_and_counts():
    async with App() as a:
        for n in range(7):
            await a.add(f"m{n}")
        await a.add("has both", tags=["auth", "web"], project="beta", agent="clu",
                    type="note")
        await a.add("has auth", tags=["auth"], project="beta")
        await a.add("the quick brown fox, other", tags=["db"], project="beta")

        page = await a.client.get("/memories", params={"limit": 3, "offset": 0})
        assert len(page.json()) == 3 and page.headers["X-Total-Count"] == "10"
        last = await a.client.get("/memories", params={"limit": 3, "offset": 9})
        assert len(last.json()) == 1
        everything = await a.client.get("/memories", params={"limit": 0})
        assert len(everything.json()) == 10 and everything.headers["X-Total-Count"] == "10"
        assert (await a.client.get("/memories", params={"limit": -1})).status_code == 422

        # Several tags are OR'd.
        both = await a.client.get("/memories", params={"tag": ["auth", "web"]})
        assert {m["content"] for m in both.json()} == {"has both", "has auth"}
        assert both.headers["X-Total-Count"] == "2"
        assert await a.ids(agent="clu") == [8]
        assert await a.ids(type="note", project="beta") == [8]
        assert await a.ids(since_days=0, project="beta") == [10, 9, 8]
        assert await a.ids(until="2000-01-01") == []

        # Words: keyword match with a snippet.
        found = (await a.client.get("/memories", params={"q": "brown"})).json()
        assert [m["id"] for m in found] == [10] and "→brown←" in found[0]["snippet"]

        desc = await a.ids(order="date_desc")
        assert await a.ids(order="date_asc") == list(reversed(desc))
        assert await a.ids(order="id_asc", limit=2) == [1, 2]


# ── tags, projects, agents, stats ────────────────────────────────────────────
async def test_tags_are_shared_by_name_and_keep_the_newest_description(session):
    await repo.add(session, "a", "t", None, _tags(("db", "the database")), None)
    await repo.add(session, "b", "t", None, [TagIn(name="DB", description=None)], None)
    await repo.add(session, "c", "t", None, _tags(("solo", "  "), ("auth", "logins, oauth, jwt")),
                   None)
    tags = {t["name"]: t for t in await repo.list_tags(session)}
    assert set(tags) == {"db", "solo", "auth"}           # one row per name, any case
    assert tags["db"]["count"] == 2
    assert tags["db"]["description"] == "the database"   # none keeps the one it has
    assert tags["solo"]["description"] == "solo"          # none, or blank: its own name
    assert tags["auth"]["description"] == "logins, oauth, jwt"
    # A later explicit description replaces the earlier one.
    await repo.add(session, "d", "t", None, _tags(("db", "storage layer")), None)
    assert {t["name"]: t["description"] for t in await repo.list_tags(session)}["db"] == \
        "storage layer"
    # A tag no memory carries is not listed.
    await repo.update(session, 3, set_tags=[])
    assert {t["name"] for t in await repo.list_tags(session)} == {"db"}


async def test_projects_agents_and_stats():
    async with App() as a:
        empty = (await a.client.get("/stats")).json()
        assert (empty["total"], empty["oldest"], empty["newest"]) == (0, None, None)
        assert (await a.client.get("/projects")).json() == []
        await a.add("a", project="alpha", agent="clu", tags=["t"])
        await a.add("b", project="alpha", agent="tron")
        await a.add("c", project=None, agent="tron")
        assert (await a.client.get("/projects")).json() == [{"project": "alpha", "count": 2}]
        assert (await a.client.get("/agents")).json() == [{"agent": "tron", "count": 2},
                                                          {"agent": "clu", "count": 1}]
        s = (await a.client.get("/stats")).json()
        assert {k: s[k] for k in ("total", "agents", "projects", "tags", "today", "week")} == {
            "total": 3, "agents": 2, "projects": 1, "tags": 1, "today": 3, "week": 3}
        assert s["oldest"] is not None and s["newest"] is not None


async def test_tag_management_routes():
    async with App() as a:
        m1 = await a.add("a", tags=["authn", "shared"])
        await a.add("b", tags=["auth2", "shared"])
        await a.add("c", tags=["auth", "web"])

        async def names():
            return {t["name"]: t for t in (await a.client.get("/tags")).json()}

        resp = await a.client.patch("/tags/auth", json={"description": "authentication"})
        assert resp.json() == {"name": "auth", "description": "authentication", "count": 1}
        assert (await a.client.patch("/tags/web", json={"name": "frontend"})).json()["name"] == \
            "frontend"
        # A rename onto an existing tag merges the two.
        merged = (await a.client.patch("/tags/auth2", json={"name": "auth"})).json()
        assert (merged["name"], merged["count"]) == ("auth", 2)
        body = (await a.client.post("/tags/merge", json={
            "sources": ["authn", "nope"], "target": "auth", "description": "logins"})).json()
        assert body == {"target": "auth", "memories_affected": 1, "removed": ["authn"]}
        assert "auth" in (await a.get(m1))["tags"]
        tags = await names()
        assert set(tags) == {"auth", "shared", "frontend"}
        assert (tags["auth"]["count"], tags["auth"]["description"]) == (3, "logins")
        # Merging into a tag that does not exist yet creates it.
        body = (await a.client.post("/tags/merge", json={"sources": ["frontend"],
                                                         "target": "ui"})).json()
        assert body["target"] == "ui" and "frontend" not in await names()

        assert (await a.client.post("/tags/shared/detach",
                                    json={"memory_ids": [m1]})).json() == {"detached": 1}
        assert (await names())["shared"]["count"] == 1
        assert (await a.client.post("/tags/shared/detach", json={})).json() == {"detached": 1}
        assert "shared" not in await names()
        assert (await a.client.delete("/tags/ui")).json() == {"removed": "ui",
                                                              "memories_affected": 1}
        resp = await a.client.patch("/tags/auth", json={"name": None, "description": "x"})
        assert resp.json()["name"] == "auth"
        assert (await a.client.patch("/tags/auth", json={"name": "  "})).status_code == 422
        for method, path, body in (("PATCH", "/tags/nope", {"description": "x"}),
                                   ("DELETE", "/tags/nope", None),
                                   ("POST", "/tags/nope/detach", {})):
            assert (await a.client.request(method, path, json=body)).status_code == 404


async def test_the_session_dependency_is_one_transaction(session):
    from agent_memory.server.db import make_sessionmaker, session_dependency

    engine = make_test_engine()
    try:
        get_session = session_dependency(make_sessionmaker(engine))
        async for s in get_session():
            await repo.add(s, "written in the dependency", "t", None, [], None)
    finally:
        await engine.dispose()
    # Committed when the request ended: another session sees it.
    assert [m["content"] for m in await repo.query(session)] == ["written in the dependency"]


# ── the server on its own ────────────────────────────────────────────────────
async def test_without_a_sessionmaker_the_app_builds_its_engine_from_the_environment(
        monkeypatch):
    from agent_memory.server.review import NullReviewer

    from conftest import PG_DSN

    monkeypatch.setenv("AGENT_MEMORY_DB", PG_DSN)
    app = create_app(token="t", embedder=NullEmbedder(), reviewer=NullReviewer(),
                     review_poll=0)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://testserver",
                                     headers={"Authorization": "Bearer t"}) as c:
            assert (await c.get("/stats")).json()["total"] == 0


async def test_the_dashboard_is_served_next_to_the_api(monkeypatch, tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log('app')")
    (tmp_path / "index.html").write_text("<title>dashboard</title>")
    (tmp_path / "favicon.ico").write_text("icon")
    monkeypatch.setenv("AGENT_MEMORY_STATIC_DIR", str(tmp_path))
    async with App() as a:
        bare = httpx.AsyncClient(transport=httpx.ASGITransport(app=a.app),
                                 base_url="http://testserver")
        async with bare:
            # Any path that is not an API route gets the page, so a reload
            # on a deep route works; a real file is served as itself.
            for path in ("/", "/app", "/app/tags", "/app/totally/unmatched/path"):
                assert (await bare.get(path)).text == "<title>dashboard</title>", path
            assert (await bare.get("/favicon.ico")).text == "icon"
            assert (await bare.get("/assets/app.js")).text == "console.log('app')"
            # The API routes still win, and still need the token.
            assert (await bare.get("/tags")).status_code == 401
        assert (await a.client.get("/tags")).status_code == 200
