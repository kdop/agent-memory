"""Async database engine + session wiring (server-side only).

One pooled `AsyncEngine` per process; `get_session` is the FastAPI dependency that
hands each request a session inside a transaction (commit on success, rollback on
error). The DSN comes from `AGENT_MEMORY_DB`; the psycopg/plain scheme is normalized
to the asyncpg driver so callers can paste a standard `postgresql://…` URL.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def resolve_async_dsn(dsn: str | None = None) -> str:
    """Normalize a Postgres DSN to the asyncpg driver. Accepts `postgres://`,
    `postgresql://`, or an already-qualified `postgresql+asyncpg://`."""
    dsn = dsn or os.environ.get("AGENT_MEMORY_DB")
    if not dsn or not dsn.startswith(("postgres://", "postgresql://", "postgresql+")):
        raise RuntimeError(
            "AGENT_MEMORY_DB must be a postgresql:// DSN (this service is Postgres-only)."
        )
    if dsn.startswith("postgresql+"):
        return dsn
    scheme, rest = dsn.split("://", 1)
    return f"postgresql+asyncpg://{rest}"


def make_engine(dsn: str | None = None, **kw) -> AsyncEngine:
    """Build the pooled async engine. `pool_pre_ping` guards against stale
    connections (important for a long-lived server behind a network)."""
    return create_async_engine(
        resolve_async_dsn(dsn), pool_pre_ping=True, pool_size=10, max_overflow=20, **kw
    )


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


def session_dependency(sessionmaker: async_sessionmaker[AsyncSession]):
    """Build the `get_session` dependency bound to a sessionmaker. One transaction
    per request: commit if the handler returns, roll back if it raises."""

    async def get_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            async with session.begin():
                yield session

    return get_session
