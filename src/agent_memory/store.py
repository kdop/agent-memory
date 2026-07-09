"""Storage layer — the backend seam.

Defines the backend-agnostic `MemoryStore` interface, the remote `ApiStore` (talks
to the FastAPI service over HTTP), and `get_store()`, which resolves the active
backend. The concrete engines live in their own modules — `SqliteStore` in
`sqlite_store.py`, `PostgresStore` in `pg_store.py` — and are imported lazily by
`get_store()`. All methods return plain data (ids, dicts, lists) in the same shapes
regardless of backend; presentation lives in the CLI.

`SqliteStore` is re-exported from this module (bottom of file) so existing
`from agent_memory.store import SqliteStore` imports keep working.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod

from .config import load_config, resolve_db_path


class MemoryStore(ABC):
    """Backend-agnostic memory operations. Methods return plain data (ids, dicts,
    lists); all presentation lives in the CLI layer. SqliteStore implements this
    today; a remote ApiStore will implement the same contract later (#6)."""

    @abstractmethod
    def initialize(self): ...
    @abstractmethod
    def add(self, content, agent, project, tags, mtype): ...
    @abstractmethod
    def query(self, *, today=False, yesterday=False, since=None, until=None,
              project=None, agent=None, tag=None, mtype=None, limit=None): ...
    @abstractmethod
    def search(self, text, *, project=None, agent=None, since=None, tag=None, limit=None): ...
    @abstractmethod
    def get(self, mid): ...
    @abstractmethod
    def update(self, mid, *, new_content=None, project=None, mtype=None,
               set_tags=None, add_tags=None, remove_tags=None): ...
    @abstractmethod
    def get_many(self, ids): ...
    @abstractmethod
    def delete(self, ids): ...
    @abstractmethod
    def list_tags(self): ...
    @abstractmethod
    def list_projects(self): ...
    @abstractmethod
    def stats(self): ...


class ApiStore(MemoryStore):
    """MemoryStore backed by the FastAPI service over HTTP (stdlib urllib only, so
    the client surface stays dependency-free). Translates each operation to a route
    and parses the JSON back into the exact shapes SqliteStore returns, so the CLI
    presentation layer is identical whether it talks to SQLite or the service."""

    # Remote: there is no local DB file to guard/create (see cli.ensure_db_or_confirm).
    db_path = None

    def __init__(self, base_url, token=None, timeout=30):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    # ---- HTTP plumbing ---------------------------------------------------
    def _call(self, method, path, *, params=None, body=None):
        """Returns (status_code, parsed_json|None). Raises on transport errors and
        unexpected HTTP statuses; 404 is returned to the caller to handle."""
        url = self.base_url + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean, doseq=True)
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return 404, None
            detail = e.read().decode(errors="replace")
            raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {detail}") from e

    # ---- operations ------------------------------------------------------
    def initialize(self):
        return []  # the server owns schema creation/migration.

    def add(self, content, agent, project, tags, mtype):
        _, data = self._call("POST", "/memories", body={
            "content": content, "agent": agent, "project": project,
            "tags": list(tags), "type": mtype,
        })
        return data["id"]

    def query(self, *, today=False, yesterday=False, since=None, until=None,
              project=None, agent=None, tag=None, mtype=None, limit=None):
        params = {
            "today": "true" if today else None,
            "yesterday": "true" if yesterday else None,
            "since": since, "until": until, "project": project,
            "agent": agent, "tag": tag, "type": mtype, "limit": limit,
        }
        _, data = self._call("GET", "/memories", params=params)
        return data or []

    def search(self, text, *, project=None, agent=None, since=None, tag=None, limit=None):
        params = {"q": text, "project": project, "agent": agent,
                  "since": since, "tag": tag, "limit": limit}
        _, data = self._call("GET", "/memories/search", params=params)
        return data or []

    def get(self, mid):
        status, data = self._call("GET", f"/memories/{mid}")
        return None if status == 404 else data

    def update(self, mid, *, new_content=None, project=None, mtype=None,
               set_tags=None, add_tags=None, remove_tags=None):
        body = {}
        if new_content is not None:
            body["content"] = new_content
        if project is not None:
            body["project"] = project
        if mtype is not None:
            body["type"] = mtype
        if set_tags is not None:
            body["set_tags"] = set_tags
        if add_tags is not None:
            body["add_tags"] = add_tags
        if remove_tags is not None:
            body["remove_tags"] = remove_tags
        status, data = self._call("PATCH", f"/memories/{mid}", body=body)
        return None if status == 404 else data["changes"]

    def get_many(self, ids):
        # No bulk endpoint; fetch each (delete previews a handful of ids).
        out = []
        for i in ids:
            row = self.get(i)
            if row is not None:
                out.append(row)
        return out

    def delete(self, ids):
        self._call("DELETE", "/memories", params={"ids": list(ids)})

    def list_tags(self):
        _, data = self._call("GET", "/tags")
        return [(t["name"], t["count"]) for t in (data or [])]

    def list_projects(self):
        _, data = self._call("GET", "/projects")
        return [(p["project"], p["count"]) for p in (data or [])]

    def stats(self):
        _, data = self._call("GET", "/stats")
        return data


def _is_pg_dsn(value):
    return isinstance(value, str) and value.startswith(("postgres://", "postgresql://"))


def get_store():
    """Resolve the active MemoryStore backend.

    Precedence: AGENT_MEMORY_API (env, else config `api_url`) selects the remote
    ApiStore; otherwise the DB target (AGENT_MEMORY_DB → config `db_path` → XDG
    default) — a `postgresql://` DSN selects PostgresStore, anything else is a
    SQLite file path. The API token comes from AGENT_MEMORY_API_TOKEN, else config
    `api_token`."""
    cfg = load_config()
    api = os.environ.get("AGENT_MEMORY_API") or cfg.get("api_url")
    if api:
        token = os.environ.get("AGENT_MEMORY_API_TOKEN") or cfg.get("api_token")
        return ApiStore(api, token)
    target = os.environ.get("AGENT_MEMORY_DB") or cfg.get("db_path")
    if _is_pg_dsn(target):
        from .pg_store import PostgresStore  # lazy: only the [postgres] extra needs psycopg
        return PostgresStore(target)
    from .sqlite_store import SqliteStore  # lazy: avoids an import cycle with sqlite_store
    return SqliteStore(resolve_db_path())


def __getattr__(name):
    """Back-compat re-export. `SqliteStore` now lives in `sqlite_store.py`, but external
    code (and the tests) still do `from agent_memory.store import SqliteStore`. Resolve
    it lazily (PEP 562) instead of importing at module top/bottom, so importing either
    module first never triggers the store <-> sqlite_store import cycle."""
    if name == "SqliteStore":
        from .sqlite_store import SqliteStore
        return SqliteStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
