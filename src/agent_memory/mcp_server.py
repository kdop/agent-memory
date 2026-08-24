"""MCP server exposing the memory API as tools.

Mirrors the CLI surface — memory_add/query/search/show/update/delete/tags/projects/
stats — each wrapping an `ApiClient` (talks HTTP to the FastAPI service, exactly like
the CLI). stdio transport. Lives in the [mcp] extra.

Every tool returns a single JSON object (lists wrapped under a key) because FastMCP
emits one content block per item for a bare-list return — a wrapper keeps each result
in one parseable block.
"""

import logging
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .client import ApiClient
from .config import get_agent_name


def create_mcp(client: Optional[ApiClient] = None) -> FastMCP:
    """Build the MCP server over an API client. If `client` is None it is resolved
    from config (AGENT_MEMORY_API / api_url); tests inject one pointed at a live
    test server."""
    api = client if client is not None else ApiClient()

    mcp = FastMCP("agent-memory")

    @mcp.tool()
    def memory_add(content: str, agent: Optional[str] = None,
                   project: Optional[str] = None, tags: Optional[list[dict]] = None,
                   type: Optional[str] = None) -> dict:
        """Add a memory. `tags` is a list of {"name": str, "description": str (optional)}
        — a brand-new tag with no description auto-defaults to its own name. Returns
        {"id": <new id>}."""
        mid = api.add(content, agent or get_agent_name(), project, tags or [], type)
        return {"id": mid}

    @mcp.tool()
    def memory_query(since_days: Optional[int] = None,
                     since: Optional[str] = None, until: Optional[str] = None,
                     project: Optional[str] = None, agent: Optional[str] = None,
                     tag: Optional[str] = None, type: Optional[str] = None,
                     limit: Optional[int] = None) -> dict:
        """Query memories by time/project/agent/tag/type. since_days is a rolling
        window: everything since the start of the day N days ago (0=today,
        7=past week). Returns {"memories": [...]}."""
        return {"memories": api.query(
            since_days=since_days, since=since, until=until,
            project=project, agent=agent, tag=tag, mtype=type, limit=limit)}

    @mcp.tool()
    def memory_search(q: str, project: Optional[str] = None, agent: Optional[str] = None,
                      since: Optional[str] = None, tag: Optional[str] = None,
                      limit: int = 20) -> dict:
        """Full-text search. Returns {"memories": [...]} with snippets."""
        return {"memories": api.search(
            q, project=project, agent=agent, since=since, tag=tag, limit=limit)}

    @mcp.tool()
    def memory_show(id: int) -> dict:
        """Fetch one memory by id. Returns {"memory": {...} | null}."""
        return {"memory": api.get(id)}

    @mcp.tool()
    def memory_update(id: int, content: Optional[str] = None, project: Optional[str] = None,
                      type: Optional[str] = None, set_tags: Optional[list[dict]] = None,
                      add_tags: Optional[list[dict]] = None, remove_tags: Optional[list[str]] = None) -> dict:
        """Update fields of a memory. `set_tags`/`add_tags` are lists of
        {"name": str, "description": str (optional)}; `remove_tags` is a list of names.
        Returns {"found": bool, "changes": [...]}."""
        changes = api.update(
            id, content=content, project=project, mtype=type,
            set_tags=set_tags, add_tags=add_tags, remove_tags=remove_tags)
        if changes is None:
            return {"found": False, "changes": []}
        return {"found": True, "changes": changes}

    @mcp.tool()
    def memory_delete(ids: list[int]) -> dict:
        """Delete memories by id. Returns {"deleted": n, "missing": [...]}."""
        return api.delete(ids)

    @mcp.tool()
    def memory_tags() -> dict:
        """List tags with counts + descriptions. Returns {"tags": [{name, count, description}, ...]}."""
        return {"tags": api.list_tags()}

    @mcp.tool()
    def memory_projects() -> dict:
        """List projects with counts. Returns {"projects": [{project, count}, ...]}."""
        return {"projects": api.list_projects()}

    @mcp.tool()
    def memory_stats() -> dict:
        """Aggregate statistics (total/agents/projects/tags/today/week/oldest/newest)."""
        return api.stats()

    return mcp


def main():
    logging.basicConfig(level=logging.WARNING)
    create_mcp().run(transport="stdio")


if __name__ == "__main__":
    main()
