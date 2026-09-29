"""Search quality with the real model: keyword, semantic and hybrid side by side.

`test_retrieval.py` scores keyword search alone and never fails on the number.
This file runs the same 20 questions from `tests/data/retrieval_set.json`
through all three search modes, with vectors from the real embedding model,
and prints one table: mode, recall@1, recall@3, recall@5. Two claims are
checked, and they are the point of the semantic work:

- hybrid search must not lose to keyword search (recall@3 is at least as high);
- semantic search must find most of the paraphrased questions, the ones that
  share no words with their answer and so are lost on keyword search.

The test needs fastembed (the `[embed]` extra) and downloads the model on
first run into `AGENT_MEMORY_EMBED_CACHE`, so it carries the `embed` marker and
skips when fastembed is not installed.
"""

import time

import pytest

from agent_memory.server import repository as repo
from agent_memory.server.db import make_sessionmaker
from conftest import load_retrieval_set, make_test_engine
from drivers import recall_at

pytestmark = pytest.mark.embed

KS = (1, 3, 5)
LIMIT = max(KS)
MODES = ("keyword", "semantic", "hybrid")

# A paraphrased question counts as found when every memory that answers it is
# in the top 3. At least this many of them must be found by semantic search.
PARAPHRASED_FOUND_MIN = 6


def _mean(values):
    return sum(values) / len(values)


async def _run_mode(session, embedder, mode, question):
    if mode == "keyword":
        return await repo.search(session, question, limit=LIMIT)
    if mode == "semantic":
        return await repo.search_semantic(session, embedder, question, limit=LIMIT)
    return await repo.search_hybrid(session, embedder, question, limit=LIMIT)


async def test_retrieval_quality_by_mode(retrieval_set, record_property):
    pytest.importorskip("fastembed")
    from agent_memory.server.embedding import FastEmbedEmbedder

    t0 = time.perf_counter()
    embedder = FastEmbedEmbedder()
    load_s = time.perf_counter() - t0

    # recall[mode][k] is the list of per-question scores; found[mode] holds
    # the paraphrased questions whose answers all sit in the top 3.
    recall = {mode: {k: [] for k in KS} for mode in MODES}
    found = {mode: 0 for mode in MODES}
    misses = {mode: [] for mode in MODES}
    paraphrased = [q for q in retrieval_set if q["paraphrase"]]
    memory_count = len(load_retrieval_set()[0])

    engine = make_test_engine()
    try:
        async with make_sessionmaker(engine)() as session:
            t0 = time.perf_counter()
            embedded = (await repo.reindex(session, embedder))["updated"]
            reindex_s = time.perf_counter() - t0
            assert embedded == memory_count, "every memory in the set should get a vector"

            for q in retrieval_set:
                for mode in MODES:
                    hits = await _run_mode(session, embedder, mode, q["question"])
                    for k in KS:
                        recall[mode][k].append(recall_at(k, hits, q["expected_ids"]))
                    at3 = recall_at(3, hits, q["expected_ids"])
                    if at3 < 1.0:
                        misses[mode].append((q["paraphrase"], at3, q["question"]))
                    elif q["paraphrase"]:
                        found[mode] += 1
    finally:
        await engine.dispose()

    print(f"\nmodel {embedder.model_name}: load {load_s:.1f}s, "
          f"{embedded} memories embedded in {reindex_s:.1f}s")
    print(f"{'mode':<10} " + " ".join(f"{'recall@' + str(k):>9}" for k in KS)
          + f" {'paraphrased found@3':>20}")
    for mode in MODES:
        cells = " ".join(f"{_mean(recall[mode][k]):>9.2f}" for k in KS)
        print(f"{mode:<10} {cells} {found[mode]:>10}/{len(paraphrased)}")
    for mode in MODES:
        for is_para, at3, question in misses[mode]:
            kind = "paraphrased" if is_para else "same words"
            print(f"  {mode} missed ({kind}, recall@3 {at3:.2f}): {question}")

    for mode in MODES:
        for k in KS:
            record_property(f"recall@{k} {mode}", f"{_mean(recall[mode][k]):.2f}")
        record_property(f"recall@3 {mode} paraphrased found",
                        f"{found[mode]}/{len(paraphrased)}")

    keyword_at3 = _mean(recall["keyword"][3])
    hybrid_at3 = _mean(recall["hybrid"][3])
    assert hybrid_at3 >= keyword_at3, (
        f"hybrid recall@3 {hybrid_at3:.2f} is below keyword recall@3 {keyword_at3:.2f}")
    assert found["semantic"] >= PARAPHRASED_FOUND_MIN, (
        f"semantic search found {found['semantic']} of {len(paraphrased)} paraphrased "
        f"questions at recall@3; at least {PARAPHRASED_FOUND_MIN} are needed")
