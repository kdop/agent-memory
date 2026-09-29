"""The model-based review of new memories.

A language model reads each new memory with its nearest verified neighbours
and the tags closest to it, and says approve, reject or rewrite. The verdict
is kept (every one, newest shown) and sets the memory's `review_status`:
`unverified` until checked, then `verified` or `flagged`. In `flag` mode the
write never waits; in `refuse` mode a reject or rewrite refuses it with 422.
A verdict may say the entry supersedes an older one (a link on the new
memory) or repeats one and adds to it (a merge suggestion). Memories written
while the model was away are caught up later, by `POST /admin/review` or by
the poll that notices when the model is back.

Layers: the review module on its own (prompt, parser, settings, the Ollama
call with `urlopen` replaced); the app with a `FakeReviewer`, in process;
the poll; and the real model, under the `review` marker, only when
AGENT_MEMORY_REVIEW_URL names a server that answers. What the CLI, the MCP
tools and the client show of all this is in test_surfaces.py.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import get_args

import httpx
import pytest
from sqlalchemy import update

from agent_memory.client import REVIEW_STATUSES
from agent_memory.server import app as app_mod
from agent_memory.server import repository as repo
from agent_memory.server import review as review_mod
from agent_memory.server.app import (
    REVIEW_MODEL_STATES,
    ReviewStatus,
    _catch_up_lock,
    _review_poll,
    _review_tick,
)
from agent_memory.server.embedding import NullEmbedder, cosine
from agent_memory.server.models import Memory, MemoryReview
from agent_memory.server.review import (
    RULES,
    STATUSES,
    TAG_COUNT,
    NullReviewer,
    OllamaReviewer,
    Reviewer,
    Verdict,
    make_reviewer,
    parse_verdict,
    review_mode,
    review_poll,
    status_for,
)
from agent_memory.server.schemas import MemoryOut, ReviewOut
from conftest import (
    APPROVE,
    REJECT,
    REWRITE,
    App,
    FakeEmbedder,
    FakeReviewer,
    add_rows,
    db_session,
    make_test_engine,
    plant_review,
    real_review_server,
    review_rows,
    set_age,
    statuses,
    until,
    vector,
)

# test_verdict_quality.py imports the check for a real model server from here.
_real_server = real_review_server

SKILL_FILE = Path(__file__).resolve().parent.parent / "skills" / "memory" / "SKILL.md"
REPEAT = Verdict("reject", None, "Says the same as #1.", None, 1)
OLD = "Chose SQLite for the store because one agent writes at a time."
NEW = "Moved the store to Postgres because several agents write at once."
MERGED = "Chose Postgres, because several agents write at once; the pool holds 10 connections."
MERGE = Verdict("rewrite", None, "Repeats #1 and adds the pool size.", MERGED, 1, ["database"])


def supersede(old):
    return Verdict("approve", None, f"Reverses #{old} and says why.", None, None, [],
                   supersedes=old)


def _messages(caplog):
    return [r.getMessage() for r in caplog.records]


# ── the prompt ───────────────────────────────────────────────────────────────
def test_rules_match_the_skill_file_word_for_word():
    # The server never reads the skill file; this test does, so the copy in
    # review.py cannot drift from the source.
    front_matter = SKILL_FILE.read_text(encoding="utf-8").split("---", 2)[1]
    found = re.findall(r"^\s+(\d+)\. (.+)$", front_matter, re.M)
    assert [(int(n), rule) for n, rule in found] == list(RULES)
    assert len(RULES) == 5


def test_system_prompt_lists_the_rules_the_keys_and_the_two_cases():
    prompt = review_mod.SYSTEM_PROMPT
    for n, rule in RULES:
        assert f"{n}. {rule}" in prompt
    assert "exactly these nine keys" in prompt
    for key in ("verdict", "rule", "reason", "rewrite", "duplicate_of"):
        assert f'"{key}"' in prompt
    assert '"tags": [<names from the list only>]' in prompt
    assert "never a name that is not on the list" in prompt
    assert '"supersedes": <id or null>' in prompt
    # The two cases that look at the neighbours, each with one example.
    assert "reverses or replaces what a listed existing entry says" in prompt
    assert "approve, supersedes that entry's id" in prompt
    assert "adds something to it" in prompt and "merged text" in prompt
    assert "duplicate_of that entry's id, and rule null" in prompt
    assert prompt.count("Example: the listed entry says") == 2
    # The examples name no id, so the model cannot copy one that is not listed.
    assert not re.search(r"supersedes \d", prompt)
    assert not re.search(r"duplicate_of \d", prompt)
    # A reversal comes before the repeat checks, so it is never called a repeat.
    assert prompt.index("reverses or replaces what a listed") < \
        prompt.index("in other words, and nothing more")


MEMORY = {"id": 7, "project": "alpha", "type": "note", "tags": ["work-log"],
          "content": "Spent the afternoon tidying the config module."}
NEIGHBOURS = [
    {"id": 3, "project": "alpha", "type": "decision", "tags": ["db"],
     "content": "Chose Postgres because several agents write at once."},
    {"id": 5, "project": "alpha", "type": "lesson", "tags": [],
     "content": "The pool ran dry because a session was held open across a slow call."},
]
OFFERED = ["database", "search", "config"]


def test_request_body_has_the_fixed_settings_and_lays_out_the_entry():
    body = OllamaReviewer("http://ollama:11434", "qwen3:14b").request_body(
        MEMORY, NEIGHBOURS, OFFERED)
    assert (body["model"], body["stream"], body["format"], body["think"]) == (
        "qwen3:14b", False, "json", False)
    assert body["options"] == {"num_ctx": 8192, "temperature": 0}
    system, user = body["messages"]
    assert (system["role"], user["role"]) == ("system", "user")
    assert system["content"] == review_mod.SYSTEM_PROMPT
    user = user["content"]
    for part in ("id: 7", "project: alpha", "type: note", "tags: work-log", MEMORY["content"],
                 "closest in meaning first", NEIGHBOURS[0]["content"], NEIGHBOURS[1]["content"]):
        assert part in user
    # The nearest neighbour comes first.
    assert user.index("id: 3") < user.index("id: 5")
    line = next(l for l in user.splitlines() if l.startswith("Existing tags you may suggest"))
    assert line.endswith("closest in meaning first: database, search, config")
    assert "work-log" not in line


def test_request_body_says_when_there_are_no_neighbours_or_tags():
    user = OllamaReviewer("http://ollama:11434").request_body(MEMORY, [])["messages"][1]["content"]
    assert "no existing entries" in user
    assert "closest in meaning" not in user
    assert "There are no existing tags to suggest: tags must be []." in user
    assert "you may suggest" not in user


# ── the parser ───────────────────────────────────────────────────────────────
def _answer(**changes):
    base = {"verdict": "reject", "rule": 2, "reason": "diary", "rewrite": None,
            "duplicate_of": None}
    base.update(changes)
    return json.dumps(base)


def test_verdict_shape_and_defaults():
    v = parse_verdict(_answer(verdict="rewrite", rule=3, rewrite="better text"))
    assert v == Verdict("rewrite", 3, "diary", "better text", None)
    assert v.as_dict() == {"verdict": "rewrite", "rule": 3, "reason": "diary",
                           "rewrite": "better text", "duplicate_of": None, "tags": [],
                           "supersedes": None}
    assert MemoryOut(id=1).review_status == "unverified"
    assert MemoryOut(id=1).supersedes is None and MemoryOut(id=1).superseded_by is None
    assert ReviewOut(verdict="approve").supersedes is None


@pytest.mark.parametrize("answer, kwargs, expected", [
    # A duplicate or supersedes of a listed neighbour is kept.
    (_answer(rule=None, duplicate_of=4), {"neighbour_ids": [3, 4]},
     Verdict("reject", None, "diary", None, 4)),
    (_answer(verdict="approve", rule=None, supersedes=4), {"neighbour_ids": [3, 4]},
     Verdict("approve", None, "diary", None, None, supersedes=4)),
    # Without the list of neighbours any id goes.
    (_answer(duplicate_of=99), {}, Verdict("reject", 2, "diary", None, 99)),
    (_answer(verdict="approve", rule=None, supersedes=99), {},
     Verdict("approve", None, "diary", None, None, supersedes=99)),
    # A missing or null supersedes is None; the reason is stripped.
    (_answer(supersedes=None, reason="  padded  "), {"neighbour_ids": [3]},
     Verdict("reject", 2, "padded", None, None)),
    # A merge: rewrite with duplicate_of, tags kept to the offer.
    (_answer(verdict="rewrite", rule=None, rewrite="old plus new", duplicate_of=3,
             tags=["db", "made-up"]), {"neighbour_ids": [3], "offered_tags": ["db"]},
     Verdict("rewrite", None, "diary", "old plus new", 3, ["db"])),
    # Tags: only offered names, in the offered spelling, once each.
    (_answer(verdict="rewrite", rule=3, rewrite="better",
             tags=["Search", "made-up", "database", " search ", "database"]),
     {"offered_tags": OFFERED}, Verdict("rewrite", 3, "diary", "better", None,
                                        ["search", "database"])),
    # No tags unless the verdict is rewrite.
    (_answer(tags=["database"]), {"offered_tags": OFFERED},
     Verdict("reject", 2, "diary", None, None)),
    (_answer(verdict="approve", rule=None, tags=["database"]), {"offered_tags": OFFERED},
     Verdict("approve", None, "diary", None, None)),
    # An empty offer keeps nothing; no offer keeps the names as given.
    (_answer(verdict="rewrite", rule=3, rewrite="b", tags=["database"]), {"offered_tags": []},
     Verdict("rewrite", 3, "diary", "b", None)),
    (_answer(verdict="rewrite", rule=3, rewrite="b", tags=["a", "A", " b "]), {},
     Verdict("rewrite", 3, "diary", "b", None, ["a", "b"])),
    # A missing or null tags key is empty.
    (_answer(verdict="rewrite", rule=3, rewrite="b", tags=None), {"offered_tags": OFFERED},
     Verdict("rewrite", 3, "diary", "b", None)),
])
def test_parse_verdict_reads_a_good_answer(answer, kwargs, expected):
    assert parse_verdict(answer, **kwargs) == expected


@pytest.mark.parametrize("text", [
    "not json at all",
    "",
    "[1, 2]",
    '"a string"',
    json.dumps({"verdict": "reject", "rule": 2, "reason": "diary"}),      # keys missing
    _answer(verdict="maybe"),
    _answer(verdict="REJECT"),
    _answer(rule="2"),
    _answer(rule=9),
    _answer(rule=True),
    _answer(reason=""),
    _answer(reason=None),
    _answer(rewrite=5),
    _answer(duplicate_of="4"),
    _answer(duplicate_of=99),                                            # not listed
    _answer(verdict="approve", rule=None, supersedes=99),                # not listed
    _answer(verdict="approve", rule=None, supersedes=7),                 # the entry itself
    *[_answer(verdict="approve", rule=None, supersedes=v)
      for v in ("4", True, 4.0, [4], {"id": 4})],
    *[_answer(verdict="rewrite", rule=3, rewrite="better", tags=t)
      for t in ("database", 5, [1, 2], ["database", None], {"a": 1})],
])
def test_parse_verdict_rejects_a_bad_answer(text):
    assert parse_verdict(text, neighbour_ids=[3, 4], offered_tags=OFFERED) is None


# ── the constants and the settings ───────────────────────────────────────────
def test_the_statuses_and_the_status_of_each_verdict():
    assert STATUSES == ("unverified", "verified", "flagged")
    assert review_mod.UNVERIFIED == "unverified"
    # The client half cannot import the server, and the route validates with
    # a Literal: neither copy may drift.
    assert REVIEW_STATUSES == STATUSES
    assert get_args(ReviewStatus) == STATUSES
    assert [status_for(v) for v in ("approve", "reject", "rewrite")] == [
        "verified", "flagged", "flagged"]
    with pytest.raises(ValueError, match="maybe"):
        status_for("maybe")
    assert review_mod.MODES == ("off", "flag", "refuse")
    assert review_mod.OLD_MODE_NAMES == {"warn": "flag", "enforce": "refuse"}
    assert REVIEW_MODEL_STATES == ("off", "reachable", "unreachable")


def test_review_mode_reads_the_three_values_and_the_old_names(monkeypatch, caplog):
    monkeypatch.delenv("AGENT_MEMORY_REVIEW", raising=False)
    assert review_mode() == "off"
    for raw, mode in (("off", "off"), ("flag", "flag"), ("refuse", "refuse"),
                      (" Refuse ", "refuse"), ("", "off"), ("block", "off")):
        monkeypatch.setenv("AGENT_MEMORY_REVIEW", raw)
        with caplog.at_level(logging.WARNING, logger="agent_memory.server.review"):
            assert review_mode() == mode
    assert caplog.records == []
    # The old names still work, with one log line each time.
    for raw, mode in (("warn", "flag"), ("enforce", "refuse"), (" Enforce ", "refuse")):
        monkeypatch.setenv("AGENT_MEMORY_REVIEW", raw)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="agent_memory.server.review"):
            assert review_mode() == mode
        (line,) = _messages(caplog)
        assert raw.strip().lower() in line and mode in line


def test_review_poll_reads_the_seconds(monkeypatch, caplog):
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


@pytest.mark.parametrize("env, reason", [
    ({}, "AGENT_MEMORY_REVIEW=off"),
    ({"AGENT_MEMORY_REVIEW_URL": "http://ollama:11434"}, "AGENT_MEMORY_REVIEW=off"),
    ({"AGENT_MEMORY_REVIEW": "flag"}, "AGENT_MEMORY_REVIEW_URL is not set"),
    ({"AGENT_MEMORY_REVIEW": "refuse"}, "AGENT_MEMORY_REVIEW_URL is not set"),
    ({"AGENT_MEMORY_REVIEW": "enforce"}, "AGENT_MEMORY_REVIEW=enforce"),
    ({"AGENT_MEMORY_REVIEW": "block", "AGENT_MEMORY_REVIEW_URL": "http://ollama:11434"},
     "'block'"),
])
def test_make_reviewer_is_off_without_a_mode_and_a_url(monkeypatch, caplog, env, reason):
    for var in ("AGENT_MEMORY_REVIEW", "AGENT_MEMORY_REVIEW_URL"):
        monkeypatch.delenv(var, raising=False)
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    with caplog.at_level(logging.INFO, logger="agent_memory.server.review"):
        r = make_reviewer()
    assert isinstance(r, NullReviewer) and r.model_name is None
    assert reason in r.reason
    if "block" in reason:
        assert "'refuse'" in r.reason          # it names the values it knows
    assert r.review({"id": 1, "content": "x"}, []) is None
    assert r.reachable() is False


def test_make_reviewer_with_a_mode_and_a_url(monkeypatch, caplog):
    monkeypatch.setenv("AGENT_MEMORY_REVIEW", " Flag ")
    monkeypatch.setenv("AGENT_MEMORY_REVIEW_URL", "http://ollama:11434/")
    for var in ("AGENT_MEMORY_REVIEW_MODEL", "AGENT_MEMORY_REVIEW_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)
    r = make_reviewer()
    assert isinstance(r, OllamaReviewer) and r.url == "http://ollama:11434"
    assert r.model_name == review_mod.DEFAULT_MODEL == "qwen3:14b"
    assert r.timeout == review_mod.DEFAULT_TIMEOUT == 30

    monkeypatch.setenv("AGENT_MEMORY_REVIEW", "enforce")      # the old name for refuse
    monkeypatch.setenv("AGENT_MEMORY_REVIEW_MODEL", "llama3:8b")
    monkeypatch.setenv("AGENT_MEMORY_REVIEW_TIMEOUT", "5")
    with caplog.at_level(logging.INFO, logger="agent_memory.server.review"):
        r = make_reviewer()
    assert (r.model_name, r.timeout) == ("llama3:8b", 5.0)
    assert "review is on (refuse)" in _messages(caplog)[-1]

    monkeypatch.setenv("AGENT_MEMORY_REVIEW_TIMEOUT", "soon")
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="agent_memory.server.review"):
        assert make_reviewer().timeout == review_mod.DEFAULT_TIMEOUT
    assert any("soon" in m for m in _messages(caplog))


# ── OllamaReviewer, with urlopen replaced ────────────────────────────────────
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
        seen["url"] = req if isinstance(req, str) else req.full_url
        seen["timeout"] = timeout
        if not isinstance(req, str) and req.data:
            seen["method"] = req.get_method()
            seen["body"] = json.loads(req.data.decode("utf-8"))
        if raise_ is not None:
            raise raise_
        payload = {"model": "qwen3:14b", "message": {"role": "assistant", "content": reply},
                   "done": True}
        return _Response(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return seen


def test_ollama_reviewer_posts_to_api_chat_and_returns_the_verdict(monkeypatch):
    seen = _fake_urlopen(monkeypatch, reply=_answer(verdict="rewrite", rule=3, rewrite="better",
                                                    tags=["made-up", "config"]))
    r = OllamaReviewer("http://ollama:11434/", "qwen3:14b", timeout=12)
    assert r.review(MEMORY, NEIGHBOURS, OFFERED) == Verdict("rewrite", 3, "diary", "better",
                                                            None, ["config"])
    assert (seen["url"], seen["method"], seen["timeout"]) == (
        "http://ollama:11434/api/chat", "POST", 12)
    assert seen["body"] == r.request_body(MEMORY, NEIGHBOURS, OFFERED)


@pytest.mark.parametrize("reply, error, logged", [
    (None, urllib.error.URLError("connection refused"), "connection refused"),
    (None, TimeoutError("timed out"), "timed out"),
    ("I think this entry is a diary line, so reject it.", None, "no usable JSON"),
    (_answer(rule=None, duplicate_of=42), None, "no usable JSON"),               # not listed
    (_answer(verdict="approve", rule=None, supersedes=42), None, "no usable JSON"),
])
def test_ollama_reviewer_without_a_usable_answer_gives_none_and_one_log_line(
        monkeypatch, caplog, reply, error, logged):
    _fake_urlopen(monkeypatch, reply=reply, raise_=error)
    with caplog.at_level(logging.WARNING, logger="agent_memory.server.review"):
        assert OllamaReviewer("http://ollama:11434").review(MEMORY, NEIGHBOURS) is None
    (line,) = _messages(caplog)
    assert "review of memory #7 failed" in line and logged in line


def test_ollama_reviewer_keeps_a_supersedes_of_a_listed_neighbour(monkeypatch):
    _fake_urlopen(monkeypatch, reply=_answer(verdict="approve", rule=None, supersedes=3))
    assert OllamaReviewer("http://ollama:11434").review(MEMORY, NEIGHBOURS).supersedes == 3


def test_reachable_asks_api_tags_with_a_two_second_timeout(monkeypatch):
    seen = _fake_urlopen(monkeypatch, reply="{}")
    assert OllamaReviewer("http://ollama:11434/").reachable() is True
    assert (seen["url"], seen["timeout"]) == ("http://ollama:11434/api/tags", 2)
    assert review_mod.REACHABLE_TIMEOUT == 2
    for error in (urllib.error.URLError("refused"), TimeoutError("timed out"),
                  ConnectionResetError("reset")):
        _fake_urlopen(monkeypatch, raise_=error)
        assert OllamaReviewer("http://ollama:11434").reachable() is False
    for call in (lambda r: r.reachable(), lambda r: r.review(MEMORY, [])):
        with pytest.raises(NotImplementedError):
            call(Reviewer())


# ── flag mode: the verdict after the write ───────────────────────────────────
async def test_flag_mode_stores_the_verdict_and_every_read_shows_it():
    fake = FakeReviewer(REJECT)
    async with App(reviewer=fake) as a:
        assert a.app.state.review_mode == "flag"
        resp = await a.post("Spent the afternoon tidying the config module.", type="note",
                            tags=["work-log"])
        # The add response is as before: the id and the warnings.
        assert set(resp.json()) == {"id", "warnings"}
        mid = resp.json()["id"]
        # The model read the memory as stored, unverified at that point.
        memory, neighbours, _ = fake.calls[-1]
        assert (memory["id"], memory["project"], memory["type"], memory["tags"]) == (
            mid, "alpha", "note", ["work-log"])
        assert memory["review_status"] == "unverified" and neighbours == []

        row = await a.get(mid)
        assert row["review"] == REJECT.as_dict() and row["review_status"] == "flagged"
        listed = (await a.client.get("/memories")).json()
        found = (await a.client.get("/memories/search", params={"q": "config"})).json()
        flagged = (await a.client.get("/memories/flagged")).json()
        for rows in (listed, found, flagged):
            assert rows[0]["review"] == REJECT.as_dict()
            assert rows[0]["review_status"] == "flagged"
        # The same shape everywhere.
        assert set(row) == set(listed[0]) == set(found[0]) == set(flagged[0])
    assert await review_rows() == [(mid, "reject", "fake-reviewer")]


@pytest.mark.parametrize("verdict, status", [
    (APPROVE, "verified"), (REJECT, "flagged"), (REWRITE, "flagged"),
])
async def test_each_verdict_gives_its_status(verdict, status):
    async with App(reviewer=FakeReviewer(verdict)) as a:
        row = await a.get(await a.add("Chose Postgres for the store."))
        assert row["review_status"] == status and row["review"] == verdict.as_dict()
        # A suggestion is advice: the memory itself is never changed.
        assert row["content"] == "Chose Postgres for the store." and row["tags"] == []


async def test_review_is_off_by_default(monkeypatch):
    for var in ("AGENT_MEMORY_REVIEW", "AGENT_MEMORY_REVIEW_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGENT_MEMORY_EMBED_MODEL", "off")
    async with App(embedder=None) as a:
        async with a.app.router.lifespan_context(a.app):
            assert isinstance(a.app.state.reviewer, NullReviewer)
            assert a.app.state.review_mode == "off"
            row = await a.get(await a.add("off by default"))
            assert row["review"] is None and row["review_status"] == "unverified"
    assert await review_rows() == []


class CountingNull(NullReviewer):
    calls = 0

    def review(self, memory, neighbours, tags=()):
        CountingNull.calls += 1
        return None


@pytest.mark.parametrize("mode", ["flag", "refuse"])
async def test_a_null_reviewer_is_never_asked_and_no_reviewer_is_fine(mode):
    CountingNull.calls = 0
    for reviewer in (CountingNull(), None):
        async with App(reviewer=reviewer, review_mode=mode) as a:
            if reviewer is not None:
                assert a.app.state.review_mode == "off"
            row = await a.get(await a.add("Spent the afternoon tidying."))
            assert row["review"] is None and row["review_status"] == "unverified"
    assert CountingNull.calls == 0
    assert await review_rows() == []


@pytest.mark.parametrize("answer, line", [
    (None, None),
    (RuntimeError("model blew up"), "review of memory #1 failed: RuntimeError: model blew up"),
])
async def test_no_verdict_leaves_the_memory_unverified_with_no_row(caplog, answer, line):
    with caplog.at_level(logging.WARNING, logger="agent_memory.server.app"):
        async with App(reviewer=FakeReviewer(answer)) as a:
            row = await a.get(await a.add("still stored"))
            assert row["review"] is None and row["review_status"] == "unverified"
    assert await review_rows() == []
    assert _messages(caplog) == ([line] if line else [])


def _by_cosine(text, others):
    vec = vector(text)
    return sorted(others, key=lambda t: -cosine(vec, vector(t)))


async def test_the_neighbours_are_the_nearest_verified_memories_in_the_project():
    texts = [f"alpha memory number {i} about topic {i}" for i in range(9)]
    ids = dict(zip(texts, await add_rows(*texts)))
    await add_rows("beta memory one", "beta memory two", project="beta")
    bare = (await add_rows("bare one", project=None))[0]
    await plant_review(bare, APPROVE)
    for t in texts[:6]:
        await plant_review(ids[t], APPROVE)
    await plant_review(ids[texts[6]], REJECT)      # flagged: not reference
    new = "alpha memory number 3 about topic 3, said again"
    fake = FakeReviewer(APPROVE)
    async with App(reviewer=fake) as a:
        mid = await a.add(new)
        memory, neighbours, _ = fake.calls[-1]
        assert memory["id"] == mid
        # Five of them, verified, from the same project, the new one not among
        # them, nearest first, each a full memory with its score.
        assert [n["id"] for n in neighbours] == [ids[t] for t in _by_cosine(new, texts[:6])[:5]]
        assert all(n["review_status"] == "verified" and n["project"] == "alpha"
                   for n in neighbours)
        scores = [n["score"] for n in neighbours]
        assert scores == sorted(scores, reverse=True)
        assert set(neighbours[0]) >= {"id", "project", "type", "tags", "content", "score"}

        # No project matches no project only.
        await a.add("bare two", project=None)
        assert [n["id"] for n in fake.calls[-1][1]] == [bare]
    # Without the embedding model there is nothing to compare: the review
    # still runs and is stored.
    async with App(reviewer=fake, embedder=NullEmbedder()) as a:
        row = await a.get(await a.add(new + " once more"))
        assert fake.calls[-1][1] == [] and row["review"] == APPROVE.as_dict()


async def test_a_forced_add_is_reviewed_too():
    fake = FakeReviewer(APPROVE)
    async with App(reviewer=fake) as a:
        await a.add("dup me")
        forced = await a.add("dup me", force="true")
        assert (await a.get(forced))["review"] == APPROVE.as_dict()
    assert len(fake.calls) == 2


# ── the tags offered to the model ────────────────────────────────────────────
async def test_the_offered_tags_are_the_nearest_by_meaning_capped_at_ten():
    fake = FakeReviewer(REWRITE)
    tags = [(f"tag{i}", f"about topic {i}") for i in range(TAG_COUNT + 3)]
    async with App(reviewer=fake) as a:
        assert (await a.add("the very first memory")) == 1
        assert fake.calls[-1][2] == []                      # no tags in use yet
        for name, desc in tags:
            await a.add(f"a memory carrying {name}", tags=[{"name": name, "description": desc}])
        new = "Chose Postgres for the store."
        await a.add(new)
    vec = vector(new)
    scored = sorted(((cosine(vec, vector(f"{n}: {d}")), n) for n, d in tags),
                    key=lambda pair: (-pair[0], pair[1]))
    assert fake.calls[-1][2] == [n for _, n in scored][:TAG_COUNT]


async def test_the_review_embeds_only_the_entry_and_reads_the_tag_vectors():
    class Recording(FakeEmbedder):
        seen = []

        def embed(self, texts):
            Recording.seen.append(list(texts))
            return super().embed(texts)

    fake = FakeReviewer(REWRITE)
    async with App(reviewer=fake, embedder=Recording()) as a:
        for name, desc in (("db", "the database layer"), ("ui", "the dashboard")):
            await a.add(f"carrier for {name}", tags=[{"name": name, "description": desc}])
        Recording.seen.clear()
        await a.add("Chose Postgres for the store.")
    # The duplicate check, the row and the review: three calls, each for the
    # entry only. No call carries a tag's text.
    assert Recording.seen == [["Chose Postgres for the store."]] * 3
    assert sorted(fake.calls[-1][2]) == ["db", "ui"]


async def test_without_a_model_the_most_used_tags_are_offered():
    fake = FakeReviewer(REWRITE)
    async with App(reviewer=fake, embedder=NullEmbedder()) as a:
        for text, tag in (("one", "rare"), ("two", "common"), ("three", "common")):
            await a.add(text, tags=[tag])
        for i in range(TAG_COUNT + 2):
            await a.add(f"filler {i}", tags=[f"filler{i}"])
        await a.add("Chose Postgres for the store.")
    offered = fake.calls[-1][2]
    # Most used first, then by name, as `memory tags` lists them.
    assert len(offered) == TAG_COUNT and offered[0] == "common"
    assert offered[1:] == sorted(offered[1:])


# ── re-review and the history ────────────────────────────────────────────────
async def test_a_re_review_adds_a_row_and_every_read_shows_the_newest():
    fake = FakeReviewer(REJECT)
    async with App(reviewer=fake) as a:
        mid = await a.add("Chose Postgres for the store.")
        fake.verdict = REWRITE
        resp = await a.client.post(f"/admin/review/{mid}")
        assert resp.status_code == 200 and resp.json() == REWRITE.as_dict()
        assert (await a.get(mid))["review_status"] == "flagged"
        fake.verdict = APPROVE
        assert (await a.client.post(f"/admin/review/{mid}")).json() == APPROVE.as_dict()

        row = await a.get(mid)
        assert row["review"] == APPROVE.as_dict() and row["review_status"] == "verified"
        assert (await a.client.get("/memories")).json()[0]["review"] == APPROVE.as_dict()
        found = (await a.client.get("/memories/search", params={"q": "Postgres"})).json()
        assert found[0]["review"] == APPROVE.as_dict()
        # The history route has them all, newest first, with their dates.
        history = (await a.client.get(f"/memories/{mid}/reviews")).json()
        assert [r["verdict"] for r in history] == ["approve", "rewrite", "reject"]
        assert set(history[0]) == {"created_at", "verdict", "rule", "reason", "rewrite",
                                   "duplicate_of", "tags"}
        assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(\+\d{2}:\d{2})?",
                                r["created_at"]) for r in history)
        assert history[1]["rewrite"] == REWRITE.rewrite and history[1]["tags"] == REWRITE.tags

        # No verdict: 502, and the newest row and the status stay.
        fake.verdict = None
        resp = await a.client.post(f"/admin/review/{mid}")
        assert resp.status_code == 502 and f"no verdict for memory #{mid}" in resp.json()["detail"]
        assert (await a.get(mid))["review"] == APPROVE.as_dict()
        assert (await a.client.post("/admin/review/999")).status_code == 404
        assert (await a.client.get("/memories/999/reviews")).status_code == 404
        other = await a.add("never reviewed", force="true")
    assert await review_rows() == [(mid, "reject", "fake-reviewer"),
                                   (mid, "rewrite", "fake-reviewer"),
                                   (mid, "approve", "fake-reviewer")]
    async with App() as a:
        assert (await a.client.get(f"/memories/{other}/reviews")).json() == []


async def test_the_history_in_the_repository(session):
    mid = await repo.add(session, "Chose Postgres for the store.", "t", "alpha", [], None)
    assert await repo.reviews(session, mid) == []
    assert await repo.reviews(session, 999) is None
    old = await repo.add(session, "first choice", "t", "alpha", [], None)
    await repo.set_review(session, mid, REJECT, "m")
    await repo.set_review(session, mid, supersede(old), "m")
    row = await repo.get(session, mid)
    assert row["review_status"] == "verified" and row["supersedes"] == old
    # The status and the link follow the newest row.
    await repo.set_review(session, mid, REWRITE, "m")
    row = await repo.get(session, mid)
    assert (row["review_status"], row["supersedes"], row["review"]) == (
        "flagged", None, REWRITE.as_dict())
    assert (await repo.get(session, old))["superseded_by"] is None
    rows = await repo.reviews(session, mid)
    assert [r["verdict"] for r in rows] == ["rewrite", "approve", "reject"]
    assert rows[0]["created_at"] >= rows[1]["created_at"] >= rows[2]["created_at"]
    assert rows[2]["reason"] == REJECT.reason
    with pytest.raises(LookupError, match="#999"):
        await repo.set_review(session, 999, APPROVE, "m")


async def test_new_content_starts_over_and_other_changes_keep_the_review():
    fake = FakeReviewer(APPROVE)
    async with App(reviewer=fake) as a:
        old = await a.add(OLD)
        fake.verdict = supersede(old)
        mid = await a.add(NEW, tags=["a"])
        fake.verdict = REWRITE
        await a.client.post(f"/admin/review/{mid}")
        fake.verdict = supersede(old)
        await a.client.post(f"/admin/review/{mid}")
        assert len((await a.client.get(f"/memories/{mid}/reviews")).json()) == 3
        calls = len(fake.calls)

        # Tags, project, type, or the same text again: the review stays.
        for body in ({"add_tags": [{"name": "b"}]}, {"remove_tags": ["a"]}, {"project": "alpha"},
                     {"type": "note"}, {"set_tags": [{"name": "c"}]}, {"content": NEW}):
            assert (await a.client.patch(f"/memories/{mid}", json=body)).status_code == 200
        row = await a.get(mid)
        assert (row["review_status"], row["supersedes"]) == ("verified", old)
        assert len((await a.client.get(f"/memories/{mid}/reviews")).json()) == 3

        # New text: every row goes, the link goes, the memory is unverified,
        # and nothing asks the model: the next catch-up will.
        resp = await a.client.patch(f"/memories/{mid}", json={"content": "other words"})
        assert resp.json() == {"changes": ["content"]}
        row = await a.get(mid)
        assert (row["review_status"], row["review"], row["supersedes"]) == (
            "unverified", None, None)
        assert (await a.get(old))["superseded_by"] is None
        assert (await a.client.get(f"/memories/{mid}/reviews")).json() == []
        assert len(fake.calls) == calls

        fake.verdict = APPROVE
        assert (await a.client.post("/admin/review")).json() == {"scheduled": 1}
        assert fake.calls[-1][0]["content"] == "other words"
        assert (await a.get(mid))["review_status"] == "verified"


async def test_deleting_a_memory_deletes_its_reviews():
    fake = FakeReviewer(REJECT)
    async with App(reviewer=fake) as a:
        mid = await a.add("short lived")
        fake.verdict = APPROVE
        await a.client.post(f"/admin/review/{mid}")
        assert len(await review_rows()) == 2
        await a.client.request("DELETE", "/memories", params={"ids": [mid]})
    assert await review_rows() == []


# ── refuse mode: the verdict before the write ────────────────────────────────
def _refusal(verdict):
    """The 422 body the server answers with for `verdict`."""
    return {"detail": {
        "reason": "review", "verdict": verdict.verdict, "rule": verdict.rule,
        "explanation": verdict.reason, "rewrite": verdict.rewrite,
        "tags": list(verdict.tags), "duplicate_of": verdict.duplicate_of}}


async def test_refuse_mode_refuses_a_reject_or_a_rewrite_and_stores_nothing():
    fake = FakeReviewer(APPROVE)
    async with App(reviewer=fake, review_mode="refuse") as a:
        assert a.app.state.review_mode == "refuse"
        assert await a.add("the original") == 1
        for verdict in (REJECT, REWRITE, REPEAT, MERGE):
            fake.verdict = verdict
            resp = await a.post("Spent the afternoon tidying.", type="note", tags=["work-log"])
            assert resp.status_code == 422
            assert resp.json() == _refusal(verdict)
        assert await a.ids() == [1]
    assert await review_rows() == [(1, "approve", "fake-reviewer")]
    # The model saw the entry as the writer sent it, with no id yet, and the
    # verified original as its neighbour, found before the write.
    memory, neighbours, tags = fake.calls[-1]
    assert memory == {"id": None, "project": "alpha", "type": "note", "tags": ["work-log"],
                      "content": "Spent the afternoon tidying."}
    assert [n["id"] for n in neighbours] == [1]


async def test_refuse_mode_approve_stores_the_memory_verified_with_one_call():
    fake = FakeReviewer(APPROVE)
    async with App(reviewer=fake, review_mode="refuse") as a:
        resp = await a.post("Chose Postgres, because several agents write at once.",
                            type="decision")
        assert resp.status_code == 201 and set(resp.json()) == {"id", "warnings"}
        row = await a.get(resp.json()["id"])
        assert row["review"] == APPROVE.as_dict() and row["review_status"] == "verified"
    assert await review_rows() == [(1, "approve", "fake-reviewer")]
    assert len(fake.calls) == 1 and fake.calls[0][0]["id"] is None


@pytest.mark.parametrize("answer, line", [
    (None, "review of a new memory gave no verdict; storing it without one"),
    (RuntimeError("model blew up"), "review of a new memory failed: RuntimeError: model blew up"),
])
async def test_refuse_mode_without_a_verdict_stores_the_memory(caplog, answer, line):
    fake = FakeReviewer(answer)
    with caplog.at_level(logging.WARNING, logger="agent_memory.server.app"):
        async with App(reviewer=fake, review_mode="refuse") as a:
            resp = await a.post("the model was away")
            assert resp.status_code == 201
            assert (await a.get(resp.json()["id"]))["review"] is None
    assert await review_rows() == []
    assert len(fake.calls) == 1 and _messages(caplog) == [line]


async def test_refuse_mode_force_stores_and_the_review_runs_after():
    fake = FakeReviewer(REJECT)
    async with App(reviewer=fake, review_mode="refuse") as a:
        resp = await a.post("Spent the afternoon tidying.", force="true")
        assert resp.status_code == 201
        mid = resp.json()["id"]
        # One call, after the write: the model saw the stored memory, id and
        # all, unverified then. The verdict has landed by now.
        (memory, _, _), = fake.calls
        assert memory["id"] == mid and memory["review_status"] == "unverified"
        assert (await a.get(mid))["review_status"] == "flagged"


async def test_refuse_mode_checks_for_a_duplicate_before_asking_the_model():
    fake = FakeReviewer(APPROVE)
    async with App(reviewer=fake, review_mode="refuse") as a:
        assert (await a.post("dup me")).status_code == 201
        resp = await a.post("dup me")
        assert resp.status_code == 409 and resp.json()["detail"]["reason"] == "duplicate"
    assert len(fake.calls) == 1


async def test_refuse_mode_finds_the_neighbours_and_tags_before_the_write():
    texts = [f"alpha memory number {i} about topic {i}" for i in range(8)]
    fake = FakeReviewer(APPROVE)
    async with App(reviewer=fake, review_mode="refuse") as a:
        ids = {t: await a.add(t, tags=[f"tag{i}"]) for i, t in enumerate(texts)}
        await a.add("beta memory one", project="beta")
        new = "alpha memory number 3 about topic 3, said again"
        assert (await a.post(new)).status_code == 201
    memory, neighbours, offered = fake.calls[-1]
    assert memory["id"] is None and memory["content"] == new
    assert [n["id"] for n in neighbours] == [ids[t] for t in _by_cosine(new, texts)[:5]]
    assert set(offered) == {f"tag{i}" for i in range(8)}
    async with App(reviewer=FakeReviewer(REJECT), embedder=NullEmbedder(),
                   review_mode="refuse") as a:
        assert (await a.post("second")).status_code == 422
        assert a.app.state.reviewer.calls[-1][1] == []


async def test_refuse_mode_holds_no_connection_while_the_model_thinks():
    """Five writes wait on the model at once, on a pool of two connections.
    All five reach the model, and a read still answers within a second: the
    writes read what the model needs in a short session and let it go before
    the call."""
    slow = FakeReviewer(APPROVE, block=True)
    started = []
    real_review = slow.review

    def counting(memory, neighbours, tags=()):
        started.append(memory["content"])
        return real_review(memory, neighbours, tags)

    slow.review = counting
    engine = make_test_engine(pool_size=2, max_overflow=0)
    writes = []
    async with App(reviewer=slow, review_mode="refuse", engine=engine) as a:
        try:
            for n in range(5):
                writes.append(asyncio.create_task(a.post(
                    f"Chose option {n} for the cache, because it fits in memory.",
                    type="decision")))
            await until(lambda: len(started) == 5, timeout=10)
            read = await asyncio.wait_for(a.client.get("/memories"), timeout=1.0)
            assert read.status_code == 200 and read.json() == []
        finally:
            slow.release.set()
            results = await asyncio.gather(*writes, return_exceptions=True)
    assert [getattr(r, "status_code", r) for r in results] == [201] * 5
    assert await statuses() == [(i, "verified") for i in range(1, 6)]


@pytest.mark.parametrize("env, injected, mode", [
    ({"AGENT_MEMORY_REVIEW": "refuse"}, False, "off"),              # no server: off
    ({"AGENT_MEMORY_REVIEW": "refuse", "AGENT_MEMORY_REVIEW_URL": "http://o:11434"}, False,
     "refuse"),
    ({"AGENT_MEMORY_REVIEW": "flag", "AGENT_MEMORY_REVIEW_URL": "http://o:11434"}, False, "flag"),
    ({"AGENT_MEMORY_REVIEW": "enforce", "AGENT_MEMORY_REVIEW_URL": "http://o:11434"}, False,
     "refuse"),
    ({"AGENT_MEMORY_REVIEW": "warn", "AGENT_MEMORY_REVIEW_URL": "http://o:11434"}, False, "flag"),
    ({"AGENT_MEMORY_REVIEW_URL": "http://o:11434"}, False, "off"),  # off by default
    # A reviewer handed to the app is used, even when the environment says off.
    ({"AGENT_MEMORY_REVIEW": "off"}, True, "flag"),
    ({"AGENT_MEMORY_REVIEW": "refuse"}, True, "refuse"),
])
async def test_the_lifespan_settles_the_mode(monkeypatch, env, injected, mode):
    for var in ("AGENT_MEMORY_REVIEW", "AGENT_MEMORY_REVIEW_URL"):
        monkeypatch.delenv(var, raising=False)
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    async with App(reviewer=FakeReviewer() if injected else None, review_poll=0) as a:
        async with a.app.router.lifespan_context(a.app):
            assert a.app.state.review_mode == mode
            assert isinstance(a.app.state.reviewer, NullReviewer) == (mode == "off")


# ── supersedes: the link a verdict sets ──────────────────────────────────────
async def test_flag_mode_stores_the_link_and_every_read_carries_both_ends():
    fake = FakeReviewer(APPROVE)
    async with App(reviewer=fake) as a:
        old = await a.add(OLD)
        fake.verdict = supersede(old)
        new = await a.add(NEW)
        # The model saw the old one as the listed neighbour, and the new one
        # with no link yet.
        memory, neighbours, _ = fake.calls[-1]
        assert memory["supersedes"] is None and [n["id"] for n in neighbours] == [old]

        row, old_row = await a.get(new), await a.get(old)
        assert (row["supersedes"], row["superseded_by"], row["review_status"]) == (
            old, None, "verified")
        assert (old_row["supersedes"], old_row["superseded_by"]) == (None, new)
        assert old_row["content"] == OLD
        for path, params in (("/memories", {}), ("/memories/search", {"q": "store"})):
            rows = (await a.client.get(path, params=params)).json()
            assert {m["id"]: (m["supersedes"], m["superseded_by"]) for m in rows} == {
                old: (None, new), new: (old, None)}


async def test_refuse_mode_stores_the_link_with_the_write():
    fake = FakeReviewer(APPROVE)
    async with App(reviewer=fake, review_mode="refuse") as a:
        old = await a.add(OLD)
        fake.verdict = supersede(old)
        new = await a.add(NEW)
        assert (await a.get(new))["supersedes"] == old
        assert (await a.get(old))["superseded_by"] == new
    # One call for the new entry, before it had an id, and no second one.
    assert len(fake.calls) == 2 and fake.calls[-1][0]["id"] is None


async def test_a_re_review_moves_or_clears_the_link():
    fake = FakeReviewer(APPROVE)
    async with App(reviewer=fake) as a:
        first = await a.add("first choice, because of A")
        second = await a.add("second choice, because of B")
        fake.verdict = supersede(first)
        new = await a.add("third choice, because of C")
        assert (await a.get(new))["supersedes"] == first

        fake.verdict = supersede(second)
        resp = await a.client.post(f"/admin/review/{new}")
        assert resp.json()["supersedes"] == second
        assert (await a.get(first))["superseded_by"] is None
        assert (await a.get(second))["superseded_by"] == new

        fake.verdict = None                     # no verdict keeps the link
        assert (await a.client.post(f"/admin/review/{new}")).status_code == 502
        assert (await a.get(new))["supersedes"] == second

        fake.verdict = APPROVE                  # a verdict without one clears it
        await a.client.post(f"/admin/review/{new}")
        assert (await a.get(new))["supersedes"] is None
        assert (await a.get(second))["superseded_by"] is None


async def test_the_request_can_never_set_the_link():
    async with App() as a:
        old = await a.add(OLD)
        resp = await a.client.post("/memories", json={
            "content": NEW, "agent": "tester", "project": "alpha", "supersedes": old})
        new = resp.json()["id"]
        resp = await a.client.patch(f"/memories/{new}", json={"supersedes": old})
        assert resp.status_code == 200 and resp.json()["changes"] == []
        assert (await a.get(new))["supersedes"] is None
        assert (await a.get(old))["superseded_by"] is None


async def test_set_review_refuses_a_self_link_and_an_unknown_target(session):
    mid = await repo.add(session, "one", "t", "alpha", [], None)
    with pytest.raises(ValueError, match="itself"):
        await repo.set_review(session, mid, supersede(mid), "m")
    with pytest.raises(LookupError, match="#999"):
        await repo.set_review(session, mid, supersede(999), "m")
    row = await repo.get(session, mid)
    assert (row["review_status"], row["review"], row["supersedes"]) == ("unverified", None, None)
    other = await repo.add(session, "two", "t", "alpha", [], None)
    view = await repo.set_review(session, mid, supersede(other), "m")
    assert view["supersedes"] == other and view["verdict"] == "approve"
    assert (await repo.get(session, other))["superseded_by"] == mid


async def test_a_verdict_naming_a_missing_memory_is_one_log_line(caplog):
    with caplog.at_level(logging.WARNING, logger="agent_memory.server.app"):
        async with App(reviewer=FakeReviewer(supersede(999))) as a:
            row = await a.get(await a.add(NEW))
            assert (row["review_status"], row["review"], row["supersedes"]) == (
                "unverified", None, None)
    assert _messages(caplog) == ["review of memory #1 failed: LookupError: Memory #999 not found"]


async def test_superseded_by_is_the_newest_by_timestamp_then_id():
    old, two, three, four = await add_rows(OLD, "second", "third", "fourth")
    for mid in (two, three, four):
        await plant_review(mid, supersede(old))

    async def superseded_by():
        async with db_session() as s:
            return (await repo.get(s, old))["superseded_by"]

    async def date(mid, when):
        async with db_session() as s:
            await s.execute(update(Memory).where(Memory.id == mid).values(timestamp=when))

    assert await superseded_by() == four
    # The timeline decides, not the id: date the third one last.
    base = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    await date(two, base)
    await date(four, base + timedelta(minutes=1))
    await date(three, base + timedelta(minutes=2))
    assert await superseded_by() == three
    # On the same timestamp the higher id wins.
    await date(four, base + timedelta(minutes=2))
    assert await superseded_by() == four


async def test_deleting_either_end_clears_the_link():
    one, two, three = await add_rows("one", "two", "three")
    await plant_review(two, supersede(one))
    await plant_review(three, supersede(two))
    async with App() as a:
        await a.client.request("DELETE", "/memories", params={"ids": [one]})
        row = await a.get(two)
        assert (row["supersedes"], row["superseded_by"]) == (None, three)
        await a.client.request("DELETE", "/memories", params={"ids": [three]})
        assert (await a.get(two))["superseded_by"] is None


async def test_current_hides_the_superseded_memories_on_every_listing():
    """#1 old, #2 supersedes #1, #3 on its own, #4 and #5 a chain on #3. All
    share the word `store`, so every search mode finds them."""
    await add_rows(OLD, NEW, "Tag names in the store are matched without regard to case.",
                   "store four, because of A", "store five, because of B", project="p")
    await plant_review(1, APPROVE)
    await plant_review(2, supersede(1))
    await plant_review(4, supersede(3))
    await plant_review(5, supersede(4))
    async with App() as a:
        assert await a.ids() == [5, 4, 3, 2, 1]
        resp = await a.client.get("/memories", params={"current": "true"})
        assert [m["id"] for m in resp.json()] == [5, 2]
        assert resp.headers["X-Total-Count"] == "2"
        assert await a.ids(current="false") == [5, 4, 3, 2, 1]
        assert await a.ids(current="true", status="verified") == [5, 2]
        assert await a.ids(current="true", project="nope") == []
        for mode in ("keyword", "semantic", "hybrid"):
            ids = await a.ids("/memories/search", q="store", mode=mode, project="p")
            assert set(ids) == {1, 2, 3, 4, 5}, mode
            ids = await a.ids("/memories/search", q="store", mode=mode, current="true")
            assert set(ids) == {2, 5}, mode
        assert (await a.client.get("/memories", params={"current": "maybe"})).status_code == 422


async def test_a_merge_is_stored_flagged_with_the_suggestion_and_changes_nothing():
    fake = FakeReviewer(APPROVE)
    async with App(reviewer=fake) as a:
        old = await a.add("Chose Postgres, because several agents write at once.",
                          tags=["database"])
        fake.verdict = MERGE
        new = await a.add(MERGED)
        row = await a.get(new)
        assert row["review_status"] == "flagged" and row["review"] == MERGE.as_dict()
        # Nothing was applied, and nothing links the two.
        assert row["content"] == MERGED and row["supersedes"] is None
        old_row = await a.get(old)
        assert old_row["content"] == "Chose Postgres, because several agents write at once."
        assert (old_row["superseded_by"], old_row["review_status"]) == (None, "verified")
        flagged = (await a.client.get("/memories/flagged")).json()
        assert [m["id"] for m in flagged] == [new]
        assert flagged[0]["review"]["duplicate_of"] == old


# ── the flagged listing ──────────────────────────────────────────────────────
async def _seed_flagged():
    """Five memories, four reviewed. Review times, oldest first: old_reject,
    rewrite, approve, new_reject. So the flagged order is new_reject,
    rewrite, old_reject; `bare` has no review."""
    names = ("old_reject", "rewrite", "approve", "new_reject", "bare")
    ids = dict(zip(names, await add_rows(
        "Spent the afternoon tidying.", "Chose Postgres.",
        "Chose Postgres because several agents write at once.",
        "Had lunch, then read the config module.", "Not reviewed yet.")))
    async with db_session() as s:
        await s.execute(update(Memory).where(Memory.id == ids["rewrite"]).values(project="beta"))
    for name, verdict, age in (("old_reject", REJECT, 30), ("rewrite", REWRITE, 20),
                               ("approve", APPROVE, 10), ("new_reject", REJECT, 0)):
        await plant_review(ids[name], verdict)
        when = datetime.now(timezone.utc) - timedelta(minutes=age)
        async with db_session() as s:
            await s.execute(update(MemoryReview).where(MemoryReview.memory_id == ids[name])
                            .values(created_at=when))
    return ids


async def test_flagged_in_the_repository(session):
    ids = await _seed_flagged()
    rows = await repo.flagged(session)
    assert [r["id"] for r in rows] == [ids["new_reject"], ids["rewrite"], ids["old_reject"]]
    # The same shape as `query`, the review included.
    assert set(rows[0]) == set((await repo.query(session))[0])
    assert rows[0]["review"] == REJECT.as_dict() and rows[1]["review"] == REWRITE.as_dict()

    async def flagged(**kw):
        return [r["id"] for r in await repo.flagged(session, **kw)]

    assert await flagged(verdict="reject") == [ids["new_reject"], ids["old_reject"]]
    assert await flagged(verdict="rewrite") == [ids["rewrite"]]
    assert await flagged(project="beta") == [ids["rewrite"]]
    assert await flagged(project="alpha", verdict="rewrite") == []
    assert await flagged(limit=2) == [ids["new_reject"], ids["rewrite"]]
    assert len(await flagged(limit=0)) == len(await flagged(limit=None)) == 3
    assert await flagged(status="verified") == [ids["approve"]]
    assert await flagged(status="unverified") == [ids["bare"]]
    assert await flagged(status="flagged", verdict="rewrite") == [ids["rewrite"]]
    assert await flagged(status="verified", verdict="reject") == []
    # The count ignores the limit.
    assert await repo.count_flagged(session) == 3
    assert await repo.count_flagged(session, verdict="reject") == 2
    assert await repo.count_flagged(session, project="beta") == 1
    assert await repo.count_flagged(session, project="gamma") == 0
    with pytest.raises(ValueError, match="approve"):
        await repo.flagged(session, verdict="approve")


async def test_flagged_orders_by_review_time_then_newest_id(session):
    a, b = await add_rows("a", "b")
    # The older memory gets the newer review, so it comes first.
    await repo.set_review(session, b, REJECT, "planted")
    await session.execute(update(MemoryReview).values(
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5)))
    await repo.set_review(session, a, REJECT, "planted")
    assert [r["id"] for r in await repo.flagged(session)] == [a, b]
    # On the same review time the newer memory comes first.
    await session.execute(update(MemoryReview).values(created_at=datetime.now(timezone.utc)))
    assert [r["id"] for r in await repo.flagged(session)] == [b, a]


async def test_flagged_and_unverified_go_by_the_newest_row(session):
    healed, fell, twice, bare = await add_rows("rejected, then approved",
                                               "approved, then rejected", "rejected twice",
                                               "never checked")
    for mid, verdicts in ((healed, (REJECT, APPROVE)), (fell, (APPROVE, REJECT)),
                          (twice, (REJECT, REWRITE))):
        for verdict in verdicts:
            await repo.set_review(session, mid, verdict, "m")
    rows = await repo.flagged(session)
    assert [(r["id"], r["review"]["verdict"]) for r in rows] == [(twice, "rewrite"),
                                                                 (fell, "reject")]
    assert [r["id"] for r in await repo.flagged(session, verdict="reject")] == [fell]
    assert await repo.count_flagged(session, verdict="reject") == 1
    verified = await repo.flagged(session, status="verified")
    assert [r["id"] for r in verified] == [healed]
    assert verified[0]["review"] == APPROVE.as_dict()
    assert await repo.unverified_ids(session) == [bare]


async def test_the_flagged_route():
    ids = await _seed_flagged()
    async with App() as a:
        # Declared before /memories/{id}, so "flagged" is not read as an id.
        resp = await a.client.get("/memories/flagged")
        assert resp.headers["X-Total-Count"] == "3"
        assert [r["id"] for r in resp.json()] == [ids["new_reject"], ids["rewrite"],
                                                  ids["old_reject"]]
        resp = await a.client.get("/memories/flagged", params={"verdict": "rewrite"})
        assert [r["id"] for r in resp.json()] == [ids["rewrite"]]
        assert resp.headers["X-Total-Count"] == "1"
        resp = await a.client.get("/memories/flagged", params={"project": "alpha",
                                                                "verdict": "reject"})
        assert [r["id"] for r in resp.json()] == [ids["new_reject"], ids["old_reject"]]
        # The limit keeps the total.
        resp = await a.client.get("/memories/flagged", params={"limit": 1})
        assert len(resp.json()) == 1 and resp.headers["X-Total-Count"] == "3"
        assert len(await a.ids("/memories/flagged", limit=0)) == 3
        resp = await a.client.get("/memories/flagged", params={"status": "unverified"})
        assert [r["id"] for r in resp.json()] == [ids["bare"]]
        assert resp.json()[0]["review"] is None
        assert await a.ids("/memories/flagged", status="unverified", project="q") == []
        for params in ({"verdict": "approve"}, {"verdict": "maybe"}, {"verdict": "REJECT"},
                       {"verdict": ""}, {"limit": -1}, {"status": "maybe"},
                       {"status": "VERIFIED"}, {"status": ""}):
            assert (await a.client.get("/memories/flagged", params=params)).status_code == 422
        for value in ("maybe", "approve"):
            assert (await a.client.get("/memories", params={"status": value})).status_code == 422
        resp = await a.client.get("/memories", params={"status": "unverified", "limit": 0})
        assert [m["id"] for m in resp.json()] == [ids["bare"]]
        assert resp.headers["X-Total-Count"] == "1"


# ── the catch-up ─────────────────────────────────────────────────────────────
async def test_unverified_ids_are_oldest_first_by_timestamp_then_id(session):
    a, b, c, d = await add_rows("a", "b", "c", "d")
    await plant_review(b, APPROVE)
    await set_age(d, 5)
    assert await repo.unverified_ids(session) == [d, a, c]
    assert await repo.unverified_ids(session, limit=2) == [d, a]
    assert await repo.unverified_ids(session, limit=0) == [d, a, c]


async def test_the_catch_up_reviews_the_unverified_oldest_first_in_one_task(monkeypatch):
    seen = []
    real = app_mod._review_in_order

    async def spy(app, ids):
        seen.append(list(ids))
        await real(app, ids)

    monkeypatch.setattr(app_mod, "_review_in_order", spy)
    ids = a, b, c, d, e = await add_rows("a", "b", "c", "d", "e")
    # Mixed ages: c is the oldest, then a, then e; b and d share a time.
    for mid, minutes in ((c, 50), (a, 40), (e, 30), (b, 20), (d, 20)):
        await set_age(mid, minutes)
    reviewed = (await add_rows("already reviewed"))[0]
    await plant_review(reviewed, REJECT)
    fake = FakeReviewer(APPROVE)
    async with App(reviewer=fake) as app:
        resp = await app.client.post("/admin/review", params={"limit": 2})
        assert resp.status_code == 200 and resp.json() == {"scheduled": 2}
        assert fake.ids == [c, a]
        assert (await app.client.post("/admin/review", params={"limit": 0})).json() == {
            "scheduled": 3}
        assert fake.ids == [c, a, e, b, d]
        # Nothing left: no task at all.
        assert (await app.client.post("/admin/review")).json() == {"scheduled": 0}
    assert seen == [[c, a], [e, b, d]]
    assert await statuses() == [(mid, "verified") for mid in ids] + [(reviewed, "flagged")]


async def test_the_catch_up_limit_defaults_to_50(monkeypatch):
    asked = []

    async def unverified_ids(session, *, limit=None):
        asked.append(limit)
        return []

    monkeypatch.setattr(repo, "unverified_ids", unverified_ids)
    async with App(reviewer=FakeReviewer()) as a:
        assert (await a.client.post("/admin/review")).json() == {"scheduled": 0}
        assert (await a.client.post("/admin/review", params={"limit": -1})).status_code == 422
    assert asked == [50]


async def test_the_catch_up_verifies_each_memory_before_the_next_is_compared():
    # Two memories with the same text: the second was not refused, since the
    # first was unverified when it was written. A third, older, other text.
    other, first, second = await add_rows("something else", "the same text", "the same text")
    for mid, minutes in ((other, 30), (first, 20), (second, 10)):
        await set_age(mid, minutes)
    fake = FakeReviewer(APPROVE)
    async with App(reviewer=fake) as a:
        assert (await a.client.post("/admin/review")).json() == {"scheduled": 3}
    assert fake.ids == [other, first, second]
    neighbours = [[(n["id"], n["review_status"]) for n in call[1]] for call in fake.calls]
    assert neighbours == [[], [(other, "verified")],
                          [(first, "verified"), (other, "verified")]]
    assert fake.calls[2][1][0]["score"] == pytest.approx(1.0)


async def test_a_failing_review_in_the_catch_up_does_not_stop_the_rest(caplog):
    one, two, three, four = await add_rows("one", "two", "three", "four")
    fake = FakeReviewer(APPROVE, verdicts={"two": RuntimeError("model blew up"),
                                           "three": REJECT, "four": None})
    with caplog.at_level(logging.WARNING, logger="agent_memory.server.app"):
        async with App(reviewer=fake) as a:
            assert (await a.client.post("/admin/review")).json() == {"scheduled": 4}
    assert await statuses() == [(one, "verified"), (two, "unverified"), (three, "flagged"),
                                (four, "unverified")]
    assert _messages(caplog) == [f"review of memory #{two} failed: RuntimeError: model blew up"]
    # The two without a verdict are still unverified: the next catch-up takes them.
    fake.verdicts.clear()
    async with App(reviewer=fake) as a:
        assert (await a.client.post("/admin/review")).json() == {"scheduled": 2}
    assert fake.ids[-2:] == [two, four]


async def test_the_catch_up_stores_the_link():
    old, new = await add_rows(OLD, NEW)
    await set_age(old, 20)
    await set_age(new, 10)
    fake = FakeReviewer(APPROVE, verdicts={NEW: supersede(old)})
    async with App(reviewer=fake) as a:
        assert (await a.client.post("/admin/review")).json() == {"scheduled": 2}
        assert (await a.get(new))["supersedes"] == old
    # The old one was verified first, so it was the neighbour the new one saw.
    assert [n["id"] for n in fake.calls[1][1]] == [old]


async def test_without_a_review_model_the_review_routes_answer_503():
    await add_rows("one")
    async with App(reviewer=NullReviewer("review is off (AGENT_MEMORY_REVIEW=off)")) as a:
        for path in ("/admin/review", "/admin/review/1"):
            resp = await a.client.post(path)
            assert resp.status_code == 503 and "AGENT_MEMORY_REVIEW=off" in resp.json()["detail"]
    assert await statuses() == [(1, "unverified")]


class Stepped(FakeReviewer):
    """A fake whose reviews each wait on their own `threading.Event`, so a
    test lets them end one at a time. `entered[i]` is set when review i
    starts; `gates[i]` lets it end."""

    def __init__(self, n, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entered = [threading.Event() for _ in range(n)]
        self.gates = [threading.Event() for _ in range(n)]

    def review(self, memory, neighbours, tags=()):
        i = len(self.calls)
        self.entered[i].set()
        if not self.gates[i].wait(timeout=10):
            raise TimeoutError("the test never let the review end")
        return super().review(memory, neighbours, tags)


async def _progress(a):
    return (await a.client.get("/health")).json()["catch_up"]


async def test_health_shows_the_catch_up_progress_while_it_runs():
    await add_rows("one", "two", "three")
    fake = Stepped(3, APPROVE)
    async with App(reviewer=fake) as a:
        assert await _progress(a) is None
        tick = asyncio.create_task(_review_tick(a.app))
        for i in range(3):
            await asyncio.to_thread(fake.entered[i].wait, 10)
            assert await _progress(a) == {"total": 3, "done": i}
            fake.gates[i].set()
        await tick
        assert await _progress(a) is None
    assert [s for _, s in await statuses()] == ["verified"] * 3


async def test_a_failing_review_counts_as_done_in_the_progress(caplog):
    await add_rows("one", "two", "three")
    fake = Stepped(3, APPROVE, verdicts={"one": RuntimeError("model blew up"), "two": None})
    with caplog.at_level(logging.WARNING, logger="agent_memory.server.app"):
        async with App(reviewer=fake) as a:
            # Started by the route this time; its task runs inside the call.
            post = asyncio.create_task(a.client.post("/admin/review"))
            await asyncio.to_thread(fake.entered[0].wait, 10)
            assert await _progress(a) == {"total": 3, "done": 0}
            fake.gates[0].set()                 # raises
            await asyncio.to_thread(fake.entered[1].wait, 10)
            assert await _progress(a) == {"total": 3, "done": 1}
            fake.gates[1].set()                 # no verdict
            await asyncio.to_thread(fake.entered[2].wait, 10)
            assert await _progress(a) == {"total": 3, "done": 2}
            fake.gates[2].set()
            assert (await post).json() == {"scheduled": 3}
            assert await _progress(a) is None
    assert [s for _, s in await statuses()] == ["unverified", "unverified", "verified"]


# ── the poll ─────────────────────────────────────────────────────────────────
def _poll_task():
    """The poll task the lifespan started, or None."""
    return next((t for t in asyncio.all_tasks() if t.get_name() == "review-poll"), None)


async def _cancel(task):
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_tick_checks_the_model_and_catches_up_when_it_answers(caplog):
    one, two = await add_rows("one", "two")
    fake = FakeReviewer(APPROVE, up=False)
    with caplog.at_level(logging.INFO, logger="agent_memory.server.app"):
        async with App(reviewer=fake) as a:
            await _review_tick(a.app)
            assert (fake.checks, fake.calls) == (1, [])
            assert (await a.client.get("/health")).json() == {
                "status": "ok", "review_model": "unreachable", "catch_up": None}
            assert await statuses() == [(one, "unverified"), (two, "unverified")]

            fake.up = True
            await _review_tick(a.app)
            assert (await a.client.get("/health")).json()["review_model"] == "reachable"
            assert fake.ids == [one, two]
            assert await statuses() == [(one, "verified"), (two, "verified")]
            assert not _catch_up_lock(a.app).locked()
            # Nothing left: the next tick checks, and that is all.
            await _review_tick(a.app)
            assert fake.checks == 3 and len(fake.calls) == 2
    assert _messages(caplog) == [
        "review model unreachable: fake-reviewer",
        "review model reachable: fake-reviewer",
        "catch-up started: 2 unverified memories to review",
        "catch-up finished",
    ]


async def test_the_loop_survives_a_failing_check_and_a_failing_selection(monkeypatch, caplog):
    (mid,) = await add_rows("one")
    fake = FakeReviewer(APPROVE)
    fake.check_error = RuntimeError("boom")
    real = repo.unverified_ids
    broken = {"on": True}

    async def unverified_ids(session, *, limit=None):
        if broken["on"]:
            raise RuntimeError("database away")
        return await real(session, limit=limit)

    monkeypatch.setattr(repo, "unverified_ids", unverified_ids)
    with caplog.at_level(logging.INFO, logger="agent_memory.server.app"):
        async with App(reviewer=fake) as a:
            task = asyncio.create_task(_review_poll(a.app, 0.01))
            await until(lambda: fake.checks >= 3)
            # A check that raises counts as unreachable.
            assert a.app.state.review_model == "unreachable" and fake.calls == []
            fake.check_error = None
            # The selection fails: the lock is let go and the loop goes on.
            await until(lambda: "review poll: RuntimeError: database away" in _messages(caplog))
            assert not _catch_up_lock(a.app).locked()
            broken["on"] = False
            await until(lambda: len(fake.calls) == 1)
            await until(lambda: not _catch_up_lock(a.app).locked())
            checks = fake.checks
            await until(lambda: fake.checks > checks)       # still ticking
            await _cancel(task)
    assert await statuses() == [(mid, "verified")]
    messages = _messages(caplog)
    assert messages[0] == "review poll started: checking the model every 0.01 s"
    assert "review poll: the check failed: RuntimeError: boom" in messages


async def test_one_catch_up_at_a_time_whoever_started_it(caplog):
    one, two = await add_rows("one", "two")
    fake = FakeReviewer(APPROVE, block=True)
    with caplog.at_level(logging.INFO, logger="agent_memory.server.app"):
        async with App(reviewer=fake) as a:
            # The poll holds a catch-up open: the route answers `running`.
            tick = asyncio.create_task(_review_tick(a.app))
            await asyncio.to_thread(fake.started.wait, 10)
            assert _catch_up_lock(a.app).locked()
            resp = await a.client.post("/admin/review")
            assert resp.json() == {"scheduled": 0, "running": True}
            assert len(fake.calls) == 1
            fake.release.set()
            await tick
            assert fake.ids == [one, two] and not _catch_up_lock(a.app).locked()

            # The route holds one open: a tick checks and does nothing else,
            # and a second route call answers `running`.
            three = (await add_rows("three"))[0]
            fake.reset(APPROVE, block=True)
            post = asyncio.create_task(a.client.post("/admin/review"))
            await asyncio.to_thread(fake.started.wait, 10)
            await _review_tick(a.app)
            assert fake.checks == 1 and len(fake.calls) == 1
            assert (await a.client.post("/admin/review")).json() == {"scheduled": 0,
                                                                     "running": True}
            fake.release.set()
            assert (await post).json() == {"scheduled": 1}
            assert fake.ids == [three] and not _catch_up_lock(a.app).locked()
    assert _messages(caplog).count("catch-up started: 2 unverified memories to review") == 1


async def test_a_cancelled_catch_up_releases_the_lock(caplog):
    await add_rows("one", "two")
    fake = FakeReviewer(APPROVE, block=True)
    with caplog.at_level(logging.INFO, logger="agent_memory.server.app"):
        async with App(reviewer=fake) as a:
            task = asyncio.create_task(_review_poll(a.app, 0.01))
            await asyncio.to_thread(fake.started.wait, 10)
            assert _catch_up_lock(a.app).locked()
            await _cancel(task)
            assert not _catch_up_lock(a.app).locked()
            fake.release.set()
    assert "catch-up stopped" in _messages(caplog)


async def test_the_lifespan_runs_the_poll_and_health_follows_it(caplog):
    one, two = await add_rows("one", "two")
    fake = FakeReviewer(APPROVE, up=False)
    with caplog.at_level(logging.INFO):
        async with App(reviewer=fake, review_poll=0.01) as a:
            async with a.app.router.lifespan_context(a.app):
                task = _poll_task()
                assert task is not None and not task.done()
                # Health needs no token: a bare client.
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=a.app),
                                             base_url="http://testserver") as c:
                    await until(lambda: fake.checks >= 1)
                    assert (await c.get("/health")).json() == {
                        "status": "ok", "review_model": "unreachable", "catch_up": None}
                    fake.up = True
                    await until(lambda: len(fake.calls) == 2)
                    await until(lambda: not _catch_up_lock(a.app).locked())
                    assert (await c.get("/health")).json()["review_model"] == "reachable"
                    fake.up = False
                    await until(lambda: a.app.state.review_model == "unreachable")
            # Shutdown cancelled the task and waited for it.
            assert task.done() and task.cancelled()
    assert await statuses() == [(one, "verified"), (two, "verified")]
    # No traceback, no error from asyncio about the task.
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
    assert not any("never retrieved" in m for m in _messages(caplog))


async def test_shutdown_while_a_check_is_running_is_clean(monkeypatch, caplog):
    # A slow check (the model server takes its 2 s to time out) must not hold
    # up the shutdown or leave an error behind. The interval comes from the
    # environment here.
    class Slow(FakeReviewer):
        def reachable(self):
            self.checks += 1
            time.sleep(0.2)
            return False

    monkeypatch.setenv("AGENT_MEMORY_REVIEW_POLL", "0.01")
    fake = Slow()
    with caplog.at_level(logging.INFO):
        async with App(reviewer=fake) as a:
            async with a.app.router.lifespan_context(a.app):
                await until(lambda: fake.checks >= 1)
                task = _poll_task()
            assert task.cancelled()
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


@pytest.mark.parametrize("reviewer, poll", [
    (CountingNull(), 0.01),          # the review is off
    (FakeReviewer(), 0),             # the poll is off
    (FakeReviewer(), -1),
    (FakeReviewer(), "env"),         # AGENT_MEMORY_REVIEW_POLL=0
])
async def test_no_loop_when_the_review_or_the_poll_is_off(monkeypatch, reviewer, poll):
    if poll == "env":
        monkeypatch.setenv("AGENT_MEMORY_REVIEW_POLL", "0")
    kwargs = {} if poll == "env" else {"review_poll": poll}
    checks = getattr(reviewer, "checks", 0)
    async with App(reviewer=reviewer, **kwargs) as a:
        async with a.app.router.lifespan_context(a.app):
            await asyncio.sleep(0.05)
            assert _poll_task() is None
            assert (await a.client.get("/health")).json()["review_model"] == "off"
    assert getattr(reviewer, "checks", 0) == checks
    # Without a lifespan at all, health says off too.
    async with App(reviewer=FakeReviewer()) as a:
        assert (await a.client.get("/health")).json() == {"status": "ok", "review_model": "off",
                                                          "catch_up": None}


# ── the real model, when there is one ────────────────────────────────────────
def _real_reviewer():
    url = real_review_server()
    if url is None:
        pytest.skip("AGENT_MEMORY_REVIEW_URL is unset or the server did not answer in 2 s")
    model = os.environ.get("AGENT_MEMORY_REVIEW_MODEL", "").strip() or review_mod.DEFAULT_MODEL
    # A long timeout: the first call may have to load the model into memory.
    return OllamaReviewer(url, model, timeout=180)


@pytest.mark.review
def test_real_model_rejects_a_diary_line_and_approves_a_decision():
    reviewer = _real_reviewer()
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
    first = reviewer.review(diary, neighbours)
    second = reviewer.review(decision, neighbours)
    print(f"\n[review] {reviewer.model_name}: diary -> {first}; decision -> {second}")
    assert first is not None and first.verdict == "reject" and first.rule == 2 and first.reason
    assert second is not None and second.verdict == "approve"
    assert (second.rule, second.rewrite, second.duplicate_of) == (None, None, None)


@pytest.mark.review
async def test_real_model_links_a_reversal_and_merges_a_partial_repeat():
    """Through the app: the prompt, the parser and the storing of the link
    with a real answer."""
    reviewer = _real_reviewer()
    sqlite = ("Chose SQLite for the memory store because it needs no server and one agent "
              "writes at a time.")
    reversal = ("Moved the memory store from SQLite to Postgres because several agents now "
                "write at the same time and SQLite locks the whole file on every write.")
    postgres = ("Chose Postgres over SQLite for the memory store because several agents "
                "write at the same time.")
    partial = ("Chose Postgres over SQLite for the memory store because several agents "
               "write at the same time; the connection pool holds 10 connections with 20 "
               "overflow.")
    # One verified decision per project, so each new entry is compared with
    # exactly that one.
    async with App() as off:
        old_a = await off.add(sqlite, project="store-a", type="decision", tags=["database"])
        old_b = await off.add(postgres, project="store-b", type="decision", tags=["database"])
    await plant_review(old_a, APPROVE)
    await plant_review(old_b, APPROVE)

    async with App(reviewer=reviewer) as a:
        new_a = await a.add(reversal, project="store-a", type="decision", tags=["database"])
        new_b = await a.add(partial, project="store-b", type="decision", tags=["database"])
        ra, rb = await a.get(new_a), await a.get(new_b)
        old_a_row, old_b_row = await a.get(old_a), await a.get(old_b)
    print(f"\n[review] reversal -> {json.dumps(ra['review'])} supersedes={ra['supersedes']}; "
          f"partial -> {json.dumps(rb['review'])}")
    assert ra["review"]["verdict"] == "approve" and ra["review_status"] == "verified"
    assert ra["supersedes"] == old_a and old_a_row["superseded_by"] == new_a
    assert rb["review"]["verdict"] == "rewrite" and rb["review_status"] == "flagged"
    assert rb["review"]["duplicate_of"] == old_b
    merged = rb["review"]["rewrite"] or ""
    assert "postgres" in merged.lower() and "pool" in merged.lower()
    assert rb["content"] == partial and rb["supersedes"] is None
    assert old_b_row["content"] == postgres and old_b_row["superseded_by"] is None
