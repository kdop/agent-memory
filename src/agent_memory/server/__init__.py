"""FastAPI service exposing the MemoryStore over HTTP (Ticket B).

Routes mirror the store contract 1:1 so the same cross-surface behavior suite
runs against the API via `ApiDriver`. Bearer-token auth guards every route
except `/health`. The client surface (CLI / urllib ApiStore) stays stdlib-only;
only this `[server]` extra pulls FastAPI + uvicorn.
"""

from .app import create_app

__all__ = ["create_app"]
