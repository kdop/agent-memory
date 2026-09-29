"""The tag review: a status on each tag and the model's keep, merge, rename
or drop (server/tag_review.py).

No model here but the last test: a `FakeTagReviewer` answers, and the
repository, the routes, the catch-up and the surfaces are checked against
the test database. `test_tag_verdict_quality` runs the tag verdict set
through the real model under the `review` marker.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import urllib.error
from pathlib import Path

import pytest
from sqlalchemy import select, update

from agent_memory.client import ApiClient
from agent_memory.server import repository as repo
from agent_memory.server import review as review_mod
from agent_memory.server import tag_review as tr
from agent_memory.server.app import _catch_up_lock, _review_tick
from agent_memory.server.models import Tag, TagReview
from agent_memory.server.review import NullReviewer, OllamaReviewer
from agent_memory.server.schemas import TagIn
from agent_memory.server.tag_review import (
    TagReviewer,
    TagVerdict,
    make_tag_reviewer,
    parse_tag_verdict,
)
from conftest import (
    APPROVE,
    App,
    FakeEmbedder,
    FakeReviewer,
    db_session,
    real_review_server,
    statuses,
    until,
)
from drivers import CliDriver

KEEP = TagVerdict("keep", None, None, "A clear subject.")
DROP = TagVerdict("drop", None, None, "It repeats the memory type.")


def merge(into):
    return TagVerdict("merge", into, None, f"Same subject as {into}.")


def rename(new_name):
    return TagVerdict("rename", None, new_name, "A clearer name.")


class FakeTagReviewer:
    """A stand-in for the tag model. `verdict` is the answer (a TagVerdict,
    None, or an exception to raise); `verdicts` maps a tag name to its own.
    Every call is kept in `calls` as `(tag, memories, neighbours)`. With
    `block`, each review waits until the test sets `release`."""

    model_name = "fake-tag-reviewer"

    def __init__(self, verdict=KEEP, verdicts=None, block=False):
        self.verdict = verdict
        self.verdicts = dict(verdicts or {})
        self.block = block
        self.calls = []
        self.started = threading.Event()
        self.release = threading.Event()

    def review(self, tag, memories, neighbours):
        self.calls.append((tag, memories, neighbours))
        if self.block:
            self.started.set()
            if not self.release.wait(timeout=10):
                raise TimeoutError("the test never released the review")
        answer = self.verdicts.get(tag["name"], self.verdict)
        if isinstance(answer, Exception):
            raise answer
        return answer

    @property
    def names(self):
        return [t["name"] for t, _, _ in self.calls]


async def add_tagged(content, *tags, project="alpha"):
    """Store a memory with these tags (names) through the repository."""
    async with db_session() as s:
        return await repo.add(s, content, "tester", project, [TagIn(name=t) for t in tags],
                              None, embedder=FakeEmbedder())


async def tag_rows():
    """`{name: review_status}` straight from the table."""
    async with db_session() as s:
        return dict((await s.execute(select(Tag.name, Tag.review_status))).all())


async def tag_id(name):
    async with db_session() as s:
        return (await s.execute(select(Tag.id).where(Tag.name == name))).scalar_one()


async def set_tag(name, **values):
    async with db_session() as s:
        await s.execute(update(Tag).where(Tag.name == name).values(**values))


async def proposal_rows():
    async with db_session() as s:
        stmt = select(TagReview.tag_name, TagReview.verdict, TagReview.resolved,
                      TagReview.tag_id).order_by(TagReview.id)
        return [tuple(r) for r in (await s.execute(stmt)).all()]


async def judge(name, verdict):
    """Store `verdict` for tag `name` the way the catch-up does."""
    tid = await tag_id(name)
    async with db_session() as s:
        return await repo.set_tag_review(s, tid, verdict, "planted", embedder=FakeEmbedder())


# ── the answer ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text, want", [
    ('{"why": "x", "verdict": "keep", "into": null, "new_name": null, "reason": "Fine."}',
     TagVerdict("keep", None, None, "Fine.")),
    # into and new_name are dropped from a verdict they do not belong to.
    ('{"verdict": "keep", "into": "api", "new_name": "x", "reason": "Fine."}',
     TagVerdict("keep", None, None, "Fine.")),
    ('{"verdict": "drop", "into": null, "new_name": null, "reason": " Says nothing. "}',
     TagVerdict("drop", None, None, "Says nothing.")),
    # into in the offered spelling.
    ('{"verdict": "merge", "into": "API", "new_name": null, "reason": "Same."}',
     TagVerdict("merge", "api", None, "Same.")),
    # new_name goes through the name cleanup.
    ('{"verdict": "rename", "into": null, "new_name": "Retry Logic", "reason": "Clearer."}',
     TagVerdict("rename", None, "retry-logic", "Clearer.")),
    # A rename to the name the tag has is a keep.
    ('{"verdict": "rename", "into": null, "new_name": "My_Tag", "reason": "Clearer."}',
     TagVerdict("keep", None, None, "Clearer.")),
    # The model's own checks win over a verdict that skips them.
    ('{"why": "", "verdict": "rename", "new_name": "x", "reason": "Vague."}',
     TagVerdict("drop", None, None, "Vague.")),
    ('{"why": "choices", "is_type": true, "verdict": "keep", "reason": "A type."}',
     TagVerdict("drop", None, None, "A type.")),
    ('{"why": "db", "same_as": "Database", "verdict": "keep", "reason": "Same."}',
     TagVerdict("merge", "database", None, "Same.")),
    # same_as that is not offered, or is the tag itself, changes nothing.
    ('{"why": "db", "same_as": "search", "verdict": "keep", "reason": "Fine."}',
     TagVerdict("keep", None, None, "Fine.")),
    ('{"why": "db", "same_as": "my-tag", "verdict": "keep", "reason": "Fine."}',
     TagVerdict("keep", None, None, "Fine.")),
])
def test_parse_tag_verdict_reads_a_good_answer(text, want):
    assert parse_tag_verdict(text, "my-tag", ["api", "database"]) == want


@pytest.mark.parametrize("text", [
    "not json",
    "[]",
    '{"verdict": "maybe", "reason": "x"}',
    '{"verdict": "keep", "reason": "  "}',
    '{"verdict": "keep"}',
    '{"verdict": "merge", "into": null, "reason": "x"}',
    '{"verdict": "merge", "into": "search", "reason": "x"}',   # not offered
    '{"verdict": "merge", "into": "My-Tag", "reason": "x"}',   # itself
    '{"verdict": "rename", "new_name": null, "reason": "x"}',
    '{"verdict": "rename", "new_name": " _ ", "reason": "x"}',
])
def test_parse_tag_verdict_refuses_a_bad_answer(text):
    assert parse_tag_verdict(text, "my-tag", ["api", "my-tag", "database"]) is None


def test_the_request_uses_the_memory_review_settings_and_lists_everything():
    chat = OllamaReviewer("http://model:11434/", "some-model", timeout=5)
    tags = TagReviewer(chat)
    assert tags.model_name == "some-model"
    tag = {"name": "caching", "description": "what is cached", "count": 4}
    memories = [{"type": "decision", "project": "atlas", "content": "Tiles are cached. " * 40}]
    neighbours = [{"name": "database", "description": "the database"}]
    body = tags.request_body(tag, memories, neighbours)
    memory_body = chat.request_body({"content": "x"}, [], [])
    assert {k: v for k, v in body.items() if k != "messages"} == \
        {k: v for k, v in memory_body.items() if k != "messages"}
    assert body["think"] is False and body["format"] == "json"
    assert body["options"]["temperature"] == 0 and body["options"]["num_ctx"] == 8192
    system, user = (m["content"] for m in body["messages"])
    assert system == tr.SYSTEM_PROMPT
    # The model looks before it judges: `why` is the first key.
    assert system.index('{"why"') < system.index('"verdict"')
    for word in ("Tag: caching", "Description: what is cached", "Used by 4 memories",
                 "type: decision, project: atlas", "- database: the database"):
        assert word in user
    # Long memories are cut.
    assert "..." in user and len(user) < 800
    user = tr.user_prompt({"name": "x", "count": 1}, [], [])
    assert "Used by 1 memory." in user and "No memory uses this tag now." in user
    assert "into must be null" in user


@pytest.mark.parametrize("name, listed, fact", [
    ("decisions", [], "The name is the memory type 'decision'."),
    ("rule", [], "The name is another word for the memory type 'constraint'."),
    ("takeaways-q3", [], "The word 'takeaways' in the name is the memory type 'lesson'"),
    ("Retry Logic", [], "cleaned it would be 'retry-logic'"),
    ("webhook", ["api", "webhooks"], "The listed tag 'webhooks' is the same name"),
])
def test_the_prompt_carries_what_the_server_knows_about_the_name(name, listed, fact):
    user = tr.user_prompt({"name": name, "count": 1}, [],
                          [{"name": n, "description": n} for n in listed])
    assert "Facts about the name, checked by the server:" in user and fact in user


def test_a_plain_name_has_no_facts():
    assert tr.name_facts("caching", ["cache-keys", "database"]) == []


def test_the_tag_reviewer_goes_with_a_real_memory_reviewer_only():
    chat = OllamaReviewer("http://model:11434")
    assert make_tag_reviewer(chat).chat is chat
    assert make_tag_reviewer(NullReviewer()) is None
    assert make_tag_reviewer(FakeReviewer()) is None
    assert make_tag_reviewer(None) is None


def test_the_tag_reviewer_asks_through_the_memory_reviewers_call(monkeypatch, caplog):
    chat = OllamaReviewer("http://model:11434")
    sent = []

    def answer(body):
        sent.append(body)
        return '{"why": "the database", "verdict": "merge", "into": "database", ' \
               '"new_name": null, ' \
               '"reason": "Same."}'

    monkeypatch.setattr(chat, "_chat", answer)
    reviewer = TagReviewer(chat)
    neighbours = [{"name": "database", "description": "db"}]
    assert reviewer.review({"name": "db", "count": 1}, [], neighbours) == \
        TagVerdict("merge", "database", None, "Same.")
    assert len(sent) == 1

    monkeypatch.setattr(chat, "_chat", lambda body: "no json")
    with caplog.at_level(logging.WARNING, logger="agent_memory.server.tag_review"):
        assert reviewer.review({"name": "db"}, [], neighbours) is None

    def down(body):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(chat, "_chat", down)
    with caplog.at_level(logging.WARNING, logger="agent_memory.server.tag_review"):
        assert reviewer.review({"name": "db"}, [], neighbours) is None
    messages = [r.getMessage() for r in caplog.records]
    assert any("gave no usable JSON" in m for m in messages)
    assert any("review of tag 'db' failed" in m and "refused" in m for m in messages)


# ── the repository ───────────────────────────────────────────────────────────
async def test_the_input_holds_three_memories_and_the_closest_verified_tags(session):
    for n in range(4):
        await add_tagged(f"caching memory {n}", "caching")
    archived = await add_tagged("an archived one", "caching")
    async with db_session() as s:
        await repo.archive(s, archived)
    for name in ("a1", "a2", "a3", "a4", "a5", "a6", "unchecked"):
        await add_tagged(f"memory for {name}", name)
    for name in ("a1", "a2", "a3", "a4", "a5", "a6"):
        await set_tag(name, review_status="verified")
    # Vectors by hand: a3 is the closest to caching, then a1, then the rest.
    await set_tag("caching", embedding=[1.0, 0.0], embedding_model="fake")
    for name, vec in (("a1", [0.8, 0.6]), ("a2", [0.0, 1.0]), ("a3", [1.0, 0.0]),
                      ("a4", [0.6, 0.8]), ("a5", [-1.0, 0.0]), ("a6", [0.7, 0.7]),
                      ("unchecked", [1.0, 0.0])):
        await set_tag(name, embedding=vec, embedding_model="fake")
    tag, memories, neighbours = await repo.tag_review_input(session, await tag_id("caching"))
    assert (tag["name"], tag["count"]) == ("caching", 4)
    assert [m["content"] for m in memories] == [
        "caching memory 3", "caching memory 2", "caching memory 1"]
    # Verified only, never itself, best first, at most five.
    assert [n["name"] for n in neighbours] == ["a3", "a1", "a6", "a4", "a2"]
    assert neighbours[0]["score"] == pytest.approx(1.0) and neighbours[0]["count"] == 1

    # Without a vector: the most used verified tags.
    await set_tag("caching", embedding=None, embedding_model=None)
    await add_tagged("another for a4", "a4")
    _, _, neighbours = await repo.tag_review_input(session, await tag_id("caching"))
    assert [n["name"] for n in neighbours][:2] == ["a4", "a1"]
    assert await repo.tag_review_input(session, 999) is None


async def test_unverified_tags_come_oldest_first(session):
    await add_tagged("one", "b-tag", "a-tag")
    await add_tagged("two", "c-tag")
    await set_tag("a-tag", review_status="verified")
    ids = await repo.unverified_tag_ids(session)
    assert ids == sorted(ids) and len(ids) == 2
    assert await repo.unverified_tag_ids(session, limit=1) == ids[:1]


async def test_keep_verifies_and_stores_no_row():
    await add_tagged("one", "caching")
    done = await judge("caching", KEEP)
    assert done == {"tag": "caching", "verdict": "keep", "status": "verified",
                    "merged_into": None, "proposal": None}
    assert await tag_rows() == {"caching": "verified"}
    assert await proposal_rows() == []


async def test_a_merge_by_name_is_applied_at_once():
    # Tags made before the name cleanup: a plural next to its singular.
    a = await add_tagged("one", "skill")
    async with db_session() as s:
        s.add(Tag(name="skills", description="skills"))
    await set_tag("skill", review_status="verified")
    async with db_session() as s:
        tid = (await s.execute(select(Tag.id).where(Tag.name == "skills"))).scalar_one()
        await s.execute(repo.MemoryTag.__table__.insert().values(memory_id=a, tag_id=tid))
    b = await add_tagged("two", "caching")
    async with db_session() as s:
        await s.execute(repo.MemoryTag.__table__.insert().values(memory_id=b, tag_id=tid))
    done = await judge("skills", merge("skill"))
    assert done["merged_into"] == "skill" and done["status"] is None
    assert await tag_rows() == {"skill": "verified", "caching": "unverified"}
    assert await proposal_rows() == [("skills", "merge", "applied", None)]
    async with db_session() as s:
        assert (await repo.get(s, b))["tags"] == ["caching", "skill"]


async def test_a_merge_by_vector_is_applied_at_once():
    await add_tagged("one", "incidents")
    await add_tagged("two", "outage")
    await set_tag("incidents", review_status="verified", embedding=[1.0, 0.0],
                  embedding_model="fake")
    await set_tag("outage", embedding=[0.95, 0.312], embedding_model="fake")  # cosine .95
    done = await judge("outage", merge("incidents"))
    assert done["merged_into"] == "incidents"
    assert await tag_rows() == {"incidents": "verified"}


@pytest.mark.parametrize("vec, model", [
    ([0.8, 0.6], "fake"),       # cosine 0.80: too far
    ([1.0, 0.0], "other"),      # another model's vector: not comparable
    (None, None),               # no vector at all
])
async def test_a_merge_without_the_second_check_waits(vec, model):
    await add_tagged("one", "incidents")
    await add_tagged("two", "outage")
    await set_tag("incidents", review_status="verified", embedding=[1.0, 0.0],
                  embedding_model="fake")
    await set_tag("outage", embedding=vec, embedding_model=model)
    done = await judge("outage", merge("incidents"))
    assert done["merged_into"] is None and done["status"] == "flagged"
    assert done["proposal"] == 1
    assert await tag_rows() == {"incidents": "verified", "outage": "flagged"}
    assert await proposal_rows() == [("outage", "merge", None, await tag_id("outage"))]


async def test_rename_drop_and_a_merge_into_nothing_wait():
    await add_tagged("one", "Retry Logic", "misc", "outage")
    await judge("retry-logic", rename("retries"))
    await judge("misc", DROP)
    await judge("outage", merge("incidents"))  # no such tag
    assert await tag_rows() == {"retry-logic": "flagged", "misc": "flagged",
                                "outage": "flagged"}
    assert [(n, v, r) for n, v, r, _ in await proposal_rows()] == [
        ("retry-logic", "rename", None), ("misc", "drop", None), ("outage", "merge", None)]
    with pytest.raises(LookupError):
        async with db_session() as s:
            await repo.set_tag_review(s, 999, KEEP, "m")


# ── the routes ───────────────────────────────────────────────────────────────
async def test_the_tag_routes_list_apply_and_reject():
    await add_tagged("one", "retry", "misc", "outage", "incidents", "keepme")
    await add_tagged("two", "incidents")
    await set_tag("incidents", review_status="verified")
    await judge("retry", rename("Retry Logic"))
    await judge("misc", DROP)
    await judge("outage", merge("incidents"))
    await judge("keepme", DROP)
    async with App() as a:
        tags = {t["name"]: t["review_status"] for t in (await a.client.get("/tags")).json()}
        assert tags == {"incidents": "verified", "retry": "flagged", "misc": "flagged",
                        "outage": "flagged", "keepme": "flagged"}
        pending = (await a.client.get("/tags/flagged")).json()
        assert [(p["id"], p["tag"], p["verdict"], p["into"], p["new_name"])
                for p in pending] == [
            (1, "retry", "rename", None, "Retry Logic"), (2, "misc", "drop", None, None),
            (3, "outage", "merge", "incidents", None), (4, "keepme", "drop", None, None)]
        assert pending[0]["model"] == "planted" and pending[0]["resolved"] is None

        resp = await a.client.post("/tags/proposals/1/apply")
        assert resp.status_code == 200, resp.text
        # The name goes through the cleanup on the way.
        assert resp.json()["result"]["name"] == "retry-logic"
        assert resp.json()["proposal"]["resolved"] == "applied"
        assert (await a.client.post("/tags/proposals/2/apply")).json()["result"] == {
            "removed": "misc", "memories_affected": 1}
        assert (await a.client.post("/tags/proposals/3/apply")).json()["result"] == {
            "target": "incidents", "memories_affected": 1, "removed": ["outage"]}
        resp = await a.client.post("/tags/proposals/4/reject")
        assert resp.json()["proposal"]["resolved"] == "rejected"

        tags = {t["name"]: (t["review_status"], t["count"])
                for t in (await a.client.get("/tags")).json()}
        assert tags == {"incidents": ("verified", 2), "retry-logic": ("verified", 1),
                        "keepme": ("verified", 1)}
        assert (await a.client.get("/tags/flagged")).json() == []

        # Resolved already: 409. No such proposal: 404.
        for path in ("/tags/proposals/1/apply", "/tags/proposals/4/reject"):
            resp = await a.client.post(path)
            assert resp.status_code == 409 and "already" in resp.json()["detail"]
        assert (await a.client.post("/tags/proposals/99/apply")).status_code == 404
        assert (await a.client.post("/tags/proposals/99/reject")).status_code == 404
    rows = await proposal_rows()
    assert [(n, r) for n, _, r, _ in rows] == [("retry", "applied"), ("misc", "applied"),
                                              ("outage", "applied"), ("keepme", "rejected")]
    # The rows outlive the tags they were about.
    assert [tid is None for *_, tid in rows] == [False, True, True, False]


async def test_a_proposal_whose_tags_are_gone_cannot_be_applied():
    await add_tagged("one", "outage", "incidents", "misc")
    await judge("outage", merge("incidents"))
    await judge("misc", DROP)
    async with App() as a:
        await a.client.delete("/tags/incidents")
        resp = await a.client.post("/tags/proposals/1/apply")
        assert resp.status_code == 409 and "'incidents' to merge into" in resp.json()["detail"]
        await a.client.delete("/tags/misc")
        # A proposal whose tag is gone is not listed, and cannot be applied.
        assert [p["id"] for p in (await a.client.get("/tags/flagged")).json()] == [1]
        resp = await a.client.post("/tags/proposals/2/apply")
        assert resp.status_code == 409 and "no longer exists" in resp.json()["detail"]
        # A rename onto a taken name merges into that tag, which is verified.
        await add_tagged("two", "retry", "retries")
        await judge("retry", rename("retries"))
        resp = await a.client.post("/tags/proposals/3/apply")
        assert resp.json()["result"]["name"] == "retries"
    assert (await tag_rows())["retries"] == "verified" and "retry" not in await tag_rows()


# ── the catch-up ─────────────────────────────────────────────────────────────
async def test_the_catch_up_reviews_tags_after_memories_oldest_first(caplog):
    await add_tagged("one", "zeta", "alpha-tag")
    await add_tagged("two", "misc")
    fake = FakeReviewer(APPROVE)
    tags = FakeTagReviewer(KEEP, verdicts={"misc": DROP, "alpha-tag": None})
    with caplog.at_level(logging.INFO, logger="agent_memory.server.app"):
        async with App(reviewer=fake, tag_reviewer=tags) as a:
            resp = await a.client.post("/admin/review")
            assert resp.json() == {"scheduled": 2, "tags": 3}
            # Oldest tag first: in the order they were made.
            assert tags.names == ["zeta", "alpha-tag", "misc"]
            # The tags came after both memories were verified.
            assert [s for _, s in await statuses()] == ["verified", "verified"]
            # A tag verified early is on the list for the later ones.
            assert [n["name"] for n in tags.calls[2][2]] == ["zeta"]
            assert tags.calls[0][0]["count"] == 1
            assert await tag_rows() == {"zeta": "verified", "alpha-tag": "unverified",
                                        "misc": "flagged"}
            # The one with no answer is picked again next time; nothing else.
            assert (await a.client.post("/admin/review")).json() == {"scheduled": 0,
                                                                     "tags": 1}
            assert tags.names[-1] == "alpha-tag"
    assert "catch-up started: 2 unverified memories and 3 unverified tags to review" in \
        [r.getMessage() for r in caplog.records]


async def test_a_failing_tag_review_does_not_stop_the_rest(caplog):
    await add_tagged("one", "first", "second")
    tags = FakeTagReviewer(KEEP, verdicts={"first": RuntimeError("model blew up")})
    with caplog.at_level(logging.WARNING, logger="agent_memory.server.app"):
        async with App(reviewer=FakeReviewer(APPROVE), tag_reviewer=tags) as a:
            await a.client.post("/admin/review")
    assert await tag_rows() == {"first": "unverified", "second": "verified"}
    assert "review of tag #1 failed: RuntimeError: model blew up" in \
        [r.getMessage() for r in caplog.records]


async def test_only_tags_to_review_still_runs_a_catch_up():
    await add_tagged("one", "solo")
    async with App(reviewer=FakeReviewer(APPROVE)) as a:
        await a.client.post("/admin/review")        # no tag reviewer: memories only
    assert await tag_rows() == {"solo": "unverified"}
    tags = FakeTagReviewer(KEEP)
    async with App(reviewer=FakeReviewer(APPROVE), tag_reviewer=tags) as a:
        await _review_tick(a.app)
        assert tags.names == ["solo"] and not _catch_up_lock(a.app).locked()


async def test_health_says_which_half_of_the_catch_up_runs():
    await add_tagged("one", "t1", "t2")
    tags = FakeTagReviewer(KEEP, block=True)
    async with App(reviewer=FakeReviewer(APPROVE), tag_reviewer=tags) as a:
        tick = asyncio.create_task(_review_tick(a.app))
        await asyncio.to_thread(tags.started.wait, 10)
        progress = (await a.client.get("/health")).json()["catch_up"]
        assert progress == {"kind": "tags", "total": 2, "done": 0}
        tags.release.set()
        await tick
        assert (await a.client.get("/health")).json()["catch_up"] is None


async def test_a_tag_merged_by_the_catch_up_is_gone():
    await add_tagged("one", "skill")
    await add_tagged("two", "caching")
    await set_tag("skill", review_status="verified")
    async with db_session() as s:
        s.add(Tag(name="skills", description="skills"))
    tags = FakeTagReviewer(KEEP, verdicts={"skills": merge("skill")})
    async with App(reviewer=FakeReviewer(APPROVE), tag_reviewer=tags) as a:
        await a.client.post("/admin/review")
    assert await tag_rows() == {"skill": "verified", "caching": "verified"}


# ── the surfaces ─────────────────────────────────────────────────────────────
@pytest.fixture
def cli(live_server):
    url, token = live_server
    return CliDriver(url, token)


def test_cli_tags_pending_apply_and_reject(cli):
    asyncio.run(add_tagged("one", "misc", "retry", "caching"))
    assert cli.raw("tags", "--pending").stdout.strip() == "No tag proposals waiting."
    asyncio.run(judge("misc", DROP))
    asyncio.run(judge("retry", rename("retries")))
    out = cli.raw("tags").stdout
    assert "misc                 (1)  [flagged]" in out
    assert "2 flagged: see 'memory tags --pending'" in out
    out = cli.raw("tags", "--pending").stdout
    assert "#1  drop 'misc'" in out and "↳ It repeats the memory type." in out
    assert "#2  rename 'retry' to 'retries'" in out and "2 waiting." in out
    assert cli.raw("tags", "--apply", "1").stdout.strip() == "✓ Applied #1: drop 'misc'"
    assert cli.raw("tags", "--reject", "2").stdout.strip() == \
        "✓ Rejected #2: 'retry' stays as it is"
    proc = cli.raw("tags", "--apply", "2")
    assert proc.returncode == 1 and "already rejected" in proc.stderr
    proc = cli.raw("tags", "--reject", "9")
    assert proc.returncode == 1 and "Proposal #9 not found" in proc.stdout
    proc = cli.raw("tags", "--pending", "--apply", "1")
    assert proc.returncode == 2
    assert asyncio.run(tag_rows()) == {"retry": "verified", "caching": "unverified"}


async def test_mcp_lists_the_tag_proposals(live_server):
    from test_surfaces import mcp_session

    url, _ = live_server
    await add_tagged("one", "misc")
    await judge("misc", DROP)
    async with mcp_session(url) as call:
        got = await call("memory_tag_proposals")
        assert [(p["id"], p["tag"], p["verdict"]) for p in got["proposals"]] == [
            (1, "misc", "drop")]
        assert (await call("memory_tags"))["tags"][0]["review_status"] == "flagged"
        tools = {t.name for t in (await call.session.list_tools()).tools}
    # The list only: applying stays with a person.
    assert "memory_tag_proposals" in tools
    assert not any("apply" in t or "reject" in t for t in tools)
    client = ApiClient(url, "test-token")
    assert client.apply_tag_proposal(5) is None and client.reject_tag_proposal(5) is None


# ── the tag verdict set ──────────────────────────────────────────────────────
TAG_SET_PATH = Path(__file__).resolve().parent / "data" / "tag_verdict_set.json"
CASES = ("keep", "merge-synonym", "merge-plural", "rename", "drop-type", "drop-meaningless")
CASE_VERDICT = {"keep": "keep", "merge-synonym": "merge", "merge-plural": "merge",
                "rename": "rename", "drop-type": "drop", "drop-meaningless": "drop"}


def load_tag_set(path=TAG_SET_PATH):
    """Read the tag verdict set and check it is well formed; returns
    `(entries, floor)`."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    entries, floor = data["entries"], data["floor"]
    for n, e in enumerate(entries):
        if e["case"] not in CASES:
            raise ValueError(f"entry {n} has an unknown case {e['case']!r}")
        x = e["expected"]
        if x["verdict"] != CASE_VERDICT[e["case"]]:
            raise ValueError(f"entry {n}: case {e['case']} expects {x['verdict']}")
        names = [nb["name"] for nb in e["neighbours"]]
        if len(names) > tr.NEIGHBOUR_COUNT or len(e["memories"]) > tr.MEMORY_COUNT:
            raise ValueError(f"entry {n} shows more than the model would see")
        if x["verdict"] == "merge" and x["into"] not in names:
            raise ValueError(f"entry {n} merges into {x['into']!r}, which is not listed")
    if not 0.0 <= floor <= 1.0:
        raise ValueError(f"floor must be between 0 and 1 (got {floor!r})")
    return entries, float(floor)


def grade(expected, verdict):
    if verdict is None:
        return ["no usable answer from the model"]
    wrong = []
    if verdict.verdict != expected["verdict"]:
        wrong.append(f"verdict {verdict.verdict}, expected {expected['verdict']}")
    elif expected["verdict"] == "merge" and verdict.into != expected["into"]:
        wrong.append(f"into {verdict.into!r}, expected {expected['into']!r}")
    return wrong


def test_the_tag_verdict_set_is_well_formed():
    entries, _ = load_tag_set()
    assert len(entries) == 20
    counts = {case: sum(1 for e in entries if e["case"] == case) for case in CASES}
    assert all(n >= 3 for n in counts.values()), counts


def test_the_prompt_shares_no_tag_with_the_set():
    # An example in the prompt that names a tag of the set would teach the
    # model the answer rather than measure it.
    entries, _ = load_tag_set()
    for e in entries:
        assert f"'{e['tag']['name']}'" not in tr.SYSTEM_PROMPT
        assert f" {e['tag']['name']}," not in tr.SYSTEM_PROMPT


@pytest.mark.review
def test_tag_verdict_quality(record_property):
    url = real_review_server()
    if url is None:
        pytest.skip("AGENT_MEMORY_REVIEW_URL is unset or the server did not answer in 2 s")
    model = os.environ.get("AGENT_MEMORY_REVIEW_MODEL", "").strip() or review_mod.DEFAULT_MODEL
    reviewer = TagReviewer(OllamaReviewer(url, model, timeout=180))
    entries, floor = load_tag_set()
    results = []
    t0 = time.perf_counter()
    for n, e in enumerate(entries):
        tag = dict(e["tag"], count=e["count"])
        verdict = reviewer.review(tag, [dict(m) for m in e["memories"]],
                                  [dict(nb) for nb in e["neighbours"]])
        results.append((e["case"], n, grade(e["expected"], verdict), verdict))
    passed = sum(1 for _, _, wrong, _ in results if not wrong)
    rate = passed / len(results)
    print(f"\n[tag review] {model} at {url}: {len(results)} tags in "
          f"{time.perf_counter() - t0:.0f}s")
    print(f"{'case':<18} {'passed':>7}")
    for case in CASES:
        rows = [r for r in results if r[0] == case]
        ok = sum(1 for r in rows if not r[2])
        print(f"{case:<18} {ok:>3}/{len(rows)}")
        record_property(f"verdict tag {case}", f"{ok}/{len(rows)}")
    print(f"{'overall':<18} {passed:>3}/{len(results)}  rate {rate:.2f}  floor {floor:.2f}")
    record_property("verdict tag overall",
                    f"{passed}/{len(results)} = {rate:.2f} (floor {floor:.2f})")
    for case, n, wrong, verdict in results:
        if wrong:
            said = verdict.as_dict() if verdict is not None else None
            print(f"  {case} #{n} failed: {'; '.join(wrong)}\n    model said: {json.dumps(said)}")
    assert rate >= floor, (f"tag verdict pass rate {rate:.2f} is below the floor {floor:.2f} "
                           f"in {TAG_SET_PATH.name}")
