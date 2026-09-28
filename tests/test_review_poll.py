"""Tests for the catch-up poll (issue #58).

The review model runs on a machine that is off at times. While it is off,
new memories are stored unverified. The server checks every
AGENT_MEMORY_REVIEW_POLL seconds whether the model answers (one cheap
`GET /api/tags`) and, when it does, runs the same catch-up as
`POST /admin/review`: the unverified memories, oldest first, one after
another. One catch-up runs at a time, whoever started it. `GET /health`
says what the last check found as `review_model`.

Layers: the setting and `reachable()` on each reviewer; one tick and the
loop with a fake reviewer whose answer the test flips; the lock between
the poll and the route, in both directions; a check that raises and a
selection that fails; the lifespan starting the loop and cancelling it at
shutdown; the health values; the cases where no loop runs; a live server
with the CLI; and the update reset that sends an edited memory back to
the catch-up.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import urllib.error
import urllib.request

import httpx
import pytest

from agent_memory.server import app as app_mod
from agent_memory.server import repository as repo
from agent_memory.server import review as review_mod
from agent_memory.server.app import (
    REVIEW_MODEL_STATES,
    _catch_up_lock,
    _review_poll,
    _review_tick,
    create_app,
)
from agent_memory.server.db import make_sessionmaker
from agent_memory.server.review import (
    NullReviewer,
    OllamaReviewer,
    Reviewer,
    review_poll,
)
from agent_memory.server.schemas import TagIn
from conftest import TOKEN, FakeEmbedder, live_app, make_test_engine
from drivers import CliDriver
from test_review import APPROVE, REJECT, FakeReviewer, _App, _review_rows
from test_review_status import _statuses_in_db, _unverified


class PollReviewer:
    """A reviewer for the poll: `up` is what `reachable()` answers (the test
    flips it), `checks` counts those calls, and `calls` records each review.
    With `block`, every review waits in its thread until the test sets
    `release`, and `started` says the first one is waiting: that is how a
    test holds a catch-up open. `check_error`, when set, is raised by
    `reachable()`."""

    model_name = "poll-fake"

    def __init__(self, up=False, verdict=APPROVE, block=False):
        self.up = up
        self.verdict = verdict
        self.block = block
        self.check_error = None
        self.checks = 0
        self.calls = []
        self.started = threading.Event()
        self.release = threading.Event()

    def reachable(self):
        self.checks += 1
        if self.check_error is not None:
            raise self.check_error
        return self.up

    def review(self, memory, neighbours, tags=()):
        self.calls.append((memory, neighbours))
        if self.block:
            self.started.set()
            if not self.release.wait(timeout=10):
                raise TimeoutError("the test never released the review")
        return self.verdict


async def _until(check, timeout=5.0):
    """Wait, without blocking the loop, until `check()` is true."""
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("the condition did not come true in time")
        await asyncio.sleep(0.01)


def _messages(caplog):
    return [r.getMessage() for r in caplog.records]


def _poll_task():
    """The poll task the lifespan started, or None."""
    for task in asyncio.all_tasks():
        if task.get_name() == "review-poll":
            return task
    return None


# ── the setting ──────────────────────────────────────────────────────────────
def test_review_poll_reads_the_seconds_and_defaults_to_five_minutes(monkeypatch, caplog):
    assert review_mod.DEFAULT_POLL == 300
    monkeypatch.delenv("AGENT_MEMORY_REVIEW_POLL", raising=False)
    assert review_poll() == 300
    for raw, seconds in (("60", 60.0), (" 0.5 ", 0.5), ("0", 0.0), ("", 300.0)):
        monkeypatch.setenv("AGENT_MEMORY_REVIEW_POLL", raw)
        assert review_poll() == seconds
    monkeypatch.setenv("AGENT_MEMORY_REVIEW_POLL", "soon")
    with caplog.at_level(logging.WARNING, logger="agent_memory.server.review"):
        assert review_poll() == 300
    assert _messages(caplog) == ["AGENT_MEMORY_REVIEW_POLL='soon' is not a number; using 300.0"]


def test_the_three_health_values():
    assert REVIEW_MODEL_STATES == ("off", "reachable", "unreachable")


# ── reachable() ──────────────────────────────────────────────────────────────
def test_null_reviewer_is_never_reachable_and_the_base_has_no_answer():
    assert NullReviewer().reachable() is False
    with pytest.raises(NotImplementedError):
        Reviewer().reachable()


def _fake_tags(monkeypatch, raise_=None):
    """Replace `urlopen` with one that records the request and answers an
    empty model list, or raises `raise_`."""
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"models": []}'

    def urlopen(url, timeout=None, **kwargs):
        seen["url"] = url if isinstance(url, str) else url.full_url
        seen["timeout"] = timeout
        if raise_ is not None:
            raise raise_
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return seen


def test_ollama_reachable_gets_api_tags_with_a_two_second_timeout(monkeypatch):
    seen = _fake_tags(monkeypatch)
    assert OllamaReviewer("http://ollama:11434/").reachable() is True
    assert seen == {"url": "http://ollama:11434/api/tags", "timeout": 2}
    assert review_mod.REACHABLE_TIMEOUT == 2


@pytest.mark.parametrize("error", [
    urllib.error.URLError("connection refused"),
    TimeoutError("timed out"),
    ConnectionResetError("reset"),
])
def test_ollama_reachable_is_false_when_the_server_does_not_answer(monkeypatch, error):
    _fake_tags(monkeypatch, raise_=error)
    assert OllamaReviewer("http://ollama:11434").reachable() is False


# ── one tick ─────────────────────────────────────────────────────────────────
async def test_a_tick_while_unreachable_skips_and_says_unreachable():
    ids = await _unverified("one", "two")
    fake = PollReviewer(up=False)
    async with _App(reviewer=fake) as a:
        await _review_tick(a.app)
        assert fake.checks == 1 and fake.calls == []
        assert a.app.state.review_model == "unreachable"
        assert (await a.client.get("/health")).json() == {"status": "ok",
                                                          "review_model": "unreachable"}
    assert await _statuses_in_db() == [(mid, "unverified") for mid in ids]


async def test_a_tick_when_reachable_runs_the_catch_up_and_says_reachable(caplog):
    one, two = await _unverified("one", "two")
    fake = PollReviewer(up=True)
    with caplog.at_level(logging.INFO, logger="agent_memory.server.app"):
        async with _App(reviewer=fake) as a:
            await _review_tick(a.app)
            assert a.app.state.review_model == "reachable"
            assert (await a.client.get("/health")).json()["review_model"] == "reachable"
            assert [m["id"] for m, _ in fake.calls] == [one, two]
            assert await _statuses_in_db() == [(one, "verified"), (two, "verified")]
            assert not _catch_up_lock(a.app).locked()

            # Nothing left: the next tick checks, and that is all.
            await _review_tick(a.app)
            assert fake.checks == 2 and len(fake.calls) == 2
    assert _messages(caplog) == [
        "review model reachable: poll-fake",
        "catch-up started: 2 unverified memories to review",
        "catch-up finished",
    ]


async def test_a_tick_uses_the_same_catch_up_as_the_route(monkeypatch):
    # The route's test spies on `_review_in_order`; the poll goes through it too.
    seen = []
    real = app_mod._review_in_order

    async def spy(app, ids):
        seen.append(list(ids))
        await real(app, ids)

    monkeypatch.setattr(app_mod, "_review_in_order", spy)
    one, two = await _unverified("one", "two")
    async with _App(reviewer=PollReviewer(up=True)) as a:
        await _review_tick(a.app)
    assert seen == [[one, two]]


# ── the loop ─────────────────────────────────────────────────────────────────
async def test_the_loop_waits_while_unreachable_and_catches_up_when_the_model_is_back(caplog):
    one, two = await _unverified("one", "two")
    fake = PollReviewer(up=False)
    with caplog.at_level(logging.INFO, logger="agent_memory.server.app"):
        async with _App(reviewer=fake) as a:
            task = asyncio.create_task(_review_poll(a.app, 0.01))
            await _until(lambda: fake.checks >= 3)
            assert fake.calls == []
            assert a.app.state.review_model == "unreachable"
            assert await _statuses_in_db() == [(one, "unverified"), (two, "unverified")]

            fake.up = True
            await _until(lambda: len(fake.calls) == 2)
            await _until(lambda: not _catch_up_lock(a.app).locked())
            assert a.app.state.review_model == "reachable"
            assert await _statuses_in_db() == [(one, "verified"), (two, "verified")]
            checks = fake.checks
            # Still ticking: a check every 0.01 s.
            await _until(lambda: fake.checks > checks)

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelled()
    messages = _messages(caplog)
    assert messages[0] == "review poll started: checking the model every 0.01 s"
    assert "review model reachable: poll-fake" in messages
    assert "catch-up started: 2 unverified memories to review" in messages
    assert "catch-up finished" in messages


async def test_the_loop_survives_a_check_that_raises(caplog):
    (mid,) = await _unverified("one")
    fake = PollReviewer(up=True)
    fake.check_error = RuntimeError("boom")
    with caplog.at_level(logging.INFO, logger="agent_memory.server.app"):
        async with _App(reviewer=fake) as a:
            task = asyncio.create_task(_review_poll(a.app, 0.01))
            await _until(lambda: fake.checks >= 3)
            assert a.app.state.review_model == "unreachable"
            assert fake.calls == []
            # The check works again: the loop is still there to notice.
            fake.check_error = None
            await _until(lambda: len(fake.calls) == 1)
            await _until(lambda: not _catch_up_lock(a.app).locked())
            assert a.app.state.review_model == "reachable"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert await _statuses_in_db() == [(mid, "verified")]
    failed = [m for m in _messages(caplog) if m.startswith("review poll: the check failed")]
    assert failed and set(failed) == {"review poll: the check failed: RuntimeError: boom"}


async def test_a_failing_selection_releases_the_lock_and_the_loop_goes_on(monkeypatch, caplog):
    (mid,) = await _unverified("one")
    fake = PollReviewer(up=True)
    real = repo.unverified_ids
    broken = {"on": True}

    async def unverified_ids(session, *, limit=None):
        if broken["on"]:
            raise RuntimeError("database away")
        return await real(session, limit=limit)

    monkeypatch.setattr(repo, "unverified_ids", unverified_ids)
    with caplog.at_level(logging.WARNING, logger="agent_memory.server.app"):
        async with _App(reviewer=fake) as a:
            task = asyncio.create_task(_review_poll(a.app, 0.01))
            await _until(lambda: fake.checks >= 3)
            assert not _catch_up_lock(a.app).locked()
            assert fake.calls == []
            broken["on"] = False
            await _until(lambda: len(fake.calls) == 1)
            await _until(lambda: not _catch_up_lock(a.app).locked())
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert await _statuses_in_db() == [(mid, "verified")]
    assert "review poll: RuntimeError: database away" in _messages(caplog)


# ── one catch-up at a time ───────────────────────────────────────────────────
async def test_a_route_call_during_a_poll_catch_up_answers_running(caplog):
    one, two = await _unverified("one", "two")
    fake = PollReviewer(up=True, block=True)
    with caplog.at_level(logging.INFO, logger="agent_memory.server.app"):
        async with _App(reviewer=fake) as a:
            tick = asyncio.create_task(_review_tick(a.app))
            await asyncio.to_thread(fake.started.wait, 10)
            assert _catch_up_lock(a.app).locked()

            resp = await a.client.post("/admin/review")
            assert resp.status_code == 200
            assert resp.json() == {"scheduled": 0, "running": True}
            # Nothing was reviewed for it: the one call is the poll's, still waiting.
            assert len(fake.calls) == 1
            assert await _statuses_in_db() == [(one, "unverified"), (two, "unverified")]

            fake.release.set()
            await tick
            assert not _catch_up_lock(a.app).locked()
            assert [m["id"] for m, _ in fake.calls] == [one, two]
            assert await _statuses_in_db() == [(one, "verified"), (two, "verified")]
            # Free again, and nothing left.
            assert (await a.client.post("/admin/review")).json() == {"scheduled": 0}
    assert _messages(caplog).count("catch-up started: 2 unverified memories to review") == 1


async def test_a_tick_during_a_route_catch_up_does_nothing(caplog):
    one, two = await _unverified("one", "two")
    fake = PollReviewer(up=True, block=True)
    with caplog.at_level(logging.INFO, logger="agent_memory.server.app"):
        async with _App(reviewer=fake) as a:
            # The transport waits for the background task, so the call is a task.
            post = asyncio.create_task(a.client.post("/admin/review"))
            await asyncio.to_thread(fake.started.wait, 10)
            assert _catch_up_lock(a.app).locked()

            await _review_tick(a.app)
            assert fake.checks == 1
            assert a.app.state.review_model == "reachable"
            # Still the route's one call: the tick started no second catch-up.
            assert len(fake.calls) == 1
            assert _catch_up_lock(a.app).locked()

            fake.release.set()
            resp = await post
            assert resp.json() == {"scheduled": 2}
            assert not _catch_up_lock(a.app).locked()
            assert [m["id"] for m, _ in fake.calls] == [one, two]
    assert await _statuses_in_db() == [(one, "verified"), (two, "verified")]
    assert _messages(caplog).count("catch-up started: 2 unverified memories to review") == 1


async def test_two_route_calls_share_the_lock_too():
    await _unverified("one")
    fake = PollReviewer(up=True, block=True)
    async with _App(reviewer=fake) as a:
        first = asyncio.create_task(a.client.post("/admin/review"))
        await asyncio.to_thread(fake.started.wait, 10)
        assert (await a.client.post("/admin/review")).json() == {"scheduled": 0, "running": True}
        fake.release.set()
        assert (await first).json() == {"scheduled": 1}
        assert len(fake.calls) == 1


async def test_a_catch_up_that_is_cancelled_releases_the_lock(caplog):
    await _unverified("one", "two")
    fake = PollReviewer(up=True, block=True)
    with caplog.at_level(logging.INFO, logger="agent_memory.server.app"):
        async with _App(reviewer=fake) as a:
            task = asyncio.create_task(_review_poll(a.app, 0.01))
            await asyncio.to_thread(fake.started.wait, 10)
            assert _catch_up_lock(a.app).locked()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not _catch_up_lock(a.app).locked()
            fake.release.set()
    assert "catch-up stopped" in _messages(caplog)


# ── the lifespan ─────────────────────────────────────────────────────────────
async def test_the_lifespan_starts_the_loop_and_shutdown_cancels_it_cleanly(caplog):
    one, two = await _unverified("one", "two")
    fake = PollReviewer(up=True)
    with caplog.at_level(logging.INFO):
        async with _App(reviewer=fake, review_poll=0.01) as a:
            async with a.app.router.lifespan_context(a.app):
                task = _poll_task()
                assert task is not None and not task.done()
                await _until(lambda: len(fake.calls) == 2)
                await _until(lambda: not _catch_up_lock(a.app).locked())
                assert a.app.state.review_model == "reachable"
            # Shutdown cancelled the task and waited for it.
            assert task.done() and task.cancelled()
    assert await _statuses_in_db() == [(one, "verified"), (two, "verified")]
    # No traceback, no error from asyncio about the task.
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
    assert not any("never retrieved" in m for m in _messages(caplog))
    assert "review poll started: checking the model every 0.01 s" in _messages(caplog)


async def test_shutdown_while_a_check_is_running_is_clean(caplog):
    # A slow check (the model server takes its 2 s to time out) must not
    # hold up the shutdown or leave an error behind.
    class Slow(PollReviewer):
        def reachable(self):
            self.checks += 1
            time.sleep(0.2)
            return False

    fake = Slow()
    with caplog.at_level(logging.INFO):
        async with _App(reviewer=fake, review_poll=0.01) as a:
            async with a.app.router.lifespan_context(a.app):
                await _until(lambda: fake.checks >= 1)
                task = _poll_task()
            assert task.cancelled()
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


async def test_the_lifespan_reads_the_interval_from_the_environment(monkeypatch):
    monkeypatch.setenv("AGENT_MEMORY_REVIEW_POLL", "0.01")
    fake = PollReviewer(up=False)
    async with _App(reviewer=fake) as a:
        async with a.app.router.lifespan_context(a.app):
            assert _poll_task() is not None
            await _until(lambda: fake.checks >= 2)
            assert a.app.state.review_model == "unreachable"


# ── health ───────────────────────────────────────────────────────────────────
async def test_health_reports_what_the_last_check_found():
    fake = PollReviewer(up=False)
    async with _App(reviewer=fake, review_poll=0.01) as a:
        async with a.app.router.lifespan_context(a.app):
            # Health needs no token: a bare client.
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=a.app),
                                         base_url="http://testserver") as c:
                await _until(lambda: fake.checks >= 1)
                assert (await c.get("/health")).json() == {"status": "ok",
                                                           "review_model": "unreachable"}
                fake.up = True
                await _until(lambda: a.app.state.review_model == "reachable")
                assert (await c.get("/health")).json() == {"status": "ok",
                                                           "review_model": "reachable"}
                fake.up = False
                await _until(lambda: a.app.state.review_model == "unreachable")
                assert (await c.get("/health")).json()["review_model"] == "unreachable"


async def test_health_says_off_when_the_review_is_off():
    async with _App(reviewer=NullReviewer(), review_poll=0.01) as a:
        async with a.app.router.lifespan_context(a.app):
            assert a.app.state.review_mode == "off"
            assert a.app.state.review_model == "off"
            assert (await a.client.get("/health")).json() == {"status": "ok",
                                                              "review_model": "off"}


async def test_health_says_off_without_a_lifespan():
    async with _App(reviewer=PollReviewer(up=True)) as a:
        assert (await a.client.get("/health")).json() == {"status": "ok", "review_model": "off"}


# ── when no loop runs ────────────────────────────────────────────────────────
async def test_no_loop_when_the_review_is_off():
    class NullWithCount(NullReviewer):
        checks = 0

        def reachable(self):
            NullWithCount.checks += 1
            return False

    async with _App(reviewer=NullWithCount(), review_poll=0.01) as a:
        async with a.app.router.lifespan_context(a.app):
            await asyncio.sleep(0.05)
            assert _poll_task() is None
    assert NullWithCount.checks == 0


@pytest.mark.parametrize("interval", [0, -1])
async def test_no_loop_when_the_poll_is_zero_or_less(interval):
    fake = PollReviewer(up=True)
    await _unverified("one")
    async with _App(reviewer=fake, review_poll=interval) as a:
        async with a.app.router.lifespan_context(a.app):
            assert a.app.state.review_mode == "flag"
            await asyncio.sleep(0.05)
            assert _poll_task() is None
            assert a.app.state.review_model == "off"
            assert (await a.client.get("/health")).json()["review_model"] == "off"
    assert fake.checks == 0 and fake.calls == []
    assert await _statuses_in_db() == [(1, "unverified")]


async def test_no_loop_when_the_environment_says_zero(monkeypatch):
    monkeypatch.setenv("AGENT_MEMORY_REVIEW_POLL", "0")
    fake = PollReviewer(up=True)
    async with _App(reviewer=fake) as a:
        async with a.app.router.lifespan_context(a.app):
            await asyncio.sleep(0.05)
            assert _poll_task() is None and a.app.state.review_model == "off"
    assert fake.checks == 0


# ── a live server, with the CLI ──────────────────────────────────────────────
def _wait_until(check, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError("the condition did not come true in time")


def test_a_live_server_catches_up_on_its_own_and_the_cli_sees_it(live_server):
    # Written through the shared server, which has no model: unverified.
    url, token = live_server
    plain = CliDriver(url, token)
    for text in ("one", "two"):
        plain.raw("add", text)

    fake = PollReviewer(up=True, verdict=REJECT)
    engine = make_test_engine()
    app = create_app(sessionmaker=make_sessionmaker(engine), token=TOKEN,
                     embedder=FakeEmbedder(), reviewer=fake, review_poll=0.05)
    with live_app(app) as polling_url:
        with httpx.Client(base_url=polling_url, timeout=10) as c:
            _wait_until(lambda: c.get("/health").json()["review_model"] == "reachable")
            _wait_until(lambda: asyncio.run(_statuses_in_db()) == [(1, "flagged"), (2, "flagged")])
        assert [m["id"] for m, _ in fake.calls] == [1, 2]
        # Nothing left for a hand-run catch-up. The poll ticks every 50 ms and
        # holds the lock while it looks for work, so a request can land inside
        # a tick and be told a catch-up is running; ask again until it is not.
        cli = CliDriver(polling_url, token)
        for _ in range(20):
            out = cli.raw("review", "--catch-up").stdout.strip()
            if out == "✓ Scheduled 0 reviews":
                break
            assert out == "✓ A catch-up is already running; nothing new scheduled", out
            time.sleep(0.05)
        assert out == "✓ Scheduled 0 reviews"
    asyncio.run(engine.dispose())


def test_the_cli_says_when_a_catch_up_is_already_running(live_server):
    url, token = live_server
    CliDriver(url, token).raw("add", "one")

    fake = PollReviewer(up=True, block=True)
    engine = make_test_engine()
    app = create_app(sessionmaker=make_sessionmaker(engine), token=TOKEN,
                     embedder=FakeEmbedder(), reviewer=fake, review_poll=0.05)
    with live_app(app) as polling_url:
        try:
            assert fake.started.wait(10)
            proc = CliDriver(polling_url, token).raw("review", "--catch-up")
            assert proc.returncode == 0, proc.stderr
            assert proc.stdout.strip() == "✓ A catch-up is already running; nothing new scheduled"
        finally:
            fake.release.set()
        _wait_until(lambda: asyncio.run(_statuses_in_db()) == [(1, "verified")])
    asyncio.run(engine.dispose())


# ── the update reset ─────────────────────────────────────────────────────────
async def _verified_with_a_row(session, content="the old text"):
    mid = await repo.add(session, content, "tester", "alpha", [TagIn(name="a")], "note",
                         embedder=FakeEmbedder())
    await repo.set_review(session, mid, APPROVE, "planted")
    await session.flush()
    row = await repo.get(session, mid)
    assert row["review_status"] == "verified" and row["review"] == APPROVE.as_dict()
    return mid


async def test_repository_update_with_new_content_resets_the_status_and_drops_the_row():
    engine = make_test_engine()
    try:
        async with make_sessionmaker(engine)() as session, session.begin():
            mid = await _verified_with_a_row(session)
            changes = await repo.update(session, mid, content="the new text",
                                        embedder=FakeEmbedder())
            assert changes == ["content"]
            await session.flush()
            row = await repo.get(session, mid)
            assert row["review_status"] == "unverified" and row["review"] is None
        assert await _review_rows() == []
        # So the next catch-up picks it up.
        async with make_sessionmaker(engine)() as session:
            assert await repo.unverified_ids(session) == [mid]
    finally:
        await engine.dispose()


async def test_repository_update_of_tags_or_project_only_keeps_the_status():
    engine = make_test_engine()
    try:
        async with make_sessionmaker(engine)() as session, session.begin():
            mid = await _verified_with_a_row(session)
            await repo.update(session, mid, add_tags=[TagIn(name="b")], remove_tags=["a"],
                              project="beta", mtype="decision", embedder=FakeEmbedder())
            # The same text again is not a change either.
            await repo.update(session, mid, content="the old text", embedder=FakeEmbedder())
            await session.flush()
            row = await repo.get(session, mid)
            assert row["review_status"] == "verified" and row["review"] == APPROVE.as_dict()
            assert row["tags"] == ["b"] and row["project"] == "beta"
        assert await _review_rows() == [(mid, "approve", "planted")]
    finally:
        await engine.dispose()


async def test_api_update_with_new_content_sends_the_memory_back_to_the_catch_up():
    fake = PollReviewer(up=True)
    async with _App(reviewer=fake) as a:
        # Flag mode: the verdict lands after the add.
        mid = (await a.add("the old text", tags=["a"]))["id"]
        assert (await a.get(mid))["review_status"] == "verified"
        assert len(fake.calls) == 1

        resp = await a.client.patch(f"/memories/{mid}", json={"content": "the new text"})
        assert resp.status_code == 200 and resp.json() == {"changes": ["content"]}
        row = await a.get(mid)
        assert row["review_status"] == "unverified" and row["review"] is None
        assert await _review_rows() == []
        # An update alone asks the model nothing.
        assert len(fake.calls) == 1

        # The next check picks the edited memory up and reviews the new text.
        await _review_tick(a.app)
        assert len(fake.calls) == 2 and fake.calls[1][0]["content"] == "the new text"
        row = await a.get(mid)
        assert row["review_status"] == "verified" and row["review"] == APPROVE.as_dict()


async def test_api_update_of_tags_or_project_only_keeps_the_verdict():
    fake = FakeReviewer(REJECT)
    async with _App(reviewer=fake) as a:
        mid = (await a.add("Spent the afternoon tidying.", tags=["a"]))["id"]
        assert (await a.get(mid))["review_status"] == "flagged"
        for body in ({"add_tags": [{"name": "b"}]}, {"remove_tags": ["a"]},
                     {"project": "beta"}, {"type": "note"}, {"set_tags": [{"name": "c"}]}):
            assert (await a.client.patch(f"/memories/{mid}", json=body)).status_code == 200
        row = await a.get(mid)
        assert row["review_status"] == "flagged" and row["review"] == REJECT.as_dict()
        assert row["tags"] == ["c"] and row["project"] == "beta"
        assert len(fake.calls) == 1
