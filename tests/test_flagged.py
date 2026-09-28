"""Tests for the list of flagged memories and the review of the ones that have
none (issue #33).

Four layers. The repository: `flagged` orders by the review's time, keeps to
reject and rewrite, and takes the project, verdict and limit filters;
`without_review` names the memories that have no row, newest first. The
routes: `GET /memories/flagged` with its header and its 422, not shadowed by
the id route; `POST /admin/review` schedules only the memories without a row
and answers 503 without a model. The CLI: `memory review` prints flagged
memories the way `query` does, and `--missing` prints what was scheduled.
The MCP tool `memory_flagged` gives the same shape as `memory_query`.

The `FakeReviewer` and the fixed verdicts come from test_review.py.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select, update

from agent_memory.server import repository as repo
from agent_memory.server.app import create_app
from agent_memory.server.db import make_sessionmaker
from agent_memory.server.models import MemoryReview
from agent_memory.server.review import NullReviewer, Verdict
from conftest import TOKEN, FakeEmbedder, _free_port, make_test_engine
from drivers import CliDriver, McpDriver
from test_review import APPROVE, REJECT, FakeReviewer

REWRITE = Verdict("rewrite", 3, "Say why.", "The same, but with the reason.", None)


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


async def _add(session, content, project="alpha"):
    return await repo.add(session, content, "tester", project, [], None)


async def _plant(session, mid, verdict, age_minutes=0):
    """Store `verdict` as the review of `mid`, dated `age_minutes` ago. The
    ordering tests need reviews whose times differ from the memories' order."""
    await repo.set_review(session, mid, verdict, "planted")
    if age_minutes:
        when = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
        await session.execute(update(MemoryReview).where(MemoryReview.memory_id == mid)
                              .values(created_at=when))


async def _review_rows():
    engine = make_test_engine()
    try:
        async with engine.connect() as conn:
            stmt = select(MemoryReview.memory_id, MemoryReview.verdict).order_by(MemoryReview.memory_id)
            return (await conn.execute(stmt)).all()
    finally:
        await engine.dispose()


async def _seed(session):
    """Five memories, four reviewed. Returns their ids by name.

    Review times, oldest first: old_reject, rewrite, approve, new_reject. So
    the flagged order is new_reject, rewrite, old_reject; `bare` has no row."""
    ids = {
        "old_reject": await _add(session, "Spent the afternoon tidying."),
        "rewrite": await _add(session, "Chose Postgres.", project="beta"),
        "approve": await _add(session, "Chose Postgres because several agents write at once."),
        "new_reject": await _add(session, "Had lunch, then read the config module."),
        "bare": await _add(session, "Not reviewed yet."),
    }
    await _plant(session, ids["old_reject"], REJECT, age_minutes=30)
    await _plant(session, ids["rewrite"], REWRITE, age_minutes=20)
    await _plant(session, ids["approve"], APPROVE, age_minutes=10)
    await _plant(session, ids["new_reject"], REJECT)
    return ids


# ── the repository ───────────────────────────────────────────────────────────
@pytest.fixture
async def session():
    engine = make_test_engine()
    try:
        async with make_sessionmaker(engine)() as s, s.begin():
            yield s
    finally:
        await engine.dispose()


async def test_flagged_lists_reject_and_rewrite_newest_review_first(session):
    ids = await _seed(session)
    rows = await repo.flagged(session)
    assert [r["id"] for r in rows] == [ids["new_reject"], ids["rewrite"], ids["old_reject"]]
    # The same shape as `query`, the review included.
    assert set(rows[0]) == set((await repo.query(session))[0])
    assert rows[0]["review"] == REJECT.as_dict()
    assert rows[1]["review"] == REWRITE.as_dict()
    assert rows[0]["content"] == "Had lunch, then read the config module."


async def test_flagged_orders_by_review_time_not_memory_id(session):
    # The oldest memory gets the newest review, so it comes first.
    first = await _add(session, "first written")
    second = await _add(session, "second written")
    await _plant(session, second, REJECT, age_minutes=5)
    await _plant(session, first, REJECT)
    assert [r["id"] for r in await repo.flagged(session)] == [first, second]


async def test_flagged_ties_on_review_time_go_newest_id_first(session):
    a = await _add(session, "a")
    b = await _add(session, "b")
    same = datetime.now(timezone.utc)
    for mid in (a, b):
        await repo.set_review(session, mid, REJECT, "planted")
    await session.execute(update(MemoryReview).values(created_at=same))
    assert [r["id"] for r in await repo.flagged(session)] == [b, a]


async def test_flagged_by_verdict(session):
    ids = await _seed(session)
    rejects = await repo.flagged(session, verdict="reject")
    assert [r["id"] for r in rejects] == [ids["new_reject"], ids["old_reject"]]
    rewrites = await repo.flagged(session, verdict="rewrite")
    assert [r["id"] for r in rewrites] == [ids["rewrite"]]


async def test_flagged_by_project(session):
    ids = await _seed(session)
    assert [r["id"] for r in await repo.flagged(session, project="beta")] == [ids["rewrite"]]
    alpha = await repo.flagged(session, project="alpha")
    assert [r["id"] for r in alpha] == [ids["new_reject"], ids["old_reject"]]
    assert await repo.flagged(session, project="gamma") == []
    assert await repo.flagged(session, project="alpha", verdict="rewrite") == []


async def test_flagged_limit(session):
    ids = await _seed(session)
    assert [r["id"] for r in await repo.flagged(session, limit=2)] == [ids["new_reject"], ids["rewrite"]]
    assert len(await repo.flagged(session, limit=0)) == 3
    assert len(await repo.flagged(session, limit=None)) == 3


async def test_flagged_rejects_an_unknown_verdict(session):
    with pytest.raises(ValueError, match="approve"):
        await repo.flagged(session, verdict="approve")


async def test_count_flagged_ignores_the_limit(session):
    await _seed(session)
    assert await repo.count_flagged(session) == 3
    assert await repo.count_flagged(session, verdict="reject") == 2
    assert await repo.count_flagged(session, project="beta") == 1
    assert await repo.count_flagged(session, project="gamma") == 0


async def test_flagged_is_empty_without_reviews(session):
    await _add(session, "nothing reviewed")
    assert await repo.flagged(session) == []
    assert await repo.count_flagged(session) == 0


async def test_without_review_names_the_bare_memories_newest_first(session):
    ids = await _seed(session)
    assert await repo.without_review(session) == [ids["bare"]]
    late = await _add(session, "also bare")
    early = await _add(session, "bare too")
    await session.execute(update(repo.Memory).where(repo.Memory.id == early)
                          .values(timestamp=datetime.now(timezone.utc) - timedelta(minutes=5)))
    assert await repo.without_review(session) == [late, ids["bare"], early]
    assert await repo.without_review(session, limit=2) == [late, ids["bare"]]
    assert await repo.without_review(session, limit=0) == [late, ids["bare"], early]


# ── GET /memories/flagged, against the live server ───────────────────────────
@pytest.fixture
def client(live_server):
    url, _ = live_server
    with httpx.Client(base_url=url, timeout=30, headers=_auth()) as c:
        yield c


async def _seeded():
    engine = make_test_engine()
    try:
        async with make_sessionmaker(engine)() as s, s.begin():
            return await _seed(s)
    finally:
        await engine.dispose()


def test_flagged_route_lists_newest_review_first_with_the_total(client):
    ids = asyncio.run(_seeded())
    resp = client.get("/memories/flagged")
    assert resp.status_code == 200
    assert resp.headers["X-Total-Count"] == "3"
    rows = resp.json()
    assert [r["id"] for r in rows] == [ids["new_reject"], ids["rewrite"], ids["old_reject"]]
    assert set(rows[0]) == set(client.get("/memories").json()[0])
    assert rows[0]["review"] == REJECT.as_dict()


def test_flagged_route_not_shadowed_by_id_route(client):
    # Without the route order, "flagged" would be read as an id and fail with 422.
    assert client.get("/memories/flagged").status_code == 200
    assert client.get("/memories/flagged").json() == []


def test_flagged_route_filters(client):
    ids = asyncio.run(_seeded())
    resp = client.get("/memories/flagged", params={"verdict": "rewrite"})
    assert [r["id"] for r in resp.json()] == [ids["rewrite"]]
    assert resp.headers["X-Total-Count"] == "1"
    resp = client.get("/memories/flagged", params={"project": "alpha", "verdict": "reject"})
    assert [r["id"] for r in resp.json()] == [ids["new_reject"], ids["old_reject"]]
    assert resp.headers["X-Total-Count"] == "2"


def test_flagged_route_limit_keeps_the_total(client):
    ids = asyncio.run(_seeded())
    resp = client.get("/memories/flagged", params={"limit": 1})
    assert [r["id"] for r in resp.json()] == [ids["new_reject"]]
    assert resp.headers["X-Total-Count"] == "3"
    assert len(client.get("/memories/flagged", params={"limit": 0}).json()) == 3


@pytest.mark.parametrize("verdict", ["approve", "maybe", "REJECT", ""])
def test_flagged_route_unknown_verdict_422(client, verdict):
    assert client.get("/memories/flagged", params={"verdict": verdict}).status_code == 422


def test_flagged_route_negative_limit_422(client):
    assert client.get("/memories/flagged", params={"limit": -1}).status_code == 422


def test_flagged_route_needs_the_token(client):
    assert client.get("/memories/flagged", headers={"Authorization": ""}).status_code == 401


def test_api_client_flagged(live_server):
    from agent_memory.client import ApiClient

    url, token = live_server
    ids = asyncio.run(_seeded())
    api = ApiClient(url, token)
    assert [r["id"] for r in api.flagged()] == [ids["new_reject"], ids["rewrite"], ids["old_reject"]]
    assert [r["id"] for r in api.flagged(verdict="rewrite")] == [ids["rewrite"]]
    assert [r["id"] for r in api.flagged(project="beta")] == [ids["rewrite"]]
    rows, total = api.flagged_with_total(limit=1)
    assert [r["id"] for r in rows] == [ids["new_reject"]] and total == 3
    assert api.flagged()[0]["review"] == REJECT.as_dict()


# ── POST /admin/review, with a FakeReviewer ──────────────────────────────────
class _App:
    """An in-process app over the test database, driven through httpx's
    ASGITransport. That transport waits for the whole request, background
    tasks included, so the scheduled reviews are stored by the time `post`
    returns."""

    def __init__(self, reviewer):
        self.engine = make_test_engine()
        self.app = create_app(sessionmaker=make_sessionmaker(self.engine), token=TOKEN,
                              embedder=FakeEmbedder(), reviewer=reviewer)

    async def __aenter__(self):
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                        base_url="http://testserver", headers=_auth())
        return self

    async def __aexit__(self, *exc):
        await self.client.aclose()
        await self.engine.dispose()


async def test_review_missing_reviews_only_the_memories_without_a_row():
    fake = FakeReviewer(REJECT)
    async with _App(reviewer=NullReviewer()) as off:
        # Written while the model was off: no rows.
        ids = []
        for text in ("one", "two", "three"):
            body = (await off.client.post("/memories", json={"content": text, "agent": "t"})).json()
            ids.append(body["id"])
    async with _App(reviewer=fake) as a:
        # Added with the model on: reviewed at once, so it already has a row.
        reviewed = (await a.client.post("/memories", json={"content": "four", "agent": "t"})).json()["id"]
        assert await _review_rows() == [(reviewed, "reject")]
        fake.calls.clear()

        resp = await a.client.post("/admin/review")
        assert resp.status_code == 200
        assert resp.json() == {"scheduled": 3}
        # Only the three without a row, newest first; the reviewed one was not asked again.
        assert [m["id"] for m, _ in fake.calls] == ids[::-1]
        assert await _review_rows() == [(mid, "reject") for mid in ids + [reviewed]]

        # Nothing left the second time.
        fake.calls.clear()
        assert (await a.client.post("/admin/review")).json() == {"scheduled": 0}
        assert fake.calls == []


async def test_review_missing_limit_takes_the_newest():
    fake = FakeReviewer(APPROVE)
    async with _App(reviewer=NullReviewer()) as off:
        ids = [(await off.client.post("/memories", json={"content": t, "agent": "t"})).json()["id"]
               for t in ("one", "two", "three")]
    async with _App(reviewer=fake) as a:
        assert (await a.client.post("/admin/review", params={"limit": 2})).json() == {"scheduled": 2}
        assert [m["id"] for m, _ in fake.calls] == ids[:0:-1]
        assert await _review_rows() == [(ids[1], "approve"), (ids[2], "approve")]
        # limit 0 means all: the one left over.
        assert (await a.client.post("/admin/review", params={"limit": 0})).json() == {"scheduled": 1}
        assert len(await _review_rows()) == 3


async def test_review_missing_default_limit_is_50():
    fake = FakeReviewer(APPROVE)
    async with _App(reviewer=NullReviewer()) as off:
        for i in range(52):
            await off.client.post("/memories", json={"content": f"memory {i}", "agent": "t"})
    async with _App(reviewer=fake) as a:
        assert (await a.client.post("/admin/review")).json() == {"scheduled": 50}
        assert len(fake.calls) == 50
        assert (await a.client.post("/admin/review")).json() == {"scheduled": 2}


async def test_review_missing_one_failure_does_not_stop_the_rest(caplog):
    import logging

    class FailsOnTwo(FakeReviewer):
        def review(self, memory, neighbours, tags=()):
            if memory["content"] == "two":
                raise RuntimeError("model blew up")
            return super().review(memory, neighbours, tags)

    async with _App(reviewer=NullReviewer()) as off:
        ids = [(await off.client.post("/memories", json={"content": t, "agent": "t"})).json()["id"]
               for t in ("one", "two", "three")]
    with caplog.at_level(logging.WARNING, logger="agent_memory.server.app"):
        async with _App(reviewer=FailsOnTwo(REJECT)) as a:
            assert (await a.client.post("/admin/review")).json() == {"scheduled": 3}
    assert await _review_rows() == [(ids[0], "reject"), (ids[2], "reject")]
    assert [r.getMessage() for r in caplog.records] == [
        f"review of memory #{ids[1]} failed: RuntimeError: model blew up"]


async def test_review_missing_no_verdict_leaves_no_row_and_still_counts():
    async with _App(reviewer=NullReviewer()) as off:
        await off.client.post("/memories", json={"content": "one", "agent": "t"})
    async with _App(reviewer=FakeReviewer(verdict=None)) as a:
        assert (await a.client.post("/admin/review")).json() == {"scheduled": 1}
    assert await _review_rows() == []


async def test_review_missing_503_with_null_reviewer():
    async with _App(reviewer=NullReviewer("review is off (AGENT_MEMORY_REVIEW=off)")) as a:
        await a.client.post("/memories", json={"content": "one", "agent": "t"})
        resp = await a.client.post("/admin/review")
        assert resp.status_code == 503
        assert "AGENT_MEMORY_REVIEW=off" in resp.json()["detail"]
    assert await _review_rows() == []


async def test_review_missing_needs_the_token():
    async with _App(reviewer=FakeReviewer()) as a:
        resp = await a.client.post("/admin/review", headers={"Authorization": ""})
        assert resp.status_code == 401


async def test_review_missing_negative_limit_422():
    async with _App(reviewer=FakeReviewer()) as a:
        assert (await a.client.post("/admin/review", params={"limit": -1})).status_code == 422


# ── the CLI, against the live server ─────────────────────────────────────────
@pytest.fixture
def cli(live_server):
    url, token = live_server
    return CliDriver(url, token)


def test_cli_review_lists_flagged_memories_like_query(cli):
    ids = asyncio.run(_seeded())
    proc = cli.raw("review")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    headers = [int(h) for h in re.findall(r"^━━━ #(\d+) ━+$", out, re.M)]
    assert headers == [ids["new_reject"], ids["rewrite"], ids["old_reject"]]
    # Each block is printed as `query` prints it: meta lines, the review line, the content.
    assert "👤 tester @ alpha" in out
    assert "review: reject, rule 2: A diary line: it says what was done, not why." in out
    assert "review: rewrite, rule 3: Say why." in out
    assert out.count("review:") == 3
    assert "Had lunch, then read the config module." in out
    assert "Not reviewed yet." not in out
    assert "Chose Postgres because" not in out
    assert "Found 3 flagged memories" in out
    # The driver's parser reads the blocks the same way it reads `query`.
    parsed = cli._parse_memories(out)
    assert [m.id for m in parsed] == headers
    assert parsed[0].content == "Had lunch, then read the config module."


def test_cli_review_empty(cli):
    proc = cli.raw("review")
    assert proc.returncode == 0
    assert "No flagged memories." in proc.stdout


def test_cli_review_filters(cli):
    ids = asyncio.run(_seeded())
    out = cli.raw("review", "--verdict", "rewrite").stdout
    assert [int(h) for h in re.findall(r"^━━━ #(\d+) ━+$", out, re.M)] == [ids["rewrite"]]
    assert "Found 1 flagged memories" in out
    out = cli.raw("review", "--project", "alpha").stdout
    assert [int(h) for h in re.findall(r"^━━━ #(\d+) ━+$", out, re.M)] == [ids["new_reject"], ids["old_reject"]]
    assert "No flagged memories." in cli.raw("review", "--project", "gamma").stdout


def test_cli_review_limit_and_all(cli):
    asyncio.run(_seeded())
    out = cli.raw("review", "--limit", "1").stdout
    assert out.count("━━━ #") == 1
    assert "Found 1 of 3 flagged memories (use --all or --limit to see more)" in out
    out = cli.raw("review", "--all", "--limit", "1").stdout   # --all wins
    assert "Found 3 flagged memories" in out


def test_cli_review_rejects_an_unknown_verdict(cli):
    proc = cli.raw("review", "--verdict", "approve")
    assert proc.returncode == 2
    assert "invalid choice: 'approve'" in proc.stderr


def test_cli_review_help_is_plain(cli):
    out = cli.raw("review", "--help").stdout
    for flag in ("--project", "--verdict", "--limit", "--all", "--missing"):
        assert flag in out
    assert "reject" in out and "rewrite" in out
    assert "no verdict yet" in out
    assert "review" in cli.raw("--help").stdout


def test_cli_review_missing_without_a_model_prints_the_reason(cli):
    # The live server runs without a review model.
    cli.raw("add", "unreviewed")
    proc = cli.raw("review", "--missing")
    assert proc.returncode == 1
    assert "✗ Cannot review:" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert "Scheduled" not in proc.stdout


def test_cli_review_missing_refuses_the_listing_flags(cli):
    proc = cli.raw("review", "--missing", "--verdict", "reject")
    assert proc.returncode == 2
    assert "--missing goes with --limit only" in proc.stdout


@pytest.fixture(scope="module")
def reviewing_server(_schema):
    """A second live server, this one with a `FakeReviewer`, so the CLI can
    run `--missing` for real. Yields `(url, token, reviewer)`."""
    import uvicorn

    fake = FakeReviewer(REJECT)
    engine = make_test_engine()
    app = create_app(sessionmaker=make_sessionmaker(engine), token=TOKEN,
                     embedder=FakeEmbedder(), reviewer=fake)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start in time")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}", TOKEN, fake
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        asyncio.run(engine.dispose())


def _wait_for_rows(n, timeout=10):
    """The reviews run after the response; wait until `n` rows are stored."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = asyncio.run(_review_rows())
        if len(rows) >= n:
            return rows
        time.sleep(0.05)
    raise AssertionError(f"expected {n} review rows, got {len(asyncio.run(_review_rows()))}")


def test_cli_review_missing_schedules_the_reviews(cli, reviewing_server):
    # Written through the server without a model: no rows.
    for text in ("one", "two", "three"):
        cli.raw("add", text)
    url, token, fake = reviewing_server
    fake.calls.clear()
    with_model = CliDriver(url, token)

    proc = with_model.raw("review", "--missing", "--limit", "2")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "✓ Scheduled 2 reviews"
    assert _wait_for_rows(2) == [(2, "reject"), (3, "reject")]
    assert [m["id"] for m, _ in fake.calls] == [3, 2]

    proc = with_model.raw("review", "--missing")
    assert proc.stdout.strip() == "✓ Scheduled 1 reviews"
    assert _wait_for_rows(3) == [(1, "reject"), (2, "reject"), (3, "reject")]
    assert with_model.raw("review", "--missing").stdout.strip() == "✓ Scheduled 0 reviews"

    # And now they show up as flagged, newest review first: #1 was reviewed
    # last, and #2 after #3 (the reviews run newest memory first).
    out = cli.raw("review").stdout
    assert [int(h) for h in re.findall(r"^━━━ #(\d+) ━+$", out, re.M)] == [1, 2, 3]


# ── the MCP tool ─────────────────────────────────────────────────────────────
@pytest.fixture
def mcp(live_server):
    url, token = live_server
    return McpDriver(url, token)


def test_mcp_flagged_has_the_query_shape(mcp):
    ids = asyncio.run(_seeded())
    data = mcp._call("memory_flagged")
    assert set(data) == {"memories"}
    rows = data["memories"]
    assert [r["id"] for r in rows] == [ids["new_reject"], ids["rewrite"], ids["old_reject"]]
    assert set(rows[0]) == set(mcp._call("memory_query")["memories"][0])
    assert rows[0]["review"] == REJECT.as_dict()
    assert rows[1]["review"] == REWRITE.as_dict()


def test_mcp_flagged_filters_and_limit(mcp):
    ids = asyncio.run(_seeded())
    assert [r["id"] for r in mcp._call("memory_flagged", verdict="rewrite")["memories"]] == [ids["rewrite"]]
    assert [r["id"] for r in mcp._call("memory_flagged", project="beta")["memories"]] == [ids["rewrite"]]
    assert [r["id"] for r in mcp._call("memory_flagged", limit=1)["memories"]] == [ids["new_reject"]]
    assert len(mcp._call("memory_flagged", limit=0)["memories"]) == 3
    assert mcp._call("memory_flagged", project="gamma") == {"memories": []}


def test_mcp_flagged_unknown_verdict_returns_error(mcp):
    data = mcp._call("memory_flagged", verdict="approve")
    assert set(data) == {"error"}
    assert "approve" in data["error"]


def test_mcp_flagged_tool_description_and_default_limit(mcp):
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    async def run():
        async with connect(mcp._mcp) as session:
            tools = (await session.list_tools()).tools
            return next(t for t in tools if t.name == "memory_flagged")

    tool = asyncio.run(run())
    assert tool.inputSchema["properties"]["limit"]["default"] == 20
    for word in ("reject", "rewrite", "newest review first", "memory_query"):
        assert word in tool.description
