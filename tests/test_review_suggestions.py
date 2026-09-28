"""Tests for the review suggestions: rewritten text and tags (issue #32).

When the model says rewrite it also names tags for the new text, picked from
a list of existing tags the server offers it: the ten closest in meaning to
the entry (by cosine of the entry against the vector each tag stores for its
`name: description`), or the ten most used when the server has no embedding
model. Names off the list are
dropped before anything is stored. The CLI prints the suggestion under the
verdict; nothing is applied.

Same layers as test_review.py: the module on its own (the prompt and the
parser, `urlopen` replaced), the app with a `FakeReviewer` that records the
tags it was offered, the CLI and MCP surfaces against the live server, and
the real model once under the `review` marker.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request

import pytest

from agent_memory.server import review as review_mod
from agent_memory.server.embedding import NullEmbedder, cosine
from agent_memory.server.review import TAG_COUNT, OllamaReviewer, Verdict, parse_verdict
from conftest import FakeEmbedder
from drivers import CliDriver, McpDriver
from test_review import (
    MEMORY,
    NEIGHBOURS,
    _answer,
    _App,
    _fake_urlopen,
    _plant_review,
    _real_server,
)

OFFERED = ["database", "search", "config"]
REWRITE = Verdict("rewrite", 3, "Say why.", "Chose Postgres, because of X.", None,
                  ["database", "search"])


class FakeReviewer:
    """Answers with a fixed verdict and records the tags it was offered."""

    model_name = "fake-reviewer"

    def __init__(self, verdict=REWRITE):
        self.verdict = verdict
        self.calls = []

    def review(self, memory, neighbours, tags=()):
        self.calls.append((memory, neighbours, list(tags)))
        return self.verdict


# ── the prompt ───────────────────────────────────────────────────────────────
def test_system_prompt_asks_for_tags_from_the_list_only():
    prompt = review_mod.SYSTEM_PROMPT
    assert '"tags": [<names from the list only>]' in prompt
    assert "exactly these nine keys" in prompt
    assert "never a name that is not on the list" in prompt


def test_user_prompt_lists_only_the_offered_tags():
    body = OllamaReviewer("http://ollama:11434").request_body(MEMORY, NEIGHBOURS, OFFERED)
    user = body["messages"][1]["content"]
    line = next(l for l in user.splitlines() if l.startswith("Existing tags you may suggest"))
    assert line.endswith("closest in meaning first: database, search, config")
    # The entry's own tag is not on offer unless the server put it there.
    assert "work-log" not in line
    # The entry and its neighbours are still laid out as before.
    assert MEMORY["content"] in user and "id: 3" in user


def test_user_prompt_says_when_there_are_no_tags():
    body = OllamaReviewer("http://ollama:11434").request_body(MEMORY, NEIGHBOURS)
    user = body["messages"][1]["content"]
    assert "There are no existing tags to suggest: tags must be []." in user
    assert "you may suggest" not in user


# ── the parser ───────────────────────────────────────────────────────────────
def test_parse_verdict_keeps_only_offered_tags_in_the_offered_spelling():
    text = _answer(verdict="rewrite", rule=3, rewrite="better",
                   tags=["Search", "made-up", "database", " search ", "database"])
    v = parse_verdict(text, offered_tags=OFFERED)
    assert v.tags == ["search", "database"]
    assert v.as_dict()["tags"] == ["search", "database"]


def test_parse_verdict_drops_tags_unless_the_verdict_is_rewrite():
    assert parse_verdict(_answer(tags=["database"]), offered_tags=OFFERED).tags == []
    approve = _answer(verdict="approve", rule=None, tags=["database"])
    assert parse_verdict(approve, offered_tags=OFFERED).tags == []


def test_parse_verdict_with_an_empty_offer_keeps_nothing():
    text = _answer(verdict="rewrite", rule=3, rewrite="better", tags=["database"])
    assert parse_verdict(text, offered_tags=[]).tags == []


def test_parse_verdict_without_an_offer_keeps_the_names_as_given():
    text = _answer(verdict="rewrite", rule=3, rewrite="better", tags=["a", "A", " b "])
    assert parse_verdict(text).tags == ["a", "b"]


def test_parse_verdict_treats_a_missing_or_null_tags_key_as_empty():
    text = _answer(verdict="rewrite", rule=3, rewrite="better")
    assert parse_verdict(text, offered_tags=OFFERED).tags == []
    assert parse_verdict(_answer(tags=None), offered_tags=OFFERED).tags == []


@pytest.mark.parametrize("tags", ["database", 5, [1, 2], ["database", None], {"a": 1}])
def test_parse_verdict_rejects_tags_that_are_not_a_list_of_strings(tags):
    text = _answer(verdict="rewrite", rule=3, rewrite="better", tags=tags)
    assert parse_verdict(text, offered_tags=OFFERED) is None


def test_ollama_reviewer_drops_a_tag_the_model_made_up(monkeypatch):
    reply = _answer(verdict="rewrite", rule=3, rewrite="better", tags=["made-up", "config"])
    seen = _fake_urlopen(monkeypatch, reply=reply)
    v = OllamaReviewer("http://ollama:11434").review(MEMORY, NEIGHBOURS, OFFERED)
    assert v == Verdict("rewrite", 3, "diary", "better", None, ["config"])
    assert "database, search, config" in seen["body"]["messages"][1]["content"]


# ── the app: which tags are offered, and what is stored ──────────────────────
async def _add_tagged(a, texts, tags):
    """Add memories whose only purpose is to put `tags` in use."""
    for text, tag in zip(texts, tags):
        await a.add(text, project="alpha", tags=[tag])


def _by_meaning(content, tags):
    """The order the server should offer `tags` in: cosine of the FakeEmbedder's
    vectors for the content and each `name: description`, ties by name."""
    emb = FakeEmbedder()
    vec = emb.embed([content])[0]
    scored = [(cosine(vec, emb.embed([f"{name}: {desc}"])[0]), name) for name, desc in tags]
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [name for _, name in scored]


async def test_offered_tags_are_the_nearest_by_meaning_capped_at_ten():
    fake = FakeReviewer(REWRITE)
    tags = [(f"tag{i}", f"about topic {i}") for i in range(TAG_COUNT + 3)]
    async with _App(reviewer=fake) as a:
        for name, desc in tags:
            resp = await a.client.post("/memories", json={
                "content": f"a memory carrying {name}", "project": "alpha", "agent": "tester",
                "tags": [{"name": name, "description": desc}]})
            assert resp.status_code == 201, resp.text
        new = "Chose Postgres for the store."
        mid = (await a.add(new, project="alpha"))["id"]

    memory, _, offered = fake.calls[-1]
    assert memory["id"] == mid
    assert len(offered) == TAG_COUNT
    assert offered == _by_meaning(new, tags)[:TAG_COUNT]
    # Only names that exist: nothing invented, nothing from another spelling.
    assert set(offered) <= {name for name, _ in tags}


async def test_offered_tags_come_from_stored_vectors_and_the_review_embeds_only_the_entry():
    class Recording(FakeEmbedder):
        seen = []

        def embed(self, texts):
            Recording.seen.append(list(texts))
            return super().embed(texts)

    fake = FakeReviewer(REWRITE)
    async with _App(reviewer=fake, embedder=Recording()) as a:
        for name, desc in (("db", "the database layer"), ("ui", "the dashboard")):
            await a.client.post("/memories", json={
                "content": f"carrier for {name}", "project": "alpha", "agent": "tester",
                "tags": [{"name": name, "description": desc}]})
        # Each tag was embedded once, as `name: description`, when it was created.
        assert ["db: the database layer"] in Recording.seen
        assert ["ui: the dashboard"] in Recording.seen
        Recording.seen.clear()
        await a.add("Chose Postgres for the store.", project="alpha")
    # The add embeds the entry for the duplicate check and for the row; the
    # review embeds it once more and reads the tag vectors from the table.
    # No call carries a tag's text.
    assert Recording.seen == [["Chose Postgres for the store."]] * 3
    _, _, offered = fake.calls[-1]
    assert sorted(offered) == ["db", "ui"]


async def test_without_a_model_the_most_used_tags_are_offered():
    fake = FakeReviewer(REWRITE)
    async with _App(reviewer=fake, embedder=NullEmbedder()) as a:
        await _add_tagged(a, ["one", "two", "three"], ["rare", "common", "common"])
        for i in range(TAG_COUNT + 2):
            await a.add(f"filler {i}", project="alpha", tags=[f"filler{i}"])
        await a.add("Chose Postgres for the store.", project="alpha")
    _, _, offered = fake.calls[-1]
    assert len(offered) == TAG_COUNT
    # Most used first, then by name, as `memory tags` lists them.
    assert offered[0] == "common"
    assert offered[1:] == sorted(offered[1:])


async def test_no_tags_in_use_means_nothing_offered():
    fake = FakeReviewer(REWRITE)
    async with _App(reviewer=fake) as a:
        await a.add("the very first memory", project="alpha")
    assert fake.calls[-1][2] == []


async def test_the_stored_review_carries_the_tags_and_every_read_shows_them():
    fake = FakeReviewer(REWRITE)
    async with _App(reviewer=fake) as a:
        mid = (await a.add("Chose Postgres for the store.", project="alpha"))["id"]
        expected = REWRITE.as_dict()
        assert expected["tags"] == ["database", "search"]
        assert (await a.get(mid))["review"] == expected
        listed = (await a.client.get("/memories", params={"project": "alpha"})).json()
        assert listed[0]["review"] == expected
        found = (await a.client.get("/memories/search", params={"q": "Postgres"})).json()
        assert found[0]["review"] == expected

        # A re-review that suggests no tags clears them.
        fake.verdict = Verdict("approve", None, "Fine now.", None, None)
        resp = await a.client.post(f"/admin/review/{mid}")
        assert resp.status_code == 200
        assert resp.json()["tags"] == []
        assert (await a.get(mid))["review"]["tags"] == []


async def test_the_memory_itself_is_never_changed_by_a_suggestion():
    async with _App(reviewer=FakeReviewer(REWRITE)) as a:
        mid = (await a.add("Chose Postgres for the store.", project="alpha",
                           tags=["original"]))["id"]
        row = await a.get(mid)
        assert row["content"] == "Chose Postgres for the store."
        assert row["tags"] == ["original"]
        assert row["review"]["rewrite"] == "Chose Postgres, because of X."
        assert row["review"]["tags"] == ["database", "search"]


# ── the CLI and MCP surfaces, against the live server ────────────────────────
def test_cli_prints_the_suggestion_under_the_verdict(live_server):
    url, token = live_server
    cli = CliDriver(url, token)
    assert "Memory #1 added" in cli.raw("add", "Chose Postgres for the store.",
                                        "--project", "p").stdout
    verdict = Verdict("rewrite", 3, "Say why.",
                      "Chose Postgres for the store,\nbecause several agents write at once.",
                      None, ["database", "search"])
    asyncio.run(_plant_review(1, verdict))

    shown = cli.raw("show", "1").stdout
    lines = shown.splitlines()
    at = lines.index("review: rewrite, rule 3: Say why.")
    assert lines[at + 1:at + 5] == [
        "suggested:",
        "    Chose Postgres for the store,",
        "    because several agents write at once.",
        "suggested tags: database, search",
    ]
    # The suggestion sits with the meta lines; the content follows, unchanged.
    assert shown.index("suggested tags:") < shown.index("\nChose Postgres for the store.")
    assert cli.get(1).content == "Chose Postgres for the store."

    queried = cli.raw("query", "--project", "p").stdout
    assert queried.count("suggested:") == 1 and "suggested tags: database, search" in queried


def test_cli_prints_no_tags_line_without_tags_and_no_block_without_a_rewrite(live_server):
    url, token = live_server
    cli = CliDriver(url, token)
    cli.raw("add", "first", "--project", "p")
    cli.raw("add", "second", "--project", "p")
    asyncio.run(_plant_review(1, Verdict("rewrite", 3, "Say why.", "first, because of X.",
                                         None, [])))
    asyncio.run(_plant_review(2, Verdict("reject", 2, "A diary line.", None, None)))

    first = cli.raw("show", "1").stdout
    assert "suggested:\n    first, because of X.\n" in first
    assert "suggested tags" not in first

    second = cli.raw("show", "2").stdout
    assert "review: reject, rule 2: A diary line." in second
    assert "suggested" not in second


def test_mcp_show_carries_the_suggestion(live_server):
    url, token = live_server
    mcp = McpDriver(url, token)
    mid = mcp.add("Chose Postgres for the store.", project="p")
    verdict = Verdict("rewrite", 3, "Say why.", "Chose Postgres, because of X.", None,
                      ["database"])
    asyncio.run(_plant_review(mid, verdict))
    shown = mcp._call("memory_show", id=mid)["memory"]
    assert shown["review"] == verdict.as_dict()
    assert shown["content"] == "Chose Postgres for the store."


# ── the real model, once ─────────────────────────────────────────────────────
@pytest.mark.review
def test_real_model_flags_a_decision_without_a_reason_and_keeps_to_the_list():
    url = _real_server()
    if url is None:
        pytest.skip("AGENT_MEMORY_REVIEW_URL is unset or the server did not answer in 2 s")
    model = os.environ.get("AGENT_MEMORY_REVIEW_MODEL", "").strip() or review_mod.DEFAULT_MODEL
    # A long timeout: the first call may have to load the model into memory.
    reviewer = OllamaReviewer(url, model, timeout=180)
    offered = ["database", "search", "config", "testing", "cli", "review", "docs", "dashboard"]
    neighbours = [{"id": 3, "project": "agent-memory", "type": "decision", "tags": ["search"],
                   "content": "Use a plain REAL[] column for vectors and compare in Python, "
                              "because the database host has no pgvector and the table is small."}]
    bare = {"id": 9, "project": "agent-memory", "type": "decision", "tags": [],
            "content": "Chose Postgres over SQLite for the memory store."}

    t0 = time.perf_counter()
    verdict = reviewer.review(bare, neighbours, offered)
    seconds = time.perf_counter() - t0
    print(f"\n[review] {model} at {url}: bare decision {seconds:.1f}s -> {verdict}")

    assert verdict is not None and verdict.verdict in ("reject", "rewrite")
    assert verdict.reason
    assert set(verdict.tags) <= set(offered)
    if verdict.verdict == "rewrite":
        assert verdict.rewrite
    else:
        assert verdict.tags == []
