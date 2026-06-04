"""MCP server exposing the memory store as tools (Ticket D).

Mirrors the CLI/API surface — memory_add/query/search/show/update/delete/tags/
projects/stats — each wrapping the resolved store (SQLite locally, or the API
when AGENT_MEMORY_API is set, via get_store()). stdio transport; streamable-http
is a documented follow-up (FastMCP.run_streamable_http_async exists). Lives in the
[mcp] extra so the client surface stays stdlib-only.

Every tool returns a single JSON object (lists wrapped under a key) because
FastMCP emits one content block per item for a bare-list return — a wrapper keeps
each result in one parseable block.
"""

import logging
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .config import get_agent_name
from .store import get_store


def create_mcp(store=None) -> FastMCP:
    """Build the MCP server over a resolved store. If `store` is None it is
    resolved once via get_store() (mirrors server.create_app); tests inject a
    store bound to a scratch DB."""
    resolved = store if store is not None else get_store()
    resolved.initialize()

    mcp = FastMCP("agent-memory")

    @mcp.tool()
    def memory_add(content: str, agent: Optional[str] = None,
                   project: Optional[str] = None, tags: Optional[list[str]] = None,
                   type: Optional[str] = None) -> dict:
        """Add a memory. Returns {"id": <new id>}."""
        mid = resolved.add(content, agent or get_agent_name(), project, tags or [], type)
        return {"id": mid}

    @mcp.tool()
    def memory_query(today: bool = False, yesterday: bool = False,
                     since: Optional[str] = None, until: Optional[str] = None,
                     project: Optional[str] = None, agent: Optional[str] = None,
                     tag: Optional[str] = None, type: Optional[str] = None,
                     limit: Optional[int] = None) -> dict:
        """Query memories by time/project/agent/tag/type. Returns {"memories": [...]}."""
        return {"memories": resolved.query(
            today=today, yesterday=yesterday, since=since, until=until,
            project=project, agent=agent, tag=tag, mtype=type, limit=limit)}

    @mcp.tool()
    def memory_search(q: str, project: Optional[str] = None, agent: Optional[str] = None,
                      since: Optional[str] = None, tag: Optional[str] = None,
                      limit: int = 20) -> dict:
        """Full-text search. Returns {"memories": [...]} with snippets."""
        return {"memories": resolved.search(
            q, project=project, agent=agent, since=since, tag=tag, limit=limit)}

    @mcp.tool()
    def memory_show(id: int) -> dict:
        """Fetch one memory by id. Returns {"memory": {...} | null}."""
        return {"memory": resolved.get(id)}

    @mcp.tool()
    def memory_update(id: int, content: Optional[str] = None, project: Optional[str] = None,
                      type: Optional[str] = None, set_tags: Optional[str] = None,
                      add_tags: Optional[str] = None, remove_tags: Optional[str] = None) -> dict:
        """Update fields of a memory. Returns {"found": bool, "changes": [...]}."""
        changes = resolved.update(
            id, new_content=content, project=project, mtype=type,
            set_tags=set_tags, add_tags=add_tags, remove_tags=remove_tags)
        if changes is None:
            return {"found": False, "changes": []}
        return {"found": True, "changes": changes}

    @mcp.tool()
    def memory_delete(ids: list[int]) -> dict:
        """Delete memories by id. Returns {"deleted": n, "missing": [...]}."""
        found = {r["id"] for r in resolved.get_many(ids)}
        to_delete = [i for i in ids if i in found]
        if to_delete:
            resolved.delete(to_delete)
        return {"deleted": len(to_delete), "missing": [i for i in ids if i not in found]}

    @mcp.tool()
    def memory_tags() -> dict:
        """List tags with counts. Returns {"tags": [[name, count], ...]}."""
        return {"tags": [[name, count] for name, count in resolved.list_tags()]}

    @mcp.tool()
    def memory_projects() -> dict:
        """List projects with counts. Returns {"projects": [[project, count], ...]}."""
        return {"projects": [[project, count] for project, count in resolved.list_projects()]}

    @mcp.tool()
    def memory_stats() -> dict:
        """Aggregate statistics (total/agents/projects/tags/today/week/oldest/newest)."""
        return resolved.stats()

    return mcp


def main():
    logging.basicConfig(level=logging.WARNING)
    create_mcp().run(transport="stdio")


if __name__ == "__main__":
    main()
