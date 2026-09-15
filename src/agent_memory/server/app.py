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

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .. import __version__
from . import repository as repo
from .auth import require_token
from .db import make_engine, make_sessionmaker
from .schemas import (
    AddResult,
    AgentCount,
    DeleteResult,
    MemoryIn,
    MemoryOut,
    ProjectCount,
    TagCount,
    TagDetachIn,
    TagMergeIn,
    TagPatch,
    UpdateIn,
    UpdateResult,
)


async def get_session(request: Request) -> AsyncSession:
    sm: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with sm() as session:
        async with session.begin():
            yield session


# scope="function": the dependency closes, and so the transaction commits, before
# the response is sent. With the default request scope the commit would land after
# the client already holds the new id.
SessionDep = Depends(get_session, scope="function")


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
    async def add_memory(body: MemoryIn, session: AsyncSession = SessionDep):
        mid = await repo.add(session, body.content, body.agent or "unknown",
                             body.project, body.tags, body.type)
        return {"id": mid}

    @app.get("/memories", response_model=list[MemoryOut], dependencies=guard)
    async def query_memories(
        response: Response,
        session: AsyncSession = SessionDep,
        q: str | None = None,
        tag: list[str] = Query(default=[]),   # repeatable; OR across all listed tags
        since_days: int | None = None,
        since: str | None = None,
        until: str | None = None,
        project: str | None = None,
        agent: str | None = None,
        type: str | None = None,
        order: str = "date_desc",
        limit: int = 100,
        offset: int = 0,
    ):
        items, total = await repo.list_memories(
            session, q=q, tags=tag, project=project, agent=agent, mtype=type,
            since_days=since_days, since=since, until=until,
            order=order, limit=limit, offset=offset)
        response.headers["X-Total-Count"] = str(total)
        return items

    # `/memories/bulk` and `/memories/search` are declared before `/memories/{mid}`
    # so the literal paths win the match.
    @app.get("/memories/bulk", dependencies=guard)
    async def get_memories_bulk(session: AsyncSession = SessionDep,
                                ids: list[int] = Query(default=[])):
        """Compact rows for a set of ids in one round trip (delete preview)."""
        return await repo.get_many(session, ids)

    @app.get("/memories/search", response_model=list[MemoryOut], dependencies=guard)
    async def search_memories(
        session: AsyncSession = SessionDep,
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
    async def get_memory(mid: int, session: AsyncSession = SessionDep):
        row = await repo.get(session, mid)
        if row is None:
            raise HTTPException(status_code=404, detail=f"Memory #{mid} not found")
        return row

    @app.patch("/memories/{mid}", response_model=UpdateResult, dependencies=guard)
    async def update_memory(mid: int, body: UpdateIn, session: AsyncSession = SessionDep):
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
    async def delete_memories(session: AsyncSession = SessionDep, ids: list[int] = Query(...)):
        found = {r["id"] for r in await repo.get_many(session, ids)}
        to_delete = [i for i in ids if i in found]
        if to_delete:
            await repo.delete(session, to_delete)
        return {"deleted": len(to_delete), "missing": [i for i in ids if i not in found]}

    @app.get("/tags", response_model=list[TagCount], dependencies=guard)
    async def list_tags(session: AsyncSession = SessionDep):
        return await repo.list_tags(session)

    @app.patch("/tags/{name}", dependencies=guard)
    async def patch_tag(name: str, body: TagPatch, session: AsyncSession = SessionDep):
        sent = body.model_fields_set
        result = await repo.patch_tag(
            session, name,
            new_name=body.name if "name" in sent else None,
            description=body.description if "description" in sent else None)
        if result is None:
            raise HTTPException(status_code=404, detail=f"Tag '{name}' not found")
        return result

    @app.delete("/tags/{name}", dependencies=guard)
    async def delete_tag(name: str, session: AsyncSession = SessionDep):
        result = await repo.delete_tag(session, name)
        if result is None:
            raise HTTPException(status_code=404, detail=f"Tag '{name}' not found")
        return result

    @app.post("/tags/merge", dependencies=guard)
    async def merge_tags(body: TagMergeIn, session: AsyncSession = SessionDep):
        return await repo.merge_tags(session, body.sources, body.target, body.description)

    @app.post("/tags/{name}/detach", dependencies=guard)
    async def detach_tag(name: str, body: TagDetachIn, session: AsyncSession = SessionDep):
        result = await repo.detach_tag(session, name, body.memory_ids)
        if result is None:
            raise HTTPException(status_code=404, detail=f"Tag '{name}' not found")
        return result

    @app.get("/projects", response_model=list[ProjectCount], dependencies=guard)
    async def list_projects(session: AsyncSession = SessionDep):
        return await repo.list_projects(session)

    @app.get("/agents", response_model=list[AgentCount], dependencies=guard)
    async def list_agents(session: AsyncSession = SessionDep):
        return await repo.list_agents(session)

    @app.get("/stats", dependencies=guard)
    async def stats(session: AsyncSession = SessionDep):
        return await repo.stats(session)

    # The built dashboard SPA, if present, is served (unauthenticated static assets;
    # its JS carries the bearer token to the API). Registered last so every API
    # route above wins on an exact path match.
    static_dir = os.environ.get("AGENT_MEMORY_STATIC_DIR")
    if static_dir and os.path.isdir(static_dir):
        assets_dir = os.path.join(static_dir, "assets")
        if os.path.isdir(assets_dir):
            app.mount("/assets", StaticFiles(directory=assets_dir), name="dashboard-assets")

        index_path = os.path.join(static_dir, "index.html")

        @app.get("/{full_path:path}")
        async def serve_dashboard(full_path: str):
            """SPA fallback for client-side (Vue Router) routes. Since this route is
            registered last, it only ever receives requests that didn't match a real
            API route above — but a hard refresh (F5) on an app route makes a real
            HTTP GET, so without this, any path deeper than `/` 404s (or worse, if it
            happens to match an API path like the old bare `/tags`, hits that route's
            auth guard instead of the SPA — hence app routes live under /app, see
            router.js). Serves a real file if one exists at this path (favicon.ico,
            mockServiceWorker.js, ...), else index.html so Vue Router takes over."""
            candidate = os.path.join(static_dir, full_path) if full_path else index_path
            if full_path and os.path.isfile(candidate):
                return FileResponse(candidate)
            return FileResponse(index_path)

    return app
