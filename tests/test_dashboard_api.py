"""Dashboard API (D1) — the unified list endpoint and tag management, tested with a
raw `httpx.Client` against the live server. These endpoints back the web dashboard;
the CLI/MCP surfaces don't exercise them, so they're pinned here.
"""

import httpx
import pytest

from conftest import TOKEN


@pytest.fixture
def client(live_server):
    url, _ = live_server
    with httpx.Client(base_url=url, timeout=30) as c:
        yield c


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def _add(client, content, tags=(), project=None):
    body = {"content": content, "agent": "t", "project": project,
            "tags": [{"name": t} if isinstance(t, str) else t for t in tags]}
    return client.post("/memories", json=body, headers=_auth()).json()["id"]


# ── unified GET /memories ─────────────────────────────────────────────────────
def test_list_pagination_and_total_header(client):
    for i in range(7):
        _add(client, f"m{i}")
    r = client.get("/memories", params={"limit": 3, "offset": 0}, headers=_auth())
    assert r.status_code == 200
    assert len(r.json()) == 3
    assert r.headers["X-Total-Count"] == "7"
    # second page
    r2 = client.get("/memories", params={"limit": 3, "offset": 6}, headers=_auth())
    assert len(r2.json()) == 1


def test_list_multi_tag_or_filter(client):
    _add(client, "has both", ["auth", "web"])
    _add(client, "has auth", ["auth"])
    _add(client, "has other", ["db"])
    r = client.get("/memories", params={"tag": ["auth", "web"]}, headers=_auth())
    contents = {m["content"] for m in r.json()}
    assert contents == {"has both", "has auth"}   # OR — any of auth/web; "has other" (db) excluded
    assert r.headers["X-Total-Count"] == "2"


def test_list_query_returns_snippet(client):
    _add(client, "the quick brown fox")
    _add(client, "nothing relevant")
    r = client.get("/memories", params={"q": "brown"}, headers=_auth())
    rows = r.json()
    assert len(rows) == 1
    assert "brown" in rows[0]["snippet"]


def test_list_order_asc_desc(client):
    _add(client, "first")
    _add(client, "second")
    desc = client.get("/memories", params={"order": "date_desc"}, headers=_auth()).json()
    asc = client.get("/memories", params={"order": "date_asc"}, headers=_auth()).json()
    assert [m["id"] for m in desc] == list(reversed([m["id"] for m in asc]))


# ── tag management ────────────────────────────────────────────────────────────
def test_patch_tag_describe(client):
    _add(client, "x", ["auth"])
    r = client.patch("/tags/auth", json={"description": "authentication"}, headers=_auth())
    assert r.status_code == 200
    assert r.json()["description"] == "authentication"


def test_patch_tag_rename(client):
    _add(client, "x", ["web"])
    r = client.patch("/tags/web", json={"name": "frontend"}, headers=_auth())
    assert r.json()["name"] == "frontend"
    assert {t["name"] for t in client.get("/tags", headers=_auth()).json()} == {"frontend"}


def test_patch_tag_rename_collision_merges(client):
    _add(client, "a", ["authn"])
    _add(client, "b", ["auth"])
    # rename authn → auth (existing) merges the two
    r = client.patch("/tags/authn", json={"name": "auth"}, headers=_auth())
    assert r.json()["name"] == "auth"
    assert r.json()["count"] == 2
    assert {t["name"] for t in client.get("/tags", headers=_auth()).json()} == {"auth"}


def test_patch_tag_404(client):
    assert client.patch("/tags/nope", json={"description": "x"}, headers=_auth()).status_code == 404


def test_delete_tag(client):
    _add(client, "x", ["temp"])
    r = client.delete("/tags/temp", headers=_auth())
    assert r.json() == {"removed": "temp", "memories_affected": 1}
    assert client.get("/tags", headers=_auth()).json() == []


def test_delete_tag_404(client):
    assert client.delete("/tags/nope", headers=_auth()).status_code == 404


def test_merge_tags(client):
    m1 = _add(client, "a", ["authn"])
    _add(client, "b", ["auth2"])
    _add(client, "c", ["auth"])
    r = client.post("/tags/merge",
                    json={"sources": ["authn", "auth2"], "target": "auth",
                          "description": "authentication"},
                    headers=_auth())
    body = r.json()
    assert body["target"] == "auth"
    assert sorted(body["removed"]) == ["auth2", "authn"]
    assert body["memories_affected"] == 2
    # the source memory now carries the target tag
    assert "auth" in client.get(f"/memories/{m1}", headers=_auth()).json()["tags"]
    names = {t["name"]: t for t in client.get("/tags", headers=_auth()).json()}
    assert set(names) == {"auth"}
    assert names["auth"]["description"] == "authentication"   # target kept the edited description
    assert names["auth"]["count"] == 3


def test_merge_into_new_target(client):
    _add(client, "a", ["x"])
    _add(client, "b", ["y"])
    r = client.post("/tags/merge", json={"sources": ["x", "y"], "target": "z"}, headers=_auth())
    assert r.json()["target"] == "z"
    assert {t["name"] for t in client.get("/tags", headers=_auth()).json()} == {"z"}


def test_detach_tag_from_specific(client):
    m1 = _add(client, "a", ["shared"])
    _add(client, "b", ["shared"])
    r = client.post("/tags/shared/detach", json={"memory_ids": [m1]}, headers=_auth())
    assert r.json() == {"detached": 1}
    # tag still exists on the other memory
    assert dict((t["name"], t["count"]) for t in client.get("/tags", headers=_auth()).json())["shared"] == 1


def test_detach_tag_from_all(client):
    _add(client, "a", ["shared"])
    _add(client, "b", ["shared"])
    r = client.post("/tags/shared/detach", json={}, headers=_auth())
    assert r.json() == {"detached": 2}
    # zero-memory tags are dropped from the listing
    assert "shared" not in {t["name"] for t in client.get("/tags", headers=_auth()).json()}


def test_detach_tag_404(client):
    assert client.post("/tags/nope/detach", json={}, headers=_auth()).status_code == 404
