"""Search by meaning scores on ids and loads only the winners (issue #70).

`search_semantic`, the meaning half of `search_hybrid`, `find_duplicate` and
the neighbours for a review all compare a vector with the stored ones. Before
this change each loaded every candidate row with its tags and reviews, then
scored. Now the first query reads only `id` and `embedding`, the score and
the sort happen in Python, and one more query loads the rows that made the
cut, in score order. `find_duplicate` needs only the best id and score, so it
stops after the first query.

Every SQL statement a test sends is recorded with a `before_cursor_execute`
listener on the engine, so a test can check what the first query selects and
how many rows the row load names. The results are checked against a plain
computation in the test: cosine over every row, sorted by score then id.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import event, select

from agent_memory.server import repository as repo
from agent_memory.server.db import make_sessionmaker
from agent_memory.server.embedding import cosine
from agent_memory.server.models import Memory
from agent_memory.server.review import NEIGHBOUR_COUNT, Verdict
from agent_memory.server.schemas import TagIn
from conftest import FakeEmbedder, make_test_engine

APPROVE = Verdict("approve", None, "Fine.", None, None)

# The first step of every search by meaning selects these two columns only.
ID_AND_VECTOR = ["memories.id", "memories.embedding"]


class QueryLog:
    """Every statement the engine sends, as `(sql, parameters)`, in order."""

    def __init__(self, engine):
        self.entries: list[tuple[str, tuple]] = []
        event.listen(engine.sync_engine, "before_cursor_execute", self._record)

    def _record(self, conn, cursor, statement, parameters, context, executemany):
        self.entries.append((statement, tuple(parameters or ())))

    def clear(self):
        self.entries.clear()

    @property
    def statements(self) -> list[str]:
        return [sql for sql, _ in self.entries]

    @staticmethod
    def columns(sql: str) -> list[str]:
        """The select list of `sql`: what comes between SELECT and FROM."""
        head = sql.split("\nFROM", 1)[0]
        assert head.startswith("SELECT ")
        return [c.strip() for c in head[len("SELECT "):].split(",")]

    def row_loads(self) -> list[tuple[str, tuple]]:
        """The statements that load whole `Memory` rows. The row load starts
        with the mapped columns in order; the loads of tags, reviews and
        superseding rows start with other columns."""
        return [(sql, params) for sql, params in self.entries
                if self.columns(sql)[:2] == ["memories.id", "memories.timestamp"]]


@pytest_asyncio.fixture
async def db():
    """A session on the (already truncated) test DB and the log of every
    statement it sends."""
    engine = make_test_engine()
    log = QueryLog(engine)
    async with make_sessionmaker(engine)() as s, s.begin():
        yield s, log
    await engine.dispose()


# ── the seeded set ───────────────────────────────────────────────────────────
# Twelve memories in two projects, with tags, some verified, one superseded.
# FakeEmbedder vectors come from a hash of the text, so every pair scores a
# different cosine and only the same text scores 1.0.
TEXTS = [
    ("alpha", "the cat sat on the mat"),
    ("alpha", "a dog in the yard"),
    ("alpha", "rain on the window"),
    ("alpha", "coffee before code"),
    ("alpha", "the moon over the hill"),
    ("alpha", "bread in the oven"),
    ("alpha", "a long walk by the river"),
    ("alpha", "snow on the road"),
    ("beta", "the cat sat on the mat"),
    ("beta", "wind in the trees"),
    ("beta", "a light in the window"),
    (None, "the last one, in no project"),
]
QUERY = "the cat sat on the mat"


async def _seed(session, emb):
    """Store the set. Returns `{id: (project, content)}`. The first eight
    rows are verified; the second one (a dog in the yard) is superseded by
    the third."""
    rows = {}
    for n, (project, content) in enumerate(TEXTS):
        tags = [TagIn(name=f"tag{n % 3}")]
        mid = await repo.add(session, content, "t", project, tags, "note", embedder=emb)
        rows[mid] = (project, content)
    ids = list(rows)
    for mid in ids[:8]:
        await repo.set_review(session, mid, APPROVE, "t")
    await repo.set_review(session, ids[2], Verdict("approve", None, "Reverses #2.", None, None,
                                                   [], supersedes=ids[1]), "t")
    await session.flush()
    return rows


def _expected(emb, text, rows, *, project=None, current=False, exclude=()):
    """What the search must return: cosine of `text` against every row's
    content, best first, ties by id newest first, as `[(id, score)]`."""
    query_vec = emb.embed([text])[0]
    superseded = {list(rows)[1]} if current else set()
    scored = [(mid, cosine(query_vec, emb.embed([content])[0]))
              for mid, (proj, content) in rows.items()
              if (project is None or proj == project)
              and mid not in superseded and mid not in exclude]
    scored.sort(key=lambda pair: (-pair[1], -pair[0]))
    return scored


def _pairs(hits):
    return [(h["id"], h["score"]) for h in hits]


def _assert_same(got, expected):
    assert [mid for mid, _ in got] == [mid for mid, _ in expected]
    for (_, a), (_, b) in zip(got, expected):
        assert a == pytest.approx(b, abs=1e-6)


# ── search_semantic ──────────────────────────────────────────────────────────
async def test_semantic_results_and_scores_match_cosine_over_all_rows(db):
    session, log = db
    emb = FakeEmbedder()
    rows = await _seed(session, emb)

    hits = await repo.search_semantic(session, emb, QUERY)
    _assert_same(_pairs(hits), _expected(emb, QUERY, rows))
    assert len(hits) == len(rows)
    # The winners come back whole: tags, review and the supersedes links.
    by_id = {h["id"]: h for h in hits}
    ids = list(rows)
    assert by_id[ids[0]]["tags"] == ["tag0"]
    assert by_id[ids[0]]["review"]["verdict"] == "approve"
    assert by_id[ids[0]]["review_status"] == "verified"
    assert by_id[ids[8]]["review_status"] == "unverified"
    assert by_id[ids[1]]["superseded_by"] == ids[2]
    assert by_id[ids[2]]["supersedes"] == ids[1]
    assert all(h["snippet"] is None for h in hits)


async def test_semantic_limit_keeps_the_top_k(db):
    session, log = db
    emb = FakeEmbedder()
    rows = await _seed(session, emb)
    everything = _expected(emb, QUERY, rows)

    top3 = await repo.search_semantic(session, emb, QUERY, limit=3)
    _assert_same(_pairs(top3), everything[:3])
    for limit in (0, None):
        _assert_same(_pairs(await repo.search_semantic(session, emb, QUERY, limit=limit)),
                     everything)


async def test_semantic_current_hides_the_superseded_row(db):
    session, log = db
    emb = FakeEmbedder()
    rows = await _seed(session, emb)
    superseded = list(rows)[1]

    hits = await repo.search_semantic(session, emb, QUERY, current=True)
    assert superseded not in [h["id"] for h in hits]
    _assert_same(_pairs(hits), _expected(emb, QUERY, rows, current=True))
    # Off by default: the superseded row is still scored and returned.
    assert superseded in [h["id"] for h in await repo.search_semantic(session, emb, QUERY)]


async def test_semantic_first_query_selects_only_id_and_vector(db):
    session, log = db
    emb = FakeEmbedder()
    rows = await _seed(session, emb)
    log.clear()

    hits = await repo.search_semantic(session, emb, QUERY, project="alpha", current=True,
                                      limit=3)
    assert len(hits) == 3

    first = log.statements[0]
    assert log.columns(first) == ID_AND_VECTOR
    # The filters ride on that first query, so no candidate outside them is read.
    assert "memories.project = " in first
    assert "memories.embedding IS NOT NULL" in first
    assert "memories.embedding_model = " in first
    assert "NOT (EXISTS" in first

    # One row load, and it names only the three winners, not the twelve rows.
    loads = log.row_loads()
    assert len(loads) == 1
    sql, params = loads[0]
    assert "memories.id IN (" in sql
    assert sorted(params) == sorted(h["id"] for h in hits)
    assert len(params) == 3 < len(rows)
    # Candidates, row load, then the three loads of tags, reviews and
    # superseding rows: five statements, whatever the table size.
    assert len(log.statements) == 5


async def test_semantic_with_no_candidate_stops_after_the_first_query(db):
    session, log = db
    emb = FakeEmbedder()
    await _seed(session, emb)
    log.clear()

    assert await repo.search_semantic(session, emb, QUERY, project="nowhere") == []
    assert len(log.statements) == 1
    assert log.columns(log.statements[0]) == ID_AND_VECTOR
    assert log.row_loads() == []


# ── find_duplicate ───────────────────────────────────────────────────────────
async def test_find_duplicate_runs_one_query_on_id_and_vector(db):
    session, log = db
    emb = FakeEmbedder()
    rows = await _seed(session, emb)
    log.clear()

    found = await repo.find_duplicate(session, emb, QUERY, "alpha")
    assert found is not None
    best_id, score = found
    # Verified rows of alpha only; the exact text is the best, at cosine 1.0.
    verified_alpha = {mid: v for mid, v in list(rows.items())[:8]}
    assert (best_id, score) == pytest.approx(_expected(emb, QUERY, verified_alpha)[0])
    assert best_id == list(rows)[0]

    assert len(log.statements) == 1
    assert log.columns(log.statements[0]) == ID_AND_VECTOR
    assert log.row_loads() == []


async def test_find_duplicate_below_the_threshold_still_runs_one_query(db):
    session, log = db
    emb = FakeEmbedder()
    await _seed(session, emb)
    log.clear()

    assert await repo.find_duplicate(session, emb, "nothing like this", "alpha") is None
    assert len(log.statements) == 1
    assert log.columns(log.statements[0]) == ID_AND_VECTOR


# ── search_hybrid ────────────────────────────────────────────────────────────
def _rrf(*ranks):
    return sum(1.0 / (repo.RRF_K + r) for r in ranks)


async def _expected_hybrid(session, emb, text, rows, limit=None, **filters):
    """The fusion, computed here from the keyword list and the plain cosine
    order: `[(id, fused score, snippet or None)]`."""
    by_words = await repo.search(session, text, limit=0, **filters)
    by_meaning = _expected(emb, text, rows, **filters)
    fused: dict[int, float] = {}
    for ids in ([h["id"] for h in by_words], [mid for mid, _ in by_meaning]):
        for rank, mid in enumerate(ids, start=1):
            fused[mid] = fused.get(mid, 0.0) + _rrf(rank)
    snippets = {h["id"]: h["snippet"] for h in by_words}
    order = sorted(fused, key=lambda mid: (-fused[mid], -mid))
    if limit:
        order = order[:limit]
    return [(mid, fused[mid], snippets.get(mid)) for mid in order]


async def test_hybrid_matches_the_fusion_of_both_lists(db):
    session, log = db
    emb = FakeEmbedder()
    rows = await _seed(session, emb)
    query = "cat on the mat"  # hits by words in two rows, by meaning in all
    expected = await _expected_hybrid(session, emb, query, rows)
    assert 0 < sum(1 for _, _, snip in expected if snip) < len(expected)

    hits = await repo.search_hybrid(session, emb, query)
    _assert_same(_pairs(hits), [(mid, score) for mid, score, _ in expected])
    assert [h["snippet"] for h in hits] == [snip for _, _, snip in expected]
    # Every hit is a whole memory, whichever list found it.
    for h in hits:
        assert h["tags"] and h["content"] == rows[h["id"]][1]
        assert h["review_status"] in ("verified", "unverified")

    top4 = await repo.search_hybrid(session, emb, query, limit=4)
    _assert_same(_pairs(top4), [(mid, score) for mid, score, _ in expected[:4]])

    current = await repo.search_hybrid(session, emb, query, current=True)
    _assert_same(_pairs(current),
                 [(mid, score) for mid, score, _ in
                  await _expected_hybrid(session, emb, query, rows, current=True)])


async def test_hybrid_scores_on_ids_and_loads_only_the_rows_words_missed(db):
    session, log = db
    emb = FakeEmbedder()
    rows = await _seed(session, emb)
    query = "cat on the mat"
    by_words = {h["id"] for h in await repo.search(session, query, limit=0)}
    log.clear()

    hits = await repo.search_hybrid(session, emb, query, limit=4)
    assert len(hits) == 4

    # Statement one is the keyword search (it loads its rows, as before);
    # the meaning half is the two-column select.
    assert "plainto_tsquery" in log.statements[0]
    two_column = [sql for sql in log.statements if log.columns(sql) == ID_AND_VECTOR]
    assert len(two_column) == 1
    # Then one row load, for the winners the keyword list did not carry.
    wanted = [h["id"] for h in hits if h["id"] not in by_words]
    loads = [(sql, params) for sql, params in log.row_loads() if "memories.id IN (" in sql]
    assert len(loads) == 1
    assert sorted(loads[0][1]) == sorted(wanted)
    assert len(wanted) < len(rows)


# ── the neighbours for a review ──────────────────────────────────────────────
async def test_neighbours_load_only_the_five_best(db):
    session, log = db
    emb = FakeEmbedder()
    rows = await _seed(session, emb)
    verified_alpha = {mid: v for mid, v in list(rows.items())[:8]}
    text = "a cat on a mat"
    log.clear()

    neighbours = await repo.neighbours_for(session, emb, text, "alpha")
    assert len(neighbours) == NEIGHBOUR_COUNT == 5
    _assert_same(_pairs(neighbours), _expected(emb, text, verified_alpha)[:5])
    assert all(n["review_status"] == "verified" and n["project"] == "alpha" for n in neighbours)
    assert all(n["tags"] for n in neighbours)

    assert log.columns(log.statements[0]) == ID_AND_VECTOR
    loads = log.row_loads()
    assert len(loads) == 1
    assert len(loads[0][1]) == 5
    assert sorted(loads[0][1]) == sorted(n["id"] for n in neighbours)


async def test_review_input_leaves_the_memory_out_and_loads_the_five_best(db):
    session, log = db
    emb = FakeEmbedder()
    rows = await _seed(session, emb)
    mine = list(rows)[0]
    others = {mid: v for mid, v in list(rows.items())[1:8]}
    log.clear()

    memory, neighbours = await repo.review_input(session, mine)
    assert memory["id"] == mine
    assert mine not in [n["id"] for n in neighbours]
    _assert_same(_pairs(neighbours), _expected(emb, rows[mine][1], others)[:5])

    # The memory itself first, then the two steps for its neighbours.
    two_column = [sql for sql in log.statements if log.columns(sql) == ID_AND_VECTOR]
    assert len(two_column) == 1
    assert "memories.id != " in two_column[0]
    loads = [(sql, params) for sql, params in log.row_loads() if "memories.id IN (" in sql]
    assert len(loads) == 1
    assert len(loads[0][1]) == 5


# ── the real model ───────────────────────────────────────────────────────────
@pytest.mark.embed
async def test_real_model_results_match_a_full_scan(retrieval_set):
    """With the real model on the retrieval set, both search modes return
    what a plain cosine over every stored vector returns, id for id and
    score for score, so the two-step shape changes nothing a caller sees."""
    pytest.importorskip("fastembed")
    from agent_memory.server.embedding import FastEmbedEmbedder

    emb = FastEmbedEmbedder()
    engine = make_test_engine()
    try:
        async with make_sessionmaker(engine)() as session, session.begin():
            await repo.reindex(session, emb)
            stored = {mid: list(vec) for mid, vec in
                      await session.execute(select(Memory.id, Memory.embedding))}
            for q in retrieval_set:
                query_vec = emb.embed([q["question"]])[0]
                expected = sorted(((mid, cosine(query_vec, vec)) for mid, vec in stored.items()),
                                  key=lambda pair: (-pair[1], -pair[0]))
                hits = await repo.search_semantic(session, emb, q["question"], limit=5)
                _assert_same(_pairs(hits), expected[:5])

                by_words = await repo.search(session, q["question"], limit=0)
                fused: dict[int, float] = {}
                for ids in ([h["id"] for h in by_words], [mid for mid, _ in expected]):
                    for rank, mid in enumerate(ids, start=1):
                        fused[mid] = fused.get(mid, 0.0) + _rrf(rank)
                order = sorted(fused, key=lambda mid: (-fused[mid], -mid))[:5]
                hybrid = await repo.search_hybrid(session, emb, q["question"], limit=5)
                _assert_same(_pairs(hybrid), [(mid, fused[mid]) for mid in order])
    finally:
        await engine.dispose()
