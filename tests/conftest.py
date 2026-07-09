"""Shared fixtures for the cross-surface test suite.

Every surface is exercised against ONE live FastAPI server backed by real
Postgres — the anti-drift guard. The whole suite is skipped unless
``AGENT_MEMORY_TEST_PG_DSN`` points at a dedicated, truncatable database.

- `live_server` (session): schema created once from the models; uvicorn runs in
  a background thread on a free port. Yields `(url, token)`.
- `_truncate` (function, autouse): wipes the three tables with RESTART IDENTITY
  before each test, so state is isolated and ids start at 1.
- `driver` (function, parametrized cli/api/mcp): one driver per surface, built
  against the live server.
"""

from __future__ import annotations

import asyncio
import os
import socket
import threading
import time

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

# The whole suite needs a dedicated, truncatable Postgres. When it isn't
# configured, skip every test (cleanly — nothing connects at import time).
PG_DSN = os.environ.get("AGENT_MEMORY_TEST_PG_DSN")

TOKEN = "test-token"


def pytest_collection_modifyitems(config, items):
    if PG_DSN:
        return
    skip = pytest.mark.skip(
        reason="AGENT_MEMORY_TEST_PG_DSN is not set — the API-first suite needs a "
        "live Postgres (e.g. postgresql://memory:memory@localhost:5433/memory_test)."
    )
    for item in items:
        item.add_marker(skip)


def _async_dsn(dsn: str) -> str:
    """Normalize a plain postgresql:// DSN to the asyncpg driver for SQLAlchemy."""
    if dsn.startswith("postgresql+"):
        return dsn
    scheme, rest = dsn.split("://", 1)
    return f"postgresql+asyncpg://{rest}"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def make_test_engine():
    """A NullPool async engine on the test DB. NullPool means every connection is
    opened and closed per use, so the engine is safe to build on one event loop and
    use from another (the uvicorn thread) without stale pooled connections."""
    return create_async_engine(_async_dsn(PG_DSN), poolclass=NullPool)


@pytest.fixture(scope="session", autouse=True)
def _schema():
    """Create the schema from the models once per session — the schema of truth.
    Autouse + session-scoped so it exists before the first `_truncate`, whatever
    test runs first (repository/migration tests don't touch `live_server`)."""
    from agent_memory.server.models import Base

    async def _do():
        eng = make_test_engine()
        try:
            async with eng.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        finally:
            await eng.dispose()

    asyncio.run(_do())
    yield


@pytest.fixture(scope="session")
def live_server(_schema):
    """Run the real app under uvicorn in a background thread. Yields (url, token).

    NullPool on the server engine: every request opens and closes its own connection,
    so between tests no server connection lingers to hold a lock against `_truncate`."""
    import uvicorn

    from agent_memory.server.app import create_app
    from agent_memory.server.db import make_sessionmaker

    engine = make_test_engine()
    app = create_app(sessionmaker=make_sessionmaker(engine), token=TOKEN)

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start in time")
        time.sleep(0.02)

    url = f"http://127.0.0.1:{port}"
    try:
        yield url, TOKEN
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        asyncio.run(engine.dispose())


@pytest.fixture(autouse=True)
def _truncate(_schema):
    """Wipe the three tables before each test (RESTART IDENTITY so ids start at 1).

    A brief settle after the commit lets the shared session-scoped uvicorn server
    (a separate thread/loop) quiesce any connection still closing from the previous
    test, so a fresh test's first write is never raced by lingering async cleanup."""
    async def _do():
        eng = make_test_engine()
        try:
            async with eng.begin() as conn:
                await conn.execute(
                    text("TRUNCATE memories, tags, memory_tags RESTART IDENTITY CASCADE")
                )
        finally:
            await eng.dispose()

    asyncio.run(_do())
    time.sleep(0.1)


@pytest.fixture(params=["cli", "api", "mcp"])
def driver(request, live_server):
    """One driver per surface, all pointed at the live server."""
    from drivers import DRIVER_FACTORIES

    url, token = live_server
    return DRIVER_FACTORIES[request.param](url, token)
