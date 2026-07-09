"""FastAPI app — thin async routes that delegate to the repository.

The engine/sessionmaker live for the app's lifetime (managed in `lifespan`, or
injected for tests). `get_session` hands each request one session inside a
transaction: commit on return, rollback on error. Agent identity is a *client*
concern — the CLI/MCP resolve it and send it; the server only falls back to
"unknown" if a caller omits it.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .. import __version__
from . import repository as repo
from .auth import require_token
from .db import make_engine, make_sessionmaker
from .schemas import (
    AddResult,
    DeleteResult,
    MemoryIn,
    MemoryOut,
    ProjectCount,
    TagCount,
    UpdateIn,
    UpdateResult,
)


async def get_session(request: Request) -> AsyncSession:
    sm: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with sm() as session:
        async with session.begin():
            yield session


def create_app(sessionmaker: async_sessionmaker | None = None, token: str | None = None) -> FastAPI:
    """Build the app. In production (`sessionmaker` omitted) the lifespan builds a
    pooled engine from AGENT_MEMORY_DB and disposes it on shutdown; tests inject a
    sessionmaker bound to their own test engine. Token falls back to
    AGENT_MEMORY_API_TOKEN."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = None
        if sessionmaker is None:
            engine = make_engine()
            app.state.sessionmaker = make_sessionmaker(engine)
        else:
            app.state.sessionmaker = sessionmaker
        try:
            yield
        finally:
            if engine is not None:
                await engine.dispose()

    app = FastAPI(title="agent-memory", version=__version__, lifespan=lifespan)
    app.state.token = token if token is not None else os.environ.get("AGENT_MEMORY_API_TOKEN")
    if sessionmaker is not None:
        # Available even when the ASGI lifespan isn't run (e.g. httpx ASGITransport tests).
        app.state.sessionmaker = sessionmaker
    guard = [Depends(require_token)]

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/memories", response_model=AddResult, status_code=201, dependencies=guard)
    async def add_memory(body: MemoryIn, session: AsyncSession = Depends(get_session)):
        mid = await repo.add(session, body.content, body.agent or "unknown",
                             body.project, body.tags, body.type)
        return {"id": mid}

    @app.get("/memories", response_model=list[MemoryOut], dependencies=guard)
    async def query_memories(
        session: AsyncSession = Depends(get_session),
        since_days: int | None = None,
        since: str | None = None,
        until: str | None = None,
        project: str | None = None,
        agent: str | None = None,
        tag: str | None = None,
        type: str | None = None,
        limit: int | None = None,
    ):
        return await repo.query(session, since_days=since_days, since=since, until=until,
                                project=project, agent=agent, tag=tag, mtype=type, limit=limit)

    # `/memories/bulk` and `/memories/search` are declared before `/memories/{mid}`
    # so the literal paths win the match.
    @app.get("/memories/bulk", dependencies=guard)
    async def get_memories_bulk(session: AsyncSession = Depends(get_session),
                                ids: list[int] = Query(default=[])):
        """Compact rows for a set of ids in one round trip (delete preview)."""
        return await repo.get_many(session, ids)

    @app.get("/memories/search", response_model=list[MemoryOut], dependencies=guard)
    async def search_memories(
        session: AsyncSession = Depends(get_session),
        q: str = "",
        project: str | None = None,
        agent: str | None = None,
        since: str | None = None,
        tag: str | None = None,
        limit: int = 20,
    ):
        return await repo.search(session, q, project=project, agent=agent,
                                 since=since, tag=tag, limit=limit)

    @app.get("/memories/{mid}", response_model=MemoryOut, dependencies=guard)
    async def get_memory(mid: int, session: AsyncSession = Depends(get_session)):
        row = await repo.get(session, mid)
        if row is None:
            raise HTTPException(status_code=404, detail=f"Memory #{mid} not found")
        return row

    @app.patch("/memories/{mid}", response_model=UpdateResult, dependencies=guard)
    async def update_memory(mid: int, body: UpdateIn, session: AsyncSession = Depends(get_session)):
        sent = body.model_fields_set
        changes = await repo.update(
            session, mid,
            content=body.content if "content" in sent else None,
            project=body.project if "project" in sent else None,
            mtype=body.type if "type" in sent else None,
            set_tags=body.set_tags if "set_tags" in sent else None,
            add_tags=body.add_tags if "add_tags" in sent else None,
            remove_tags=body.remove_tags if "remove_tags" in sent else None,
        )
        if changes is None:
            raise HTTPException(status_code=404, detail=f"Memory #{mid} not found")
        return {"changes": changes}

    @app.delete("/memories", response_model=DeleteResult, dependencies=guard)
    async def delete_memories(session: AsyncSession = Depends(get_session), ids: list[int] = Query(...)):
        found = {r["id"] for r in await repo.get_many(session, ids)}
        to_delete = [i for i in ids if i in found]
        if to_delete:
            await repo.delete(session, to_delete)
        return {"deleted": len(to_delete), "missing": [i for i in ids if i not in found]}

    @app.get("/tags", response_model=list[TagCount], dependencies=guard)
    async def list_tags(session: AsyncSession = Depends(get_session)):
        return await repo.list_tags(session)

    @app.get("/projects", response_model=list[ProjectCount], dependencies=guard)
    async def list_projects(session: AsyncSession = Depends(get_session)):
        return await repo.list_projects(session)

    @app.get("/stats", dependencies=guard)
    async def stats(session: AsyncSession = Depends(get_session)):
        return await repo.stats(session)

    return app
