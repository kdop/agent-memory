"""Tests for the model-based review on add (issue #31).

Three layers. The review module on its own: the rules, the verdict parser,
`make_reviewer` and the Ollama request, with `urlopen` replaced so no server
is needed. The app with a `FakeReviewer`: the verdict is stored after an add
and shown with the memory, review is off by default, a failing reviewer costs
nothing, the neighbours are the nearest by vector in the project, and the
re-review route replaces the row. The CLI line, against the live server. The
real model runs once, under the `review` marker, only when
AGENT_MEMORY_REVIEW_URL names a server that answers.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from agent_memory.server import repository as repo
from agent_memory.server import review as review_mod
from agent_memory.server.app import create_app
from agent_memory.server.db import make_sessionmaker
from agent_memory.server.embedding import NullEmbedder, cosine
from agent_memory.server.models import MemoryReview
from agent_memory.server.review import (
    RULES,
    NullReviewer,
    OllamaReviewer,
    Verdict,
    make_reviewer,
    parse_verdict,
)
from conftest import TOKEN, FakeEmbedder, make_test_engine
from drivers import CliDriver

SKILL_FILE = Path(__file__).resolve().parent.parent / "skills" / "memory" / "SKILL.md"

REJECT = Verdict("reject", 2, "A diary line: it says what was done, not why.", None, None)
APPROVE = Verdict("approve", None, "A decision with its reason.", None, None)


class FakeReviewer:
    """Answers with a fixed verdict and records what it was asked."""

    model_name = "fake-reviewer"

    def __init__(self, verdict=REJECT):
        self.verdict = verdict
        self.calls = []

    def review(self, memory, neighbours):
        self.calls.append((memory, neighbours))
        return self.verdict


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


# ── the rules ────────────────────────────────────────────────────────────────
def test_rules_match_the_skill_file_word_for_word():
    # The server never reads the skill file; this test does, so the copy in
    # review.py cannot drift from the source.
    text = SKILL_FILE.read_text(encoding="utf-8")
    front_matter = text.split("---", 2)[1]
    found = re.findall(r"^\s+(\d+)\. (.+)$", front_matter, re.M)
    assert [(int(n), rule) for n, rule in found] == list(RULES)
    assert len(RULES) == 5


def test_system_prompt_lists_every_rule_and_the_answer_shape():
    for n, rule in RULES:
        assert f"{n}. {rule}" in review_mod.SYSTEM_PROMPT
    for key in ("verdict", "rule", "reason", "rewrite", "duplicate_of"):
        assert f'"{key}"' in review_mod.SYSTEM_PROMPT


# ── parse_verdict ────────────────────────────────────────────────────────────
def _answer(**changes):
    base = {"verdict": "reject", "rule": 2, "reason": "diary", "rewrite": None,
            "duplicate_of": None}
    base.update(changes)
    return json.dumps(base)


def test_parse_verdict_reads_the_full_shape():
    v = parse_verdict(_answer(verdict="rewrite", rule=3, rewrite="better text"))
    assert v == Verdict("rewrite", 3, "diary", "better text", None)
    assert v.as_dict() == {"verdict": "rewrite", "rule": 3, "reason": "diary",
                           "rewrite": "better text", "duplicate_of": None}


def test_parse_verdict_keeps_a_duplicate_of_a_listed_neighbour():
    v = parse_verdict(_answer(rule=None, duplicate_of=4), neighbour_ids=[3, 4])
    assert v.duplicate_of == 4 and v.rule is None


def test_parse_verdict_strips_the_reason():
    assert parse_verdict(_answer(reason="  padded  ")).reason == "padded"


@pytest.mark.parametrize("text", [
    "not json at all",
    "",
    "[1, 2]",
    '"a string"',
    json.dumps({"verdict": "reject", "rule": 2, "reason": "diary"}),      # keys missing
    _answer(verdict="maybe"),                                            # unknown verdict
    _answer(verdict="REJECT"),
    _answer(rule="2"),                                                   # rule not a number
    _answer(rule=9),                                                     # no such rule
    _answer(rule=True),
    _answer(reason=""),
    _answer(reason=None),
    _answer(rewrite=5),
    _answer(duplicate_of="4"),
])
def test_parse_verdict_rejects_bad_answers(text):
    assert parse_verdict(text, neighbour_ids=[3, 4]) is None


def test_parse_verdict_rejects_a_duplicate_of_an_entry_not_shown():
    assert parse_verdict(_answer(duplicate_of=99), neighbour_ids=[3, 4]) is None


def test_parse_verdict_without_neighbour_ids_accepts_any_duplicate_id():
    assert parse_verdict(_answer(duplicate_of=99)).duplicate_of == 99


# ── NullReviewer and make_reviewer ───────────────────────────────────────────
def test_null_reviewer_has_no_model_and_answers_nothing():
    null = NullReviewer("review is off")
    assert null.model_name is None
    assert null.reason == "review is off"
    assert null.review({"id": 1, "content": "x"}, []) is None


def test_make_reviewer_is_off_by_default(monkeypatch, caplog):
    for var in ("AGENT_MEMORY_REVIEW", "AGENT_MEMORY_REVIEW_URL"):
        monkeypatch.delenv(var, raising=False)
    with caplog.at_level(logging.INFO, logger="agent_memory.server.review"):
        r = make_reviewer()
    assert isinstance(r, NullReviewer)
    assert "AGENT_MEMORY_REVIEW=off" in r.reason
    assert len(caplog.records) == 1


def test_make_reviewer_warn_without_url_is_off(monkeypatch, caplog):
    monkeypatch.setenv("AGENT_MEMORY_REVIEW", "warn")
    monkeypatch.delenv("AGENT_MEMORY_REVIEW_URL", raising=False)
    with caplog.at_level(logging.INFO, logger="agent_memory.server.review"):
        r = make_reviewer()
    assert isinstance(r, NullReviewer)
    assert "AGENT_MEMORY_REVIEW_URL is not set" in r.reason
    assert len(caplog.records) == 1


def test_make_reviewer_unknown_mode_is_off(monkeypatch):
    monkeypatch.setenv("AGENT_MEMORY_REVIEW", "block")
    monkeypatch.setenv("AGENT_MEMORY_REVIEW_URL", "http://ollama:11434")
    r = make_reviewer()
    assert isinstance(r, NullReviewer)
    assert "'block'" in r.reason


def test_make_reviewer_warn_with_url_uses_the_defaults(monkeypatch):
    monkeypatch.setenv("AGENT_MEMORY_REVIEW", " Warn ")
    monkeypatch.setenv("AGENT_MEMORY_REVIEW_URL", "http://ollama:11434/")
    for var in ("AGENT_MEMORY_REVIEW_MODEL", "AGENT_MEMORY_REVIEW_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)
    r = make_reviewer()
    assert isinstance(r, OllamaReviewer)
    assert r.url == "http://ollama:11434"
    assert r.model_name == review_mod.DEFAULT_MODEL == "qwen3:14b"
    assert r.timeout == review_mod.DEFAULT_TIMEOUT == 30


def test_make_reviewer_reads_model_and_timeout(monkeypatch):
    monkeypatch.setenv("AGENT_MEMORY_REVIEW", "warn")
    monkeypatch.setenv("AGENT_MEMORY_REVIEW_URL", "http://ollama:11434")
    monkeypatch.setenv("AGENT_MEMORY_REVIEW_MODEL", "llama3:8b")
    monkeypatch.setenv("AGENT_MEMORY_REVIEW_TIMEOUT", "5")
    r = make_reviewer()
    assert r.model_name == "llama3:8b"
    assert r.timeout == 5.0


def test_make_reviewer_bad_timeout_falls_back_to_default(monkeypatch, caplog):
    monkeypatch.setenv("AGENT_MEMORY_REVIEW", "warn")
    monkeypatch.setenv("AGENT_MEMORY_REVIEW_URL", "http://ollama:11434")
    monkeypatch.setenv("AGENT_MEMORY_REVIEW_TIMEOUT", "soon")
    with caplog.at_level(logging.WARNING, logger="agent_memory.server.review"):
        r = make_reviewer()
    assert r.timeout == review_mod.DEFAULT_TIMEOUT
    assert any("soon" in rec.getMessage() for rec in caplog.records)


# ── OllamaReviewer, with urlopen replaced ────────────────────────────────────
MEMORY = {"id": 7, "project": "alpha", "type": "note", "tags": ["work-log"],
          "content": "Spent the afternoon tidying the config module."}
NEIGHBOURS = [
    {"id": 3, "project": "alpha", "type": "decision", "tags": ["db"],
     "content": "Chose Postgres because several agents write at once."},
    {"id": 5, "project": "alpha", "type": "lesson", "tags": [],
     "content": "The pool ran dry because a session was held open across a slow call."},
]


class _Response(io.BytesIO):
    """Just enough of an HTTP response for `urlopen`: bytes plus a context manager."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _fake_urlopen(monkeypatch, reply=None, raise_=None):
    """Replace `urlopen` with one that records the request and answers `reply`
    (the model's message text) or raises `raise_`. Returns the record."""
    seen = {}

    def urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["body"] = json.loads(req.data.decode("utf-8"))
        seen["timeout"] = timeout
        if raise_ is not None:
            raise raise_
        payload = {"model": "qwen3:14b", "message": {"role": "assistant", "content": reply},
                   "done": True}
        return _Response(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return seen


def test_request_body_has_the_fixed_settings():
    body = OllamaReviewer("http://ollama:11434", "qwen3:14b").request_body(MEMORY, NEIGHBOURS)
    assert body["model"] == "qwen3:14b"
    assert body["stream"] is False
    assert body["format"] == "json"
    assert body["think"] is False
    assert body["options"] == {"num_ctx": 8192, "temperature": 0}
    system, user = body["messages"]
    assert system["role"] == "system" and user["role"] == "user"
    for n, rule in RULES:
        assert f"{n}. {rule}" in system["content"]


def test_request_body_lays_out_the_memory_and_its_neighbours():
    body = OllamaReviewer("http://ollama:11434").request_body(MEMORY, NEIGHBOURS)
    user = body["messages"][1]["content"]
    assert "id: 7" in user and "project: alpha" in user and "type: note" in user
    assert "tags: work-log" in user
    assert MEMORY["content"] in user
    assert "closest in meaning first" in user
    assert "id: 3" in user and "id: 5" in user
    assert NEIGHBOURS[0]["content"] in user and NEIGHBOURS[1]["content"] in user
    # Order is kept: the nearest neighbour comes first.
    assert user.index("id: 3") < user.index("id: 5")


def test_request_body_says_when_there_are_no_neighbours():
    body = OllamaReviewer("http://ollama:11434").request_body(MEMORY, [])
    user = body["messages"][1]["content"]
    assert "no existing entries" in user
    assert "closest in meaning" not in user


def test_ollama_reviewer_posts_to_api_chat_and_returns_the_verdict(monkeypatch):
    seen = _fake_urlopen(monkeypatch, reply=_answer(rule=2))
    r = OllamaReviewer("http://ollama:11434/", "qwen3:14b", timeout=12)
    v = r.review(MEMORY, NEIGHBOURS)
    assert v == Verdict("reject", 2, "diary", None, None)
    assert seen["url"] == "http://ollama:11434/api/chat"
    assert seen["method"] == "POST"
    assert seen["timeout"] == 12
    assert seen["body"] == r.request_body(MEMORY, NEIGHBOURS)


def test_ollama_reviewer_unreachable_gives_none_and_one_log_line(monkeypatch, caplog):
    _fake_urlopen(monkeypatch, raise_=urllib.error.URLError("connection refused"))
    with caplog.at_level(logging.WARNING, logger="agent_memory.server.review"):
        assert OllamaReviewer("http://ollama:11434").review(MEMORY, NEIGHBOURS) is None
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "review of memory #7 failed" in message and "connection refused" in message


def test_ollama_reviewer_timeout_gives_none_and_one_log_line(monkeypatch, caplog):
    _fake_urlopen(monkeypatch, raise_=TimeoutError("timed out"))
    with caplog.at_level(logging.WARNING, logger="agent_memory.server.review"):
        assert OllamaReviewer("http://ollama:11434").review(MEMORY, NEIGHBOURS) is None
    assert len(caplog.records) == 1
    assert "timed out" in caplog.records[0].getMessage()


def test_ollama_reviewer_prose_answer_gives_none_and_one_log_line(monkeypatch, caplog):
    _fake_urlopen(monkeypatch, reply="I think this entry is a diary line, so reject it.")
    with caplog.at_level(logging.WARNING, logger="agent_memory.server.review"):
        assert OllamaReviewer("http://ollama:11434").review(MEMORY, NEIGHBOURS) is None
    assert len(caplog.records) == 1
    assert "no usable JSON" in caplog.records[0].getMessage()


def test_ollama_reviewer_duplicate_of_an_unlisted_entry_is_bad_json(monkeypatch):
    _fake_urlopen(monkeypatch, reply=_answer(rule=None, duplicate_of=42))
    assert OllamaReviewer("http://ollama:11434").review(MEMORY, NEIGHBOURS) is None


# ── the app, with a FakeReviewer ─────────────────────────────────────────────
class _App:
    """An in-process app over the test database, driven through httpx's
    ASGITransport. That transport waits for the whole request, background task
    included, so a verdict is stored by the time `post` returns."""

    def __init__(self, reviewer=None, embedder=None, **kwargs):
        self.engine = make_test_engine()
        self.app = create_app(sessionmaker=make_sessionmaker(self.engine), token=TOKEN,
                              embedder=embedder if embedder is not None else FakeEmbedder(),
                              reviewer=reviewer, **kwargs)

    async def __aenter__(self):
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                        base_url="http://testserver", headers=_auth())
        return self

    async def __aexit__(self, *exc):
        await self.client.aclose()
        await self.engine.dispose()

    async def add(self, content, project="alpha", type=None, tags=(), **params):
        resp = await self.client.post("/memories", params=params, json={
            "content": content, "project": project, "type": type, "agent": "tester",
            "tags": [{"name": t} for t in tags]})
        assert resp.status_code == 201, resp.text
        return resp.json()

    async def get(self, mid):
        resp = await self.client.get(f"/memories/{mid}")
        assert resp.status_code == 200, resp.text
        return resp.json()


async def _review_rows():
    engine = make_test_engine()
    try:
        async with engine.connect() as conn:
            stmt = select(MemoryReview.memory_id, MemoryReview.verdict, MemoryReview.model)
            return (await conn.execute(stmt.order_by(MemoryReview.memory_id))).all()
    finally:
        await engine.dispose()


async def test_add_stores_the_verdict_and_every_read_shows_it():
    fake = FakeReviewer(REJECT)
    async with _App(reviewer=fake) as a:
        body = await a.add("Spent the afternoon tidying the config module.", type="note")
        # The add response is as before: the id and the warnings, nothing about the review.
        assert set(body) == {"id", "warnings"}
        mid = body["id"]

        expected = REJECT.as_dict()
        assert (await a.get(mid))["review"] == expected
        listed = (await a.client.get("/memories", params={"project": "alpha"})).json()
        assert listed[0]["review"] == expected
        found = (await a.client.get("/memories/search", params={"q": "config"})).json()
        assert found[0]["review"] == expected
        assert await _review_rows() == [(mid, "reject", "fake-reviewer")]

        # The reviewer saw the memory as stored, with its id, project, type and tags.
        memory, neighbours = fake.calls[-1]
        assert memory["id"] == mid and memory["project"] == "alpha" and memory["type"] == "note"
        assert memory["content"] == "Spent the afternoon tidying the config module."
        assert neighbours == []


async def test_review_is_off_by_default(monkeypatch):
    for var in ("AGENT_MEMORY_REVIEW", "AGENT_MEMORY_REVIEW_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGENT_MEMORY_EMBED_MODEL", "off")
    engine = make_test_engine()
    app = create_app(sessionmaker=make_sessionmaker(engine), token=TOKEN)
    try:
        async with app.router.lifespan_context(app):
            assert isinstance(app.state.reviewer, NullReviewer)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                         headers=_auth()) as c:
                mid = (await c.post("/memories", json={"content": "off by default",
                                                        "agent": "t"})).json()["id"]
                assert (await c.get(f"/memories/{mid}")).json()["review"] is None
        assert await _review_rows() == []
    finally:
        await engine.dispose()


async def test_null_reviewer_is_never_asked():
    class CountingNull(NullReviewer):
        calls = 0

        def review(self, memory, neighbours):
            CountingNull.calls += 1
            return None

    async with _App(reviewer=CountingNull()) as a:
        mid = (await a.add("never reviewed"))["id"]
        assert (await a.get(mid))["review"] is None
    assert CountingNull.calls == 0
    assert await _review_rows() == []


async def test_app_without_a_reviewer_and_no_lifespan_still_adds():
    async with _App() as a:
        mid = (await a.add("no reviewer at all"))["id"]
        assert (await a.get(mid))["review"] is None


async def test_failing_reviewer_leaves_no_row_and_the_add_succeeds(caplog):
    class Broken(FakeReviewer):
        def review(self, memory, neighbours):
            raise RuntimeError("model blew up")

    with caplog.at_level(logging.WARNING, logger="agent_memory.server.app"):
        async with _App(reviewer=Broken()) as a:
            body = await a.add("still stored")
            assert (await a.get(body["id"]))["review"] is None
    assert await _review_rows() == []
    lines = [r.getMessage() for r in caplog.records]
    assert lines == [f"review of memory #{body['id']} failed: RuntimeError: model blew up"]


async def test_reviewer_answering_none_leaves_no_row():
    async with _App(reviewer=FakeReviewer(verdict=None)) as a:
        mid = (await a.add("no answer"))["id"]
        assert (await a.get(mid))["review"] is None
    assert await _review_rows() == []


async def test_neighbours_are_the_nearest_by_vector_in_the_project():
    fake = FakeReviewer(APPROVE)
    alpha = [f"alpha memory number {i} about topic {i}" for i in range(8)]
    async with _App(reviewer=fake) as a:
        ids = {}
        for text in alpha:
            ids[text] = (await a.add(text, project="alpha"))["id"]
        for text in ("beta memory one", "beta memory two"):
            await a.add(text, project="beta")
        new = "alpha memory number 3 about topic 3, said again"
        new_id = (await a.add(new, project="alpha"))["id"]

    memory, neighbours = fake.calls[-1]
    assert memory["id"] == new_id
    # Five of them, from the same project, the new one not among them, in
    # cosine order: the same order this test gets from the same vectors.
    emb = FakeEmbedder()
    vec = emb.embed([new])[0]
    by_cosine = sorted(alpha, key=lambda t: -cosine(vec, emb.embed([t])[0]))
    assert [n["id"] for n in neighbours] == [ids[t] for t in by_cosine[:5]]
    assert all(n["project"] == "alpha" for n in neighbours)
    assert new_id not in [n["id"] for n in neighbours]
    scores = [n["score"] for n in neighbours]
    assert scores == sorted(scores, reverse=True)
    # Each neighbour is a full memory: the model sees its type, tags and text.
    assert set(neighbours[0]) >= {"id", "project", "type", "tags", "content"}


async def test_neighbours_with_no_project_match_no_project_only():
    fake = FakeReviewer(APPROVE)
    async with _App(reviewer=fake) as a:
        bare = (await a.add("bare one", project=None))["id"]
        await a.add("in a project", project="alpha")
        await a.add("bare two", project=None)
    _, neighbours = fake.calls[-1]
    assert [n["id"] for n in neighbours] == [bare]


async def test_no_neighbours_without_the_embedding_model():
    fake = FakeReviewer(APPROVE)
    async with _App(reviewer=fake, embedder=NullEmbedder()) as a:
        await a.add("first", project="alpha")
        mid = (await a.add("second", project="alpha"))["id"]
        # The review still runs and is stored; it just had nothing to compare with.
        assert (await a.get(mid))["review"] == APPROVE.as_dict()
    memory, neighbours = fake.calls[-1]
    assert memory["id"] == mid
    assert neighbours == []


async def test_forced_add_is_reviewed_too():
    fake = FakeReviewer(REJECT)
    async with _App(reviewer=fake) as a:
        await a.add("dup me")
        forced = (await a.add("dup me", force="true"))["id"]
        assert (await a.get(forced))["review"] == REJECT.as_dict()
    assert len(fake.calls) == 2


async def test_re_review_replaces_the_row():
    fake = FakeReviewer(REJECT)
    async with _App(reviewer=fake) as a:
        mid = (await a.add("first take"))["id"]
        assert (await a.get(mid))["review"]["verdict"] == "reject"

        fake.verdict = Verdict("rewrite", 3, "Say why.", "First take, because of X.", None)
        resp = await a.client.post(f"/admin/review/{mid}")
        assert resp.status_code == 200
        assert resp.json() == fake.verdict.as_dict()
        assert (await a.get(mid))["review"] == fake.verdict.as_dict()
        assert await _review_rows() == [(mid, "rewrite", "fake-reviewer")]


async def test_re_review_keeps_the_old_row_when_the_model_gives_nothing():
    fake = FakeReviewer(REJECT)
    async with _App(reviewer=fake) as a:
        mid = (await a.add("first take"))["id"]
        fake.verdict = None
        resp = await a.client.post(f"/admin/review/{mid}")
        assert resp.status_code == 502
        assert f"no verdict for memory #{mid}" in resp.json()["detail"]
        assert (await a.get(mid))["review"] == REJECT.as_dict()


async def test_re_review_unknown_memory_404():
    async with _App(reviewer=FakeReviewer()) as a:
        resp = await a.client.post("/admin/review/999")
        assert resp.status_code == 404
        assert "#999" in resp.json()["detail"]


async def test_re_review_503_with_null_reviewer():
    async with _App(reviewer=NullReviewer("review is off (AGENT_MEMORY_REVIEW=off)")) as a:
        mid = (await a.add("anything"))["id"]
        resp = await a.client.post(f"/admin/review/{mid}")
        assert resp.status_code == 503
        assert "AGENT_MEMORY_REVIEW=off" in resp.json()["detail"]


async def test_re_review_needs_the_token():
    async with _App(reviewer=FakeReviewer()) as a:
        resp = await a.client.post("/admin/review/1", headers={"Authorization": ""})
        assert resp.status_code == 401


async def test_deleting_the_memory_deletes_its_review():
    async with _App(reviewer=FakeReviewer()) as a:
        mid = (await a.add("short lived"))["id"]
        assert await _review_rows() == [(mid, "reject", "fake-reviewer")]
        await a.client.request("DELETE", "/memories", params={"ids": [mid]})
    assert await _review_rows() == []


# ── the CLI line, against the live server ────────────────────────────────────
async def _plant_review(mid, verdict, model="planted"):
    """Write a verdict straight into the table. The live server runs without
    a reviewer, so this is how a memory there gets one."""
    engine = make_test_engine()
    try:
        async with make_sessionmaker(engine)() as session, session.begin():
            await repo.set_review(session, mid, verdict, model)
    finally:
        await engine.dispose()


def test_cli_show_and_query_print_one_review_line(live_server):
    url, token = live_server
    cli = CliDriver(url, token)
    assert "Memory #1 added" in cli.raw("add", "Spent the afternoon tidying.", "--project", "p").stdout
    assert "Memory #2 added" in cli.raw("add", "Nothing to say about this one.", "--project", "p").stdout
    asyncio.run(_plant_review(1, REJECT))

    shown = cli.raw("show", "1").stdout
    assert "review: reject, rule 2: A diary line: it says what was done, not why." in shown
    assert shown.count("review:") == 1
    # The line sits with the meta lines, before the content.
    assert shown.index("review:") < shown.index("Spent the afternoon")

    queried = cli.raw("query", "--project", "p").stdout
    assert queried.count("review:") == 1
    assert "review: reject, rule 2:" in queried
    # The parsed content is unchanged by the extra line.
    assert cli.get(1).content == "Spent the afternoon tidying."
    assert "review:" not in cli.raw("show", "2").stdout


def test_cli_prints_the_repeat_and_approve_forms(live_server):
    url, token = live_server
    cli = CliDriver(url, token)
    cli.raw("add", "the original", "--project", "p")
    cli.raw("add", "the original, again", "--project", "p", "--force")
    cli.raw("add", "a real decision, because of X", "--project", "p")
    asyncio.run(_plant_review(2, Verdict("reject", None, "Says the same as #1.", None, 1)))
    asyncio.run(_plant_review(3, APPROVE))
    assert "review: reject, duplicate of #1: Says the same as #1." in cli.raw("show", "2").stdout
    assert "review: approve: A decision with its reason." in cli.raw("show", "3").stdout


def test_api_client_passes_the_review_through(live_server):
    from agent_memory.client import ApiClient

    url, token = live_server
    api = ApiClient(url, token)
    mid = api.add("reviewed later", "tester", "p", [], None)
    assert api.get(mid)["review"] is None
    asyncio.run(_plant_review(mid, APPROVE))
    assert api.get(mid)["review"] == APPROVE.as_dict()
    assert api.query(project="p")[0]["review"] == APPROVE.as_dict()


# ── the real model, once ─────────────────────────────────────────────────────
def _real_server():
    """The Ollama server to test against, or None: unset, or no answer in 2 s."""
    url = os.environ.get("AGENT_MEMORY_REVIEW_URL", "").strip().rstrip("/")
    if not url:
        return None
    try:
        with urllib.request.urlopen(url + "/api/tags", timeout=2):
            return url
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


@pytest.mark.review
def test_real_model_rejects_a_diary_line_and_approves_a_decision():
    url = _real_server()
    if url is None:
        pytest.skip("AGENT_MEMORY_REVIEW_URL is unset or the server did not answer in 2 s")
    model = os.environ.get("AGENT_MEMORY_REVIEW_MODEL", "").strip() or review_mod.DEFAULT_MODEL
    # A long timeout: the first call may have to load the model into memory.
    reviewer = OllamaReviewer(url, model, timeout=180)
    neighbours = [{"id": 3, "project": "agent-memory", "type": "decision", "tags": ["search"],
                   "content": "Use a plain REAL[] column for vectors and compare in Python, "
                              "because the database host has no pgvector and the table is small."}]
    diary = {"id": 7, "project": "agent-memory", "type": "note", "tags": ["work-log"],
             "content": "Spent the afternoon reading through the config module and tidied "
                        "up a few of its helper functions."}
    decision = {"id": 8, "project": "agent-memory", "type": "decision", "tags": ["database"],
                "content": "Chose Postgres over SQLite for the memory store because several "
                           "agents write at the same time and SQLite locks the whole file on "
                           "every write; SQLite was rejected for that reason."}

    t0 = time.perf_counter()
    first = reviewer.review(diary, neighbours)
    diary_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    second = reviewer.review(decision, neighbours)
    decision_s = time.perf_counter() - t0
    print(f"\n[review] {model} at {url}: diary {diary_s:.1f}s -> {first}; "
          f"decision {decision_s:.1f}s -> {second}")

    assert first is not None and first.verdict == "reject" and first.rule == 2
    assert first.reason
    assert second is not None and second.verdict == "approve"
    assert second.rule is None and second.rewrite is None and second.duplicate_of is None
