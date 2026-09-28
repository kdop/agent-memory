"""Tests for the review history (issue #71).

`memory_reviews` keeps every verdict the model gave on a memory, one row
each: a re-review adds a row and never writes over one, since nothing in
the timeline is rewritten. Every read shows the newest row as `review`, and
the memory's `review_status` and `supersedes` follow it. `GET
/memories/{id}/reviews`, the client's `reviews(mid)`, `memory show <id>
--reviews` and the MCP tool's `reviews` flag list the whole history. The
flagged listing and the catch-up work off the newest row per memory. An
update with new content deletes every row, as it deleted the one before.

Layers: the repository; the app with a `FakeReviewer` (re-review, status,
flagged, catch-up, update); the route, the client, the CLI and the MCP tool
against the live server; and the migration up and down with three rows for
one memory.

The `FakeReviewer`, the fixed verdicts and the in-process `_App` come from
test_review.py; the migration helpers from test_migration.py.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from agent_memory.client import ApiClient
from agent_memory.server import repository as repo
from agent_memory.server.db import make_sessionmaker
from agent_memory.server.models import Memory, MemoryReview
from agent_memory.server.review import NullReviewer, Verdict
from conftest import TOKEN, make_test_engine
from drivers import CliDriver, McpDriver
from test_flagged import REWRITE
from test_migration import (
    REVIEW_HISTORY,
    TAG_EMBEDDING,
    _alembic,
    _column_values,
    _columns,
    _create_all,
    _exec,
    _index_exists,
    _insert_memory,
    _scalar,
)
from test_review import APPROVE, REJECT, FakeReviewer, _App, _plant_review, _review_rows

ENTRY_KEYS = {"created_at", "verdict", "rule", "reason", "rewrite", "duplicate_of", "tags"}


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def _rows_stmt(mid):
    return (select(MemoryReview.verdict, MemoryReview.created_at)
            .where(MemoryReview.memory_id == mid)
            .order_by(MemoryReview.created_at, MemoryReview.id))


async def _rows_in(session, mid):
    """(verdict, created_at) of every row of memory `mid`, oldest first, as
    `session` sees them (the repository tests never commit)."""
    return (await session.execute(_rows_stmt(mid))).all()


async def _rows_of(mid):
    """The same, on a connection of its own: what is committed."""
    engine = make_test_engine()
    try:
        async with engine.connect() as conn:
            return (await conn.execute(_rows_stmt(mid))).all()
    finally:
        await engine.dispose()


async def _backdate(mid, verdict, minutes):
    """Move the row of `mid` with `verdict` `minutes` into the past, so the
    order of the rows is plain to see in the output."""
    engine = make_test_engine()
    try:
        async with engine.begin() as conn:
            when = datetime.now(timezone.utc) - timedelta(minutes=minutes)
            await conn.execute(update(MemoryReview)
                               .where(MemoryReview.memory_id == mid, MemoryReview.verdict == verdict)
                               .values(created_at=when))
    finally:
        await engine.dispose()


def _plant_history(mid):
    """Three verdicts on `mid`, oldest first: reject, rewrite, approve, dated
    30, 20 and 10 minutes ago. The newest is the approve."""
    asyncio.run(_plant_review(mid, REJECT))
    asyncio.run(_plant_review(mid, REWRITE))
    asyncio.run(_plant_review(mid, APPROVE))
    asyncio.run(_backdate(mid, "reject", 30))
    asyncio.run(_backdate(mid, "rewrite", 20))
    asyncio.run(_backdate(mid, "approve", 10))


# ── the repository ───────────────────────────────────────────────────────────
@pytest.fixture
async def session():
    engine = make_test_engine()
    try:
        async with make_sessionmaker(engine)() as s, s.begin():
            yield s
    finally:
        await engine.dispose()


async def test_set_review_adds_a_row_each_time_and_a_read_shows_the_newest(session):
    mid = await repo.add(session, "Chose Postgres for the store.", "t", "alpha", [], None)
    assert (await repo.get(session, mid))["review"] is None
    await repo.set_review(session, mid, REJECT, "m")
    assert (await repo.get(session, mid))["review"] == REJECT.as_dict()
    await repo.set_review(session, mid, APPROVE, "m")
    # The first verdict is still there; the read shows the second.
    assert [v for v, _ in await _rows_in(session, mid)] == ["reject", "approve"]
    assert (await repo.get(session, mid))["review"] == APPROVE.as_dict()
    assert (await repo.get(session, mid))["review_status"] == "verified"


async def test_reviews_lists_every_row_newest_first(session):
    mid = await repo.add(session, "one", "t", "alpha", [], None)
    assert await repo.reviews(session, mid) == []
    await repo.set_review(session, mid, REJECT, "m")
    await repo.set_review(session, mid, REWRITE, "m")
    await repo.set_review(session, mid, APPROVE, "m")
    rows = await repo.reviews(session, mid)
    assert [r["verdict"] for r in rows] == ["approve", "rewrite", "reject"]
    assert all(set(r) == ENTRY_KEYS for r in rows)
    # Newest first by date; each entry carries its verdict whole.
    assert rows[0]["created_at"] >= rows[1]["created_at"] >= rows[2]["created_at"]
    assert rows[1]["rewrite"] == REWRITE.rewrite and rows[1]["rule"] == 3
    assert rows[2]["reason"] == REJECT.reason
    assert await repo.reviews(session, 999) is None


async def test_status_and_supersedes_follow_the_newest_row(session):
    old = await repo.add(session, "first choice, because of A", "t", "alpha", [], None)
    mid = await repo.add(session, "second choice, because of B", "t", "alpha", [], None)
    await repo.set_review(session, mid, Verdict("approve", None, "Fine.", None, None, supersedes=old), "m")
    row = await repo.get(session, mid)
    assert row["review_status"] == "verified" and row["supersedes"] == old
    await repo.set_review(session, mid, REJECT, "m")
    row = await repo.get(session, mid)
    assert row["review_status"] == "flagged" and row["supersedes"] is None
    assert row["review"] == REJECT.as_dict()
    assert (await repo.get(session, old))["superseded_by"] is None
    assert len(await repo.reviews(session, mid)) == 2


async def test_flagged_and_count_use_the_newest_row(session):
    healed = await repo.add(session, "rejected, then approved", "t", "alpha", [], None)
    fell = await repo.add(session, "approved, then rejected", "t", "alpha", [], None)
    twice = await repo.add(session, "rejected twice", "t", "alpha", [], None)
    await repo.set_review(session, healed, REJECT, "m")
    await repo.set_review(session, healed, APPROVE, "m")
    await repo.set_review(session, fell, APPROVE, "m")
    await repo.set_review(session, fell, REJECT, "m")
    await repo.set_review(session, twice, REJECT, "m")
    await repo.set_review(session, twice, REWRITE, "m")
    # `healed` is out, since its newest verdict is an approve; the other two
    # are in once each, newest review first, with the newest verdict.
    rows = await repo.flagged(session)
    assert [r["id"] for r in rows] == [twice, fell]
    assert [r["review"]["verdict"] for r in rows] == ["rewrite", "reject"]
    assert await repo.count_flagged(session) == 2
    assert [r["id"] for r in await repo.flagged(session, verdict="reject")] == [fell]
    assert await repo.count_flagged(session, verdict="reject") == 1
    # With `status`, the same rule: the older reject on `healed` does not
    # make it flagged, and a verified listing shows its approve.
    assert [r["id"] for r in await repo.flagged(session, status="flagged")] == [twice, fell]
    verified = await repo.flagged(session, status="verified")
    assert [r["id"] for r in verified] == [healed]
    assert verified[0]["review"] == APPROVE.as_dict()
    assert await repo.flagged(session, status="verified", verdict="reject") == []


async def test_unverified_ids_leave_out_a_memory_with_any_verdict(session):
    bare = await repo.add(session, "never checked", "t", "alpha", [], None)
    healed = await repo.add(session, "rejected, then approved", "t", "alpha", [], None)
    await repo.set_review(session, healed, REJECT, "m")
    await repo.set_review(session, healed, APPROVE, "m")
    assert await repo.unverified_ids(session) == [bare]


async def test_update_with_new_content_deletes_every_row(session):
    mid = await repo.add(session, "the old text", "t", "alpha", [], None)
    await repo.set_review(session, mid, REJECT, "m")
    await repo.set_review(session, mid, APPROVE, "m")
    assert len(await _rows_in(session, mid)) == 2
    # A change that leaves the text alone keeps the history.
    await repo.update(session, mid, project="beta")
    assert len(await _rows_in(session, mid)) == 2
    assert await repo.update(session, mid, content="the new text") == ["content"]
    assert await _rows_in(session, mid) == []
    row = await repo.get(session, mid)
    assert row["review"] is None and row["review_status"] == "unverified"
    assert await repo.reviews(session, mid) == []


# ── the app, with a FakeReviewer ─────────────────────────────────────────────
async def test_re_review_adds_a_row_and_every_read_shows_the_newest():
    fake = FakeReviewer(REJECT)
    async with _App(reviewer=fake) as a:
        mid = (await a.add("Chose Postgres for the store."))["id"]
        assert (await a.get(mid))["review"] == REJECT.as_dict()

        fake.verdict = REWRITE
        assert (await a.client.post(f"/admin/review/{mid}")).status_code == 200
        fake.verdict = APPROVE
        resp = await a.client.post(f"/admin/review/{mid}")
        assert resp.status_code == 200 and resp.json() == APPROVE.as_dict()

        # Three rows, in the order they were given; every read shows the last.
        assert [v for v, _ in await _rows_of(mid)] == ["reject", "rewrite", "approve"]
        assert (await a.get(mid))["review"] == APPROVE.as_dict()
        listed = (await a.client.get("/memories", params={"project": "alpha"})).json()
        assert listed[0]["review"] == APPROVE.as_dict()
        found = (await a.client.get("/memories/search", params={"q": "Postgres"})).json()
        assert found[0]["review"] == APPROVE.as_dict()

        # The history route has them all, newest first.
        history = (await a.client.get(f"/memories/{mid}/reviews")).json()
        assert [r["verdict"] for r in history] == ["approve", "rewrite", "reject"]


async def test_status_follows_the_newest_row_through_the_app():
    fake = FakeReviewer(REJECT)
    async with _App(reviewer=fake) as a:
        mid = (await a.add("Chose Postgres for the store."))["id"]
        assert (await a.get(mid))["review_status"] == "flagged"
        fake.verdict = APPROVE
        await a.client.post(f"/admin/review/{mid}")
        assert (await a.get(mid))["review_status"] == "verified"
        # No verdict adds no row and moves nothing.
        fake.verdict = None
        assert (await a.client.post(f"/admin/review/{mid}")).status_code == 502
        row = await a.get(mid)
        assert row["review_status"] == "verified" and row["review"] == APPROVE.as_dict()
        assert [v for v, _ in await _rows_of(mid)] == ["reject", "approve"]


async def test_flagged_route_uses_the_newest_row():
    fake = FakeReviewer(REJECT)
    async with _App(reviewer=fake) as a:
        healed = (await a.add("rejected, then approved"))["id"]
        stays = (await a.add("rejected, and again"))["id"]
        fake.verdict = APPROVE
        await a.client.post(f"/admin/review/{healed}")
        fake.verdict = REWRITE
        await a.client.post(f"/admin/review/{stays}")
        resp = await a.client.get("/memories/flagged")
        assert [r["id"] for r in resp.json()] == [stays]
        assert resp.json()[0]["review"] == REWRITE.as_dict()
        assert resp.headers["X-Total-Count"] == "1"
        assert (await a.client.get("/memories/flagged", params={"status": "verified"})).json()[0]["id"] == healed


async def test_catch_up_reads_the_newest_row_and_a_fresh_review_after_an_update():
    fake = FakeReviewer(REJECT)
    async with _App(reviewer=fake) as a:
        healed = (await a.add("rejected, then approved"))["id"]
        fake.verdict = APPROVE
        await a.client.post(f"/admin/review/{healed}")
        # Nothing is unverified: the older reject does not count.
        fake.calls.clear()
        assert (await a.client.post("/admin/review")).json() == {"scheduled": 0}
        assert fake.calls == []

        # New text: the history goes, the memory is unverified, and the
        # catch-up reviews it again, starting a new history of one row.
        resp = await a.client.patch(f"/memories/{healed}", json={"content": "new text, because of X"})
        assert resp.json() == {"changes": ["content"]}
        assert await _rows_of(healed) == []
        assert (await a.get(healed))["review_status"] == "unverified"
        assert (await a.client.post("/admin/review")).json() == {"scheduled": 1}
        assert [m["id"] for m, _ in fake.calls] == [healed]
        assert [v for v, _ in await _rows_of(healed)] == ["approve"]
        assert (await a.get(healed))["review_status"] == "verified"


async def test_update_through_the_route_deletes_every_row():
    fake = FakeReviewer(REJECT)
    async with _App(reviewer=fake) as a:
        mid = (await a.add("the old text"))["id"]
        fake.verdict = REWRITE
        await a.client.post(f"/admin/review/{mid}")
        assert len(await _rows_of(mid)) == 2
        await a.client.patch(f"/memories/{mid}", json={"add_tags": [{"name": "db"}]})
        assert len(await _rows_of(mid)) == 2
        await a.client.patch(f"/memories/{mid}", json={"content": "the new text"})
        assert await _review_rows() == []
        assert (await a.client.get(f"/memories/{mid}/reviews")).json() == []


async def test_deleting_the_memory_deletes_every_row():
    fake = FakeReviewer(REJECT)
    async with _App(reviewer=fake) as a:
        mid = (await a.add("short lived"))["id"]
        fake.verdict = APPROVE
        await a.client.post(f"/admin/review/{mid}")
        assert len(await _rows_of(mid)) == 2
        await a.client.request("DELETE", "/memories", params={"ids": [mid]})
    assert await _review_rows() == []


# ── GET /memories/{id}/reviews, against the live server ──────────────────────
@pytest.fixture
def client(live_server):
    url, _ = live_server
    with httpx.Client(base_url=url, timeout=30, headers=_auth()) as c:
        yield c


def _add(client, content="Chose Postgres, because several agents write at once."):
    resp = client.post("/memories", json={"content": content, "agent": "tester", "project": "p"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_reviews_route_lists_newest_first_with_dates(client):
    mid = _add(client)
    _plant_history(mid)
    resp = client.get(f"/memories/{mid}/reviews")
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["verdict"] for r in rows] == ["approve", "rewrite", "reject"]
    assert all(set(r) == ENTRY_KEYS for r in rows)
    dates = [r["created_at"] for r in rows]
    assert dates == sorted(dates, reverse=True)
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(\+\d{2}:\d{2})?", d) for d in dates)
    assert rows[1] == {"created_at": dates[1], **{k: v for k, v in REWRITE.as_dict().items()
                                                  if k != "supersedes"}}
    # The memory itself shows only the newest.
    assert client.get(f"/memories/{mid}").json()["review"] == APPROVE.as_dict()


def test_reviews_route_empty_404_and_token(client):
    mid = _add(client)
    assert client.get(f"/memories/{mid}/reviews").json() == []
    resp = client.get("/memories/999/reviews")
    assert resp.status_code == 404 and "#999" in resp.json()["detail"]
    assert client.get(f"/memories/{mid}/reviews", headers={"Authorization": ""}).status_code == 401


def test_api_client_reviews(live_server):
    url, token = live_server
    api = ApiClient(url, token)
    mid = api.add("Chose Postgres, because several agents write at once.", "tester", "p", [], None)
    assert api.reviews(mid) == []
    assert api.reviews(999) is None
    _plant_history(mid)
    rows = api.reviews(mid)
    assert [r["verdict"] for r in rows] == ["approve", "rewrite", "reject"]
    assert rows[0]["created_at"] > rows[2]["created_at"]


# ── the CLI ──────────────────────────────────────────────────────────────────
@pytest.fixture
def cli(live_server):
    url, token = live_server
    return CliDriver(url, token)


def test_cli_show_reviews_prints_the_history_oldest_first(cli):
    assert "Memory #1 added" in cli.raw("add", "Chose Postgres, because several agents write at once.",
                                         "--project", "p").stdout
    _plant_history(1)
    dates = {r["verdict"]: r["created_at"] for r in ApiClient(cli.url, cli.token).reviews(1)}

    proc = cli.raw("show", "1", "--reviews")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    # The memory as `show` prints it: the header, the newest verdict on the
    # review line, the content; then the history under it, oldest first.
    assert "━━━ #1 status: verified" in out
    assert "review: approve: A decision with its reason." in out
    lines = re.findall(r"^review (\S+ \S+): (.+)$", out, re.M)
    assert lines == [
        (dates["reject"], "reject, rule 2: A diary line: it says what was done, not why."),
        (dates["rewrite"], "rewrite, rule 3: Say why."),
        (dates["approve"], "approve: A decision with its reason."),
    ]
    assert out.index("Chose Postgres") < out.index(f"review {dates['reject']}")
    # The rewrite carries its suggested text, indented, right under its line.
    at = out.index(f"review {dates['rewrite']}")
    assert out[at:].split("\n")[1:3] == ["suggested:", "    The same, but with the reason."]


def test_cli_show_without_the_flag_prints_no_history(cli):
    cli.raw("add", "Chose Postgres, because several agents write at once.", "--project", "p")
    _plant_history(1)
    out = cli.raw("show", "1").stdout
    assert out.count("review") == 1
    assert "review: approve" in out


def test_cli_show_reviews_says_when_there_are_none(cli):
    cli.raw("add", "Nothing checked this one.", "--project", "p")
    out = cli.raw("show", "1", "--reviews").stdout
    assert "No reviews yet." in out
    assert "review " not in out


def test_cli_show_reviews_help(cli):
    out = cli.raw("show", "--help").stdout
    assert "--reviews" in out and "oldest first" in out


# ── the MCP tool ─────────────────────────────────────────────────────────────
@pytest.fixture
def mcp(live_server):
    url, token = live_server
    return McpDriver(url, token)


def test_mcp_show_returns_the_history_when_asked(mcp):
    mid = mcp.add("Chose Postgres, because several agents write at once.", project="p")
    _plant_history(mid)
    plain = mcp._call("memory_show", id=mid)
    assert set(plain) == {"memory"}
    assert plain["memory"]["review"] == APPROVE.as_dict()
    data = mcp._call("memory_show", id=mid, reviews=True)
    assert set(data) == {"memory", "reviews"}
    assert data["memory"]["review"] == APPROVE.as_dict()
    assert [r["verdict"] for r in data["reviews"]] == ["approve", "rewrite", "reject"]
    assert all(set(r) == ENTRY_KEYS for r in data["reviews"])
    assert mcp._call("memory_show", id=999, reviews=True) == {"memory": None, "reviews": None}


def test_mcp_show_tool_has_the_flag(mcp):
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    async def run():
        async with connect(mcp._mcp) as session:
            tools = (await session.list_tools()).tools
            return next(t for t in tools if t.name == "memory_show")

    tool = asyncio.run(run())
    assert tool.inputSchema["properties"]["reviews"]["default"] is False
    assert "newest first" in tool.description


# ── the migration ────────────────────────────────────────────────────────────
async def _primary_key() -> list[str]:
    return await _column_values(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "WHERE i.indrelid = 'memory_reviews'::regclass AND i.indisprimary ORDER BY a.attnum")


async def test_review_history_revision_upgrades_and_downgrades():
    # Clean slate, then stop one step short: one row per memory, keyed by it.
    await _exec("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    try:
        _alembic("upgrade", TAG_EMBEDDING)
        assert await _columns("memory_reviews", "id") == {}
        assert await _primary_key() == ["memory_id"]
        mid = await _insert_memory("Chose Postgres.")
        other = await _insert_memory("Something else.")
        await _exec(
            "INSERT INTO memory_reviews (memory_id, verdict, rule, reason, model, created_at) "
            f"VALUES ({mid}, 'reject', 2, 'a diary line', 'm', now() - interval '30 minutes'), "
            f"({other}, 'approve', NULL, 'fine', 'm', now() - interval '25 minutes')")
        with pytest.raises(IntegrityError, match="memory_reviews_pkey"):
            await _exec("INSERT INTO memory_reviews (memory_id, verdict, reason, model) "
                        f"VALUES ({mid}, 'approve', 'fine', 'm')")

        # Upgrade one step: `id` is the key, the rows are kept and have one,
        # the index is there, and a memory can now hold several rows.
        _alembic("upgrade", REVIEW_HISTORY)
        assert await _columns("memory_reviews", "id") == {"id": ("int8", False)}
        assert await _primary_key() == ["id"]
        assert await _index_exists("ix_memory_reviews_memory_id_created_at")
        assert await _scalar("SELECT count(*) FROM memory_reviews") == 2
        assert await _scalar("SELECT count(DISTINCT id) FROM memory_reviews") == 2
        await _exec(
            "INSERT INTO memory_reviews (memory_id, verdict, rule, reason, rewrite, model, created_at) "
            f"VALUES ({mid}, 'rewrite', 3, 'say why', 'Chose Postgres, because of X.', 'm', "
            "now() - interval '20 minutes'); "
            "INSERT INTO memory_reviews (memory_id, verdict, reason, model, created_at) "
            f"VALUES ({mid}, 'approve', 'fine now', 'm', now() - interval '10 minutes')")
        assert await _column_values(
            f"SELECT verdict FROM memory_reviews WHERE memory_id = {mid} ORDER BY created_at"
        ) == ["reject", "rewrite", "approve"]

        # The repository at head sees the three rows and shows the newest.
        eng = make_test_engine()
        try:
            async with make_sessionmaker(eng)() as s, s.begin():
                assert (await repo.get(s, mid))["review"]["verdict"] == "approve"
                assert [r["verdict"] for r in await repo.reviews(s, mid)] == ["approve", "rewrite", "reject"]
                # Deleting the memory takes every row with it.
                await repo.delete(s, [other])
        finally:
            await eng.dispose()
        assert await _scalar("SELECT count(*) FROM memory_reviews") == 3

        # Downgrade one step: the newest row of the memory stays, the two
        # older ones go, `id` and the index are gone, `memory_id` is the key
        # again and refuses a second row.
        _alembic("downgrade", TAG_EMBEDDING)
        assert await _columns("memory_reviews", "id") == {}
        assert not await _index_exists("ix_memory_reviews_memory_id_created_at")
        assert await _primary_key() == ["memory_id"]
        assert await _column_values("SELECT verdict FROM memory_reviews") == ["approve"]
        assert await _scalar("SELECT reason FROM memory_reviews") == "fine now"
        with pytest.raises(IntegrityError, match="memory_reviews_pkey"):
            await _exec("INSERT INTO memory_reviews (memory_id, verdict, reason, model) "
                        f"VALUES ({mid}, 'reject', 'again', 'm')")
        assert await _scalar("SELECT count(*) FROM memories") == 1

        # Up again: the one row kept gets an id and the history grows from it.
        _alembic("upgrade", REVIEW_HISTORY)
        assert await _primary_key() == ["id"]
        await _exec("INSERT INTO memory_reviews (memory_id, verdict, reason, model) "
                    f"VALUES ({mid}, 'reject', 'again', 'm')")
        assert await _column_values("SELECT verdict FROM memory_reviews ORDER BY id") == ["approve", "reject"]
    finally:
        await _exec("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        await _create_all()
