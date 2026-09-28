"""FastAPI app — thin async routes that delegate to the repository.

The engine/sessionmaker live for the app's lifetime (managed in `lifespan`, or
injected for tests). `get_session` hands each request one session inside a
transaction: commit on return, rollback on error. Agent identity is a *client*
concern — the CLI/MCP resolve it and send it; the server only falls back to
"unknown" if a caller omits it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .. import __version__
from . import repository as repo
from .auth import require_token
from .checks import warnings_for
from .db import make_engine, make_sessionmaker
from .embedding import Embedder, make_embedder
from .review import Reviewer, Verdict, make_reviewer
from .schemas import (
    AddResult,
    AgentCount,
    DeleteResult,
    MemoryIn,
    MemoryOut,
    ProjectCount,
    ReviewOut,
    TagCount,
    TagDetachIn,
    TagMergeIn,
    TagPatch,
    UpdateIn,
    UpdateResult,
)

log = logging.getLogger(__name__)


async def get_session(request: Request) -> AsyncSession:
    sm: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with sm() as session:
        async with session.begin():
            yield session


# scope="function": the dependency closes, and so the transaction commits, before
# the response is sent. With the default request scope the commit would land after
# the client already holds the new id.
SessionDep = Depends(get_session, scope="function")


def get_embedder(request: Request) -> Embedder | None:
    """The embedder the lifespan put on `app.state`. None when an app was built
    without one and its lifespan never ran (some in-process tests)."""
    return getattr(request.app.state, "embedder", None)


EmbedderDep = Depends(get_embedder)


def get_reviewer(request: Request) -> Reviewer | None:
    """The reviewer the lifespan put on `app.state`. None when an app was built
    without one and its lifespan never ran (some in-process tests)."""
    return getattr(request.app.state, "reviewer", None)


ReviewerDep = Depends(get_reviewer)


def _has_model(backend) -> bool:
    """True when an embedder or reviewer is present and has a real model."""
    return backend is not None and backend.model_name is not None


async def _review_memory(app: FastAPI, mid: int) -> Verdict | None:
    """Ask the reviewer about memory `mid` and store what it says.

    Reads the memory, its nearest neighbours and the tags the model may
    suggest in one session, closes it, calls the model in a thread (it may
    take many seconds, and a database connection should not sit idle that
    long), then writes the verdict in a second session. Returns the verdict,
    or None when the model gave none; then nothing is written and an earlier
    verdict, if any, stays. Raises `LookupError` when there is no such
    memory."""
    reviewer: Reviewer = app.state.reviewer
    sessionmaker = app.state.sessionmaker
    embedder = getattr(app.state, "embedder", None)
    async with sessionmaker() as session, session.begin():
        found = await repo.review_input(session, mid)
        if found is None:
            raise LookupError(f"Memory #{mid} not found")
        memory, neighbours = found
        tags = await repo.tags_for_review(session, embedder, memory["content"])
    verdict = await asyncio.to_thread(reviewer.review, memory, neighbours, tags)
    if verdict is None:
        return None
    async with sessionmaker() as session, session.begin():
        await repo.set_review(session, mid, verdict, reviewer.model_name)
    return verdict


async def _review_in_background(app: FastAPI, mid: int) -> None:
    """The review as a background task after an add. Whatever goes wrong is
    one log line; the memory is already stored and stays as it is."""
    try:
        await _review_memory(app, mid)
    except Exception as e:
        log.warning("review of memory #%d failed: %s: %s", mid, type(e).__name__, e)


async def _reindex_at_startup(app: FastAPI) -> None:
    """Fill in vectors for rows that have none, or one from another model, once
    at server start. A failure here is logged and must not stop the server: the
    rows can still be fixed later with `POST /admin/reindex`."""
    try:
        async with app.state.sessionmaker() as session, session.begin():
            n = await repo.reindex(session, app.state.embedder)
        log.info("reindex at startup: %d memories updated", n)
    except Exception:
        log.exception("reindex at startup failed; the server keeps running")


def create_app(
    sessionmaker: async_sessionmaker | None = None,
    token: str | None = None,
    embedder: Embedder | None = None,
    reviewer: Reviewer | None = None,
) -> FastAPI:
    """Build the app. In production (`sessionmaker` omitted) the lifespan builds a
    pooled engine from AGENT_MEMORY_DB and disposes it on shutdown; tests inject a
    sessionmaker bound to their own test engine. Token falls back to
    AGENT_MEMORY_API_TOKEN. The embedder lands on `app.state.embedder`; when
    omitted, the lifespan builds one from the environment with `make_embedder()`
    (a `NullEmbedder` when the model is off or not installed). The reviewer
    lands on `app.state.reviewer` the same way, through `make_reviewer()`
    (a `NullReviewer` unless AGENT_MEMORY_REVIEW=warn and a server is set)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = None
        if sessionmaker is None:
            engine = make_engine()
            app.state.sessionmaker = make_sessionmaker(engine)
        else:
            app.state.sessionmaker = sessionmaker
        # Built here, not at construction, so the model loads once at server
        # start and an app built for tests stays cheap.
        app.state.embedder = embedder if embedder is not None else make_embedder()
        app.state.reviewer = reviewer if reviewer is not None else make_reviewer()
        if app.state.embedder.model_name is not None:
            await _reindex_at_startup(app)
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
    if embedder is not None:
        app.state.embedder = embedder
    if reviewer is not None:
        app.state.reviewer = reviewer
    guard = [Depends(require_token)]

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/memories", response_model=AddResult, status_code=201, dependencies=guard)
    async def add_memory(body: MemoryIn, request: Request, background: BackgroundTasks,
                         session: AsyncSession = SessionDep,
                         embedder: Embedder | None = EmbedderDep,
                         reviewer: Reviewer | None = ReviewerDep,
                         force: bool = Query(default=False,
                                             description="Store even when a near-duplicate exists")):
        # A memory that already exists in this project is refused, not stored
        # twice. `force=true` skips the check; a server without a model never
        # refuses, since it has no vectors to compare.
        if not force:
            dup = await repo.find_duplicate(session, embedder, body.content, body.project)
            if dup is not None:
                existing_id, score = dup
                raise HTTPException(status_code=409, detail={
                    "reason": "duplicate", "existing_id": existing_id, "score": score})
        mid = await repo.add(session, body.content, body.agent or "unknown",
                             body.project, body.tags, body.type, embedder=embedder)
        # The model's review runs after the response is sent, so the writer
        # never waits for it. The session above commits before the response
        # goes out, so the task sees the new row in its own session.
        if _has_model(reviewer):
            background.add_task(_review_in_background, request.app, mid)
        # Stored either way; the warnings only tell the writer what the entry lacks.
        return {"id": mid, "warnings": warnings_for(body)}

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
        limit: int = Query(default=100, ge=0, description="0 = no limit"),
        offset: int = Query(default=0, ge=0),
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
        response: Response,
        session: AsyncSession = SessionDep,
        embedder: Embedder | None = EmbedderDep,
        q: str = "",
        mode: Literal["keyword", "semantic", "hybrid"] = "keyword",
        project: str | None = None,
        agent: str | None = None,
        since: str | None = None,
        tag: str | None = None,
        limit: int = Query(default=20, ge=0, description="0 = no limit"),
    ):
        filters = dict(project=project, agent=agent, since=since, tag=tag, limit=limit)
        if mode == "keyword":
            return await repo.search(session, q, **filters)
        # The other two modes need a real model. A NullEmbedder knows why it has none.
        has_model = embedder is not None and embedder.model_name is not None
        if mode == "hybrid":
            if has_model:
                return await repo.search_hybrid(session, embedder, q, **filters)
            # Hybrid without a model is just its keyword half. Serve that, and
            # say so in a header, so the caller can tell the results were not fused.
            response.headers["X-Search-Fallback"] = "keyword"
            return await repo.search(session, q, **filters)
        if not has_model:
            reason = getattr(embedder, "reason", "the embedding model is off or not installed")
            raise HTTPException(
                status_code=400,
                detail=f"semantic search is not available: {reason}. Set "
                       "AGENT_MEMORY_EMBED_MODEL and install the [embed] extra to enable it.",
            )
        return await repo.search_semantic(session, embedder, q, **filters)

    @app.get("/memories/{mid}", response_model=MemoryOut, dependencies=guard)
    async def get_memory(mid: int, session: AsyncSession = SessionDep):
        row = await repo.get(session, mid)
        if row is None:
            raise HTTPException(status_code=404, detail=f"Memory #{mid} not found")
        return row

    @app.patch("/memories/{mid}", response_model=UpdateResult, dependencies=guard)
    async def update_memory(mid: int, body: UpdateIn, session: AsyncSession = SessionDep,
                            embedder: Embedder | None = EmbedderDep):
        sent = body.model_fields_set
        changes = await repo.update(
            session, mid,
            content=body.content if "content" in sent else None,
            project=body.project if "project" in sent else None,
            mtype=body.type if "type" in sent else None,
            set_tags=body.set_tags if "set_tags" in sent else None,
            add_tags=body.add_tags if "add_tags" in sent else None,
            remove_tags=body.remove_tags if "remove_tags" in sent else None,
            embedder=embedder,
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

    @app.post("/admin/reindex", dependencies=guard)
    async def reindex(session: AsyncSession = SessionDep,
                      embedder: Embedder | None = EmbedderDep):
        """Give every row a vector from the current model: rows with none, and
        rows embedded by another model. Returns how many rows changed."""
        if embedder is None or embedder.model_name is None:
            reason = getattr(embedder, "reason", "the server has no embedding model")
            raise HTTPException(status_code=503, detail=f"Cannot reindex: {reason}")
        return {"updated": await repo.reindex(session, embedder)}

    @app.post("/admin/review/{mid}", response_model=ReviewOut, dependencies=guard)
    async def review_memory(mid: int, request: Request,
                            reviewer: Reviewer | None = ReviewerDep):
        """Run the review for one memory now, replacing any earlier verdict, and
        return the new one. 503 when the server has no review model, 404 when
        there is no such memory, 502 when the model gave no usable answer."""
        if not _has_model(reviewer):
            reason = getattr(reviewer, "reason", "the server has no review model")
            raise HTTPException(status_code=503, detail=f"Cannot review: {reason}")
        try:
            verdict = await _review_memory(request.app, mid)
        except LookupError as e:
            raise HTTPException(status_code=404, detail=str(e))
        if verdict is None:
            raise HTTPException(
                status_code=502,
                detail=f"The review model gave no verdict for memory #{mid}; see the server log.")
        return verdict.as_dict()

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
