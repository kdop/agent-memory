"""Cross-surface test drivers.

One `MemoryDriver` contract, implemented per surface, so the same behavioral
tests run against every surface and prove they don't drift (PLAN.md → Testing).

- `CliDriver`  — subprocess against `memory-cli`       (A0).
- `ApiDriver`  — httpx TestClient over the FastAPI app  (Ticket B).
- `McpDriver`  — MCP client                            (Ticket D).

Drivers return plain structured data (`Memory`, lists, dicts) — never raw CLI
chrome. Surface-specific output/exit-code/format assertions live in
`test_cli_surface.py`, not here. Every driver runs against a scratch
`AGENT_MEMORY_DB` (rule #1: the live DB is never touched).
"""

# Keep the PEP 604 (`str | None`) annotations below evaluable as strings so the
# harness imports on every supported runtime, not just 3.10+.
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

MEMORY_CLI = Path(__file__).resolve().parent.parent / "memory-cli"

# Lines a CLI block can start with that are NOT content.
_SEP = re.compile(r"^[─━]{10,}$")


@dataclass
class Memory:
    id: int
    agent: str | None = None
    project: str | None = None
    type: str | None = None
    tags: list = field(default_factory=list)
    content: str = ""
    snippet: str | None = None


class CliDriver:
    """Drives the CLI as a subprocess and parses its output back into data."""

    name = "cli"

    def __init__(self, db_path, agent="tester", extra_env=None):
        self.db_path = Path(db_path)
        self.agent = agent
        self.extra_env = extra_env or {}

    # ---- plumbing --------------------------------------------------------
    def _env(self):
        env = {**os.environ, "AGENT_NAME": self.agent}
        env["AGENT_MEMORY_DB"] = str(self.db_path)
        env.update(self.extra_env)
        return env

    def raw(self, *args, stdin=None):
        """Run the CLI verbatim; returns the CompletedProcess (for surface tests)."""
        return subprocess.run(
            [sys.executable, str(MEMORY_CLI), *args],
            env=self._env(), input=stdin, capture_output=True, text=True,
        )

    def initialize(self):
        # Prime the scratch DB so the one-time "Initialized…" banner doesn't bleed
        # into later assertions. --yes auto-creates (no TTY in a subprocess).
        self.raw("--yes", "stats")

    # ---- semantic operations --------------------------------------------
    def add(self, content, *, project=None, tags=None, type=None, agent=None):
        args = ["add", content]
        if project:
            args += ["--project", project]
        if tags:
            args += ["--tags", tags if isinstance(tags, str) else ",".join(tags)]
        if type:
            args += ["--type", type]
        if agent:
            args += ["--agent", agent]
        m = re.search(r"Memory #(\d+) added", self.raw(*args).stdout)
        return int(m.group(1)) if m else None

    def query(self, **filters):
        args = ["query"]
        for flag in ("today", "yesterday"):
            if filters.get(flag):
                args.append(f"--{flag}")
        for flag in ("since", "until", "project", "agent", "tag", "type"):
            if filters.get(flag):
                args += [f"--{flag}", str(filters[flag])]
        if filters.get("limit") is not None:
            args += ["--limit", str(filters["limit"])]
        return self._parse_memories(self.raw(*args).stdout)

    def search(self, text, **filters):
        args = ["search", text]
        for flag in ("project", "agent", "since", "tag"):
            if filters.get(flag):
                args += [f"--{flag}", str(filters[flag])]
        if filters.get("limit") is not None:
            args += ["--limit", str(filters["limit"])]
        out = self.raw(*args).stdout
        if "No memories found for" in out:
            return []
        return self._parse_memories(out, snippet=True)

    def get(self, mid):
        proc = self.raw("show", str(mid))
        if proc.returncode != 0 or "not found" in proc.stdout:
            return None
        mems = self._parse_memories(proc.stdout)
        return mems[0] if mems else None

    def update(self, mid, *, content=None, project=None, type=None,
               set_tags=None, add_tags=None, remove_tags=None, stdin=None):
        """Returns 'updated' | 'nochange' | None (not found)."""
        args = ["update", str(mid)]
        if content is not None:
            args += ["--content", content]
        if project is not None:
            args += ["--project", project]
        if type is not None:
            args += ["--type", type]
        if set_tags is not None:
            args += ["--set-tags", set_tags]
        if add_tags:
            args += ["--add-tags", add_tags]
        if remove_tags:
            args += ["--remove-tags", remove_tags]
        proc = self.raw(*args, stdin=stdin)
        if proc.returncode != 0 or "not found" in proc.stdout:
            return None
        return "updated" if "updated" in proc.stdout else "nochange"

    def delete(self, *ids):
        """Actually deletes (the CLI dry-run is a surface concern). Returns count removed."""
        proc = self.raw("delete", *[str(i) for i in ids], "--yes")
        m = re.search(r"Deleted (\d+)", proc.stdout)
        return int(m.group(1)) if m else 0

    def tags(self):
        out = self.raw("tags").stdout
        if "No tags found" in out:
            return []
        res = []
        for line in out.splitlines():
            m = re.match(r"\s+(\S+)\s+\((\d+)\)\s*$", line)
            if m:
                res.append((m.group(1), int(m.group(2))))
        return res

    def projects(self):
        out = self.raw("projects").stdout
        if "No projects found" in out:
            return []
        res = []
        for line in out.splitlines():
            m = re.match(r"\s+(\S+)\s+\((\d+) memories\)\s*$", line)
            if m:
                res.append((m.group(1), int(m.group(2))))
        return res

    def stats(self):
        out = self.raw("stats").stdout
        d = {}
        for key, label in (("total", "Total memories"), ("agents", "Agents"),
                           ("projects", "Projects"), ("tags", "Tags"),
                           ("today", "Today"), ("week", "Last 7 days")):
            m = re.search(rf"{re.escape(label)}:\s+(\d+)", out)
            if m:
                d[key] = int(m.group(1))
        return d

    # ---- output parsing --------------------------------------------------
    def _parse_memories(self, text, snippet=False):
        if "No memories found" in text:
            return []
        parts = re.split(r"━+ #(\d+)[^\n]*\n", text)
        out = []
        it = iter(parts[1:])  # parts[0] is the preamble before the first block
        for mid, body in zip(it, it):
            out.append(self._parse_block(int(mid), body, snippet=snippet))
        return out

    def _parse_block(self, mid, body, snippet=False):
        agent = project = type_ = None
        tags = []
        collected = []
        for line in body.split("\n"):
            if line.startswith("🕒"):
                continue
            if line.startswith("👤"):
                meta = line[len("👤"):].strip()
                tm = re.search(r"\[([^\]]+)\]\s*$", meta)
                if tm:
                    type_ = tm.group(1)
                    meta = meta[:tm.start()].strip()
                if " @ " in meta:
                    agent, project = (p.strip() for p in meta.split(" @ ", 1))
                else:
                    agent = meta.strip()
                continue
            if line.lstrip().startswith("🏷"):
                tagstr = re.sub(r"^[^\w]+", "", line.strip())
                tags = [t.strip() for t in tagstr.split(",") if t.strip()]
                continue
            if _SEP.match(line.strip()) or line.startswith("Found "):
                break
            collected.append(line)
        text_body = "\n".join(collected).strip()
        return Memory(
            id=mid, agent=agent, project=project, type=type_, tags=tags,
            content="" if snippet else text_body,
            snippet=text_body if snippet else None,
        )


class ApiDriver:
    """Drives the FastAPI service in-process via Starlette's httpx TestClient.

    Same `MemoryDriver` contract as `CliDriver`, so the cross-surface suite runs
    against the HTTP routes unchanged. A fixed bearer token is sent on every
    request; the app is built over a `SqliteStore` on the scratch DB (rule #1).
    Surface-only concerns (401, /health) live in `test_api_surface.py`.
    """

    name = "api"
    TOKEN = "test-token"

    def __init__(self, db_path, agent="tester", extra_env=None):
        # Imported lazily so a cli-only test run never needs FastAPI installed.
        from fastapi.testclient import TestClient

        from agent_memory.server import create_app
        from agent_memory.store import SqliteStore

        self.db_path = Path(db_path)
        self.agent = agent
        self._client = TestClient(create_app(store=SqliteStore(self.db_path), token=self.TOKEN))
        self._headers = {"Authorization": f"Bearer {self.TOKEN}"}

    def initialize(self):
        # create_app() already ran store.initialize(); nothing to prime.
        pass

    # ---- plumbing --------------------------------------------------------
    def _get(self, path, **params):
        clean = {k: v for k, v in params.items() if v is not None}
        return self._client.get(path, params=clean, headers=self._headers)

    @staticmethod
    def _to_memory(d, snippet=False):
        return Memory(
            id=d["id"], agent=d.get("agent"), project=d.get("project"),
            type=d.get("type"), tags=d.get("tags") or [],
            content="" if snippet else (d.get("content") or ""),
            snippet=d.get("snippet") if snippet else None,
        )

    # ---- semantic operations --------------------------------------------
    def add(self, content, *, project=None, tags=None, type=None, agent=None):
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        body = {
            "content": content, "project": project, "type": type,
            "agent": agent or self.agent, "tags": tags or [],
        }
        resp = self._client.post("/memories", json=body, headers=self._headers)
        return resp.json()["id"] if resp.status_code == 201 else None

    def query(self, **filters):
        resp = self._get(
            "/memories",
            today=filters.get("today"), yesterday=filters.get("yesterday"),
            since=filters.get("since"), until=filters.get("until"),
            project=filters.get("project"), agent=filters.get("agent"),
            tag=filters.get("tag"), type=filters.get("type"),
            limit=filters.get("limit"),
        )
        return [self._to_memory(d) for d in resp.json()]

    def search(self, text, **filters):
        resp = self._get(
            "/memories/search", q=text,
            project=filters.get("project"), agent=filters.get("agent"),
            since=filters.get("since"), tag=filters.get("tag"),
            limit=filters.get("limit"),
        )
        return [self._to_memory(d, snippet=True) for d in resp.json()]

    def get(self, mid):
        resp = self._get(f"/memories/{mid}")
        if resp.status_code == 404:
            return None
        return self._to_memory(resp.json())

    def update(self, mid, *, content=None, project=None, type=None,
               set_tags=None, add_tags=None, remove_tags=None, stdin=None):
        """Returns 'updated' | 'nochange' | None (not found)."""
        body = {}
        if content is not None:
            body["content"] = content
        if project is not None:
            body["project"] = project
        if type is not None:
            body["type"] = type
        if set_tags is not None:
            body["set_tags"] = set_tags
        if add_tags:
            body["add_tags"] = add_tags
        if remove_tags:
            body["remove_tags"] = remove_tags
        resp = self._client.patch(f"/memories/{mid}", json=body, headers=self._headers)
        if resp.status_code == 404:
            return None
        return "updated" if resp.json()["changes"] else "nochange"

    def delete(self, *ids):
        """Returns count removed."""
        resp = self._client.request(
            "DELETE", "/memories",
            params={"ids": list(ids)}, headers=self._headers,
        )
        return resp.json()["deleted"]

    def tags(self):
        return [(t["name"], t["count"]) for t in self._get("/tags").json()]

    def projects(self):
        return [(p["project"], p["count"]) for p in self._get("/projects").json()]

    def stats(self):
        return self._get("/stats").json()


class McpDriver:
    """Drives the MCP server over a real in-process client session (no subprocess).

    Same `MemoryDriver` contract, so the cross-surface suite runs against the MCP
    tools too. Each call opens an in-memory MCP session to the FastMCP server
    (built over a `SqliteStore` on the scratch DB) and parses the tool's single
    JSON content block. Tool I/O is async; methods bridge via `asyncio.run`.
    """

    name = "mcp"

    def __init__(self, db_path, agent="tester", extra_env=None):
        # Imported lazily so cli/api-only runs never need the mcp SDK installed.
        from agent_memory.mcp_server import create_mcp
        from agent_memory.store import SqliteStore

        self.db_path = Path(db_path)
        self.agent = agent
        self._mcp = create_mcp(store=SqliteStore(self.db_path))

    def initialize(self):
        # create_mcp() already ran store.initialize(); nothing to prime.
        pass

    # ---- plumbing --------------------------------------------------------
    def _call(self, tool, **args):
        import asyncio
        import json

        from mcp.shared.memory import create_connected_server_and_client_session as connect

        clean = {k: v for k, v in args.items() if v is not None}

        async def run():
            async with connect(self._mcp) as session:
                result = await session.call_tool(tool, clean)
                text = result.content[0].text if result.content else None
                return json.loads(text) if text else None

        return asyncio.run(run())

    @staticmethod
    def _to_memory(d, snippet=False):
        return Memory(
            id=d["id"], agent=d.get("agent"), project=d.get("project"),
            type=d.get("type"), tags=d.get("tags") or [],
            content="" if snippet else (d.get("content") or ""),
            snippet=d.get("snippet") if snippet else None,
        )

    # ---- semantic operations --------------------------------------------
    def add(self, content, *, project=None, tags=None, type=None, agent=None):
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        return self._call("memory_add", content=content, agent=agent or self.agent,
                          project=project, tags=tags or [], type=type)["id"]

    def query(self, **filters):
        data = self._call(
            "memory_query",
            today=filters.get("today"), yesterday=filters.get("yesterday"),
            since=filters.get("since"), until=filters.get("until"),
            project=filters.get("project"), agent=filters.get("agent"),
            tag=filters.get("tag"), type=filters.get("type"),
            limit=filters.get("limit"),
        )
        return [self._to_memory(d) for d in data["memories"]]

    def search(self, text, **filters):
        data = self._call(
            "memory_search", q=text,
            project=filters.get("project"), agent=filters.get("agent"),
            since=filters.get("since"), tag=filters.get("tag"),
            limit=filters.get("limit"),
        )
        return [self._to_memory(d, snippet=True) for d in data["memories"]]

    def get(self, mid):
        mem = self._call("memory_show", id=mid)["memory"]
        return None if mem is None else self._to_memory(mem)

    def update(self, mid, *, content=None, project=None, type=None,
               set_tags=None, add_tags=None, remove_tags=None, stdin=None):
        """Returns 'updated' | 'nochange' | None (not found)."""
        data = self._call("memory_update", id=mid, content=content, project=project,
                          type=type, set_tags=set_tags,
                          add_tags=add_tags or None, remove_tags=remove_tags or None)
        if not data["found"]:
            return None
        return "updated" if data["changes"] else "nochange"

    def delete(self, *ids):
        """Returns count removed."""
        return self._call("memory_delete", ids=list(ids))["deleted"]

    def tags(self):
        return [(name, count) for name, count in self._call("memory_tags")["tags"]]

    def projects(self):
        return [(project, count) for project, count in self._call("memory_projects")["projects"]]

    def stats(self):
        return self._call("memory_stats")


class StoreDriver:
    """Adapts any `MemoryStore` directly to the `MemoryDriver` contract (no transport).

    Lets the cross-surface behavior suite run against a store engine itself — used to
    prove `PostgresStore` satisfies the same contract as `SqliteStore` (Ticket F).
    """

    name = "store"

    def __init__(self, store, agent="tester"):
        self.store = store
        self.agent = agent

    def initialize(self):
        self.store.initialize()

    @staticmethod
    def _to_memory(d, snippet=False):
        return Memory(
            id=d["id"], agent=d.get("agent"), project=d.get("project"),
            type=d.get("type"), tags=d.get("tags") or [],
            content="" if snippet else (d.get("content") or ""),
            snippet=d.get("snippet") if snippet else None,
        )

    def add(self, content, *, project=None, tags=None, type=None, agent=None):
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        return self.store.add(content, agent or self.agent, project, tags or [], type)

    def query(self, **f):
        rows = self.store.query(
            today=f.get("today", False), yesterday=f.get("yesterday", False),
            since=f.get("since"), until=f.get("until"), project=f.get("project"),
            agent=f.get("agent"), tag=f.get("tag"), mtype=f.get("type"),
            limit=f.get("limit"))
        return [self._to_memory(d) for d in rows]

    def search(self, text, **f):
        rows = self.store.search(
            text, project=f.get("project"), agent=f.get("agent"),
            since=f.get("since"), tag=f.get("tag"), limit=f.get("limit"))
        return [self._to_memory(d, snippet=True) for d in rows]

    def get(self, mid):
        d = self.store.get(mid)
        return None if d is None else self._to_memory(d)

    def update(self, mid, *, content=None, project=None, type=None,
               set_tags=None, add_tags=None, remove_tags=None, stdin=None):
        changes = self.store.update(
            mid, new_content=content, project=project, mtype=type,
            set_tags=set_tags, add_tags=add_tags, remove_tags=remove_tags)
        if changes is None:
            return None
        return "updated" if changes else "nochange"

    def delete(self, *ids):
        count = len(self.store.get_many(list(ids)))
        self.store.delete(list(ids))
        return count

    def tags(self):
        return [(name, count) for name, count in self.store.list_tags()]

    def projects(self):
        return [(project, count) for project, count in self.store.list_projects()]

    def stats(self):
        return self.store.stats()


class PgDriver(StoreDriver):
    """StoreDriver over a fresh PostgresStore. Each instance truncates the schema
    (RESTART IDENTITY) so per-test state is isolated and ids start at 1, matching the
    other drivers' scratch-DB behavior. Requires AGENT_MEMORY_TEST_PG_DSN."""

    name = "pg"

    def __init__(self, db_path, agent="tester", extra_env=None):
        import os

        import psycopg

        from agent_memory.pg_store import PostgresStore

        dsn = os.environ["AGENT_MEMORY_TEST_PG_DSN"]
        store = PostgresStore(dsn)
        store.initialize()
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute("TRUNCATE memories, tags, memory_tags RESTART IDENTITY CASCADE")
        super().__init__(store, agent=agent)

    def initialize(self):
        pass  # already initialized + truncated in __init__
