"""The checks a new memory goes through on write.

The warnings (`server/checks.py`): rules a program can partly see, returned
with the id, never blocking. The duplicate check: a new entry whose vector is
as close as `DUPLICATE_THRESHOLD` to a verified memory in the same project is
refused with 409, unless `force=true`. And `scripts/replay_write_checks.py`,
which runs both checks over past memories of a database copy.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_memory.server import repository as repo
from agent_memory.server.checks import REASONING_WORDS, warnings_for
from agent_memory.server.embedding import NullEmbedder
from agent_memory.server.schemas import MemoryIn
from conftest import APPROVE, PG_DSN, REJECT, App, FakeEmbedder, add_rows, plant_review

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
    assert lines[3:] == ["", "totals: 3 memories, 1 refused, "
                             "warned (short 0, no-project 0, no-reasoning 0), 2 clean"]

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
