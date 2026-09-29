"""Shared fixtures and fakes for the test suite.

Every test runs against a real Postgres. The whole suite is skipped unless
``AGENT_MEMORY_TEST_PG_DSN`` points at a dedicated, truncatable database.

Fixtures:
- `_truncate` (autouse): wipes the tables before each test, so ids start at 1.
- `session`: a session on the test DB, for repository tests.
- `live_server` (session): the real app under uvicorn with no review model.
  Yields `(url, token)`. The CLI subprocess, the MCP tools and the client
  talk HTTP, so they need a real server.
- `reviewing`: a second live server with a `FakeReviewer`, reset per test.
- `retrieval_set`: loads `tests/data/retrieval_set.json` into the test DB.

Fakes and helpers:
- `FakeEmbedder`: deterministic vectors from a hash of the text.
- `FakeReviewer`: answers a fixed verdict (or one per content), records every
  call, and can hold a review open or say the model is away.
- `App`: an in-process app driven through httpx's ASGI transport. That
  transport waits for background tasks, so a verdict is stored by the time
  a request returns.
- `QueryLog`: every SQL statement an engine sends.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import socket
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, select, text, update
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from agent_memory.server.review import Verdict

# The whole suite needs a dedicated, truncatable Postgres. When it isn't
# configured, skip every test (cleanly: nothing connects at import time).
PG_DSN = os.environ.get("AGENT_MEMORY_TEST_PG_DSN")

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

APPROVE = Verdict("approve", None, "A decision with its reason.", None, None)
# Rule 1, not 2 or 4: a reject under those archives the memory, and most tests
# want a flagged memory that stays in view. ARCHIVING is the one that archives.
REJECT = Verdict("reject", 1, "It will not matter in a later session.", None, None)
ARCHIVING = Verdict("reject", 2, "A diary line: it says what was done, not why.", None, None)
REWRITE = Verdict("rewrite", 3, "Say why.", "Chose Postgres,\nbecause of X.", None,
                  ["database", "search"])


class FakeEmbedder:
    """Deterministic vectors from a hash of the text: same text, same vector,
    every run, no model. Unit length, 8 wide, so `cosine` on them is a dot
    product like on the real thing. Only the same text scores 1.0."""

    model_name = "fake"
    dim = 8

    def embed(self, texts):
        return [self._one(t) for t in texts]

    def _one(self, text):
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        # Each byte becomes a signed value in [-1, 1); eight of them make the vector.
        raw = [(b - 128) / 128.0 for b in digest[: self.dim]]
        norm = math.sqrt(sum(x * x for x in raw)) or 1.0
        return [x / norm for x in raw]


def vector(text):
    return FakeEmbedder().embed([text])[0]


class FakeReviewer:
    """A stand-in for the review model.

    `verdict` is the answer: a Verdict, None for no answer, or an exception
    to raise. `verdicts` maps a memory's content to its own answer. Every
    call is kept in `calls` as `(memory, neighbours, tags)`.

    For the poll: `up` is what `reachable()` answers, `checks` counts those
    calls, and `check_error` is raised by it when set. With `block`, every
    review waits in its thread until the test sets `release`; `started` is
    set when the first one waits."""

    model_name = "fake-reviewer"

    def __init__(self, verdict=REJECT, verdicts=None, up=True, block=False):
        self.reset(verdict, verdicts, up, block)

    def reset(self, verdict=REJECT, verdicts=None, up=True, block=False):
        self.verdict = verdict
        self.verdicts = dict(verdicts or {})
        self.up = up
        self.block = block
        self.check_error = None
        self.checks = 0
        self.calls = []
        self.started = threading.Event()
        self.release = threading.Event()

    def reachable(self):
        self.checks += 1
        if self.check_error is not None:
            raise self.check_error
        return self.up

    def review(self, memory, neighbours, tags=()):
        self.calls.append((memory, neighbours, list(tags)))
        if self.block:
            self.started.set()
            if not self.release.wait(timeout=10):
                raise TimeoutError("the test never released the review")
        answer = self.verdicts.get(memory["content"], self.verdict)
        if isinstance(answer, Exception):
            raise answer
        return answer

    @property
    def ids(self):
        """The ids of the memories reviewed, in order."""
        return [m["id"] for m, _, _ in self.calls]


def pytest_collection_modifyitems(config, items):
    if PG_DSN:
        return
    skip = pytest.mark.skip(
        reason="AGENT_MEMORY_TEST_PG_DSN is not set: the suite needs a live Postgres "
        "(e.g. postgresql://memory:memory@localhost:5433/memory_test)."
    )
    for item in items:
        item.add_marker(skip)


def _async_dsn(dsn: str) -> str:
    """Normalize a plain postgresql:// DSN to the asyncpg driver for SQLAlchemy."""
    if dsn.startswith("postgresql+"):
        return dsn
    return "postgresql+asyncpg://" + dsn.split("://", 1)[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_test_engine(**kwargs):
    """An async engine on the test DB. NullPool by default: every connection
    is opened and closed per use, so the engine is safe to build on one event
    loop and use from another (the uvicorn thread)."""
    if "pool_size" not in kwargs:
        kwargs["poolclass"] = NullPool
    return create_async_engine(_async_dsn(PG_DSN), **kwargs)


@asynccontextmanager
async def db_session():
    """A session on the test DB, committed when the block ends."""
    from agent_memory.server.db import make_sessionmaker

    engine = make_test_engine()
    try:
        async with make_sessionmaker(engine)() as s, s.begin():
            yield s
    finally:
        await engine.dispose()


async def create_schema():
    from agent_memory.server.models import Base

    engine = make_test_engine()
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def _schema():
    """Create the schema from the models once per session."""
    asyncio.run(create_schema())


# Emptying the tables with DELETE and restarting the id sequences is several
# times faster than TRUNCATE on tables this small, and this runs before every test.
_EMPTY = ("DELETE FROM memory_reviews; DELETE FROM tag_reviews; DELETE FROM memory_tags; "
          "DELETE FROM tags; DELETE FROM memories; ALTER SEQUENCE memories_id_seq RESTART; "
          "ALTER SEQUENCE tags_id_seq RESTART; ALTER SEQUENCE memory_reviews_id_seq RESTART; "
          "ALTER SEQUENCE tag_reviews_id_seq RESTART")


@pytest.fixture(autouse=True)
def _truncate(_schema):
    """Empty the tables before each test, so ids start at 1."""
    import asyncpg

    async def _do():
        conn = await asyncpg.connect(PG_DSN)
        try:
            await conn.execute(_EMPTY)
        finally:
            await conn.close()

    asyncio.run(_do())


@pytest_asyncio.fixture
async def session():
    """A session on the (already truncated) test DB, the same sessionmaker the
    server uses. Tests read their own writes; nothing needs a commit."""
    async with db_session() as s:
        yield s


@contextmanager
def live_app(app):
    """Run `app` under uvicorn in a background thread for the length of the
    block; yields its URL."""
    import uvicorn

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start in time")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture(scope="session")
def live_server(_schema):
    """The real app under uvicorn, with FakeEmbedder and no review model.
    Yields `(url, token)`."""
    from agent_memory.server.app import create_app
    from agent_memory.server.db import make_sessionmaker

    engine = make_test_engine()
    app = create_app(sessionmaker=make_sessionmaker(engine), token=TOKEN,
                     embedder=FakeEmbedder(), review_poll=0)
    with live_app(app) as url:
        yield url, TOKEN
    asyncio.run(engine.dispose())


class Reviewing:
    """What the `reviewing` fixture hands a test: the URL of a live server
    whose review model is `fake`, and its `app`, to set the review mode."""

    def __init__(self, url, fake, app):
        self.url, self.fake, self.app = url, fake, app
        self.embedder = app.state.embedder


@pytest.fixture(scope="session")
def _reviewing_server(_schema):
    from agent_memory.server.app import create_app
    from agent_memory.server.db import make_sessionmaker

    engine = make_test_engine()
    fake = FakeReviewer()
    app = create_app(sessionmaker=make_sessionmaker(engine), token=TOKEN,
                     embedder=FakeEmbedder(), reviewer=fake, review_poll=0)
    with live_app(app) as url:
        yield Reviewing(url, fake, app)
    asyncio.run(engine.dispose())


@pytest.fixture
def reviewing(_reviewing_server):
    """The live server with a FakeReviewer, reset for this test: flag mode,
    FakeEmbedder, a reject verdict, no calls."""
    r = _reviewing_server
    r.app.state.review_mode = "flag"
    r.app.state.embedder = r.embedder
    r.fake.reset()
    return r


def wait_until(check, timeout=10):
    """Wait in a sync test until `check()` is true."""
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("the condition did not come true in time")
        time.sleep(0.02)


async def until(check, timeout=5.0):
    """Wait, without blocking the loop, until `check()` is true."""
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("the condition did not come true in time")
        await asyncio.sleep(0.01)


class App:
    """An in-process app over the test database, driven through httpx's
    ASGITransport, with the token sent on every request."""

    def __init__(self, reviewer=None, embedder="fake", engine=None, **kwargs):
        """`embedder` is FakeEmbedder unless given; None builds the app with
        none at all."""
        from agent_memory.server.app import create_app
        from agent_memory.server.db import make_sessionmaker

        # A pooled engine: the app lives on this test's loop only, and reusing
        # connections saves a connect per request.
        self.engine = engine or make_test_engine(pool_size=5)
        if embedder == "fake":
            embedder = FakeEmbedder()
        self.app = create_app(sessionmaker=make_sessionmaker(self.engine), token=TOKEN,
                              embedder=embedder, reviewer=reviewer, **kwargs)

    async def __aenter__(self):
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                        base_url="http://testserver", headers=AUTH)
        return self

    async def __aexit__(self, *exc):
        await self.client.aclose()
        await self.engine.dispose()

    async def post(self, content, project="alpha", type=None, tags=(), agent="tester",
                   **params):
        """POST /memories. `tags` are names or {name, description} dicts."""
        return await self.client.post("/memories", params=params, json={
            "content": content, "project": project, "type": type, "agent": agent,
            "tags": [t if isinstance(t, dict) else {"name": t} for t in tags]})

    async def add(self, content, **kw):
        """Add a memory that must be stored; returns its id."""
        resp = await self.post(content, **kw)
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    async def get(self, mid):
        resp = await self.client.get(f"/memories/{mid}")
        assert resp.status_code == 200, resp.text
        return resp.json()

    async def ids(self, path="/memories", **params):
        """The ids a listing route returns."""
        resp = await self.client.get(path, params=params)
        assert resp.status_code == 200, resp.text
        return [m["id"] for m in resp.json()]


# ── the test database, straight ──────────────────────────────────────────────
async def add_rows(*contents, project="alpha", embedder=None):
    """Store memories through the repository, unverified; returns their ids.
    With FakeEmbedder by default, so the rows have vectors."""
    from agent_memory.server import repository as repo

    emb = embedder if embedder is not None else FakeEmbedder()
    async with db_session() as s:
        return [await repo.add(s, c, "tester", project, [], None, embedder=emb)
                for c in contents]


async def plant_review(mid, verdict, model="planted"):
    """Store a verdict for `mid` the way the server does."""
    from agent_memory.server import repository as repo

    async with db_session() as s:
        await repo.set_review(s, mid, verdict, model)


def verify(*ids):
    """Plant an approve verdict for each id, which makes it `verified`: only
    verified memories count as reference for the duplicate check and the
    neighbours."""
    for mid in ids:
        asyncio.run(plant_review(mid, APPROVE))


def flag(*ids):
    for mid in ids:
        asyncio.run(plant_review(mid, REJECT))


async def set_age(mid, minutes):
    """Date memory `mid` `minutes` ago."""
    from agent_memory.server.models import Memory

    when = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    async with db_session() as s:
        await s.execute(update(Memory).where(Memory.id == mid).values(timestamp=when))


async def review_rows():
    """`[(memory_id, verdict, model)]` straight from the table."""
    from agent_memory.server.models import MemoryReview

    async with db_session() as s:
        stmt = select(MemoryReview.memory_id, MemoryReview.verdict, MemoryReview.model)
        return [tuple(r) for r in
                (await s.execute(stmt.order_by(MemoryReview.memory_id, MemoryReview.id))).all()]


async def statuses():
    """`[(id, review_status)]` straight from the table."""
    from agent_memory.server.models import Memory

    async with db_session() as s:
        stmt = select(Memory.id, Memory.review_status).order_by(Memory.id)
        return [tuple(r) for r in (await s.execute(stmt)).all()]


async def stored_vector(mid, table="memories", session=None):
    """`(embedding, embedding_model)` of a row; no route returns them. With
    `session`, read through it (it sees what it has not committed yet)."""
    sql = text(f"SELECT embedding, embedding_model FROM {table} WHERE id = :m")
    if session is not None:
        await session.flush()
        row = (await session.execute(sql, {"m": mid})).one()
    else:
        async with db_session() as s:
            row = (await s.execute(sql, {"m": mid})).one()
    return (list(row[0]) if row[0] is not None else None, row[1])


def same_vector(got, text_):
    """True when the stored vector `got` is the FakeEmbedder vector of `text_`."""
    want = vector(text_)
    return got is not None and all(abs(a - b) < 1e-6 for a, b in zip(got, want))


class QueryLog:
    """Every statement an engine sends, as `(sql, parameters)`, in order."""

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
        """The statements that load whole `Memory` rows by id."""
        return [(sql, params) for sql, params in self.entries
                if self.columns(sql)[:2] == ["memories.id", "memories.timestamp"]
                and "memories.id IN (" in sql]


def real_review_server():
    """The Ollama server to test against, or None: unset, or no answer in 2 s."""
    import urllib.error
    import urllib.request

    url = os.environ.get("AGENT_MEMORY_REVIEW_URL", "").strip().rstrip("/")
    if not url:
        return None
    try:
        with urllib.request.urlopen(url + "/api/tags", timeout=2):
            return url
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


# ── the retrieval set ────────────────────────────────────────────────────────
RETRIEVAL_SET_PATH = Path(__file__).resolve().parent / "data" / "retrieval_set.json"


def load_retrieval_set(path=RETRIEVAL_SET_PATH):
    """Read the retrieval set file and check it is well formed.

    Returns `(memories, questions)`. Each memory has `content`, `project`, `type`
    and `tags`; each question has `question`, `answers` (indexes into memories)
    and `paraphrase`. A bad index or an empty answer list is a data error, so it
    raises rather than letting a test score against nothing."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    memories, questions = data["memories"], data["questions"]
    for n, q in enumerate(questions):
        if not q["answers"]:
            raise ValueError(f"question {n} has no answers: {q['question']!r}")
        for i in q["answers"]:
            if not 0 <= i < len(memories):
                raise ValueError(f"question {n} points at memory {i}, "
                                 f"but there are only {len(memories)}")
        q.setdefault("paraphrase", False)
    return memories, questions


@pytest.fixture
def retrieval_set():
    """Insert the retrieval set's memories into the (already truncated) test DB.

    Returns the questions, each with an `expected_ids` list holding the ids the
    answering memories were given on insert."""
    from agent_memory.server import repository as repo
    from agent_memory.server.schemas import TagIn

    memories, questions = load_retrieval_set()

    async def _load():
        async with db_session() as s:
            return [await repo.add(s, m["content"], "retrieval-set", m["project"],
                                   [TagIn(**t) for t in m.get("tags", [])], m["type"])
                    for m in memories]

    ids = asyncio.run(_load())
    return [dict(q, expected_ids=[ids[i] for i in q["answers"]]) for q in questions]


def pytest_terminal_summary(terminalreporter):
    """Print recorded retrieval and verdict scores at the end of the run, so
    they show even under `-q`, where a passing test's stdout is hidden."""
    lines = []
    for rep in terminalreporter.stats.get("passed", []):
        for name, value in getattr(rep, "user_properties", []):
            if name.startswith(("recall@", "verdict ")):
                lines.append(f"{rep.nodeid}: {name} = {value}")
    if lines:
        terminalreporter.write_sep("-", "quality scores")
        for line in lines:
            terminalreporter.write_line(line)

