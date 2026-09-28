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
from contextlib import asynccontextmanager, suppress
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
from .review import review_mode as review_mode_from_env
from .review import review_poll as review_poll_from_env
from .schemas import (
    AddResult,
    AgentCount,
    DeleteResult,
    MemoryIn,
    MemoryOut,
    ProjectCount,
    ReviewEntry,
    ReviewOut,
    TagCount,
    TagDetachIn,
    TagMergeIn,
    TagPatch,
    UpdateIn,
    UpdateResult,
)

log = logging.getLogger(__name__)

# The `status` query parameter: one of review.STATUSES. Spelled out so FastAPI
# answers 422 for anything else; a test checks the two lists match.
ReviewStatus = Literal["unverified", "verified", "flagged"]

# What `GET /health` says about the review model, as `review_model`: `off`
# when the review or the poll is off, else what the poll's last check found.
REVIEW_MODEL_STATES = ("off", "reachable", "unreachable")


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


def get_review_mode(request: Request) -> str:
    """The review mode on `app.state`: `off`, `warn` or `enforce`. `warn`
    when nothing set it, so an app built for tests with a reviewer and no
    lifespan behaves as before."""
    return getattr(request.app.state, "review_mode", "warn")


ReviewModeDep = Depends(get_review_mode)


def _has_model(backend) -> bool:
    """True when an embedder or reviewer is present and has a real model."""
    return backend is not None and backend.model_name is not None


def _mode_for(reviewer: Reviewer | None, wanted: str | None) -> str:
    """The mode the server runs the review in. `off` without a model, whatever
    was asked. With one, `enforce` only when asked for; otherwise `warn`, so a
    reviewer handed to `create_app` is used even when the environment says
    off."""
    if not _has_model(reviewer):
        return "off"
    return "enforce" if wanted == "enforce" else "warn"


def _refusal(verdict: Verdict) -> dict:
    """The `detail` of the 422 an enforced review answers with. `explanation`
    is the model's reason; `rewrite` and `tags` carry the suggestion when the
    verdict is a rewrite (None and [] for a reject)."""
    return {"reason": "review", "verdict": verdict.verdict, "rule": verdict.rule,
            "explanation": verdict.reason, "rewrite": verdict.rewrite,
            "tags": list(verdict.tags), "duplicate_of": verdict.duplicate_of}


async def _review_before_store(session: AsyncSession, reviewer: Reviewer,
                               embedder: Embedder | None, body: MemoryIn) -> Verdict | None:
    """Ask the reviewer about an entry that is not stored yet. The memory it
    sees has no id; its neighbours and the tags on offer are found the way
    the stored path finds them. Returns the verdict, or None when the model
    gave none or failed: then the caller stores the entry as warn mode would,
    since an absent model must never block a write. The request's session
    stays open while the model thinks; enforce mode pays that price so the
    write can wait for the answer."""
    memory = {"id": None, "project": body.project, "type": body.type,
              "tags": [t.name for t in body.tags], "content": body.content}
    neighbours = await repo.neighbours_for(session, embedder, body.content, body.project)
    tags = await repo.tags_for_review(session, embedder, body.content)
    try:
        verdict = await asyncio.to_thread(reviewer.review, memory, neighbours, tags)
    except Exception as e:
        log.warning("review of a new memory failed: %s: %s", type(e).__name__, e)
        return None
    if verdict is None:
        log.warning("review of a new memory gave no verdict; storing it without one")
    return verdict


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


async def _review_in_order(app: FastAPI, ids: list[int]) -> None:
    """The catch-up as one background task: review `ids` one after another,
    in the order given. Each verdict is stored (and the memory's status set)
    before the next memory is read, so a memory verified here is already
    part of the reference set for the ones after it. A review that fails is
    one log line, and the rest still run."""
    for mid in ids:
        await _review_in_background(app, mid)


def _catch_up_lock(app: FastAPI) -> asyncio.Lock:
    """The one lock the poll and `POST /admin/review` share, so one catch-up
    runs at a time. Made on first use, so an app whose lifespan never ran
    has one too."""
    lock = getattr(app.state, "catch_up_lock", None)
    if lock is None:
        lock = app.state.catch_up_lock = asyncio.Lock()
    return lock


async def _begin_catch_up(app: FastAPI, session: AsyncSession,
                          limit: int | None = None) -> list[int] | None:
    """Start a catch-up: take the lock and pick the unverified memories,
    oldest first, up to `limit` (None or 0 means all). Returns their ids
    with the lock held; the caller hands them to `_run_catch_up`, which
    releases it. Returns an empty list, with the lock released, when there
    is nothing to review, and None, without touching anything, when a
    catch-up is already running. The lock is never waited for: a caller
    that finds it taken does nothing."""
    lock = _catch_up_lock(app)
    if lock.locked():
        return None
    # Free, and nothing else ran between the check and here, so this
    # returns at once.
    await lock.acquire()
    try:
        ids = await repo.unverified_ids(session, limit=limit)
    except BaseException:
        lock.release()
        raise
    if not ids:
        lock.release()
    return ids


async def _run_catch_up(app: FastAPI, ids: list[int]) -> None:
    """One catch-up, under the lock `_begin_catch_up` took: review `ids` in
    order, then release the lock, whatever happened. One log line at the
    start, with the count, and one at the end."""
    log.info("catch-up started: %d unverified memories to review", len(ids))
    done = False
    try:
        await _review_in_order(app, ids)
        done = True
    finally:
        _catch_up_lock(app).release()
        log.info("catch-up %s", "finished" if done else "stopped")


async def _review_tick(app: FastAPI) -> None:
    """One check of the poll: ask the reviewer whether the model answers,
    note the answer on `app.state.review_model`, and when it does, run the
    catch-up for the unverified memories, unless one is already running. A
    reviewer whose check raises counts as unreachable, with one log line."""
    reviewer: Reviewer = app.state.reviewer
    try:
        up = await asyncio.to_thread(reviewer.reachable)
    except Exception as e:
        log.warning("review poll: the check failed: %s: %s", type(e).__name__, e)
        up = False
    state = "reachable" if up else "unreachable"
    if state != getattr(app.state, "review_model", None):
        log.info("review model %s: %s", state, reviewer.model_name)
    app.state.review_model = state
    if not up:
        return
    async with app.state.sessionmaker() as session, session.begin():
        ids = await _begin_catch_up(app, session)
    if ids:
        await _run_catch_up(app, ids)


async def _review_poll(app: FastAPI, interval: float) -> None:
    """The loop the lifespan runs while the review is on: one check now,
    then one every `interval` seconds, until the task is cancelled at
    shutdown. Whatever a check raises is one log line, and the loop goes
    on. This is how memories written while the model was off get their
    verdict without anyone running `memory review --catch-up`."""
    log.info("review poll started: checking the model every %g s", interval)
    while True:
        try:
            await _review_tick(app)
        except Exception as e:
            log.warning("review poll: %s: %s", type(e).__name__, e)
        await asyncio.sleep(interval)


async def _reindex_at_startup(app: FastAPI) -> None:
    """Fill in vectors for rows that have none, or one from another model, once
    at server start. A failure here is logged and must not stop the server: the
    rows can still be fixed later with `POST /admin/reindex`."""
    try:
        async with app.state.sessionmaker() as session, session.begin():
            done = await repo.reindex(session, app.state.embedder)
        log.info("reindex at startup: %d memories and %d tags updated",
                 done["updated"], done["tags"])
    except Exception:
        log.exception("reindex at startup failed; the server keeps running")


def create_app(
    sessionmaker: async_sessionmaker | None = None,
    token: str | None = None,
    embedder: Embedder | None = None,
    reviewer: Reviewer | None = None,
    review_mode: str | None = None,
    review_poll: float | None = None,
) -> FastAPI:
    """Build the app. In production (`sessionmaker` omitted) the lifespan builds a
    pooled engine from AGENT_MEMORY_DB and disposes it on shutdown; tests inject a
    sessionmaker bound to their own test engine. Token falls back to
    AGENT_MEMORY_API_TOKEN. The embedder lands on `app.state.embedder`; when
    omitted, the lifespan builds one from the environment with `make_embedder()`
    (a `NullEmbedder` when the model is off or not installed). The reviewer
    lands on `app.state.reviewer` the same way, through `make_reviewer()`
    (a `NullReviewer` unless AGENT_MEMORY_REVIEW is warn or enforce and a
    server is set). `app.state.review_mode` says how the review runs: `off`
    without a model; else `enforce` when `review_mode` (or, when it is
    omitted, AGENT_MEMORY_REVIEW) says so, and `warn` otherwise. When the
    review is on, the lifespan also runs the poll (`_review_poll`): every
    `review_poll` seconds (or, when it is omitted, AGENT_MEMORY_REVIEW_POLL,
    default 300; 0 or less means no poll) it checks whether the model
    answers and, when it does, reviews the unverified memories.
    `app.state.review_model` holds what the last check found (`off`,
    `reachable` or `unreachable`; `GET /health` reports it)."""

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
        wanted = review_mode if review_mode is not None else review_mode_from_env()
        app.state.review_mode = _mode_for(app.state.reviewer, wanted)
        if app.state.embedder.model_name is not None:
            await _reindex_at_startup(app)
        app.state.review_model = "off"
        interval = review_poll if review_poll is not None else review_poll_from_env()
        poll = None
        if app.state.review_mode != "off" and interval > 0:
            # Nothing is known until the first check answers.
            app.state.review_model = "unreachable"
            poll = asyncio.create_task(_review_poll(app, interval), name="review-poll")
        try:
            yield
        finally:
            if poll is not None:
                poll.cancel()
                with suppress(asyncio.CancelledError):
                    await poll
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
        app.state.review_mode = _mode_for(reviewer, review_mode)
    guard = [Depends(require_token)]

    @app.get("/health")
    async def health(request: Request):
        """Open to all. `review_model` is what the poll's last check found:
        `reachable`, `unreachable`, or `off` when the review or the poll
        is off (and for an app whose lifespan never ran)."""
        return {"status": "ok",
                "review_model": getattr(request.app.state, "review_model", "off")}

    @app.post("/memories", response_model=AddResult, status_code=201, dependencies=guard)
    async def add_memory(body: MemoryIn, request: Request, background: BackgroundTasks,
                         session: AsyncSession = SessionDep,
                         embedder: Embedder | None = EmbedderDep,
                         reviewer: Reviewer | None = ReviewerDep,
                         mode: str = ReviewModeDep,
                         force: bool = Query(
                             default=False,
                             description="Store even when a near-duplicate exists, or "
                                         "when the review model would refuse the entry")):
        # A memory that already exists in this project is refused, not stored
        # twice. Only verified memories (ones the model approved) count as
        # reference. `force=true` skips the check; a server without a model
        # never refuses, since it has no vectors to compare.
        if not force:
            dup = await repo.find_duplicate(session, embedder, body.content, body.project)
            if dup is not None:
                existing_id, score = dup
                raise HTTPException(status_code=409, detail={
                    "reason": "duplicate", "existing_id": existing_id, "score": score})
        # In enforce mode the model reads the entry first, and a reject or
        # rewrite refuses it with the verdict and the suggestion. `force=true`
        # skips this too. A model that gives no answer refuses nothing.
        verdict = None
        enforced = _has_model(reviewer) and mode == "enforce" and not force
        if enforced:
            verdict = await _review_before_store(session, reviewer, embedder, body)
            if verdict is not None and verdict.verdict != "approve":
                raise HTTPException(status_code=422, detail=_refusal(verdict))
        # Stored as `unverified`; the verdict, when one lands, sets the status.
        mid = await repo.add(session, body.content, body.agent or "unknown",
                             body.project, body.tags, body.type, embedder=embedder)
        if verdict is not None:
            # The approve from just now is the review: no second model call,
            # and the memory is `verified` from the start. When the model
            # said the entry supersedes an older memory, the link is set
            # here too; the request body can never set it.
            await repo.set_review(session, mid, verdict, reviewer.model_name)
        elif _has_model(reviewer) and not enforced:
            # Warn mode, or a forced write: the review runs after the response
            # is sent, so the writer never waits for it. The session above
            # commits before the response goes out, so the task sees the new
            # row in its own session. The memory stays `unverified` until
            # the verdict is stored.
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
        status: ReviewStatus | None = None,
        current: bool = Query(
            default=False,
            description="Hide the memories a newer one supersedes"),
        order: str = "date_desc",
        limit: int = Query(default=100, ge=0, description="0 = no limit"),
        offset: int = Query(default=0, ge=0),
    ):
        items, total = await repo.list_memories(
            session, q=q, tags=tag, project=project, agent=agent, mtype=type,
            status=status, current=current, since_days=since_days, since=since, until=until,
            order=order, limit=limit, offset=offset)
        response.headers["X-Total-Count"] = str(total)
        return items

    # `/memories/bulk`, `/memories/search` and `/memories/flagged` are declared
    # before `/memories/{mid}` so the literal paths win the match.
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
        current: bool = Query(
            default=False,
            description="Hide the memories a newer one supersedes"),
        limit: int = Query(default=20, ge=0, description="0 = no limit"),
    ):
        filters = dict(project=project, agent=agent, since=since, tag=tag, current=current,
                       limit=limit)
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

    @app.get("/memories/flagged", response_model=list[MemoryOut], dependencies=guard)
    async def flagged_memories(
        response: Response,
        session: AsyncSession = SessionDep,
        project: str | None = None,
        verdict: Literal["reject", "rewrite"] | None = None,
        status: ReviewStatus | None = None,
        limit: int = Query(default=100, ge=0, description="0 = no limit"),
    ):
        """The memories the review flagged (verdict reject or rewrite, or only
        `verdict`), newest review first, each with its review. With `status`,
        the memories with that review status instead, so `status=unverified`
        lists what the model has not checked yet. `X-Total-Count` carries the
        match count ignoring the limit, as on `GET /memories`."""
        filters = dict(project=project, verdict=verdict, status=status)
        items = await repo.flagged(session, limit=limit, **filters)
        response.headers["X-Total-Count"] = str(await repo.count_flagged(session, **filters))
        return items

    @app.get("/memories/{mid}", response_model=MemoryOut, dependencies=guard)
    async def get_memory(mid: int, session: AsyncSession = SessionDep):
        row = await repo.get(session, mid)
        if row is None:
            raise HTTPException(status_code=404, detail=f"Memory #{mid} not found")
        return row

    @app.get("/memories/{mid}/reviews", response_model=list[ReviewEntry], dependencies=guard)
    async def list_reviews(mid: int, session: AsyncSession = SessionDep):
        """The memory's review history: every verdict the model gave on it,
        newest first. Every read of the memory shows only the newest as
        `review`; this is where the earlier ones are. Empty for a memory
        that has not been reviewed; 404 when there is no such memory."""
        rows = await repo.reviews(session, mid)
        if rows is None:
            raise HTTPException(status_code=404, detail=f"Memory #{mid} not found")
        return rows

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
    async def patch_tag(name: str, body: TagPatch, session: AsyncSession = SessionDep,
                        embedder: Embedder | None = EmbedderDep):
        sent = body.model_fields_set
        result = await repo.patch_tag(
            session, name,
            new_name=body.name if "name" in sent else None,
            description=body.description if "description" in sent else None,
            embedder=embedder)
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
    async def merge_tags(body: TagMergeIn, session: AsyncSession = SessionDep,
                         embedder: Embedder | None = EmbedderDep):
        return await repo.merge_tags(session, body.sources, body.target, body.description,
                                     embedder=embedder)

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
        """Give every memory and every tag a vector from the current model:
        rows with none, and rows embedded by another model. Returns how many
        changed, as `{"updated": memories, "tags": tags}`. The model runs in
        a thread, batch by batch, so other requests are answered meanwhile."""
        if embedder is None or embedder.model_name is None:
            reason = getattr(embedder, "reason", "the server has no embedding model")
            raise HTTPException(status_code=503, detail=f"Cannot reindex: {reason}")
        return await repo.reindex(session, embedder)

    @app.post("/admin/review", dependencies=guard)
    async def review_catch_up(request: Request, background: BackgroundTasks,
                              session: AsyncSession = SessionDep,
                              reviewer: Reviewer | None = ReviewerDep,
                              limit: int = Query(default=50, ge=0, description="0 = no limit")):
        """The catch-up: review the unverified memories, oldest first, up to
        `limit`. This is how memories written while the model was off get
        their verdict later; the poll runs the same catch-up on its own when
        it finds the model back. The reviews run one after another, in that
        order, in one background task after this response, so each verdict
        is stored before the next memory is compared; one that fails is a
        log line, as after an add, and the rest still run. Returns how many
        were scheduled; `{"scheduled": 0, "running": true}` when a catch-up
        (this route's or the poll's) is already running, since only one runs
        at a time. 503 when the server has no review model."""
        if not _has_model(reviewer):
            reason = getattr(reviewer, "reason", "the server has no review model")
            raise HTTPException(status_code=503, detail=f"Cannot review: {reason}")
        ids = await _begin_catch_up(request.app, session, limit=limit)
        if ids is None:
            return {"scheduled": 0, "running": True}
        if ids:
            background.add_task(_run_catch_up, request.app, ids)
        return {"scheduled": len(ids)}

    @app.post("/admin/review/{mid}", response_model=ReviewOut, dependencies=guard)
    async def review_memory(mid: int, request: Request,
                            reviewer: Reviewer | None = ReviewerDep):
        """Run the review for one memory now and return the new verdict. It is
        stored as one more row of the memory's history, next to the earlier
        ones, and every read shows it from now on. 503 when the server has no
        review model, 404 when there is no such memory, 502 when the model
        gave no usable answer (then the earlier verdict still shows)."""
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
