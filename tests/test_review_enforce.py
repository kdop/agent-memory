"""Tests for the review enforce mode (issue #34).

With AGENT_MEMORY_REVIEW=enforce the model reads each new entry before it is
stored. A reject or rewrite verdict refuses the write with a 422 that carries
the verdict, the rule, the model's explanation and, for a rewrite, the
suggested text and tags; nothing is stored. An approve stores the memory and
its verdict in one go. No verdict (model unreachable, timeout, bad JSON)
stores the memory as warn mode would, with no row and one log line: an
absent model never blocks a write. `force=true` skips the review as it
skips the duplicate check; the review then runs in the background.

Same layers as test_review.py: the settings, the app with a `FakeReviewer`,
the CLI and MCP surfaces against a live server in enforce mode, and the real
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
from conftest import TOKEN, FakeEmbedder, _free_port, make_test_engine
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
    for raw, mode in (("off", "off"), ("warn", "warn"), ("enforce", "enforce"),
                      (" Enforce ", "enforce"), ("", "off"), ("block", "off")):
        monkeypatch.setenv("AGENT_MEMORY_REVIEW", raw)
        assert review_mode() == mode
    monkeypatch.delenv("AGENT_MEMORY_REVIEW")
    assert review_mode() == "off"
    assert review_mod.MODES == ("off", "warn", "enforce")


def test_make_reviewer_enforce_with_url_builds_the_reviewer(monkeypatch, caplog):
    monkeypatch.setenv("AGENT_MEMORY_REVIEW", "enforce")
    monkeypatch.setenv("AGENT_MEMORY_REVIEW_URL", "http://ollama:11434")
    with caplog.at_level(logging.INFO, logger="agent_memory.server.review"):
        r = make_reviewer()
    assert isinstance(r, OllamaReviewer)
    assert r.url == "http://ollama:11434"
    assert "review is on (enforce)" in caplog.records[-1].getMessage()


def test_make_reviewer_enforce_without_url_is_off(monkeypatch):
    monkeypatch.setenv("AGENT_MEMORY_REVIEW", "enforce")
    monkeypatch.delenv("AGENT_MEMORY_REVIEW_URL", raising=False)
    r = make_reviewer()
    assert isinstance(r, NullReviewer)
    assert "AGENT_MEMORY_REVIEW=enforce" in r.reason
    assert "AGENT_MEMORY_REVIEW_URL is not set" in r.reason


def test_make_reviewer_unknown_mode_names_the_three_values(monkeypatch):
    monkeypatch.setenv("AGENT_MEMORY_REVIEW", "block")
    monkeypatch.setenv("AGENT_MEMORY_REVIEW_URL", "http://ollama:11434")
    r = make_reviewer()
    assert isinstance(r, NullReviewer)
    assert "'block'" in r.reason and "'enforce'" in r.reason


# ── the app in enforce mode, with a FakeReviewer ─────────────────────────────
async def test_reject_refuses_with_the_exact_body_and_stores_nothing():
    fake = FakeReviewer(REJECT)
    async with _App(reviewer=fake, review_mode="enforce") as a:
        assert a.app.state.review_mode == "enforce"
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
    async with _App(reviewer=FakeReviewer(APPROVE), review_mode="enforce") as a:
        assert (await _post(a, "the original")).status_code == 201
        a.app.state.reviewer.verdict = REPEAT
        resp = await _post(a, "the original, said again")
        assert resp.status_code == 422
        assert resp.json() == _refusal(REPEAT)
        assert resp.json()["detail"]["rule"] is None
        assert resp.json()["detail"]["duplicate_of"] == 1
        assert await _count(a) == 1


async def test_rewrite_refuses_with_the_suggested_text_and_tags():
    async with _App(reviewer=FakeReviewer(REWRITE), review_mode="enforce") as a:
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
    async with _App(reviewer=fake, review_mode="enforce") as a:
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
        async with _App(reviewer=fake, review_mode="enforce") as a:
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
        async with _App(reviewer=Broken(), review_mode="enforce") as a:
            resp = await _post(a, "still stored")
            assert resp.status_code == 201
            assert (await a.get(resp.json()["id"]))["review"] is None
    assert await _review_rows() == []
    lines = [r.getMessage() for r in caplog.records]
    assert lines == ["review of a new memory failed: RuntimeError: model blew up"]


async def test_force_stores_and_the_row_comes_from_the_background_review():
    fake = FakeReviewer(REJECT)
    async with _App(reviewer=fake, review_mode="enforce") as a:
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
    async with _App(reviewer=fake, review_mode="enforce") as a:
        assert (await _post(a, "dup me")).status_code == 201
        resp = await _post(a, "dup me")
        assert resp.status_code == 409
        assert resp.json()["detail"]["reason"] == "duplicate"
    # The second add never reached the model.
    assert len(fake.calls) == 1


async def test_neighbours_and_tags_are_found_before_the_write():
    fake = FakeReviewer(APPROVE)
    alpha = [f"alpha memory number {i} about topic {i}" for i in range(8)]
    async with _App(reviewer=fake, review_mode="enforce") as a:
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
    async with _App(reviewer=fake, embedder=NullEmbedder(), review_mode="enforce") as a:
        await _post(a, "first", force="true")
        resp = await _post(a, "second")
        assert resp.status_code == 422
    assert fake.calls[-1][0]["content"] == "second"
    assert fake.calls[-1][1] == []


async def test_the_refusal_needs_the_token():
    async with _App(reviewer=FakeReviewer(REJECT), review_mode="enforce") as a:
        resp = await a.client.post("/memories", headers={"Authorization": ""},
                                   json={"content": "x", "agent": "t"})
        assert resp.status_code == 401


# ── off and warn are untouched ───────────────────────────────────────────────
async def test_off_with_a_null_reviewer_stores_and_asks_nothing():
    class CountingNull(NullReviewer):
        calls = 0

        def review(self, memory, neighbours, tags=()):
            CountingNull.calls += 1
            return None

    async with _App(reviewer=CountingNull(), review_mode="enforce") as a:
        assert a.app.state.review_mode == "off"
        resp = await _post(a, "Spent the afternoon tidying.")
        assert resp.status_code == 201
        assert (await a.get(resp.json()["id"]))["review"] is None
    assert CountingNull.calls == 0
    assert await _review_rows() == []


async def test_warn_still_stores_first_and_reviews_after():
    fake = FakeReviewer(REJECT)
    async with _App(reviewer=fake, review_mode="warn") as a:
        assert a.app.state.review_mode == "warn"
        resp = await _post(a, "Spent the afternoon tidying.")
        assert resp.status_code == 201
        mid = resp.json()["id"]
        assert (await a.get(mid))["review"] == REJECT.as_dict()
    assert await _review_rows() == [(mid, "reject", "fake-reviewer")]
    # The model saw the stored memory, id and all.
    assert fake.calls[0][0]["id"] == mid


async def test_a_reviewer_without_a_mode_runs_in_warn_mode():
    async with _App(reviewer=FakeReviewer(REJECT)) as a:
        assert a.app.state.review_mode == "warn"
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


async def test_enforce_in_the_environment_without_a_server_is_off(monkeypatch):
    reviewer, mode = await _with_lifespan(monkeypatch, {"AGENT_MEMORY_REVIEW": "enforce"})
    assert isinstance(reviewer, NullReviewer)
    assert mode == "off"


async def test_enforce_in_the_environment_with_a_server_is_enforce(monkeypatch):
    reviewer, mode = await _with_lifespan(monkeypatch, {
        "AGENT_MEMORY_REVIEW": "enforce", "AGENT_MEMORY_REVIEW_URL": "http://ollama:11434"})
    assert isinstance(reviewer, OllamaReviewer)
    assert mode == "enforce"


async def test_warn_in_the_environment_is_warn(monkeypatch):
    _, mode = await _with_lifespan(monkeypatch, {
        "AGENT_MEMORY_REVIEW": "warn", "AGENT_MEMORY_REVIEW_URL": "http://ollama:11434"})
    assert mode == "warn"


async def test_off_by_default_even_with_a_server(monkeypatch):
    reviewer, mode = await _with_lifespan(monkeypatch, {
        "AGENT_MEMORY_REVIEW_URL": "http://ollama:11434"})
    assert isinstance(reviewer, NullReviewer)
    assert mode == "off"


async def test_an_injected_reviewer_ignores_off_in_the_environment(monkeypatch):
    # Tests hand the app a reviewer; the environment says off. It is used, in warn mode.
    _, mode = await _with_lifespan(monkeypatch, {"AGENT_MEMORY_REVIEW": "off"},
                                   reviewer=FakeReviewer())
    assert mode == "warn"
    _, mode = await _with_lifespan(monkeypatch, {"AGENT_MEMORY_REVIEW": "enforce"},
                                   reviewer=FakeReviewer())
    assert mode == "enforce"


# ── the CLI, the MCP tool and the client, against a live server in enforce mode
@pytest.fixture(scope="module")
def enforcing_server(_schema):
    """A live server with a `FakeReviewer` in enforce mode. Yields
    `(url, token, reviewer)`; a test sets `reviewer.verdict` as it needs."""
    import uvicorn

    fake = FakeReviewer(REJECT)
    engine = make_test_engine()
    app = create_app(sessionmaker=make_sessionmaker(engine), token=TOKEN,
                     embedder=FakeEmbedder(), reviewer=fake, review_mode="enforce",
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
def enforcing(enforcing_server):
    """The server above with its reviewer reset to a reject for this test."""
    url, token, fake = enforcing_server
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


def test_cli_reject_prints_the_verdict_and_exits_4(enforcing):
    url, token, _ = enforcing
    cli = CliDriver(url, token)
    proc = cli.raw("add", "Spent the afternoon tidying.", "--project", "p")
    assert proc.returncode == 4
    assert proc.stdout.splitlines() == [
        "✗ Review: reject, rule 2: A diary line: it says what was done, not why.",
        "Fix the entry, or pass --force to store it as written.",
    ]
    assert "Traceback" not in proc.stderr
    assert cli.raw("query").stdout.strip() == "No memories found."


def test_cli_rewrite_prints_the_suggestion_like_show_does(enforcing):
    url, token, fake = enforcing
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


def test_cli_repeat_names_the_memory_it_repeats(enforcing):
    url, token, fake = enforcing
    fake.verdict = APPROVE
    cli = CliDriver(url, token)
    assert cli.add("the original", project="p") == 1
    fake.verdict = REPEAT
    proc = cli.raw("add", "the original, said again", "--project", "p")
    assert proc.returncode == 4
    assert proc.stdout.splitlines()[0] == "✗ Review: reject, duplicate of #1: Says the same as #1."


def test_cli_force_stores_and_the_review_follows(enforcing):
    url, token, fake = enforcing
    cli = CliDriver(url, token)
    proc = cli.raw("add", "Spent the afternoon tidying.", "--project", "p", "--force")
    assert proc.returncode == 0, proc.stderr
    assert "✓ Memory #1 added (tester)" in proc.stdout
    assert _wait_for_rows(1) == [(1, "reject", "fake-reviewer")]
    assert "review: reject, rule 2:" in cli.raw("show", "1").stdout


def test_cli_approve_stores_and_shows_the_verdict(enforcing):
    url, token, fake = enforcing
    fake.verdict = APPROVE
    cli = CliDriver(url, token)
    proc = cli.raw("add", "Chose Postgres, because of X.", "--project", "p")
    assert proc.returncode == 0, proc.stderr
    assert "✓ Memory #1 added (tester)" in proc.stdout
    assert "review: approve: A decision with its reason." in cli.raw("show", "1").stdout
    assert len(fake.calls) == 1


def test_mcp_add_returns_the_review_error_shape(enforcing):
    url, token, fake = enforcing
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


def test_api_client_raises_review_refused_with_every_field(enforcing):
    url, token, fake = enforcing
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


def test_api_client_keeps_other_422s_as_they_were(enforcing):
    url, token, _ = enforcing
    api = ApiClient(url, token)
    # A bad type is refused by the request model, not by the review.
    with pytest.raises(RuntimeError) as caught:
        api.add("anything", "tester", "p", [], "feedback")
    assert not isinstance(caught.value, ReviewRefused)
    assert "HTTP 422" in str(caught.value)


# ── the real model, once ─────────────────────────────────────────────────────
@pytest.mark.review
async def test_real_model_in_enforce_mode_refuses_a_diary_line_and_stores_a_decision():
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
    async with _App(reviewer=reviewer, review_mode="enforce") as a:
        t0 = time.perf_counter()
        first = await _post(a, diary, project="agent-memory", type="note", tags=["work-log"])
        diary_s = time.perf_counter() - t0
        stored_after_diary = await _count(a)
        t0 = time.perf_counter()
        second = await _post(a, decision, project="agent-memory", type="decision",
                             tags=["database"])
        decision_s = time.perf_counter() - t0
        print(f"\n[review] {model} at {url}, enforce: diary {diary_s:.1f}s -> "
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
