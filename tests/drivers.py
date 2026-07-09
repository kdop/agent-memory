"""Cross-surface test drivers.

One `MemoryDriver` contract, implemented per surface, so the same behavioral
tests run against every surface and prove they don't drift. Every driver talks
to ONE live FastAPI server backed by real Postgres — constructed with the
`(url, token)` the `live_server` fixture yields:

- `CliDriver`  — subprocess against `memory-cli`, steered at the server via
  `AGENT_MEMORY_API` / `AGENT_MEMORY_API_TOKEN`.
- `ApiDriver`  — a SYNC `httpx.Client` against the server URL.
- `McpDriver`  — an in-memory MCP session over `create_mcp(client=ApiClient(...))`.

Drivers return plain structured data (`Memory`, lists, dicts) — never raw CLI
chrome. Surface-specific output/exit-code/format assertions live in
`test_cli_surface.py` / `test_api_surface.py`, not here.
"""

# Keep the PEP 604 (`str | None`) annotations below evaluable as strings so the
# harness imports on every supported runtime, not just 3.10+.
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

MEMORY_CLI = Path(__file__).resolve().parent.parent / "memory-cli"


def _normalize_tags(tags):
    """Canonicalize a driver-level `tags`/`set_tags`/`add_tags` argument into the
    wire shape every surface expects: a list of {"name": ..., "description": ...}
    dicts (description omitted unless given). Accepts, for test convenience: None,
    a comma-separated string of bare names, a list of bare names, or already-shaped
    dicts (mixed freely)."""
    if tags is None:
        return None
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    return [t if isinstance(t, dict) else {"name": t} for t in tags]


def _normalize_tag_names(names):
    """Canonicalize a driver-level `remove_tags` argument into a plain list of tag
    names. Accepts None, a comma-separated string, or a list."""
    if names is None:
        return None
    if isinstance(names, str):
        return [t.strip() for t in names.split(",") if t.strip()]
    return list(names)


def _cli_launcher():
    """Interpreter prefix for launching the CLI subprocess. Under coverage
    (``AGENT_MEMORY_COV`` set) run the CLI under ``coverage run --parallel-mode``
    so the subprocess's lines are recorded; otherwise just the interpreter."""
    if os.environ.get("AGENT_MEMORY_COV"):
        return [sys.executable, "-m", "coverage", "run", "--parallel-mode"]
    return [sys.executable]


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
    """Drives the CLI as a subprocess and parses its output back into data.

    The subprocess is pointed at the live server via `AGENT_MEMORY_API` /
    `AGENT_MEMORY_API_TOKEN`, so it runs the real remote path (ApiClient → HTTP).
    """

    name = "cli"

    def __init__(self, url, token, agent="tester", extra_env=None):
        self.url = url
        self.token = token
        self.agent = agent
        self.extra_env = extra_env or {}

    # ---- plumbing --------------------------------------------------------
    def _env(self):
        env = {**os.environ, "AGENT_NAME": self.agent}
        env["AGENT_MEMORY_API"] = self.url
        if self.token is not None:
            env["AGENT_MEMORY_API_TOKEN"] = self.token
        env.update(self.extra_env)
        return env

    def raw(self, *args, stdin=None):
        """Run the CLI verbatim; returns the CompletedProcess (for surface tests)."""
        return subprocess.run(
            [*_cli_launcher(), str(MEMORY_CLI), *args],
            env=self._env(), input=stdin, capture_output=True, text=True,
        )

    # ---- semantic operations --------------------------------------------
    def add(self, content, *, project=None, tags=None, type=None, agent=None):
        args = ["add", content]
        if project:
            args += ["--project", project]
        norm = _normalize_tags(tags)
        if norm:
            args += ["--tags", json.dumps(norm)]
        if type:
            args += ["--type", type]
        if agent:
            args += ["--agent", agent]
        m = re.search(r"Memory #(\d+) added", self.raw(*args).stdout)
        return int(m.group(1)) if m else None

    def query(self, **filters):
        args = ["query"]
        if filters.get("since_days") is not None:
            args += ["--since-days", str(filters["since_days"])]
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
            args += ["--set-tags", json.dumps(_normalize_tags(set_tags))]
        norm_add = _normalize_tags(add_tags)
        if norm_add:
            args += ["--add-tags", json.dumps(norm_add)]
        norm_remove = _normalize_tag_names(remove_tags)
        if norm_remove:
            args += ["--remove-tags", ",".join(norm_remove)]
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

    def tags_with_descriptions(self):
        """Like tags(), but also captures the '↳ description' line under each tag
        (empty string when the listing omitted it — the auto-defaulted case)."""
        out = self.raw("tags").stdout
        if "No tags found" in out:
            return []
        res, pending = [], None
        for line in out.splitlines():
            m = re.match(r"\s+(\S+)\s+\((\d+)\)\s*$", line)
            if m:
                if pending:
                    res.append(pending)
                pending = [m.group(1), int(m.group(2)), ""]
                continue
            dm = re.match(r"\s*↳\s+(.*)$", line)
            if dm and pending:
                pending[2] = dm.group(1)
        if pending:
            res.append(pending)
        return [tuple(r) for r in res]

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
    """Drives the live FastAPI service over a SYNC `httpx.Client`.

    Same `MemoryDriver` contract as `CliDriver`, so the cross-surface suite runs
    against the real HTTP routes unchanged. A fixed bearer token is sent on every
    request. Surface-only concerns (401, /health, headers) live in
    `test_api_surface.py`.
    """

    name = "api"

    def __init__(self, url, token, agent="tester", extra_env=None):
        import httpx

        self.url = url.rstrip("/")
        self.token = token
        self.agent = agent
        self._client = httpx.Client(
            base_url=self.url,
            headers={"Authorization": f"Bearer {token}"} if token else {},
            timeout=30,
        )

    # ---- plumbing --------------------------------------------------------
    def _get(self, path, **params):
        clean = {k: v for k, v in params.items() if v is not None}
        return self._client.get(path, params=clean)

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
        body = {
            "content": content, "project": project, "type": type,
            "agent": agent or self.agent, "tags": _normalize_tags(tags) or [],
        }
        resp = self._client.post("/memories", json=body)
        return resp.json()["id"] if resp.status_code == 201 else None

    def query(self, **filters):
        resp = self._get(
            "/memories",
            since_days=filters.get("since_days"),
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
            body["set_tags"] = _normalize_tags(set_tags)
        norm_add = _normalize_tags(add_tags)
        if norm_add:
            body["add_tags"] = norm_add
        norm_remove = _normalize_tag_names(remove_tags)
        if norm_remove:
            body["remove_tags"] = norm_remove
        resp = self._client.patch(f"/memories/{mid}", json=body)
        if resp.status_code == 404:
            return None
        return "updated" if resp.json()["changes"] else "nochange"

    def delete(self, *ids):
        """Returns count removed."""
        resp = self._client.request("DELETE", "/memories", params={"ids": list(ids)})
        return resp.json()["deleted"]

    def tags(self):
        return [(t["name"], t["count"]) for t in self._get("/tags").json()]

    def tags_with_descriptions(self):
        return [(t["name"], t["count"], t.get("description", "")) for t in self._get("/tags").json()]

    def projects(self):
        return [(p["project"], p["count"]) for p in self._get("/projects").json()]

    def stats(self):
        return self._get("/stats").json()


class McpDriver:
    """Drives the MCP server over a real in-memory client session (no subprocess).

    Same `MemoryDriver` contract, so the cross-surface suite runs against the MCP
    tools too. The FastMCP server wraps an `ApiClient` pointed at the live server,
    so an MCP call still traverses the full HTTP → server → Postgres path. Each
    call opens an in-memory MCP session and parses the tool's single JSON content
    block. Tool I/O is async; methods bridge via `asyncio.run`.
    """

    name = "mcp"

    def __init__(self, url, token, agent="tester", extra_env=None):
        from agent_memory.client import ApiClient
        from agent_memory.mcp_server import create_mcp

        self.url = url
        self.token = token
        self.agent = agent
        self._mcp = create_mcp(client=ApiClient(url, token))

    # ---- plumbing --------------------------------------------------------
    def _call(self, tool, **args):
        import asyncio

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
        return self._call("memory_add", content=content, agent=agent or self.agent,
                          project=project, tags=_normalize_tags(tags) or [], type=type)["id"]

    def query(self, **filters):
        data = self._call(
            "memory_query",
            since_days=filters.get("since_days"),
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
                          type=type, set_tags=_normalize_tags(set_tags),
                          add_tags=_normalize_tags(add_tags) or None,
                          remove_tags=_normalize_tag_names(remove_tags) or None)
        if not data["found"]:
            return None
        return "updated" if data["changes"] else "nochange"

    def delete(self, *ids):
        """Returns count removed."""
        return self._call("memory_delete", ids=list(ids))["deleted"]

    def tags(self):
        return [(t["name"], t["count"]) for t in self._call("memory_tags")["tags"]]

    def tags_with_descriptions(self):
        return [(t["name"], t["count"], t.get("description", ""))
                for t in self._call("memory_tags")["tags"]]

    def projects(self):
        return [(p["project"], p["count"]) for p in self._call("memory_projects")["projects"]]

    def stats(self):
        return self._call("memory_stats")


DRIVER_FACTORIES = {"cli": CliDriver, "api": ApiDriver, "mcp": McpDriver}
