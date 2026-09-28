"""Tests for the supersedes link and the merged rewrite (issue #59).

A new memory that reverses or replaces an older one is stored with
`supersedes` set to the old id, from the model's verdict and never from the
request; the old one reads as `superseded_by` the newest such memory. Both
stay in the timeline, sorted as before; `current=true` (`--current`) hides
the superseded ones and is off by default. A new entry that repeats an older
one and adds to it gets a `rewrite` verdict with the merged text and
`duplicate_of` the old id: in flag mode the memory is stored and flagged with
that suggestion, in refuse mode the write is refused and the message says to
apply the merged text to the old memory with `memory update`. The model never
changes a stored memory.

Layers: the verdict and the parser; the prompt; the link stored from a
verdict on the three paths (flag background, refuse, catch-up) and on a
re-review, never from a request; `superseded_by`; deletion and the update
reset; the CLI header and the drivers; `--current` on query and on each
search mode on the three surfaces; the merge suggestion in flag and in
refuse mode; the real model once, under the `review` marker. The migration
is in tests/test_migration.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select, update

from agent_memory.client import ApiClient, ReviewRefused
from agent_memory.server import repository as repo
from agent_memory.server import review as review_mod
from agent_memory.server.app import create_app
from agent_memory.server.db import make_sessionmaker
from agent_memory.server.models import Memory
from agent_memory.server.review import NullReviewer, OllamaReviewer, Verdict, parse_verdict
from agent_memory.server.schemas import MemoryOut, ReviewOut
from conftest import TOKEN, FakeEmbedder, live_app, make_test_engine, verify
from drivers import CliDriver, McpDriver
from test_review import (
    APPROVE,
    MEMORY,
    NEIGHBOURS,
    REJECT,
    FakeReviewer,
    _answer,
    _App,
    _fake_urlopen,
    _plant_review,
    _real_server,
    _review_rows,
)
from test_review_status import Scripted, _post, _set_age, _unverified

OLD = "Chose SQLite for the store because one agent writes at a time."
NEW = "Moved the store to Postgres because several agents write at once."
OTHER = "Tag names in the store are matched without regard to case because writers spell them differently."

# The model's answer to an entry that reverses the listed neighbour #1.
SUPERSEDE = Verdict("approve", None, "Reverses #1 and says why.", None, None, [], supersedes=1)
# The model's answer to an entry that repeats #1 and adds to it: the merged text.
MERGED = "Chose Postgres, because several agents write at once; the pool holds 10 connections."
MERGE = Verdict("rewrite", None, "Repeats #1 and adds the pool size.", MERGED, 1, ["database"])
REPEAT = Verdict("reject", None, "Says the same as #1.", None, 1)
REWRITE = Verdict("rewrite", 3, "Say why.", "Chose Postgres, because of X.", None, ["database"])


def _supersede(old):
    return Verdict("approve", None, f"Reverses #{old} and says why.", None, None, [],
                   supersedes=old)


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


async def _links_in_db():
    """`[(id, supersedes), ...]` straight from the table, by id."""
    engine = make_test_engine()
    try:
        async with engine.connect() as conn:
            stmt = select(Memory.id, Memory.supersedes).order_by(Memory.id)
            return [tuple(r) for r in (await conn.execute(stmt)).all()]
    finally:
        await engine.dispose()


async def _get(mid):
    """One memory as the API would dump it, through the repository."""
    engine = make_test_engine()
    try:
        async with make_sessionmaker(engine)() as session:
            return await repo.get(session, mid)
    finally:
        await engine.dispose()


async def _date(mid, when):
    """Set memory `mid`'s timestamp to exactly `when`."""
    engine = make_test_engine()
    try:
        async with make_sessionmaker(engine)() as session, session.begin():
            await session.execute(update(Memory).where(Memory.id == mid).values(timestamp=when))
    finally:
        await engine.dispose()


# ── the verdict and the parser ───────────────────────────────────────────────
def test_verdict_has_supersedes_with_none_as_the_default():
    plain = Verdict("approve", None, "Fine.", None, None)
    assert plain.supersedes is None
    assert plain.as_dict()["supersedes"] is None
    assert SUPERSEDE.as_dict() == {
        "verdict": "approve", "rule": None, "reason": "Reverses #1 and says why.",
        "rewrite": None, "duplicate_of": None, "tags": [], "supersedes": 1}
    assert MERGE.as_dict()["duplicate_of"] == 1 and MERGE.as_dict()["supersedes"] is None


def test_memory_out_and_review_out_carry_the_links():
    m = MemoryOut(id=1)
    assert m.supersedes is None and m.superseded_by is None
    assert {"supersedes", "superseded_by"} <= set(MemoryOut.model_fields)
    assert ReviewOut(verdict="approve").supersedes is None


def test_parse_verdict_keeps_a_supersedes_of_a_listed_neighbour():
    v = parse_verdict(_answer(verdict="approve", rule=None, supersedes=4), neighbour_ids=[3, 4])
    assert v.verdict == "approve" and v.supersedes == 4 and v.duplicate_of is None


def test_parse_verdict_rejects_a_supersedes_outside_the_neighbours():
    text = _answer(verdict="approve", rule=None, supersedes=99)
    assert parse_verdict(text, neighbour_ids=[3, 4]) is None
    # The entry itself is never a listed neighbour, so it cannot be named.
    assert parse_verdict(_answer(verdict="approve", rule=None, supersedes=7),
                         neighbour_ids=[3, 4]) is None


@pytest.mark.parametrize("value", ["4", True, 4.0, [4], {"id": 4}])
def test_parse_verdict_rejects_a_supersedes_that_is_not_a_number(value):
    text = _answer(verdict="approve", rule=None, supersedes=value)
    assert parse_verdict(text, neighbour_ids=[3, 4]) is None


def test_parse_verdict_treats_a_missing_or_null_supersedes_as_none():
    assert parse_verdict(_answer(), neighbour_ids=[3, 4]).supersedes is None
    assert parse_verdict(_answer(supersedes=None), neighbour_ids=[3, 4]).supersedes is None


def test_parse_verdict_without_neighbour_ids_accepts_any_supersedes():
    assert parse_verdict(_answer(verdict="approve", rule=None, supersedes=99)).supersedes == 99


def test_parse_verdict_reads_a_merge():
    text = _answer(verdict="rewrite", rule=None, rewrite="old plus new", duplicate_of=3,
                   tags=["db", "made-up"])
    v = parse_verdict(text, neighbour_ids=[3], offered_tags=["db"])
    assert v == Verdict("rewrite", None, "diary", "old plus new", 3, ["db"])
    assert v.supersedes is None


def test_ollama_reviewer_a_supersedes_of_an_unlisted_entry_is_bad_json(monkeypatch):
    _fake_urlopen(monkeypatch, reply=_answer(verdict="approve", rule=None, supersedes=42))
    assert OllamaReviewer("http://ollama:11434").review(MEMORY, NEIGHBOURS) is None
    _fake_urlopen(monkeypatch, reply=_answer(verdict="approve", rule=None, supersedes=3))
    v = OllamaReviewer("http://ollama:11434").review(MEMORY, NEIGHBOURS)
    assert v is not None and v.supersedes == 3


# ── the prompt ───────────────────────────────────────────────────────────────
def test_system_prompt_asks_for_supersedes_and_lists_the_two_cases():
    prompt = review_mod.SYSTEM_PROMPT
    assert "exactly these seven keys" in prompt
    assert '"supersedes": <id or null>' in prompt
    # The two cases sit in the checklist, each with one example.
    assert "reverses or replaces what a listed existing entry says" in prompt
    assert "approve, supersedes that entry's id" in prompt
    assert "adds something to it" in prompt and "merged text" in prompt
    assert "duplicate_of that entry's id, and rule null" in prompt
    assert prompt.count("Example: the listed entry says") == 2
    # The examples name no id, so the model cannot copy one that is not listed.
    assert not re.search(r"supersedes \d", prompt)
    assert not re.search(r"duplicate_of \d", prompt)
    # A reversal comes before the repeat checks, so it is never called a repeat.
    assert prompt.index("reverses or replaces what a listed") < prompt.index("in other words, and nothing more")


# ── the link is stored from a verdict, on every path ─────────────────────────
async def test_flag_mode_stores_the_link_from_the_background_verdict():
    fake = FakeReviewer(APPROVE)
    async with _App(reviewer=fake) as a:
        old = (await a.add(OLD))["id"]
        fake.verdict = SUPERSEDE
        new = (await a.add(NEW))["id"]
        # The model saw the old one as the listed neighbour, and the new one
        # with no link yet.
        memory, neighbours = fake.calls[-1]
        assert memory["id"] == new and memory["supersedes"] is None
        assert [n["id"] for n in neighbours] == [old]

        row = await a.get(new)
        assert row["supersedes"] == old and row["superseded_by"] is None
        assert row["review_status"] == "verified"
        assert row["review"] == SUPERSEDE.as_dict()
        old_row = await a.get(old)
        assert old_row["supersedes"] is None and old_row["superseded_by"] == new
        assert old_row["content"] == OLD

        # Every read carries both ends, in the same shape.
        listed = (await a.client.get("/memories")).json()
        assert {m["id"]: (m["supersedes"], m["superseded_by"]) for m in listed} == {
            old: (None, new), new: (old, None)}
        found = (await a.client.get("/memories/search", params={"q": "store"})).json()
        assert {m["id"]: (m["supersedes"], m["superseded_by"]) for m in found} == {
            old: (None, new), new: (old, None)}
        assert set(listed[0]) == set(found[0]) == set(row)
    assert await _links_in_db() == [(old, None), (new, old)]


async def test_refuse_mode_stores_the_link_with_the_write():
    fake = FakeReviewer(APPROVE)
    async with _App(reviewer=fake, review_mode="refuse") as a:
        old = (await a.add(OLD))["id"]
        fake.verdict = SUPERSEDE
        resp = await _post(a, NEW)
        assert resp.status_code == 201
        assert set(resp.json()) == {"id", "warnings"}
        new = resp.json()["id"]
        row = await a.get(new)
        assert row["supersedes"] == old and row["review_status"] == "verified"
        assert (await a.get(old))["superseded_by"] == new
    # One call for the new entry, before it had an id, and no second one.
    assert len(fake.calls) == 2 and fake.calls[-1][0]["id"] is None
    assert await _review_rows() == [(old, "approve", "fake-reviewer"),
                                    (new, "approve", "fake-reviewer")]
    assert await _links_in_db() == [(old, None), (new, old)]


async def test_catch_up_stores_the_link():
    old, new = await _unverified(OLD, NEW)
    await _set_age(old, 20)
    await _set_age(new, 10)
    scripted = Scripted({NEW: _supersede(old)})
    async with _App(reviewer=scripted) as a:
        assert (await a.client.post("/admin/review")).json() == {"scheduled": 2}
        assert (await a.get(new))["supersedes"] == old
        assert (await a.get(old))["superseded_by"] == new
    # The old one was verified first, so it was the listed neighbour when
    # the new one was read.
    assert [m["id"] for m, _ in scripted.calls] == [old, new]
    assert [n["id"] for n in scripted.calls[1][1]] == [old]
    assert await _links_in_db() == [(old, None), (new, old)]


async def test_a_re_review_moves_or_clears_the_link():
    fake = FakeReviewer(APPROVE)
    async with _App(reviewer=fake) as a:
        first = (await a.add("first choice, because of A"))["id"]
        second = (await a.add("second choice, because of B"))["id"]
        fake.verdict = _supersede(first)
        new = (await a.add("third choice, because of C"))["id"]
        assert (await a.get(new))["supersedes"] == first

        # A new verdict names the other one: the link moves.
        fake.verdict = _supersede(second)
        resp = await a.client.post(f"/admin/review/{new}")
        assert resp.status_code == 200 and resp.json()["supersedes"] == second
        assert (await a.get(new))["supersedes"] == second
        assert (await a.get(first))["superseded_by"] is None
        assert (await a.get(second))["superseded_by"] == new

        # No verdict keeps the link, as it keeps the row.
        fake.verdict = None
        assert (await a.client.post(f"/admin/review/{new}")).status_code == 502
        assert (await a.get(new))["supersedes"] == second

        # A verdict without one clears it.
        fake.verdict = APPROVE
        assert (await a.client.post(f"/admin/review/{new}")).status_code == 200
        assert (await a.get(new))["supersedes"] is None
        assert (await a.get(second))["superseded_by"] is None
    assert await _links_in_db() == [(first, None), (second, None), (new, None)]


async def test_the_request_can_never_set_the_link():
    async with _App(reviewer=NullReviewer()) as a:
        old = (await a.add(OLD))["id"]
        resp = await a.client.post("/memories", json={
            "content": NEW, "agent": "tester", "project": "alpha", "supersedes": old})
        assert resp.status_code == 201
        new = resp.json()["id"]
        assert (await a.get(new))["supersedes"] is None
        assert (await a.get(old))["superseded_by"] is None
        # Nor can an update.
        resp = await a.client.patch(f"/memories/{new}", json={"supersedes": old})
        assert resp.status_code == 200 and resp.json()["changes"] == []
        assert (await a.get(new))["supersedes"] is None
    assert await _links_in_db() == [(old, None), (new, None)]


def test_the_mcp_tools_take_current_but_never_supersedes(live_server):
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    url, token = live_server
    mcp = McpDriver(url, token)._mcp

    async def schemas():
        async with connect(mcp) as session:
            tools = (await session.list_tools()).tools
            return {t.name: set(t.inputSchema.get("properties", {})) for t in tools}

    fields = asyncio.run(schemas())
    assert "supersedes" not in fields["memory_add"]
    assert "supersedes" not in fields["memory_update"]
    assert "current" in fields["memory_query"] and "current" in fields["memory_search"]


async def test_set_review_refuses_a_self_link_and_an_unknown_target():
    engine = make_test_engine()
    try:
        async with make_sessionmaker(engine)() as s, s.begin():
            mid = await repo.add(s, "one", "t", "alpha", [], None)
            with pytest.raises(ValueError, match="itself"):
                await repo.set_review(s, mid, _supersede(mid), "m")
            with pytest.raises(LookupError, match="#999"):
                await repo.set_review(s, mid, _supersede(999), "m")
            # Nothing was changed by either.
            row = await repo.get(s, mid)
            assert row["review_status"] == "unverified" and row["review"] is None
            assert row["supersedes"] is None
            # A good one sets both the status and the link, and the returned
            # view carries the link.
            other = await repo.add(s, "two", "t", "alpha", [], None)
            view = await repo.set_review(s, mid, _supersede(other), "m")
            assert view["supersedes"] == other and view["verdict"] == "approve"
            assert (await repo.get(s, mid))["supersedes"] == other
            assert (await repo.get(s, other))["superseded_by"] == mid
    finally:
        await engine.dispose()


async def test_a_verdict_naming_a_missing_memory_is_one_log_line(caplog):
    fake = FakeReviewer(_supersede(999))
    with caplog.at_level(logging.WARNING, logger="agent_memory.server.app"):
        async with _App(reviewer=fake) as a:
            mid = (await a.add(NEW))["id"]
            row = await a.get(mid)
            # Stored, unverified, no row, no link: the verdict was unusable.
            assert row["review_status"] == "unverified" and row["review"] is None
            assert row["supersedes"] is None
    assert [r.getMessage() for r in caplog.records] == [
        f"review of memory #{mid} failed: LookupError: Memory #999 not found"]
    assert await _review_rows() == []


# ── superseded_by ────────────────────────────────────────────────────────────
async def test_superseded_by_is_the_newest_by_timestamp_then_id():
    old, two, three, four = await _unverified(OLD, "second", "third", "fourth")
    for mid in (two, three, four):
        await _plant_review(mid, _supersede(old))
    # All three point at the old one; the newest wins, which is the highest
    # id while the timestamps follow the ids.
    assert (await _get(old))["superseded_by"] == four
    assert [(await _get(mid))["supersedes"] for mid in (two, three, four)] == [old, old, old]

    # The timeline decides, not the id: date the third one last.
    base = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    await _date(two, base)
    await _date(four, base + timedelta(minutes=1))
    await _date(three, base + timedelta(minutes=2))
    assert (await _get(old))["superseded_by"] == three
    # On the same timestamp the higher id wins.
    await _date(four, base + timedelta(minutes=2))
    assert (await _get(old))["superseded_by"] == four


async def test_deleting_either_end_clears_the_link():
    one, two, three = await _unverified("one", "two", "three")
    await _plant_review(two, _supersede(one))
    await _plant_review(three, _supersede(two))
    assert await _links_in_db() == [(one, None), (two, one), (three, two)]
    async with _App(reviewer=NullReviewer()) as a:
        # The old end goes: the link on the new one is cleared, nothing else moves.
        await a.client.request("DELETE", "/memories", params={"ids": [one]})
        row = await a.get(two)
        assert row["supersedes"] is None and row["superseded_by"] == three
        # The new end goes: the old one is current again.
        await a.client.request("DELETE", "/memories", params={"ids": [three]})
        assert (await a.get(two))["superseded_by"] is None
    assert await _links_in_db() == [(two, None)]


async def test_new_content_clears_the_link_and_tags_alone_keep_it():
    old, new = await _unverified(OLD, NEW)
    await _plant_review(new, _supersede(old))
    async with _App(reviewer=NullReviewer()) as a:
        resp = await a.client.patch(f"/memories/{new}", json={"add_tags": [{"name": "db"}]})
        assert resp.status_code == 200
        row = await a.get(new)
        assert row["supersedes"] == old and row["review"] is not None
        assert row["review_status"] == "verified"

        resp = await a.client.patch(f"/memories/{new}", json={"content": "other words"})
        assert resp.status_code == 200 and resp.json()["changes"] == ["content"]
        row = await a.get(new)
        assert row["supersedes"] is None and row["review"] is None
        assert row["review_status"] == "unverified"
        assert (await a.get(old))["superseded_by"] is None
    assert await _links_in_db() == [(old, None), (new, None)]


# ── the CLI header and the drivers ───────────────────────────────────────────
def _three(driver):
    """#1 old, #2 supersedes #1 (both verified), #3 on its own. All three
    share the word `store`, so every search mode finds them."""
    assert [driver.add(t, project="p") for t in (OLD, NEW, OTHER)] == [1, 2, 3]
    verify(1, 3)
    asyncio.run(_plant_review(2, SUPERSEDE))


def test_cli_prints_both_links_in_the_header(live_server):
    url, token = live_server
    cli = CliDriver(url, token)
    _three(cli)

    assert re.search(r"^━━━ #2 status: verified supersedes #1 ━+$", cli.raw("show", "2").stdout, re.M)
    assert re.search(r"^━━━ #1 status: verified superseded by #2 ━+$", cli.raw("show", "1").stdout, re.M)
    assert re.search(r"^━━━ #3 status: verified ━+$", cli.raw("show", "3").stdout, re.M)

    queried = cli.raw("query", "--project", "p").stdout
    assert re.findall(r"^━━━ #(\d+) status: verified ?(.*?) ━+$", queried, re.M) == [
        ("3", ""), ("2", "supersedes #1"), ("1", "superseded by #2")]

    # A search result keeps its score, after the link.
    found = cli.raw("search", "store", "--project", "p").stdout
    assert re.search(r"^━━━ #2 status: verified supersedes #1 score \d\.\d\d ━+$", found, re.M)
    assert re.search(r"^━━━ #1 status: verified superseded by #2 score \d\.\d\d ━+$", found, re.M)

    # A memory can do both: #4 supersedes #2.
    assert cli.add("Back to SQLite for the store because the server went away.", project="p") == 4
    asyncio.run(_plant_review(4, _supersede(2)))
    assert re.search(r"^━━━ #2 status: verified supersedes #1 superseded by #4 ━+$",
                     cli.raw("show", "2").stdout, re.M)
    # The review listing prints the same header.
    asyncio.run(_plant_review(3, REJECT))
    assert re.search(r"^━━━ #3 status: flagged ━+$", cli.raw("review").stdout, re.M)

    # The driver reads both out of the header; the content is unchanged.
    two = cli.get(2)
    assert (two.supersedes, two.superseded_by, two.content) == (1, 4, NEW)
    one = cli.get(1)
    assert (one.supersedes, one.superseded_by) == (None, 2)
    assert [(m.id, m.supersedes, m.superseded_by) for m in cli.query(project="p")] == [
        (4, 2, None), (3, None, None), (2, 1, 4), (1, None, 2)]
    hit = next(m for m in cli.search("store", project="p") if m.id == 2)
    assert hit.supersedes == 1 and hit.superseded_by == 4 and hit.score is not None


def test_every_driver_reports_both_links(driver):
    _three(driver)
    two, one, three = driver.get(2), driver.get(1), driver.get(3)
    assert (two.supersedes, two.superseded_by) == (1, None)
    assert (one.supersedes, one.superseded_by) == (None, 2)
    assert (three.supersedes, three.superseded_by) == (None, None)
    assert {m.id: (m.supersedes, m.superseded_by) for m in driver.query(project="p")} == {
        1: (None, 2), 2: (1, None), 3: (None, None)}
    hit = next(m for m in driver.search("store", project="p") if m.id == 2)
    assert hit.supersedes == 1 and hit.score is not None


# ── --current, on query and on each search mode, on the three surfaces ───────
def test_current_hides_superseded_memories_on_query_and_the_default_shows_them(driver):
    _three(driver)
    assert [m.id for m in driver.query(project="p")] == [3, 2, 1]
    assert [m.id for m in driver.query(project="p", current=True)] == [3, 2]
    # Together with another filter.
    assert [m.id for m in driver.query(project="p", current=True, status="verified")] == [3, 2]
    assert [m.id for m in driver.query(project="nope", current=True)] == []


@pytest.mark.parametrize("mode", ["keyword", "semantic", "hybrid"])
def test_current_hides_superseded_memories_on_search(driver, mode):
    _three(driver)
    assert {m.id for m in driver.search("store", mode=mode, project="p")} == {1, 2, 3}
    assert {m.id for m in driver.search("store", mode=mode, project="p", current=True)} == {2, 3}
    # The default is unchanged when the flag is off.
    assert {m.id for m in driver.search("store", mode=mode, project="p", current=False)} == {1, 2, 3}


def test_current_keeps_only_the_end_of_a_chain(driver):
    texts = ["store one, because of A", "store two, because of B", "store three, because of C"]
    assert [driver.add(t, project="p") for t in texts] == [1, 2, 3]
    asyncio.run(_plant_review(2, _supersede(1)))
    asyncio.run(_plant_review(3, _supersede(2)))
    assert [m.id for m in driver.query(project="p")] == [3, 2, 1]
    assert [m.id for m in driver.query(project="p", current=True)] == [3]
    for mode in ("keyword", "semantic", "hybrid"):
        assert [m.id for m in driver.search("store", mode=mode, project="p", current=True)] == [3]


def test_api_current_counts_the_total_after_the_filter(live_server):
    url, token = live_server
    _three(CliDriver(url, token))
    with httpx.Client(base_url=url, headers=_auth()) as c:
        resp = c.get("/memories", params={"current": "true"})
        assert [m["id"] for m in resp.json()] == [3, 2]
        assert resp.headers["X-Total-Count"] == "2"
        assert c.get("/memories").headers["X-Total-Count"] == "3"
        assert c.get("/memories", params={"current": "false"}).headers["X-Total-Count"] == "3"
        for mode in ("keyword", "semantic", "hybrid"):
            hits = c.get("/memories/search", params={"q": "store", "mode": mode, "current": "true"})
            assert {m["id"] for m in hits.json()} == {2, 3}, mode
        # Not a boolean: 422, like any bad query value.
        assert c.get("/memories", params={"current": "maybe"}).status_code == 422
        assert c.get("/memories/search", params={"q": "store", "current": "maybe"}).status_code == 422


def test_cli_current_flag_on_query_and_search(live_server):
    url, token = live_server
    cli = CliDriver(url, token)
    _three(cli)
    out = cli.raw("query", "--project", "p", "--current").stdout
    assert re.findall(r"^━━━ #(\d+) ", out, re.M) == ["3", "2"]
    assert "Found 2 memories" in out
    assert "superseded by" not in out and "supersedes #1" in out
    out = cli.raw("search", "store", "--project", "p", "--current", "--mode", "hybrid").stdout
    assert sorted(re.findall(r"^━━━ #(\d+) ", out, re.M)) == ["2", "3"]
    for command in ("query", "search"):
        assert "--current" in cli.raw(command, "--help").stdout


def test_mcp_current_on_query_and_search(live_server):
    url, token = live_server
    mcp = McpDriver(url, token)
    _three(mcp)
    assert [m["id"] for m in mcp._call("memory_query", project="p", current=True)["memories"]] == [3, 2]
    assert [m["id"] for m in mcp._call("memory_query", project="p")["memories"]] == [3, 2, 1]
    for mode in ("keyword", "semantic", "hybrid"):
        rows = mcp._call("memory_search", q="store", mode=mode, project="p", current=True)["memories"]
        assert {m["id"] for m in rows} == {2, 3}, mode
        rows = mcp._call("memory_search", q="store", mode=mode, project="p")["memories"]
        assert {m["id"] for m in rows} == {1, 2, 3}, mode
    shown = mcp._call("memory_show", id=1)["memory"]
    assert shown["supersedes"] is None and shown["superseded_by"] == 2


def test_api_client_passes_current_through(live_server):
    url, token = live_server
    _three(CliDriver(url, token))
    api = ApiClient(url, token)
    assert [m["id"] for m in api.query(project="p", current=True)] == [3, 2]
    rows, total = api.query_with_total(project="p", current=True, limit=1)
    assert [m["id"] for m in rows] == [3] and total == 2
    assert [m["id"] for m in api.query(project="p")] == [3, 2, 1]
    for mode in ("keyword", "semantic", "hybrid"):
        assert {m["id"] for m in api.search("store", project="p", mode=mode, current=True)} == {2, 3}
    assert {m["id"] for m in api.search("store", project="p")} == {1, 2, 3}
    assert api.get(2)["supersedes"] == 1 and api.get(1)["superseded_by"] == 2


# ── the merge suggestion in flag mode ────────────────────────────────────────
async def test_flag_mode_stores_a_merge_flagged_with_the_suggestion_and_changes_nothing():
    fake = FakeReviewer(APPROVE)
    async with _App(reviewer=fake) as a:
        old = (await a.add("Chose Postgres, because several agents write at once.",
                           tags=["database"]))["id"]
        fake.verdict = MERGE
        text = "Chose Postgres, because several agents write at once; the pool holds 10 connections."
        new = (await a.add(text))["id"]
        row = await a.get(new)
        assert row["review_status"] == "flagged"
        assert row["review"] == MERGE.as_dict()
        assert row["review"]["duplicate_of"] == old and row["review"]["rewrite"] == MERGED
        assert row["review"]["rule"] is None
        # Nothing was applied, and nothing links the two.
        assert row["content"] == text and row["supersedes"] is None
        old_row = await a.get(old)
        assert old_row["content"] == "Chose Postgres, because several agents write at once."
        assert old_row["superseded_by"] is None and old_row["review_status"] == "verified"
        # It is listed with the other flagged memories.
        flagged = (await a.client.get("/memories/flagged")).json()
        assert [m["id"] for m in flagged] == [new]
        assert flagged[0]["review"]["duplicate_of"] == old
    assert await _review_rows() == [(old, "approve", "fake-reviewer"),
                                    (new, "rewrite", "fake-reviewer")]


def test_cli_shows_the_merge_suggestion_and_the_writer_applies_it(live_server):
    url, token = live_server
    cli = CliDriver(url, token)
    assert cli.add("Chose Postgres, because several agents write at once.", project="p") == 1
    assert cli.add("Chose Postgres, because several agents write at once; the pool holds 10 "
                   "connections.", project="p", force=True) == 2
    verify(1)
    asyncio.run(_plant_review(2, MERGE))

    shown = cli.raw("show", "2").stdout
    lines = shown.splitlines()
    at = lines.index("review: rewrite, duplicate of #1: Repeats #1 and adds the pool size.")
    assert lines[at + 1:at + 4] == ["suggested:", f"    {MERGED}", "suggested tags: database"]
    assert re.search(r"^━━━ #2 status: flagged ━+$", shown, re.M)
    listed = cli.raw("review").stdout
    assert "review: rewrite, duplicate of #1:" in listed and f"    {MERGED}" in listed

    # The merge is the writer's to apply, on the old memory.
    proc = cli.raw("update", "1", "--content", MERGED)
    assert proc.returncode == 0 and "updated: content" in proc.stdout
    assert cli.get(1).content == MERGED
    assert cli.get(2).content.startswith("Chose Postgres, because several agents write at once; the pool")


# ── the merge in refuse mode ────────────────────────────────────────────────
async def test_refuse_mode_refuses_a_merge_with_the_old_id_and_the_merged_text():
    fake = FakeReviewer(APPROVE)
    async with _App(reviewer=fake, review_mode="refuse") as a:
        old = (await a.add("Chose Postgres, because several agents write at once."))["id"]
        fake.verdict = MERGE
        resp = await _post(a, "Chose Postgres, because several agents write at once; the pool "
                              "holds 10 connections.")
        assert resp.status_code == 422
        assert resp.json() == {"detail": {
            "reason": "review", "verdict": "rewrite", "rule": None,
            "explanation": "Repeats #1 and adds the pool size.", "rewrite": MERGED,
            "tags": ["database"], "duplicate_of": old}}
        listed = (await a.client.get("/memories", params={"limit": 0})).json()
        assert [m["id"] for m in listed] == [old]
        assert listed[0]["content"] == "Chose Postgres, because several agents write at once."
    assert await _review_rows() == [(old, "approve", "fake-reviewer")]


@pytest.fixture(scope="module")
def refusing_server(_schema):
    """A live server with a `FakeReviewer` in refuse mode. Yields
    `(url, token, reviewer)`; a test sets `reviewer.verdict` as it needs."""
    fake = FakeReviewer(APPROVE)
    engine = make_test_engine()
    app = create_app(sessionmaker=make_sessionmaker(engine), token=TOKEN,
                     embedder=FakeEmbedder(), reviewer=fake, review_mode="refuse",
                     review_poll=0)
    with live_app(app) as url:
        yield url, TOKEN, fake
    asyncio.run(engine.dispose())


@pytest.fixture
def refusing(refusing_server):
    """The server above with #1 stored and verified, and the reviewer set to
    answer the merge for whatever comes next."""
    url, token, fake = refusing_server
    fake.verdict = APPROVE
    fake.calls.clear()
    cli = CliDriver(url, token)
    assert cli.add("Chose Postgres, because several agents write at once.", project="p") == 1
    fake.verdict = MERGE
    return url, token, fake


def test_cli_merge_message_says_to_update_the_old_memory(refusing):
    url, token, _ = refusing
    cli = CliDriver(url, token)
    proc = cli.raw("add", "Chose Postgres, because several agents write at once; the pool "
                          "holds 10 connections.", "--project", "p")
    assert proc.returncode == 4
    assert proc.stdout.splitlines() == [
        "✗ Review: rewrite, duplicate of #1: Repeats #1 and adds the pool size.",
        "suggested:",
        f"    {MERGED}",
        "suggested tags: database",
        "Apply it with 'memory update 1' instead of adding.",
    ]
    assert "Traceback" not in proc.stderr
    assert [m.id for m in cli.query()] == [1]
    # Applying it is the writer's call, and it goes on the old memory.
    proc = cli.raw("update", "1", "--content", MERGED)
    assert proc.returncode == 0, proc.stderr
    assert cli.get(1).content == MERGED
    assert [m.id for m in cli.query()] == [1]


def test_cli_other_refusals_keep_their_last_line(refusing):
    url, token, fake = refusing
    cli = CliDriver(url, token)
    fake.verdict = REPEAT
    proc = cli.raw("add", "Chose Postgres because several agents write at once.", "--project", "p")
    assert proc.returncode == 4
    assert proc.stdout.splitlines() == [
        "✗ Review: reject, duplicate of #1: Says the same as #1.",
        "Fix the entry, or pass --force to store it as written.",
    ]
    fake.verdict = REWRITE
    proc = cli.raw("add", "Chose Postgres.", "--project", "p")
    assert proc.returncode == 4
    assert proc.stdout.splitlines()[-1] == "Fix the entry, or pass --force to store it as written."
    assert "Apply it" not in proc.stdout


def test_cli_refuse_mode_stores_a_reversal_with_the_link(refusing):
    url, token, fake = refusing
    cli = CliDriver(url, token)
    fake.verdict = SUPERSEDE
    proc = cli.raw("add", "Moved to SQLite, because the server went away.", "--project", "p")
    assert proc.returncode == 0, proc.stderr
    assert "✓ Memory #2 added (tester)" in proc.stdout
    assert re.search(r"^━━━ #2 status: verified supersedes #1 ━+$", cli.raw("show", "2").stdout, re.M)
    assert re.search(r"^━━━ #1 status: verified superseded by #2 ━+$", cli.raw("show", "1").stdout, re.M)
    assert [m.id for m in cli.query(project="p", current=True)] == [2]
    assert len(fake.calls) == 2


def test_mcp_merge_error_carries_duplicate_of_and_the_merged_text(refusing):
    url, token, _ = refusing
    mcp = McpDriver(url, token)
    out = mcp._call("memory_add", content="Chose Postgres, because several agents write at once; "
                                          "the pool holds 10 connections.",
                    project="p", agent="tester")
    assert out == {"error": "review", "verdict": "rewrite", "rule": None,
                   "explanation": "Repeats #1 and adds the pool size.", "rewrite": MERGED,
                   "tags": ["database"], "duplicate_of": 1}
    assert [m.id for m in mcp.query()] == [1]
    # The writer applies it through the update tool.
    assert mcp._call("memory_update", id=1, content=MERGED) == {"found": True, "changes": ["content"]}
    assert mcp.get(1).content == MERGED


def test_api_client_raises_review_refused_with_the_merge(refusing):
    url, token, _ = refusing
    api = ApiClient(url, token)
    with pytest.raises(ReviewRefused) as caught:
        api.add("Chose Postgres, because several agents write at once; the pool holds 10 "
                "connections.", "tester", "p", [], "decision")
    e = caught.value
    assert (e.verdict, e.rule, e.duplicate_of, e.rewrite, e.tags) == (
        "rewrite", None, 1, MERGED, ["database"])
    assert [m["id"] for m in api.query(project="p")] == [1]


# ── the real model, once ─────────────────────────────────────────────────────
@pytest.mark.review
async def test_real_model_links_a_reversal_and_merges_a_partial_repeat():
    url = _real_server()
    if url is None:
        pytest.skip("AGENT_MEMORY_REVIEW_URL is unset or the server did not answer in 2 s")
    model = os.environ.get("AGENT_MEMORY_REVIEW_MODEL", "").strip() or review_mod.DEFAULT_MODEL
    # A long timeout: the first call may have to load the model into memory.
    reviewer = OllamaReviewer(url, model, timeout=180)
    sqlite = ("Chose SQLite for the memory store because it needs no server and one agent "
              "writes at a time.")
    reversal = ("Moved the memory store from SQLite to Postgres because several agents now "
                "write at the same time and SQLite locks the whole file on every write.")
    postgres = ("Chose Postgres over SQLite for the memory store because several agents "
                "write at the same time.")
    partial = ("Chose Postgres over SQLite for the memory store because several agents "
               "write at the same time; the connection pool holds 10 connections with 20 "
               "overflow.")

    # One verified decision per project, planted, so each new entry is
    # compared with exactly that one.
    async with _App(reviewer=NullReviewer()) as off:
        old_a = (await off.add(sqlite, project="store-a", type="decision", tags=["database"]))["id"]
        old_b = (await off.add(postgres, project="store-b", type="decision", tags=["database"]))["id"]
    await _plant_review(old_a, APPROVE)
    await _plant_review(old_b, APPROVE)

    async with _App(reviewer=reviewer) as a:
        t0 = time.perf_counter()
        new_a = (await a.add(reversal, project="store-a", type="decision", tags=["database"]))["id"]
        reversal_s = time.perf_counter() - t0
        t0 = time.perf_counter()
        new_b = (await a.add(partial, project="store-b", type="decision", tags=["database"]))["id"]
        partial_s = time.perf_counter() - t0
        ra, rb = await a.get(new_a), await a.get(new_b)
        old_a_row, old_b_row = await a.get(old_a), await a.get(old_b)
    print(f"\n[review] {model} at {url}: reversal {reversal_s:.1f}s -> "
          f"{json.dumps(ra['review'])} supersedes={ra['supersedes']}; "
          f"partial {partial_s:.1f}s -> {json.dumps(rb['review'])}")

    # The reversal: approved, linked to the old decision, which reads as superseded.
    assert ra["review"] is not None, ra
    assert ra["review"]["verdict"] == "approve" and ra["review_status"] == "verified"
    assert ra["supersedes"] == old_a
    assert old_a_row["superseded_by"] == new_a and old_a_row["content"] == sqlite

    # The partial repeat: a rewrite with the merged text, pointing at the old one.
    assert rb["review"] is not None, rb
    assert rb["review"]["verdict"] == "rewrite" and rb["review_status"] == "flagged"
    assert rb["review"]["duplicate_of"] == old_b
    merged = rb["review"]["rewrite"] or ""
    assert "postgres" in merged.lower() and "pool" in merged.lower()
    # Nothing was applied or linked.
    assert rb["content"] == partial and rb["supersedes"] is None
    assert old_b_row["content"] == postgres and old_b_row["superseded_by"] is None
