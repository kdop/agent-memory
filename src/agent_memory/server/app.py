"""FastAPI app factory. Routes mirror MemoryStore 1:1; all data work delegates to
the store, so the API stays a thin transport over the exact same logic the CLI uses.
"""

import os
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request

from .. import __version__
from ..config import get_agent_name
from ..store import MemoryStore, get_store
from .auth import require_token
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


def create_app(store: Optional[MemoryStore] = None, token: Optional[str] = None) -> FastAPI:
    """Build the app over a ready store. If `store` is None it is resolved via
    `get_store()` (honoring AGENT_MEMORY_API/DB/config); if `token` is None it
    falls back to AGENT_MEMORY_API_TOKEN. The store schema is ensured up front."""
    if store is None:
        store = get_store()
    store.initialize()

    app = FastAPI(title="agent-memory", version=__version__)
    app.state.store = store
    app.state.token = token if token is not None else os.environ.get("AGENT_MEMORY_API_TOKEN")

    guard = [Depends(require_token)]

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/memories", response_model=AddResult, status_code=201, dependencies=guard)
    def add_memory(body: MemoryIn, request: Request):
        s = request.app.state.store
        agent = body.agent or get_agent_name()
        tags = [t.model_dump() for t in body.tags]
        mid = s.add(body.content, agent, body.project, tags, body.type)
        return {"id": mid}

    @app.get("/memories", response_model=List[MemoryOut], dependencies=guard)
    def query_memories(
        request: Request,
        since_days: Optional[int] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        project: Optional[str] = None,
        agent: Optional[str] = None,
        tag: Optional[str] = None,
        type: Optional[str] = None,
        limit: Optional[int] = None,
    ):
        return request.app.state.store.query(
            since_days=since_days, since=since, until=until,
            project=project, agent=agent, tag=tag, mtype=type, limit=limit,
        )

    # Declared before /memories/{mid} so the literal path wins the match.
    @app.get("/memories/search", response_model=List[MemoryOut], dependencies=guard)
    def search_memories(
        request: Request,
        q: str,
        project: Optional[str] = None,
        agent: Optional[str] = None,
        since: Optional[str] = None,
        tag: Optional[str] = None,
        limit: int = 20,
    ):
        return request.app.state.store.search(
            q, project=project, agent=agent, since=since, tag=tag, limit=limit,
        )

    @app.get("/memories/{mid}", response_model=MemoryOut, dependencies=guard)
    def get_memory(mid: int, request: Request):
        row = request.app.state.store.get(mid)
        if row is None:
            raise HTTPException(status_code=404, detail=f"Memory #{mid} not found")
        return row

    @app.patch("/memories/{mid}", response_model=UpdateResult, dependencies=guard)
    def update_memory(mid: int, body: UpdateIn, request: Request):
        fields = body.model_dump(exclude_unset=True)
        changes = request.app.state.store.update(
            mid,
            new_content=fields.get("content"),
            project=fields.get("project"),
            mtype=fields.get("type"),
            set_tags=fields.get("set_tags"),
            add_tags=fields.get("add_tags"),
            remove_tags=fields.get("remove_tags"),
        )
        if changes is None:
            raise HTTPException(status_code=404, detail=f"Memory #{mid} not found")
        return {"changes": changes}

    @app.delete("/memories", response_model=DeleteResult, dependencies=guard)
    def delete_memories(request: Request, ids: List[int] = Query(...)):
        s = request.app.state.store
        found = {row["id"] for row in s.get_many(ids)}
        to_delete = [i for i in ids if i in found]
        if to_delete:
            s.delete(to_delete)
        return {"deleted": len(to_delete), "missing": [i for i in ids if i not in found]}

    @app.get("/tags", response_model=List[TagCount], dependencies=guard)
    def list_tags(request: Request):
        return [{"name": r[0], "count": r[1], "description": r[2]} for r in request.app.state.store.list_tags()]

    @app.get("/projects", response_model=List[ProjectCount], dependencies=guard)
    def list_projects(request: Request):
        return [{"project": r[0], "count": r[1]} for r in request.app.state.store.list_projects()]

    @app.get("/stats", dependencies=guard)
    def stats(request: Request):
        return request.app.state.store.stats()

    return app
