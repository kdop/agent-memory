"""Search quality against the fixed retrieval set (`tests/data/retrieval_set.json`).

The set holds memories written like real entries and questions with known
answers. Half the questions use the same words as their answer; the other half
are paraphrased on purpose, so a word-matching search cannot find them. The
baseline test here scores the current keyword search with recall@3 and records
the number. It never fails on the score: it measures, it does not gate. Later
search work (semantic search, hybrid ranking) is compared against this number.
"""

from agent_memory.server import repository as repo
from agent_memory.server.db import make_sessionmaker
from conftest import load_retrieval_set, make_test_engine
from drivers import recall_at

K = 3


def test_retrieval_set_is_well_formed():
    """The file must hold the set the issue asks for: about 30 memories, 20
    questions, at least 8 of them paraphrased and marked as such."""
    memories, questions = load_retrieval_set()
    assert 25 <= len(memories) <= 35
    assert len(questions) == 20
    assert sum(q["paraphrase"] for q in questions) >= 8
    assert len({m["project"] for m in memories}) >= 3
    assert {m["type"] for m in memories} == {"decision", "lesson", "preference", "note"}
    for m in memories:
        assert m["content"].strip()
        assert all(t["name"] for t in m["tags"])


def test_recall_at():
    assert recall_at(3, [1, 2, 3], {1}) == 1.0
    assert recall_at(3, [9, 8, 7, 1], {1}) == 0.0
    assert recall_at(3, [1, 5, 2], {1, 2, 3, 4}) == 0.5
    assert recall_at(3, [{"id": 4}], {4}) == 1.0
    assert recall_at(3, [], {4}) == 0.0


async def test_retrieval_keyword_baseline(retrieval_set, record_property):
    """Run every question through the keyword search and record recall@3.

    This test does not fail on a low score. It prints the number and stores it
    as a test property, and conftest echoes it in the terminal summary."""
    engine = make_test_engine()
    scores = {True: [], False: []}
    misses = []
    try:
        async with make_sessionmaker(engine)() as session:
            for q in retrieval_set:
                hits = await repo.search(session, q["question"], limit=K)
                score = recall_at(K, hits, q["expected_ids"])
                scores[q["paraphrase"]].append(score)
                if score < 1.0:
                    misses.append(q["question"])
    finally:
        await engine.dispose()

    every = scores[True] + scores[False]
    overall = sum(every) / len(every)
    direct = sum(scores[False]) / len(scores[False])
    paraphrase = sum(scores[True]) / len(scores[True])

    print(f"keyword search recall@{K}: {overall:.2f} over {len(every)} questions "
          f"(same words: {direct:.2f} over {len(scores[False])}, "
          f"paraphrased: {paraphrase:.2f} over {len(scores[True])})")
    for question in misses:
        print(f"  missed: {question}")

    record_property(f"recall@{K} keyword", f"{overall:.2f}")
    record_property(f"recall@{K} keyword same-words", f"{direct:.2f}")
    record_property(f"recall@{K} keyword paraphrased", f"{paraphrase:.2f}")
    assert 0.0 <= overall <= 1.0
