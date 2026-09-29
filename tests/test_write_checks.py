"""The checks a new memory goes through on write.

The warnings (`server/checks.py`): rules a program can partly see, returned
with the id, never blocking. The duplicate check: a new entry whose vector is
as close as `DUPLICATE_THRESHOLD` to a verified memory in the same project is
refused with 409, unless `force=true`. And `scripts/replay_write_checks.py`,
which runs both checks over past memories of a database copy. And the tags:
a name is cleaned before lookup, a plural or a tag of the same meaning reuses
the existing tag, each reuse is a note in the add response, and more than
`TAG_LIMIT` tags is a warning.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_memory.server import repository as repo
from agent_memory.server.checks import REASONING_WORDS, TAG_LIMIT, warnings_for
from agent_memory.server.embedding import NullEmbedder
from agent_memory.server.schemas import MemoryIn, TagIn
from conftest import APPROVE, PG_DSN, REJECT, App, FakeEmbedder, add_rows, plant_review
from drivers import CliDriver
from test_surfaces import mcp_session

# Long enough to clear the `short` rule, says nothing about why.
LONG_NO_WHY = "Switched the build to run on every push to any branch of the repo."
LONG_WITH_WHY = "Switched the build to run on every push because the nightly was too slow."


# ── the warnings ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("content, project, mtype, expected", [
    (LONG_WITH_WHY, "proj", "decision", []),
    (LONG_NO_WHY, "proj", "note", []),
    ("x" * 39, "proj", None, ["short"]),
    ("x" * 40, "proj", None, []),
    (LONG_NO_WHY, None, None, ["no-project"]),
    (LONG_NO_WHY, "  ", None, ["no-project"]),
    (LONG_NO_WHY, "proj", "decision", ["no-reasoning"]),
    (LONG_NO_WHY, "proj", "lesson", ["no-reasoning"]),
    (LONG_NO_WHY, "proj", "constraint", ["no-reasoning"]),
    (LONG_WITH_WHY, "proj", "constraint", []),
    (LONG_NO_WHY, "proj", "preference", []),
    ("We picked the first option BECAUSE it was the simplest of the three.", "proj",
     "lesson", []),
    ("tiny", None, "decision", ["short", "no-project", "no-reasoning"]),
])
def test_warnings_for(content, project, mtype, expected):
    assert warnings_for(MemoryIn(content=content, project=project, type=mtype)) == expected


@pytest.mark.parametrize("word", REASONING_WORDS)
def test_each_reasoning_word_clears_the_rule(word):
    content = f"We picked the first option, {word} it was the simplest of the three."
    assert warnings_for(MemoryIn(content=content, project="p", type="decision")) == []


async def test_add_returns_the_warnings_and_stores_the_memory_anyway():
    async with App() as a:
        resp = await a.post("tiny", project=None, type="decision")
        assert resp.status_code == 201
        assert resp.json()["warnings"] == ["short", "no-project", "no-reasoning"]
        assert (await a.get(resp.json()["id"]))["content"] == "tiny"
        clean = await a.post(LONG_WITH_WHY, project="ci", type="decision")
        assert clean.json()["warnings"] == []


# ── the duplicate check, in the repository ───────────────────────────────────
def test_duplicate_threshold_value():
    assert repo.DUPLICATE_THRESHOLD == 0.92


async def test_find_duplicate_matches_the_same_text_in_the_same_project(session):
    emb = FakeEmbedder()
    other = await repo.add(session, "not a dup", "t", "proj", [], None, embedder=emb)
    mid = await repo.add(session, "dup me", "t", "proj", [], None, embedder=emb)
    bare = await repo.add(session, "dup me", "t", None, [], None, embedder=emb)
    for m in (other, mid, bare):
        await repo.set_review(session, m, APPROVE, "t")

    existing_id, score = await repo.find_duplicate(session, emb, "dup me", "proj")
    assert existing_id == mid and score == pytest.approx(1.0, abs=1e-5)
    # No project matches no project only; another project never matches.
    assert (await repo.find_duplicate(session, emb, "dup me", None))[0] == bare
    assert await repo.find_duplicate(session, emb, "dup me", "beta") is None
    # Other text scores far under the threshold.
    assert await repo.find_duplicate(session, emb, "something else", "proj") is None
    # Without a model there is nothing to compare.
    assert await repo.find_duplicate(session, NullEmbedder(), "dup me", "proj") is None
    assert await repo.find_duplicate(session, None, "dup me", "proj") is None


async def test_find_duplicate_sees_verified_rows_with_a_vector_only(session):
    emb = FakeEmbedder()
    # Written while the model was off: no vector, nothing to compare with.
    no_vector = await repo.add(session, "no vector", "t", "alpha", [], None)
    await repo.set_review(session, no_vector, APPROVE, "t")
    assert await repo.find_duplicate(session, emb, "no vector", "alpha") is None

    mid = await repo.add(session, "dup me", "t", "alpha", [], None, embedder=emb)
    assert await repo.find_duplicate(session, emb, "dup me", "alpha") is None   # unverified
    await repo.set_review(session, mid, REJECT, "m")
    assert await repo.find_duplicate(session, emb, "dup me", "alpha") is None   # flagged
    await repo.set_review(session, mid, APPROVE, "m")
    assert (await repo.find_duplicate(session, emb, "dup me", "alpha"))[0] == mid


# ── the duplicate check, on the route ────────────────────────────────────────
async def test_add_refuses_a_duplicate_with_409_and_stores_nothing():
    async with App() as a:
        first = await a.add("dup me", project="proj")
        # Not verified yet, so not reference: the same text is stored.
        assert (await a.post("dup me", project="proj")).status_code == 201
        await plant_review(first, APPROVE)

        again = await a.post("dup me", project="proj")
        assert again.status_code == 409
        assert again.json() == {"detail": {"reason": "duplicate", "existing_id": first,
                                           "score": pytest.approx(1.0, abs=1e-5)}}
        assert (await a.post("dup me", project="proj", force="false")).status_code == 409
        assert len(await a.ids()) == 2

        # Another project, or none, is not a duplicate; force stores it anyway.
        assert (await a.post("dup me", project="beta")).status_code == 201
        assert (await a.post("dup me", project=None)).status_code == 201
        forced = await a.post("dup me", project="proj", force="true")
        assert forced.status_code == 201 and forced.json()["id"] == 5


async def test_add_without_a_model_never_refuses():
    await plant_review((await add_rows("dup me", project="proj"))[0], APPROVE)
    async with App(embedder=NullEmbedder()) as a:
        assert (await a.post("dup me", project="proj")).status_code == 201


# ── scripts/replay_write_checks.py ───────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
SAME = "Moved the nightly build to run on every push to any branch of the repo."
DECISION = "Kept the nightly build because a build on every push made the runners too slow."


def _replay(*args):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "replay_write_checks.py"),
                           *args], capture_output=True, text=True, env=env)


async def test_replay_reports_the_second_of_an_identical_pair_as_a_refusal():
    first, second, third = await add_rows(SAME, SAME, DECISION, project="proj",
                                          embedder=NullEmbedder())
    out = _replay("--dsn", PG_DSN, "--embedder", "fake")
    assert out.returncode == 0, out.stderr
    lines = out.stdout.splitlines()
    by_id = {int(line.split("  ")[0]): line for line in lines[:3]}
    assert "  proj  ok  -" in by_id[first]
    assert f"  proj  refuse #{first} 1.00  -" in by_id[second]
    assert "  proj  ok  -" in by_id[third]
    assert lines[3:] == ["", "totals: 3 memories, 1 refused, warned (short 0, "
                             "no-project 0, no-reasoning 0, too-many-tags 0), 2 clean"]

    # Both rows were written today: a window ending yesterday shows nothing,
    # one starting today shows both, the second still refused.
    out = _replay("--dsn", PG_DSN, "--embedder", "fake", "--until", "2000-01-01")
    assert out.stdout.splitlines()[0] == "" and "totals: 0 memories" in out.stdout
    out = _replay("--dsn", PG_DSN, "--embedder", "fake", "--since", "2000-01-01")
    assert f"refuse #{first} 1.00" in out.stdout


def test_replay_refuses_a_dsn_that_is_not_local():
    out = _replay("--dsn", "postgresql://memory:memory@db.example.org:5432/memory",
                  "--embedder", "fake")
    assert out.returncode == 2
    assert "only against a copy" in out.stderr and out.stdout == ""


# ── tags: the soft limit ─────────────────────────────────────────────────────
def _with_tags(n):
    return MemoryIn(content=LONG_NO_WHY, project="p", tags=[TagIn(name=f"t{i}") for i in range(n)])


def test_more_than_ten_tags_is_a_warning():
    assert TAG_LIMIT == 10
    assert warnings_for(_with_tags(TAG_LIMIT)) == []
    assert warnings_for(_with_tags(TAG_LIMIT + 1)) == ["too-many-tags"]


async def test_add_with_too_many_tags_is_stored_anyway():
    async with App() as a:
        resp = await a.post(LONG_NO_WHY, tags=[f"t{i}" for i in range(12)])
        assert resp.status_code == 201
        assert resp.json()["warnings"] == ["too-many-tags"]
        assert len((await a.get(resp.json()["id"]))["tags"]) == 12


# ── tags: the name rule ──────────────────────────────────────────────────────
@pytest.mark.parametrize("raw, clean", [
    ("skill", "skill"),
    ("  Skill ", "skill"),
    ("Code Review", "code-review"),
    ("code_review", "code-review"),
    ("code -- _ review", "code-review"),
    ("-edge-", "edge"),
])
def test_clean_tag_name(raw, clean):
    assert repo.clean_tag_name(raw) == clean


def _names(tags):
    return [TagIn(name=n) for n in tags]


async def _tag_rows(session):
    return {t["name"]: t["description"] for t in await repo.list_tags(session)}


async def test_a_plural_reuses_the_singular_and_the_reverse(session):
    await repo.add(session, "a", "t", "p", [TagIn(name="skill", description="what I can do")],
                   None)
    await repo.add(session, "b", "t", "p", _names(["bugs"]), None)
    notes = []
    mid = await repo.add(session, "c", "t", "p",
                         [TagIn(name="Skills", description="other words"), TagIn(name="bug"),
                          TagIn(name="Code Review")], None, notes=notes)
    assert notes == ['tag "Skills" stored as "skill"', 'tag "bug" stored as "bugs"',
                     'tag "Code Review" stored as "code-review"']
    assert (await repo.get(session, mid))["tags"] == ["bugs", "code-review", "skill"]
    # Existing tags keep their name and, on a plural match, their description.
    assert await _tag_rows(session) == {"skill": "what I can do", "bugs": "bugs",
                                        "code-review": "code-review"}
    # "-es" works too, and a name written the stored way gives no note.
    await repo.add(session, "d", "t", "p", _names(["box"]), None)
    notes = []
    await repo.add(session, "e", "t", "p", _names(["boxes", "skill"]), None, notes=notes)
    assert notes == ['tag "boxes" stored as "box"']


async def test_the_same_tag_twice_in_one_add_is_one_link(session):
    await repo.add(session, "a", "t", "p", _names(["skill"]), None)
    mid = await repo.add(session, "b", "t", "p", _names(["skill", "Skills"]), None)
    assert (await repo.get(session, mid))["tags"] == ["skill"]


async def test_update_tags_reuse_and_say_so(session):
    await repo.add(session, "a", "t", "p", _names(["skill"]), None)
    mid = await repo.add(session, "b", "t", "p", [], None)
    assert await repo.update(session, mid, add_tags=_names(["Skills"])) == [
        "+tags: skill", 'tag "Skills" stored as "skill"']
    assert await repo.update(session, mid, set_tags=_names(["skills", "Code_Review"])) == [
        "tags set to: skill, code-review", 'tag "skills" stored as "skill"',
        'tag "Code_Review" stored as "code-review"']


# ── tags: the meaning rule ───────────────────────────────────────────────────
class MeaningEmbedder(FakeEmbedder):
    """FakeEmbedder, except that the texts in `near` all get one vector, and the
    texts in `far` get a vector at cosine 0.8 from it."""

    def __init__(self, near=(), far=(), model_name="fake"):
        self.near, self.far, self.model_name = set(near), set(far), model_name

    def _one(self, text):
        if text in self.near:
            return [1.0] + [0.0] * 7
        if text in self.far:
            return [0.8, 0.6] + [0.0] * 6
        return super()._one(text)


COMMS = "communication: talking with the team"


async def test_a_tag_of_the_same_meaning_reuses_the_existing_one(session):
    emb = MeaningEmbedder(near={COMMS, "comms: talking with the team"},
                          far={"chat: talking with the team"})
    await repo.add(session, "a", "t", "p",
                   [TagIn(name="communication", description="talking with the team")], None,
                   embedder=emb)
    notes = []
    mid = await repo.add(session, "b", "t", "p",
                         [TagIn(name="comms", description="talking with the team"),
                          TagIn(name="chat", description="talking with the team")], None,
                         embedder=emb, notes=notes)
    assert notes == ['tag "comms" stored as "communication"']
    # Under 0.90 is another tag.
    assert (await repo.get(session, mid))["tags"] == ["chat", "communication"]
    assert set(await _tag_rows(session)) == {"communication", "chat"}


async def test_the_meaning_rule_needs_a_vector_from_the_same_model(session):
    comms = "comms: talking with the team"
    await repo.add(session, "a", "t", "p",
                   [TagIn(name="communication", description="talking with the team")], None,
                   embedder=MeaningEmbedder(near={COMMS, comms}, model_name="old"))
    notes = []
    await repo.add(session, "b", "t", "p",
                   [TagIn(name="comms", description="talking with the team")], None,
                   embedder=MeaningEmbedder(near={COMMS, comms}), notes=notes)
    assert notes == []
    assert set(await _tag_rows(session)) == {"communication", "comms"}


@pytest.mark.parametrize("embedder", [None, NullEmbedder()])
async def test_without_a_model_only_the_name_rule_applies(session, embedder):
    await repo.add(session, "a", "t", "p",
                   [TagIn(name="communication", description="talking with the team")], None,
                   embedder=FakeEmbedder())
    notes = []
    await repo.add(session, "b", "t", "p",
                   [TagIn(name="comms", description="talking with the team"),
                    TagIn(name="Communications")], None, embedder=embedder, notes=notes)
    assert notes == ['tag "Communications" stored as "communication"']
    assert set(await _tag_rows(session)) == {"communication", "comms"}


# ── tags: the note on each surface ───────────────────────────────────────────
async def test_the_add_route_returns_the_notes():
    async with App() as a:
        await a.add(LONG_WITH_WHY, tags=["skill"])
        resp = await a.post(LONG_NO_WHY, tags=["Skills", "skill"])
        assert resp.json()["notes"] == ['tag "Skills" stored as "skill"']
        assert (await a.post(LONG_NO_WHY + " Again.", tags=["skill"])).json()["notes"] == []


def test_the_cli_prints_one_line_per_note(live_server):
    cli = CliDriver(*live_server)
    cli.raw("add", LONG_WITH_WHY, "--project", "p", "--tags", '[{"name":"skill"}]')
    out = cli.raw("add", "tiny", "--project", "p", "--tags",
                  '[{"name":"Skills"},{"name":"Deploy Steps"}]')
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines()[1:] == [
        "warning: short", 'note: tag "Skills" stored as "skill"',
        'note: tag "Deploy Steps" stored as "deploy-steps"']


async def test_mcp_add_returns_the_notes(live_server):
    async with mcp_session(live_server[0]) as call:
        await call("memory_add", content=LONG_WITH_WHY, project="p", tags=[{"name": "skill"}])
        assert await call("memory_add", content=LONG_NO_WHY, project="p",
                          tags=[{"name": "skills"}]) == {
            "id": 2, "warnings": [], "notes": ['tag "skills" stored as "skill"']}
