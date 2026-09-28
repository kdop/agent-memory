"""Tests for the review status on every memory (issue #57).

Every memory carries `review_status`: `unverified` until the model has
checked it, then `verified` (approve) or `flagged` (reject or rewrite). The
reference set for any check (the neighbours handed to the model, the
candidates for the duplicate check) is the verified memories only. The
catch-up (`POST /admin/review`, `memory review --catch-up`) reviews the
unverified memories oldest first, one after another, so each verdict is
stored before the next memory is compared.

Layers: the constants; the status on write in flag and refuse mode and
after each verdict; the reference-set rule in the repository and through
the app; the catch-up order and its one background task; the `status`
filter on the CLI, the API and the MCP tools; the header line the CLI
prints and the drivers parse; and the real model once, under the `review`
marker. The migration is in tests/test_migration.py.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import get_args

import httpx
import pytest
from sqlalchemy import select, update

from agent_memory.client import REVIEW_STATUSES, ApiClient, ApiRefused
from agent_memory.server import repository as repo
from agent_memory.server import review as review_mod
from agent_memory.server.app import ReviewStatus, create_app
from agent_memory.server.db import make_sessionmaker
from agent_memory.server.embedding import cosine
from agent_memory.server.models import Memory
from agent_memory.server.review import (
    STATUSES,
    NullReviewer,
    OllamaReviewer,
    Verdict,
    status_for,
)
from agent_memory.server.schemas import MemoryOut
from conftest import TOKEN, FakeEmbedder, live_app, make_test_engine, verify
from drivers import CliDriver, McpDriver
from test_review import APPROVE, REJECT, FakeReviewer, _App, _plant_review, _real_server

REWRITE = Verdict("rewrite", 3, "Say why.", "The same, but with the reason.", None)


class Scripted:
    """A reviewer that answers per memory: `verdicts` maps a content to a
    Verdict, to None (no answer) or to an exception to raise; `default`
    covers the rest. Records every call as `(memory, neighbours)`."""

    model_name = "scripted"

    def __init__(self, verdicts=None, default=APPROVE):
        self.verdicts = dict(verdicts or {})
        self.default = default
        self.calls = []

    def review(self, memory, neighbours, tags=()):
        self.calls.append((memory, neighbours))
        answer = self.verdicts.get(memory["content"], self.default)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


async def _post(a, content, project="alpha", type=None, tags=(), **params):
    return await a.client.post("/memories", params=params, json={
        "content": content, "project": project, "type": type, "agent": "tester",
        "tags": [{"name": t} for t in tags]})


async def _status(a, mid):
    return (await a.get(mid))["review_status"]


async def _statuses_in_db():
    """`[(id, review_status), ...]` straight from the table, by id."""
    engine = make_test_engine()
    try:
        async with engine.connect() as conn:
            stmt = select(Memory.id, Memory.review_status).order_by(Memory.id)
            return [tuple(r) for r in (await conn.execute(stmt)).all()]
    finally:
        await engine.dispose()


async def _set_age(mid, minutes):
    """Date memory `mid` `minutes` ago, so the catch-up order can be checked."""
    engine = make_test_engine()
    try:
        async with make_sessionmaker(engine)() as session, session.begin():
            when = datetime.now(timezone.utc) - timedelta(minutes=minutes)
            await session.execute(update(Memory).where(Memory.id == mid).values(timestamp=when))
    finally:
        await engine.dispose()


def _flag(mid):
    """Plant a reject verdict from a sync test; the status becomes flagged."""
    asyncio.run(_plant_review(mid, REJECT))


async def _unverified(*contents, project="alpha"):
    """Add memories through an app with no review model: stored unverified.
    Returns their ids."""
    async with _App(reviewer=NullReviewer()) as off:
        return [(await off.add(text, project=project))["id"] for text in contents]


# ── the constants ────────────────────────────────────────────────────────────
def test_the_three_statuses_and_the_status_of_each_verdict():
    assert STATUSES == ("unverified", "verified", "flagged")
    assert review_mod.UNVERIFIED == "unverified"
    assert status_for("approve") == "verified"
    assert status_for("reject") == "flagged"
    assert status_for("rewrite") == "flagged"
    with pytest.raises(ValueError, match="maybe"):
        status_for("maybe")


def test_the_client_and_the_route_use_the_same_list():
    # The client half cannot import the server; its copy must not drift, and
    # neither may the Literal the route validates with.
    assert REVIEW_STATUSES == STATUSES
    assert get_args(ReviewStatus) == STATUSES


def test_memory_out_defaults_to_unverified():
    assert MemoryOut(id=1).review_status == "unverified"
    assert "review_status" in MemoryOut.model_fields


# ── the status on write ──────────────────────────────────────────────────────
async def test_flag_mode_stores_unverified_and_the_verdict_sets_the_status():
    fake = FakeReviewer(REJECT)
    async with _App(reviewer=fake) as a:
        mid = (await a.add("Spent the afternoon tidying."))["id"]
        # The model read the memory as stored: unverified at that point.
        memory, _ = fake.calls[-1]
        assert memory["id"] == mid and memory["review_status"] == "unverified"
        # Once the verdict is stored, the status follows it.
        assert await _status(a, mid) == "flagged"

        fake.verdict = APPROVE
        second = (await a.add("Chose Postgres, because several agents write at once."))["id"]
        assert fake.calls[-1][0]["review_status"] == "unverified"
        assert await _status(a, second) == "verified"
    assert await _statuses_in_db() == [(mid, "flagged"), (second, "verified")]


async def test_without_a_review_model_a_memory_stays_unverified():
    async with _App(reviewer=NullReviewer()) as a:
        mid = (await a.add("never checked"))["id"]
        assert await _status(a, mid) == "unverified"
    async with _App() as a:
        mid = (await a.add("no reviewer at all"))["id"]
        assert await _status(a, mid) == "unverified"


async def test_no_verdict_or_a_failing_model_leaves_unverified(caplog):
    class Broken(FakeReviewer):
        def review(self, memory, neighbours, tags=()):
            raise RuntimeError("model blew up")

    async with _App(reviewer=FakeReviewer(verdict=None)) as a:
        mid = (await a.add("no answer"))["id"]
        assert await _status(a, mid) == "unverified"
    with caplog.at_level(logging.WARNING, logger="agent_memory.server.app"):
        async with _App(reviewer=Broken()) as a:
            mid = (await a.add("model down"))["id"]
            assert await _status(a, mid) == "unverified"
    assert await _statuses_in_db() == [(1, "unverified"), (2, "unverified")]


async def test_refuse_mode_approve_is_verified_at_once():
    fake = FakeReviewer(APPROVE)
    async with _App(reviewer=fake, review_mode="refuse") as a:
        resp = await _post(a, "Chose Postgres, because several agents write at once.",
                           type="decision")
        assert resp.status_code == 201
        mid = resp.json()["id"]
        assert await _status(a, mid) == "verified"
        row = await a.get(mid)
        assert row["review"] == APPROVE.as_dict()
    # One model call, on the entry before it had an id; the status was set
    # with the write, not by a later task.
    assert len(fake.calls) == 1 and fake.calls[0][0]["id"] is None
    assert await _statuses_in_db() == [(mid, "verified")]


async def test_refuse_mode_refusal_stores_nothing_so_there_is_no_status():
    async with _App(reviewer=FakeReviewer(REJECT), review_mode="refuse") as a:
        assert (await _post(a, "Spent the afternoon tidying.")).status_code == 422
    assert await _statuses_in_db() == []


async def test_refuse_mode_force_is_unverified_until_the_background_verdict_lands():
    fake = FakeReviewer(REJECT)
    async with _App(reviewer=fake, review_mode="refuse") as a:
        resp = await _post(a, "Spent the afternoon tidying.", force="true")
        assert resp.status_code == 201
        mid = resp.json()["id"]
        # The review ran after the write and read the stored memory: it was
        # unverified then. ASGITransport waits for the task, so by now the
        # verdict has landed and the status follows it.
        assert len(fake.calls) == 1
        memory, _ = fake.calls[0]
        assert memory["id"] == mid and memory["review_status"] == "unverified"
        assert await _status(a, mid) == "flagged"


# ── the status after each verdict ────────────────────────────────────────────
@pytest.mark.parametrize("verdict, status", [
    (APPROVE, "verified"), (REJECT, "flagged"), (REWRITE, "flagged"),
])
async def test_each_verdict_gives_its_status(verdict, status):
    async with _App(reviewer=FakeReviewer(verdict)) as a:
        mid = (await a.add("Chose Postgres for the store."))["id"]
        row = await a.get(mid)
        assert row["review_status"] == status
        assert row["review"]["verdict"] == verdict.verdict


async def test_a_re_review_that_changes_the_verdict_changes_the_status():
    fake = FakeReviewer(REJECT)
    async with _App(reviewer=fake) as a:
        mid = (await a.add("Chose Postgres for the store."))["id"]
        assert await _status(a, mid) == "flagged"

        fake.verdict = APPROVE
        assert (await a.client.post(f"/admin/review/{mid}")).status_code == 200
        assert await _status(a, mid) == "verified"

        fake.verdict = REWRITE
        assert (await a.client.post(f"/admin/review/{mid}")).status_code == 200
        assert await _status(a, mid) == "flagged"

        # A re-review that gets no verdict keeps the row and the status.
        fake.verdict = None
        assert (await a.client.post(f"/admin/review/{mid}")).status_code == 502
        row = await a.get(mid)
        assert row["review_status"] == "flagged" and row["review"]["verdict"] == "rewrite"


async def test_set_review_sets_the_status_and_needs_a_memory():
    engine = make_test_engine()
    try:
        async with make_sessionmaker(engine)() as s, s.begin():
            mid = await repo.add(s, "one", "t", "alpha", [], None)
            assert (await repo.get(s, mid))["review_status"] == "unverified"
            await repo.set_review(s, mid, REJECT, "m")
            assert (await repo.get(s, mid))["review_status"] == "flagged"
            await repo.set_review(s, mid, APPROVE, "m")
            assert (await repo.get(s, mid))["review_status"] == "verified"
            with pytest.raises(LookupError, match="#999"):
                await repo.set_review(s, 999, APPROVE, "m")
    finally:
        await engine.dispose()


# ── the reference set: verified memories only ────────────────────────────────
async def test_an_unverified_duplicate_is_not_refused_and_a_verified_one_is():
    async with _App(reviewer=NullReviewer()) as a:
        first = (await a.add("dup me"))["id"]
        assert await _status(a, first) == "unverified"
        # The same text again: the first is not checked yet, so it is not
        # reference, and nothing is refused.
        second = (await _post(a, "dup me")).status_code
        assert second == 201
        # Once the first is verified it is reference, and the text is refused.
        await _plant_review(first, APPROVE)
        resp = await _post(a, "dup me")
        assert resp.status_code == 409
        assert resp.json()["detail"]["existing_id"] == first


async def test_a_flagged_memory_is_not_reference_either():
    async with _App(reviewer=FakeReviewer(REJECT)) as a:
        first = (await a.add("dup me"))["id"]
        assert await _status(a, first) == "flagged"
        assert (await _post(a, "dup me")).status_code == 201


async def test_find_duplicate_sees_verified_rows_only():
    emb = FakeEmbedder()
    engine = make_test_engine()
    try:
        async with make_sessionmaker(engine)() as s, s.begin():
            mid = await repo.add(s, "dup me", "t", "alpha", [], None, embedder=emb)
            assert await repo.find_duplicate(s, emb, "dup me", "alpha") is None
            await repo.set_review(s, mid, REJECT, "m")
            assert await repo.find_duplicate(s, emb, "dup me", "alpha") is None
            await repo.set_review(s, mid, APPROVE, "m")
            found = await repo.find_duplicate(s, emb, "dup me", "alpha")
            assert found is not None and found[0] == mid
    finally:
        await engine.dispose()


def _by_cosine(text, others):
    """`others` sorted the way `_nearest` sorts them for `text`."""
    emb = FakeEmbedder()
    vec = emb.embed([text])[0]
    return sorted(others, key=lambda t: -cosine(vec, emb.embed([t])[0]))


async def test_neighbours_are_verified_memories_only_in_the_repository():
    emb = FakeEmbedder()
    texts = [f"alpha memory number {i} about topic {i}" for i in range(6)]
    engine = make_test_engine()
    try:
        async with make_sessionmaker(engine)() as s, s.begin():
            ids = {t: await repo.add(s, t, "t", "alpha", [], None, embedder=emb) for t in texts}
            for t in texts[:3]:
                await repo.set_review(s, ids[t], APPROVE, "m")
            await repo.set_review(s, ids[texts[3]], REJECT, "m")
            # texts[4] and texts[5] stay unverified.
            new = "alpha memory number 3 about topic 3, said again"
            new_id = await repo.add(s, new, "t", "alpha", [], None, embedder=emb)

            _, neighbours = await repo.review_input(s, new_id)
            assert [n["id"] for n in neighbours] == [ids[t] for t in _by_cosine(new, texts[:3])]
            assert all(n["review_status"] == "verified" for n in neighbours)

            before = await repo.neighbours_for(s, emb, new, "alpha")
            assert [n["id"] for n in before] == [n["id"] for n in neighbours]
    finally:
        await engine.dispose()


async def test_neighbours_are_verified_memories_only_through_the_app():
    texts = [f"alpha memory number {i} about topic {i}" for i in range(6)]
    ids = dict(zip(texts, await _unverified(*texts)))
    for t in texts[:3]:
        await _plant_review(ids[t], APPROVE)
    await _plant_review(ids[texts[3]], REJECT)
    new = "alpha memory number 3 about topic 3, said again"
    expected = [ids[t] for t in _by_cosine(new, texts[:3])]

    # Flag mode: the stored memory's neighbours.
    fake = FakeReviewer(APPROVE)
    async with _App(reviewer=fake) as a:
        ids[new] = (await a.add(new))["id"]
    _, neighbours = fake.calls[-1]
    assert [n["id"] for n in neighbours] == expected
    assert all(n["review_status"] == "verified" for n in neighbours)

    # Refuse mode: the neighbours of the entry before it is stored. The
    # approved one from just above is verified now, so it is reference too.
    once_more = new + ", once more"
    fake = FakeReviewer(REJECT)
    async with _App(reviewer=fake, review_mode="refuse") as a:
        assert (await _post(a, once_more)).status_code == 422
    _, neighbours = fake.calls[-1]
    assert [n["id"] for n in neighbours] == [ids[t] for t in _by_cosine(once_more, texts[:3] + [new])]
    assert all(n["review_status"] == "verified" for n in neighbours)


# ── the catch-up ─────────────────────────────────────────────────────────────
async def test_catch_up_runs_oldest_first_by_timestamp_then_id():
    ids = await _unverified("a", "b", "c", "d", "e")
    a, b, c, d, e = ids
    # Mixed ages: c is the oldest, then a, then e; b and d share a time.
    for mid, minutes in ((c, 50), (a, 40), (e, 30), (b, 20), (d, 20)):
        await _set_age(mid, minutes)
    scripted = Scripted()
    async with _App(reviewer=scripted) as app:
        resp = await app.client.post("/admin/review")
        assert resp.status_code == 200 and resp.json() == {"scheduled": 5}
        assert [m["id"] for m, _ in scripted.calls] == [c, a, e, b, d]
        assert await _statuses_in_db() == [(mid, "verified") for mid in ids]

        # A second catch-up finds nothing.
        scripted.calls.clear()
        assert (await app.client.post("/admin/review")).json() == {"scheduled": 0}
        assert scripted.calls == []


async def test_catch_up_verifies_each_memory_before_the_next_is_compared():
    # Two memories with the same text: the second was not refused, since the
    # first was unverified when it was written. A third, older, with other text.
    other, first, second = await _unverified("something else", "the same text", "the same text")
    await _set_age(other, 30)
    await _set_age(first, 20)
    await _set_age(second, 10)
    scripted = Scripted()
    async with _App(reviewer=scripted) as app:
        assert (await app.client.post("/admin/review")).json() == {"scheduled": 3}
    assert [m["id"] for m, _ in scripted.calls] == [other, first, second]

    # The oldest saw no reference: nothing was verified yet.
    assert scripted.calls[0][1] == []
    # The first "same text" saw the other one, verified by then.
    assert [(n["id"], n["review_status"]) for n in scripted.calls[1][1]] == [(other, "verified")]
    # The second "same text" saw the first one as its closest neighbour
    # (cosine 1.0), verified by then, and the other one after it.
    neighbours = scripted.calls[2][1]
    assert [(n["id"], n["review_status"]) for n in neighbours] == [
        (first, "verified"), (other, "verified")]
    assert neighbours[0]["score"] == pytest.approx(1.0)
    assert await _statuses_in_db() == [(other, "verified"), (first, "verified"),
                                       (second, "verified")]


async def test_catch_up_a_failing_review_leaves_that_row_unverified_and_goes_on(caplog):
    one, two, three = await _unverified("one", "two", "three")
    scripted = Scripted({"two": RuntimeError("model blew up"), "three": REJECT})
    with caplog.at_level(logging.WARNING, logger="agent_memory.server.app"):
        async with _App(reviewer=scripted) as app:
            assert (await app.client.post("/admin/review")).json() == {"scheduled": 3}
    assert [m["id"] for m, _ in scripted.calls] == [one, two, three]
    assert await _statuses_in_db() == [(one, "verified"), (two, "unverified"), (three, "flagged")]
    assert [r.getMessage() for r in caplog.records] == [
        f"review of memory #{two} failed: RuntimeError: model blew up"]

    # The one that failed is still unverified, so the next catch-up picks it
    # up again, and only it.
    scripted.verdicts.clear()
    scripted.calls.clear()
    async with _App(reviewer=scripted) as app:
        assert (await app.client.post("/admin/review")).json() == {"scheduled": 1}
    assert [m["id"] for m, _ in scripted.calls] == [two]
    assert await _statuses_in_db() == [(one, "verified"), (two, "verified"), (three, "flagged")]


async def test_catch_up_no_answer_leaves_unverified():
    (mid,) = await _unverified("no answer")
    async with _App(reviewer=FakeReviewer(verdict=None)) as app:
        assert (await app.client.post("/admin/review")).json() == {"scheduled": 1}
        assert await _status(app, mid) == "unverified"


async def test_catch_up_limit_takes_the_oldest():
    ids = await _unverified("a", "b", "c", "d")
    a, b, c, d = ids
    for mid, minutes in ((d, 40), (b, 30), (a, 20), (c, 10)):
        await _set_age(mid, minutes)
    scripted = Scripted()
    async with _App(reviewer=scripted) as app:
        assert (await app.client.post("/admin/review", params={"limit": 2})).json() == {"scheduled": 2}
        assert [m["id"] for m, _ in scripted.calls] == [d, b]
        assert await _statuses_in_db() == [(a, "unverified"), (b, "verified"),
                                           (c, "unverified"), (d, "verified")]
        # limit 0 means all: the two left, oldest first.
        scripted.calls.clear()
        assert (await app.client.post("/admin/review", params={"limit": 0})).json() == {"scheduled": 2}
        assert [m["id"] for m, _ in scripted.calls] == [a, c]


async def test_catch_up_is_one_background_task_with_the_ids_in_order(monkeypatch):
    from agent_memory.server import app as app_mod

    seen = []
    real = app_mod._review_in_order

    async def spy(app, ids):
        seen.append(list(ids))
        await real(app, ids)

    monkeypatch.setattr(app_mod, "_review_in_order", spy)
    a, b, c = await _unverified("a", "b", "c")
    await _set_age(b, 10)
    async with _App(reviewer=Scripted()) as app:
        assert (await app.client.post("/admin/review")).json() == {"scheduled": 3}
        # Nothing to schedule: no task at all.
        assert (await app.client.post("/admin/review")).json() == {"scheduled": 0}
    assert seen == [[b, a, c]]


async def test_catch_up_503_with_a_null_reviewer():
    await _unverified("one")
    async with _App(reviewer=NullReviewer("review is off (AGENT_MEMORY_REVIEW=off)")) as app:
        resp = await app.client.post("/admin/review")
        assert resp.status_code == 503
        assert "AGENT_MEMORY_REVIEW=off" in resp.json()["detail"]
    assert await _statuses_in_db() == [(1, "unverified")]


async def test_catch_up_skips_verified_and_flagged_memories():
    one, two, three = await _unverified("one", "two", "three")
    await _plant_review(one, APPROVE)
    await _plant_review(two, REJECT)
    scripted = Scripted()
    async with _App(reviewer=scripted) as app:
        assert (await app.client.post("/admin/review")).json() == {"scheduled": 1}
    assert [m["id"] for m, _ in scripted.calls] == [three]


# ── the catch-up through the client and the CLI ──────────────────────────────
@pytest.fixture(scope="module")
def scripted_server(_schema):
    """A live server with a `Scripted` reviewer in flag mode, for the CLI and
    the client. Yields `(url, reviewer)`."""
    scripted = Scripted()
    engine = make_test_engine()
    app = create_app(sessionmaker=make_sessionmaker(engine), token=TOKEN,
                     embedder=FakeEmbedder(), reviewer=scripted, review_poll=0)
    with live_app(app) as url:
        yield url, scripted
    asyncio.run(engine.dispose())


def _wait_until(check, timeout=10):
    """The reviews run after the response; wait until `check()` is true."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError("the background reviews did not finish in time")


def _verified_count():
    return sum(status == "verified" for _, status in asyncio.run(_statuses_in_db()))


def test_client_review_catch_up_and_its_old_name(live_server, scripted_server):
    url, token = live_server
    ApiClient(url, token).add("one", "tester", "p", [], None)
    ApiClient(url, token).add("two", "tester", "p", [], None)
    api = ApiClient(scripted_server[0], token)
    assert ApiClient.review_missing is ApiClient.review_catch_up
    assert api.review_catch_up(limit=1) == {"scheduled": 1, "running": False}
    _wait_until(lambda: _verified_count() == 1)
    assert api.review_missing()["scheduled"] == 1
    _wait_until(lambda: _verified_count() == 2)
    assert api.review_catch_up() == {"scheduled": 0, "running": False}
    # The live server without a model answers 503, which the client raises.
    with pytest.raises(ApiRefused) as caught:
        ApiClient(url, token).review_catch_up()
    assert caught.value.status == 503


def test_cli_catch_up_and_its_old_name(live_server, scripted_server):
    url, token = live_server
    plain = CliDriver(url, token)
    for text in ("one", "two", "three"):
        plain.raw("add", text)
    cli = CliDriver(scripted_server[0], token)

    proc = cli.raw("review", "--catch-up", "--limit", "1")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "✓ Scheduled 1 reviews"
    _wait_until(lambda: _verified_count() == 1)
    proc = cli.raw("review", "--missing")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "✓ Scheduled 2 reviews"
    _wait_until(lambda: _verified_count() == 3)
    assert cli.raw("review", "--catch-up").stdout.strip() == "✓ Scheduled 0 reviews"
    # The old name is an alias, not a listed flag.
    assert "--catch-up" in cli.raw("review", "--help").stdout

    proc = cli.raw("review", "--catch-up", "--status", "verified")
    assert proc.returncode == 2
    assert "--catch-up goes with --limit only" in proc.stdout


# ── the status filter on the three surfaces ──────────────────────────────────
def _three(driver):
    """Three memories: #1 verified, #2 flagged, #3 unverified."""
    assert [driver.add(t, project="p") for t in ("one", "two", "three")] == [1, 2, 3]
    verify(1)
    _flag(2)


def test_query_status_filter(driver):
    _three(driver)
    assert [m.id for m in driver.query(status="verified")] == [1]
    assert [m.id for m in driver.query(status="flagged")] == [2]
    assert [m.id for m in driver.query(status="unverified")] == [3]
    rows = driver.query()
    assert [(m.id, m.status) for m in rows] == [(3, "unverified"), (2, "flagged"), (1, "verified")]
    assert [m.id for m in driver.query(status="verified", project="nope")] == []


def test_every_driver_reports_the_status(driver):
    mid = driver.add("one", project="p")
    assert driver.get(mid).status == "unverified"
    verify(mid)
    assert driver.get(mid).status == "verified"
    assert driver.query()[0].status == "verified"
    assert driver.search("one")[0].status == "verified"


def test_api_unknown_status_422(live_server):
    url, _ = live_server
    with httpx.Client(base_url=url, headers=_auth()) as c:
        for value in ("maybe", "VERIFIED", "", "approve"):
            assert c.get("/memories", params={"status": value}).status_code == 422
            assert c.get("/memories/flagged", params={"status": value}).status_code == 422
        assert c.get("/memories", params={"status": "verified"}).status_code == 200


def test_cli_unknown_status_exits_2(live_server):
    url, token = live_server
    cli = CliDriver(url, token)
    for command in ("query", "review"):
        proc = cli.raw(command, "--status", "maybe")
        assert proc.returncode == 2
        assert "invalid choice: 'maybe'" in proc.stderr
    assert "--status" in cli.raw("query", "--help").stdout


def test_mcp_unknown_status_returns_error(live_server):
    url, token = live_server
    mcp = McpDriver(url, token)
    for tool in ("memory_query", "memory_flagged"):
        data = mcp._call(tool, status="maybe")
        assert set(data) == {"error"} and "maybe" in data["error"]


def test_api_query_total_counts_the_filtered_rows(live_server):
    url, token = live_server
    _three(CliDriver(url, token))
    with httpx.Client(base_url=url, headers=_auth()) as c:
        resp = c.get("/memories", params={"status": "unverified", "limit": 0})
        assert [m["id"] for m in resp.json()] == [3]
        assert resp.headers["X-Total-Count"] == "1"
        assert c.get("/memories").headers["X-Total-Count"] == "3"


def test_flagged_route_status_lists_by_status(live_server):
    url, token = live_server
    cli = CliDriver(url, token)
    for text in ("one", "two", "three", "four", "five"):
        cli.raw("add", text, "--project", "p")
    verify(4)
    _flag(5)
    with httpx.Client(base_url=url, headers=_auth()) as c:
        # Without a status: the flagged ones, as before.
        assert [m["id"] for m in c.get("/memories/flagged").json()] == [5]
        assert [m["id"] for m in c.get("/memories/flagged", params={"status": "flagged"}).json()] == [5]
        # With one: the memories with that status, newest first.
        resp = c.get("/memories/flagged", params={"status": "unverified"})
        assert [m["id"] for m in resp.json()] == [3, 2, 1]
        assert resp.headers["X-Total-Count"] == "3"
        assert all(m["review_status"] == "unverified" and m["review"] is None for m in resp.json())
        resp = c.get("/memories/flagged", params={"status": "verified"})
        assert [m["id"] for m in resp.json()] == [4]
        assert resp.json()[0]["review"]["verdict"] == "approve"
        # The limit keeps the total; the verdict narrows a status too.
        resp = c.get("/memories/flagged", params={"status": "unverified", "limit": 1})
        assert [m["id"] for m in resp.json()] == [3] and resp.headers["X-Total-Count"] == "3"
        assert c.get("/memories/flagged", params={"status": "flagged", "verdict": "reject"}).json()[0]["id"] == 5
        assert c.get("/memories/flagged", params={"status": "flagged", "verdict": "rewrite"}).json() == []
        assert c.get("/memories/flagged", params={"status": "verified", "verdict": "reject"}).json() == []
        assert c.get("/memories/flagged", params={"status": "unverified", "project": "q"}).json() == []


def test_cli_review_status_lists_by_status(live_server):
    url, token = live_server
    cli = CliDriver(url, token)
    _three(cli)
    out = cli.raw("review", "--status", "unverified").stdout
    assert re.findall(r"^━━━ #(\d+) status: unverified ━+$", out, re.M) == ["3"]
    assert "Found 1 unverified memories" in out
    out = cli.raw("review", "--status", "verified").stdout
    assert re.findall(r"^━━━ #(\d+) status: verified ━+$", out, re.M) == ["1"]
    assert "review: approve" in out
    assert cli.raw("review", "--status", "verified", "--project", "q").stdout.strip() == "No verified memories."
    # Without --status the listing is the flagged ones, as before.
    out = cli.raw("review").stdout
    assert re.findall(r"^━━━ #(\d+) status: flagged ━+$", out, re.M) == ["2"]
    assert "Found 1 flagged memories" in out


def test_mcp_flagged_status_lists_by_status(live_server):
    url, token = live_server
    mcp = McpDriver(url, token)
    _three(mcp)
    assert [m["id"] for m in mcp._call("memory_flagged", status="unverified")["memories"]] == [3]
    assert [m["id"] for m in mcp._call("memory_flagged", status="verified")["memories"]] == [1]
    assert [m["id"] for m in mcp._call("memory_flagged")["memories"]] == [2]
    rows = mcp._call("memory_query", status="flagged")["memories"]
    assert [(m["id"], m["review_status"]) for m in rows] == [(2, "flagged")]


def test_api_client_passes_the_status_through(live_server):
    url, token = live_server
    _three(CliDriver(url, token))
    api = ApiClient(url, token)
    assert [m["id"] for m in api.query(status="unverified")] == [3]
    rows, total = api.query_with_total(status="flagged")
    assert [m["id"] for m in rows] == [2] and total == 1
    assert [m["id"] for m in api.flagged(status="verified")] == [1]
    rows, total = api.flagged_with_total(status="unverified", limit=1)
    assert [m["id"] for m in rows] == [3] and total == 1
    assert api.get(1)["review_status"] == "verified"


# ── the CLI header line ──────────────────────────────────────────────────────
def test_cli_prints_the_status_right_after_the_id_on_every_listing(live_server):
    url, token = live_server
    cli = CliDriver(url, token)
    _three(cli)

    shown = cli.raw("show", "1").stdout
    assert re.search(r"^━━━ #1 status: verified ━+$", shown, re.M)
    assert re.search(r"^━━━ #2 status: flagged ━+$", cli.raw("show", "2").stdout, re.M)
    assert re.search(r"^━━━ #3 status: unverified ━+$", cli.raw("show", "3").stdout, re.M)
    # The header is the first line of the block; the meta lines follow.
    lines = [l for l in shown.splitlines() if l]
    assert lines[0].startswith("━━━ #1 status: verified ") and lines[1].startswith("🕒")

    queried = cli.raw("query", "--project", "p").stdout
    assert re.findall(r"^━━━ #(\d+) status: (\w+) ━+$", queried, re.M) == [
        ("3", "unverified"), ("2", "flagged"), ("1", "verified")]

    # A search result keeps its score, after the status.
    found = cli.raw("search", "one", "--project", "p").stdout
    assert re.search(r"^━━━ #1 status: verified score \d\.\d\d ━+$", found, re.M)

    flagged = cli.raw("review").stdout
    assert re.findall(r"^━━━ #(\d+) status: (\w+) ━+$", flagged, re.M) == [("2", "flagged")]

    # The driver reads the status out of the header, and the content is unchanged.
    assert cli.get(1).status == "verified" and cli.get(1).content == "one"
    assert [m.status for m in cli.query(project="p")] == ["unverified", "flagged", "verified"]
    hit = cli.search("one", project="p")[0]
    assert hit.status == "verified" and hit.score is not None


# ── the API shape ────────────────────────────────────────────────────────────
def test_every_read_carries_review_status(live_server):
    url, token = live_server
    _three(CliDriver(url, token))
    with httpx.Client(base_url=url, headers=_auth()) as c:
        one = c.get("/memories/1").json()
        assert one["review_status"] == "verified"
        listed = c.get("/memories").json()
        assert {m["id"]: m["review_status"] for m in listed} == {
            1: "verified", 2: "flagged", 3: "unverified"}
        found = c.get("/memories/search", params={"q": "two"}).json()
        assert found[0]["review_status"] == "flagged"
        flagged = c.get("/memories/flagged").json()
        assert flagged[0]["review_status"] == "flagged"
        # The same shape everywhere.
        assert set(one) == set(listed[0]) == set(found[0]) == set(flagged[0])
        assert "review_status" in set(one)


# ── the real model, once ─────────────────────────────────────────────────────
@pytest.mark.review
async def test_real_model_catch_up_checks_every_unverified_memory():
    url = _real_server()
    if url is None:
        pytest.skip("AGENT_MEMORY_REVIEW_URL is unset or the server did not answer in 2 s")
    model = os.environ.get("AGENT_MEMORY_REVIEW_MODEL", "").strip() or review_mod.DEFAULT_MODEL
    # A long timeout: the first call may have to load the model into memory.
    reviewer = OllamaReviewer(url, model, timeout=180)
    diary = ("Spent the afternoon reading through the config module and tidied up a "
             "few of its helper functions.")
    decision = ("Chose Postgres over SQLite for the memory store because several agents "
                "write at the same time and SQLite locks the whole file on every write; "
                "SQLite was rejected for that reason.")
    lesson = ("The connection pool ran dry because a session was held open across a slow "
              "model call; read what the model needs, close the session, then call it.")

    # Written while the model was off: all three unverified.
    async with _App(reviewer=NullReviewer()) as off:
        ids = {}
        for text, kind in ((diary, "note"), (decision, "decision"), (lesson, "lesson")):
            ids[text] = (await off.add(text, project="agent-memory", type=kind))["id"]
    assert await _statuses_in_db() == [(mid, "unverified") for mid in ids.values()]

    async with _App(reviewer=reviewer) as a:
        t0 = time.perf_counter()
        resp = await a.client.post("/admin/review")
        seconds = time.perf_counter() - t0
        assert resp.status_code == 200 and resp.json() == {"scheduled": 3}
        rows = {text: await a.get(mid) for text, mid in ids.items()}
    print(f"\n[review] {model} at {url}, catch-up of 3 in {seconds:.1f}s: "
          + "; ".join(f"#{r['id']} {r['review_status']} ({r['review']['verdict'] if r['review'] else 'no verdict'})"
                      for r in rows.values()))

    # Every one of them got a verdict, and the diary line was flagged.
    for row in rows.values():
        assert row["review_status"] in ("verified", "flagged"), row
        assert row["review"] is not None
    assert rows[diary]["review_status"] == "flagged"
    assert rows[diary]["review"]["verdict"] == "reject"
