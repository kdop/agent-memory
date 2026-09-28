"""Tests for the review refuse mode (issues #34 and #73).

With AGENT_MEMORY_REVIEW=refuse the model reads each new entry before it is
stored. A reject or rewrite verdict refuses the write with a 422 that carries
the verdict, the rule, the model's explanation and, for a rewrite, the
suggested text and tags; nothing is stored. An approve stores the memory and
its verdict in one go. No verdict (model unreachable, timeout, bad JSON)
stores the memory as flag mode would, with no row and one log line: an
absent model never blocks a write. `force=true` skips the review as it
skips the duplicate check; the review then runs in the background. The old
setting names `warn` and `enforce` still mean `flag` and `refuse`, with one
log line. While the model thinks, the write holds no database connection:
the reads run in a short session of their own, closed before the call.

Same layers as test_review.py: the settings, the app with a `FakeReviewer`,
the CLI and MCP surfaces against a live server in refuse mode, and the real
model once under the `review` marker.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from agent_memory.client import ApiClient, ReviewRefused
from agent_memory.server import review as review_mod
from agent_memory.server.app import create_app
from agent_memory.server.db import make_sessionmaker
from agent_memory.server.embedding import NullEmbedder, cosine
from agent_memory.server.review import (
    NullReviewer,
    OllamaReviewer,
    Verdict,
    make_reviewer,
    review_mode,
)
from conftest import PG_DSN, TOKEN, FakeEmbedder, _async_dsn, _free_port, make_test_engine
from drivers import CliDriver, McpDriver
from test_review import APPROVE, REJECT, _App, _real_server, _review_rows

REWRITE = Verdict("rewrite", 3, "Say why.", "Chose Postgres,\nbecause of X.", None,
                  ["database", "search"])
REPEAT = Verdict("reject", None, "Says the same as #1.", None, 1)


class FakeReviewer:
    """Answers with a fixed verdict and records everything it was asked."""

    model_name = "fake-reviewer"

    def __init__(self, verdict=REJECT):
        self.verdict = verdict
        self.calls = []

    def review(self, memory, neighbours, tags=()):
        self.calls.append((memory, neighbours, list(tags)))
        return self.verdict


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def _refusal(verdict):
    """The 422 body the server answers with for `verdict`."""
    return {"detail": {
        "reason": "review", "verdict": verdict.verdict, "rule": verdict.rule,
        "explanation": verdict.reason, "rewrite": verdict.rewrite,
        "tags": list(verdict.tags), "duplicate_of": verdict.duplicate_of}}


async def _post(a, content, project="alpha", type=None, tags=(), **params):
    return await a.client.post("/memories", params=params, json={
        "content": content, "project": project, "type": type, "agent": "tester",
        "tags": [{"name": t} for t in tags]})


async def _count(a):
    return len((await a.client.get("/memories", params={"limit": 0})).json())


# ── the settings ─────────────────────────────────────────────────────────────
def test_review_mode_reads_the_three_values(monkeypatch):
    for raw, mode in (("off", "off"), ("flag", "flag"), ("refuse", "refuse"),
                      (" Refuse ", "refuse"), ("", "off"), ("block", "off")):
        monkeypatch.setenv("AGENT_MEMORY_REVIEW", raw)
        assert review_mode() == mode
    monkeypatch.delenv("AGENT_MEMORY_REVIEW")
    assert review_mode() == "off"
    assert review_mod.MODES == ("off", "flag", "refuse")


def test_review_mode_maps_the_old_names_with_one_log_line(monkeypatch, caplog):
    assert review_mod.OLD_MODE_NAMES == {"warn": "flag", "enforce": "refuse"}
    for raw, mode in (("warn", "flag"), ("enforce", "refuse"), (" Enforce ", "refuse")):
        monkeypatch.setenv("AGENT_MEMORY_REVIEW", raw)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="agent_memory.server.review"):
            assert review_mode() == mode
        lines = [r.getMessage() for r in caplog.records]
        assert len(lines) == 1, lines
        assert raw.strip().lower() in lines[0] and mode in lines[0]
    # The new names log nothing.
    monkeypatch.setenv("AGENT_MEMORY_REVIEW", "refuse")
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="agent_memory.server.review"):
        assert review_mode() == "refuse"
    assert caplog.records == []


def test_make_reviewer_takes_an_old_name_as_the_new_mode(monkeypatch, caplog):
    monkeypatch.setenv("AGENT_MEMORY_REVIEW", "enforce")
    monkeypatch.setenv("AGENT_MEMORY_REVIEW_URL", "http://ollama:11434")
    with caplog.at_level(logging.INFO, logger="agent_memory.server.review"):
        r = make_reviewer()
    assert isinstance(r, OllamaReviewer)
    assert "review is on (refuse)" in caplog.records[-1].getMessage()
    monkeypatch.delenv("AGENT_MEMORY_REVIEW_URL")
    r = make_reviewer()
    assert isinstance(r, NullReviewer)
    assert "AGENT_MEMORY_REVIEW=enforce" in r.reason


def test_make_reviewer_refuse_mode_with_url_builds_the_reviewer(monkeypatch, caplog):
    monkeypatch.setenv("AGENT_MEMORY_REVIEW", "refuse")
    monkeypatch.setenv("AGENT_MEMORY_REVIEW_URL", "http://ollama:11434")
    with caplog.at_level(logging.INFO, logger="agent_memory.server.review"):
        r = make_reviewer()
    assert isinstance(r, OllamaReviewer)
    assert r.url == "http://ollama:11434"
    assert "review is on (refuse)" in caplog.records[-1].getMessage()


def test_make_reviewer_refuse_mode_without_url_is_off(monkeypatch):
    monkeypatch.setenv("AGENT_MEMORY_REVIEW", "refuse")
    monkeypatch.delenv("AGENT_MEMORY_REVIEW_URL", raising=False)
    r = make_reviewer()
    assert isinstance(r, NullReviewer)
    assert "AGENT_MEMORY_REVIEW=refuse" in r.reason
    assert "AGENT_MEMORY_REVIEW_URL is not set" in r.reason


def test_make_reviewer_unknown_mode_names_the_three_values(monkeypatch):
    monkeypatch.setenv("AGENT_MEMORY_REVIEW", "block")
    monkeypatch.setenv("AGENT_MEMORY_REVIEW_URL", "http://ollama:11434")
    r = make_reviewer()
    assert isinstance(r, NullReviewer)
    assert "'block'" in r.reason and "'refuse'" in r.reason


# ── the app in refuse mode, with a FakeReviewer ─────────────────────────────
async def test_reject_refuses_with_the_exact_body_and_stores_nothing():
    fake = FakeReviewer(REJECT)
    async with _App(reviewer=fake, review_mode="refuse") as a:
        assert a.app.state.review_mode == "refuse"
        resp = await _post(a, "Spent the afternoon tidying.", type="note", tags=["work-log"])
        assert resp.status_code == 422
        assert resp.json() == _refusal(REJECT)
        assert resp.json()["detail"]["rewrite"] is None
        assert await _count(a) == 0
    assert await _review_rows() == []
    # The model saw the entry as the writer sent it, with no id yet.
    assert len(fake.calls) == 1
    memory, neighbours, _ = fake.calls[0]
    assert memory == {"id": None, "project": "alpha", "type": "note", "tags": ["work-log"],
                      "content": "Spent the afternoon tidying."}
    assert neighbours == []


async def test_a_repeat_refuses_with_duplicate_of_and_no_rule():
    async with _App(reviewer=FakeReviewer(APPROVE), review_mode="refuse") as a:
        assert (await _post(a, "the original")).status_code == 201
        a.app.state.reviewer.verdict = REPEAT
        resp = await _post(a, "the original, said again")
        assert resp.status_code == 422
        assert resp.json() == _refusal(REPEAT)
        assert resp.json()["detail"]["rule"] is None
        assert resp.json()["detail"]["duplicate_of"] == 1
        assert await _count(a) == 1


async def test_rewrite_refuses_with_the_suggested_text_and_tags():
    async with _App(reviewer=FakeReviewer(REWRITE), review_mode="refuse") as a:
        resp = await _post(a, "Chose Postgres.", type="decision")
        assert resp.status_code == 422
        assert resp.json() == _refusal(REWRITE)
        detail = resp.json()["detail"]
        assert detail["verdict"] == "rewrite"
        assert detail["rewrite"] == "Chose Postgres,\nbecause of X."
        assert detail["tags"] == ["database", "search"]
        assert await _count(a) == 0
    assert await _review_rows() == []


async def test_approve_stores_the_memory_and_its_row_with_one_model_call():
    fake = FakeReviewer(APPROVE)
    async with _App(reviewer=fake, review_mode="refuse") as a:
        resp = await _post(a, "Chose Postgres, because several agents write at once.",
                           type="decision")
        assert resp.status_code == 201
        assert set(resp.json()) == {"id", "warnings"}
        mid = resp.json()["id"]
        row = await a.get(mid)
        assert row["content"] == "Chose Postgres, because several agents write at once."
        assert row["review"] == APPROVE.as_dict()
    assert await _review_rows() == [(mid, "approve", "fake-reviewer")]
    assert len(fake.calls) == 1


async def test_no_verdict_stores_without_a_row_and_logs_one_line(caplog):
    fake = FakeReviewer(verdict=None)
    with caplog.at_level(logging.WARNING, logger="agent_memory.server.app"):
        async with _App(reviewer=fake, review_mode="refuse") as a:
            resp = await _post(a, "the model was away")
            assert resp.status_code == 201
            assert (await a.get(resp.json()["id"]))["review"] is None
    assert await _review_rows() == []
    assert len(fake.calls) == 1
    lines = [r.getMessage() for r in caplog.records]
    assert lines == ["review of a new memory gave no verdict; storing it without one"]


async def test_a_failing_reviewer_stores_without_a_row_and_logs_one_line(caplog):
    class Broken(FakeReviewer):
        def review(self, memory, neighbours, tags=()):
            raise RuntimeError("model blew up")

    with caplog.at_level(logging.WARNING, logger="agent_memory.server.app"):
        async with _App(reviewer=Broken(), review_mode="refuse") as a:
            resp = await _post(a, "still stored")
            assert resp.status_code == 201
            assert (await a.get(resp.json()["id"]))["review"] is None
    assert await _review_rows() == []
    lines = [r.getMessage() for r in caplog.records]
    assert lines == ["review of a new memory failed: RuntimeError: model blew up"]


async def test_force_stores_and_the_row_comes_from_the_background_review():
    fake = FakeReviewer(REJECT)
    async with _App(reviewer=fake, review_mode="refuse") as a:
        resp = await _post(a, "Spent the afternoon tidying.", force="true")
        assert resp.status_code == 201
        mid = resp.json()["id"]
        # ASGITransport waits for the background task, so the row is there now.
        assert (await a.get(mid))["review"] == REJECT.as_dict()
    assert await _review_rows() == [(mid, "reject", "fake-reviewer")]
    # One call, after the write: the model saw the stored memory, id and all.
    assert len(fake.calls) == 1
    assert fake.calls[0][0]["id"] == mid


async def test_the_duplicate_check_comes_before_the_review():
    fake = FakeReviewer(APPROVE)
    async with _App(reviewer=fake, review_mode="refuse") as a:
        assert (await _post(a, "dup me")).status_code == 201
        resp = await _post(a, "dup me")
        assert resp.status_code == 409
        assert resp.json()["detail"]["reason"] == "duplicate"
    # The second add never reached the model.
    assert len(fake.calls) == 1


async def test_neighbours_and_tags_are_found_before_the_write():
    fake = FakeReviewer(APPROVE)
    alpha = [f"alpha memory number {i} about topic {i}" for i in range(8)]
    async with _App(reviewer=fake, review_mode="refuse") as a:
        ids = {}
        for i, text in enumerate(alpha):
            ids[text] = (await a.add(text, project="alpha", tags=[f"tag{i}"]))["id"]
        await a.add("beta memory one", project="beta")
        new = "alpha memory number 3 about topic 3, said again"
        assert (await _post(a, new)).status_code == 201

    memory, neighbours, offered = fake.calls[-1]
    assert memory["id"] is None and memory["content"] == new
    emb = FakeEmbedder()
    vec = emb.embed([new])[0]
    by_cosine = sorted(alpha, key=lambda t: -cosine(vec, emb.embed([t])[0]))
    assert [n["id"] for n in neighbours] == [ids[t] for t in by_cosine[:5]]
    assert all(n["project"] == "alpha" for n in neighbours)
    assert set(neighbours[0]) >= {"id", "project", "type", "tags", "content", "score"}
    assert len(offered) == 8 and set(offered) == {f"tag{i}" for i in range(8)}


async def test_no_neighbours_without_the_embedding_model():
    fake = FakeReviewer(REJECT)
    async with _App(reviewer=fake, embedder=NullEmbedder(), review_mode="refuse") as a:
        await _post(a, "first", force="true")
        resp = await _post(a, "second")
        assert resp.status_code == 422
    assert fake.calls[-1][0]["content"] == "second"
    assert fake.calls[-1][1] == []


async def test_the_refusal_needs_the_token():
    async with _App(reviewer=FakeReviewer(REJECT), review_mode="refuse") as a:
        resp = await a.client.post("/memories", headers={"Authorization": ""},
                                   json={"content": "x", "agent": "t"})
        assert resp.status_code == 401


# ── off and flag are untouched ───────────────────────────────────────────────
async def test_off_with_a_null_reviewer_stores_and_asks_nothing():
    class CountingNull(NullReviewer):
        calls = 0

        def review(self, memory, neighbours, tags=()):
            CountingNull.calls += 1
            return None

    async with _App(reviewer=CountingNull(), review_mode="refuse") as a:
        assert a.app.state.review_mode == "off"
        resp = await _post(a, "Spent the afternoon tidying.")
        assert resp.status_code == 201
        assert (await a.get(resp.json()["id"]))["review"] is None
    assert CountingNull.calls == 0
    assert await _review_rows() == []


async def test_flag_still_stores_first_and_reviews_after():
    fake = FakeReviewer(REJECT)
    async with _App(reviewer=fake, review_mode="flag") as a:
        assert a.app.state.review_mode == "flag"
        resp = await _post(a, "Spent the afternoon tidying.")
        assert resp.status_code == 201
        mid = resp.json()["id"]
        assert (await a.get(mid))["review"] == REJECT.as_dict()
    assert await _review_rows() == [(mid, "reject", "fake-reviewer")]
    # The model saw the stored memory, id and all.
    assert fake.calls[0][0]["id"] == mid


async def test_a_reviewer_without_a_mode_runs_in_flag_mode():
    async with _App(reviewer=FakeReviewer(REJECT)) as a:
        assert a.app.state.review_mode == "flag"
        assert (await _post(a, "Spent the afternoon tidying.")).status_code == 201


async def _with_lifespan(monkeypatch, env, **kwargs):
    """Build an app with `env` in place and run its lifespan; returns the
    reviewer and the mode it settled on."""
    for var in ("AGENT_MEMORY_REVIEW", "AGENT_MEMORY_REVIEW_URL"):
        monkeypatch.delenv(var, raising=False)
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    monkeypatch.setenv("AGENT_MEMORY_EMBED_MODEL", "off")
    # No poll: it would check the made-up server address.
    monkeypatch.setenv("AGENT_MEMORY_REVIEW_POLL", "0")
    engine = make_test_engine()
    app = create_app(sessionmaker=make_sessionmaker(engine), token=TOKEN, **kwargs)
    try:
        async with app.router.lifespan_context(app):
            return app.state.reviewer, app.state.review_mode
    finally:
        await engine.dispose()


async def test_refuse_mode_in_the_environment_without_a_server_is_off(monkeypatch):
    reviewer, mode = await _with_lifespan(monkeypatch, {"AGENT_MEMORY_REVIEW": "refuse"})
    assert isinstance(reviewer, NullReviewer)
    assert mode == "off"


async def test_refuse_mode_in_the_environment_with_a_server_is_refuse(monkeypatch):
    reviewer, mode = await _with_lifespan(monkeypatch, {
        "AGENT_MEMORY_REVIEW": "refuse", "AGENT_MEMORY_REVIEW_URL": "http://ollama:11434"})
    assert isinstance(reviewer, OllamaReviewer)
    assert mode == "refuse"


async def test_flag_in_the_environment_is_flag(monkeypatch):
    _, mode = await _with_lifespan(monkeypatch, {
        "AGENT_MEMORY_REVIEW": "flag", "AGENT_MEMORY_REVIEW_URL": "http://ollama:11434"})
    assert mode == "flag"


async def test_an_old_name_in_the_environment_runs_the_new_mode(monkeypatch):
    _, mode = await _with_lifespan(monkeypatch, {
        "AGENT_MEMORY_REVIEW": "enforce", "AGENT_MEMORY_REVIEW_URL": "http://ollama:11434"})
    assert mode == "refuse"
    _, mode = await _with_lifespan(monkeypatch, {
        "AGENT_MEMORY_REVIEW": "warn", "AGENT_MEMORY_REVIEW_URL": "http://ollama:11434"})
    assert mode == "flag"


async def test_off_by_default_even_with_a_server(monkeypatch):
    reviewer, mode = await _with_lifespan(monkeypatch, {
        "AGENT_MEMORY_REVIEW_URL": "http://ollama:11434"})
    assert isinstance(reviewer, NullReviewer)
    assert mode == "off"


async def test_an_injected_reviewer_ignores_off_in_the_environment(monkeypatch):
    # Tests hand the app a reviewer; the environment says off. It is used, in flag mode.
    _, mode = await _with_lifespan(monkeypatch, {"AGENT_MEMORY_REVIEW": "off"},
                                   reviewer=FakeReviewer())
    assert mode == "flag"
    _, mode = await _with_lifespan(monkeypatch, {"AGENT_MEMORY_REVIEW": "refuse"},
                                   reviewer=FakeReviewer())
    assert mode == "refuse"


# ── the pool is free while the model thinks ──────────────────────────────────
class BlockingReviewer:
    """Approves, but only once `release` is set: each call sits in its thread
    until then, like a model that takes its time. `started` counts the calls
    that have reached it."""

    model_name = "slow-reviewer"

    def __init__(self):
        self.release = threading.Event()
        self.started = 0
        self._lock = threading.Lock()

    def review(self, memory, neighbours, tags=()):
        with self._lock:
            self.started += 1
        assert self.release.wait(30), "the test never released the reviewer"
        return APPROVE


async def test_refuse_mode_holds_no_connection_while_the_model_thinks():
    """Five writes wait on the model at once, on a pool of two connections.
    All five reach the model, and a read still answers within a second: the
    writes read what the model needs in a short session and let it go before
    the call. Before this, each write kept its request's connection for the
    whole call, so only two reached the model and the read waited on the
    pool."""
    slow = BlockingReviewer()
    engine = create_async_engine(_async_dsn(PG_DSN), pool_size=2, max_overflow=0)
    app = create_app(sessionmaker=make_sessionmaker(engine), token=TOKEN,
                     embedder=FakeEmbedder(), reviewer=slow, review_mode="refuse")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                               base_url="http://testserver", headers=_auth())
    writes = []
    try:
        for n in range(5):
            writes.append(asyncio.create_task(client.post("/memories", json={
                "content": f"Chose option {n} for the cache, because it fits in memory.",
                "project": "alpha", "type": "decision", "agent": "tester", "tags": []})))
        deadline = time.monotonic() + 10
        while slow.started < 5:
            assert time.monotonic() < deadline, \
                f"only {slow.started} of 5 writes reached the model in 10 s"
            await asyncio.sleep(0.02)
        read = await asyncio.wait_for(client.get("/memories"), timeout=1.0)
        assert read.status_code == 200 and read.json() == []
    finally:
        slow.release.set()
        results = await asyncio.gather(*writes, return_exceptions=True)
        await client.aclose()
        await engine.dispose()
    assert [getattr(r, "status_code", r) for r in results] == [201] * 5
    ids = sorted(r.json()["id"] for r in results)
    assert ids == [1, 2, 3, 4, 5]
    rows = await _review_rows()
    assert [(mid, v) for mid, v, _ in rows] == [(i, "approve") for i in ids]


# ── the CLI, the MCP tool and the client, against a live server in refuse mode
@pytest.fixture(scope="module")
def refusing_server(_schema):
    """A live server with a `FakeReviewer` in refuse mode. Yields
    `(url, token, reviewer)`; a test sets `reviewer.verdict` as it needs."""
    import uvicorn

    fake = FakeReviewer(REJECT)
    engine = make_test_engine()
    app = create_app(sessionmaker=make_sessionmaker(engine), token=TOKEN,
                     embedder=FakeEmbedder(), reviewer=fake, review_mode="refuse",
                     review_poll=0)
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


@pytest.fixture
def refusing(refusing_server):
    """The server above with its reviewer reset to a reject for this test."""
    url, token, fake = refusing_server
    fake.verdict = REJECT
    fake.calls.clear()
    return url, token, fake


def _wait_for_rows(n, timeout=10):
    """A forced write is reviewed after the response; wait for `n` rows."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = asyncio.run(_review_rows())
        if len(rows) >= n:
            return rows
        time.sleep(0.05)
    raise AssertionError(f"expected {n} review rows, got {len(asyncio.run(_review_rows()))}")


def test_cli_reject_prints_the_verdict_and_exits_4(refusing):
    url, token, _ = refusing
    cli = CliDriver(url, token)
    proc = cli.raw("add", "Spent the afternoon tidying.", "--project", "p")
    assert proc.returncode == 4
    assert proc.stdout.splitlines() == [
        "✗ Review: reject, rule 2: A diary line: it says what was done, not why.",
        "Fix the entry, or pass --force to store it as written.",
    ]
    assert "Traceback" not in proc.stderr
    assert cli.raw("query").stdout.strip() == "No memories found."


def test_cli_rewrite_prints_the_suggestion_like_show_does(refusing):
    url, token, fake = refusing
    fake.verdict = REWRITE
    cli = CliDriver(url, token)
    proc = cli.raw("add", "Chose Postgres.", "--project", "p", "--type", "decision")
    assert proc.returncode == 4
    assert proc.stdout.splitlines() == [
        "✗ Review: rewrite, rule 3: Say why.",
        "suggested:",
        "    Chose Postgres,",
        "    because of X.",
        "suggested tags: database, search",
        "Fix the entry, or pass --force to store it as written.",
    ]
    assert cli.query() == []


def test_cli_repeat_names_the_memory_it_repeats(refusing):
    url, token, fake = refusing
    fake.verdict = APPROVE
    cli = CliDriver(url, token)
    assert cli.add("the original", project="p") == 1
    fake.verdict = REPEAT
    proc = cli.raw("add", "the original, said again", "--project", "p")
    assert proc.returncode == 4
    assert proc.stdout.splitlines()[0] == "✗ Review: reject, duplicate of #1: Says the same as #1."


def test_cli_force_stores_and_the_review_follows(refusing):
    url, token, fake = refusing
    cli = CliDriver(url, token)
    proc = cli.raw("add", "Spent the afternoon tidying.", "--project", "p", "--force")
    assert proc.returncode == 0, proc.stderr
    assert "✓ Memory #1 added (tester)" in proc.stdout
    assert _wait_for_rows(1) == [(1, "reject", "fake-reviewer")]
    assert "review: reject, rule 2:" in cli.raw("show", "1").stdout


def test_cli_approve_stores_and_shows_the_verdict(refusing):
    url, token, fake = refusing
    fake.verdict = APPROVE
    cli = CliDriver(url, token)
    proc = cli.raw("add", "Chose Postgres, because of X.", "--project", "p")
    assert proc.returncode == 0, proc.stderr
    assert "✓ Memory #1 added (tester)" in proc.stdout
    assert "review: approve: A decision with its reason." in cli.raw("show", "1").stdout
    assert len(fake.calls) == 1


def test_mcp_add_returns_the_review_error_shape(refusing):
    url, token, fake = refusing
    mcp = McpDriver(url, token)
    out = mcp._call("memory_add", content="Spent the afternoon tidying.", project="p",
                    agent="tester")
    assert out == {"error": "review", "verdict": "reject", "rule": 2,
                   "explanation": "A diary line: it says what was done, not why.",
                   "rewrite": None, "tags": [], "duplicate_of": None}
    assert mcp.query() == []

    fake.verdict = REWRITE
    out = mcp._call("memory_add", content="Chose Postgres.", project="p", agent="tester")
    assert out == {"error": "review", "verdict": "rewrite", "rule": 3,
                   "explanation": "Say why.", "rewrite": "Chose Postgres,\nbecause of X.",
                   "tags": ["database", "search"], "duplicate_of": None}

    forced = mcp._call("memory_add", content="Chose Postgres.", project="p", agent="tester",
                       force=True)
    assert set(forced) == {"id", "warnings"}


def test_api_client_raises_review_refused_with_every_field(refusing):
    url, token, fake = refusing
    api = ApiClient(url, token)
    fake.verdict = REWRITE
    with pytest.raises(ReviewRefused) as caught:
        api.add("Chose Postgres.", "tester", "p", [], "decision")
    e = caught.value
    assert (e.verdict, e.rule, e.explanation) == ("rewrite", 3, "Say why.")
    assert e.rewrite == "Chose Postgres,\nbecause of X."
    assert e.tags == ["database", "search"]
    assert e.duplicate_of is None
    assert str(e) == "review: rewrite: Say why."
    assert api.query(project="p") == []
    # force skips the review on the client side too.
    assert api.add("Chose Postgres.", "tester", "p", [], "decision", force=True) == 1


def test_api_client_keeps_other_422s_as_they_were(refusing):
    url, token, _ = refusing
    api = ApiClient(url, token)
    # A bad type is refused by the request model, not by the review.
    with pytest.raises(RuntimeError) as caught:
        api.add("anything", "tester", "p", [], "feedback")
    assert not isinstance(caught.value, ReviewRefused)
    assert "HTTP 422" in str(caught.value)


# ── the real model, once ─────────────────────────────────────────────────────
@pytest.mark.review
async def test_real_model_in_refuse_mode_mode_refuses_a_diary_line_and_stores_a_decision():
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
    async with _App(reviewer=reviewer, review_mode="refuse") as a:
        t0 = time.perf_counter()
        first = await _post(a, diary, project="agent-memory", type="note", tags=["work-log"])
        diary_s = time.perf_counter() - t0
        stored_after_diary = await _count(a)
        t0 = time.perf_counter()
        second = await _post(a, decision, project="agent-memory", type="decision",
                             tags=["database"])
        decision_s = time.perf_counter() - t0
        print(f"\n[review] {model} at {url}, refuse: diary {diary_s:.1f}s -> "
              f"{first.status_code} {first.json()}; decision {decision_s:.1f}s -> "
              f"{second.status_code} {second.json()}")

        assert first.status_code == 422, first.text
        detail = first.json()["detail"]
        assert detail["reason"] == "review"
        assert detail["verdict"] == "reject" and detail["rule"] == 2
        assert detail["explanation"]
        assert stored_after_diary == 0

        assert second.status_code == 201, second.text
        row = await a.get(second.json()["id"])
        assert row["content"] == decision
        assert row["review"]["verdict"] == "approve"
        assert row["review"]["rule"] is None
        assert await _count(a) == 1
