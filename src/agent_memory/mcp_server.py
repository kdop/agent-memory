"""MCP server exposing the memory API as tools.

Mirrors the CLI surface — memory_add/query/search/flagged/show/update/delete/tags/
projects/stats — each wrapping an `ApiClient` (talks HTTP to the FastAPI service, exactly like
the CLI). stdio transport. Lives in the [mcp] extra.

Every tool returns a single JSON object (lists wrapped under a key) because FastMCP
emits one content block per item for a bare-list return — a wrapper keeps each result
in one parseable block.
"""

import logging
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .client import ApiClient, ApiRefused, DuplicateMemory
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
                   type: Optional[str] = None, force: bool = False) -> dict:
        """Add a memory. `tags` is a list of {"name": str, "description": str (optional)}
        — a brand-new tag with no description auto-defaults to its own name. Returns
        {"id": <new id>, "warnings": [...]}: warnings are rule names the entry breaks
        (short, no-project, no-reasoning). The memory is stored either way, unless a
        near-duplicate already exists in the same project: then nothing is stored and
        the result is {"error": "duplicate", "existing_id": <id>, "score": <cosine>}.
        Update that memory instead, or pass force=true to store this one anyway."""
        try:
            mid, warnings = api.add_with_warnings(
                content, agent or get_agent_name(), project, tags or [], type, force=force)
        except DuplicateMemory as e:
            return {"error": "duplicate", "existing_id": e.existing_id, "score": e.score}
        return {"id": mid, "warnings": warnings}

    @mcp.tool()
    def memory_query(since_days: Optional[int] = None,
                     since: Optional[str] = None, until: Optional[str] = None,
                     project: Optional[str] = None, agent: Optional[str] = None,
                     tag: Optional[str] = None, type: Optional[str] = None,
                     limit: Optional[int] = None) -> dict:
        """Query memories by time/project/agent/tag/type. since_days is a rolling
        window: everything since the start of the day N days ago (0=today,
        7=past week). limit defaults to 100 on the server; pass 0 for every match.
        Returns {"memories": [...]}."""
        return {"memories": api.query(
            since_days=since_days, since=since, until=until,
            project=project, agent=agent, tag=tag, mtype=type, limit=limit)}

    @mcp.tool()
    def memory_search(q: str, project: Optional[str] = None, agent: Optional[str] = None,
                      since: Optional[str] = None, tag: Optional[str] = None,
                      limit: int = 20, mode: str = "keyword") -> dict:
        """Search memories. `mode` picks how to match: "keyword" finds memories
        that contain the words in `q`; "semantic" finds memories that mean the
        same thing as `q`, even in other words, and needs the embedding model on
        the server; "hybrid" combines both. limit 0 returns every match. Returns
        {"memories": [...]}, best match first, each with a `score` and, for
        keyword mode, a `snippet` with the matches marked. When the server cannot
        serve the mode it returns {"error": "<why>"}."""
        try:
            rows = api.search(q, project=project, agent=agent, since=since, tag=tag,
                              limit=limit, mode=mode)
        except ApiRefused as e:
            return {"error": str(e)}
        return {"memories": rows}

    @mcp.tool()
    def memory_flagged(project: Optional[str] = None, verdict: Optional[str] = None,
                       limit: int = 20) -> dict:
        """List the memories the review model flagged: those whose review says
        "reject" or "rewrite", newest review first. `verdict` narrows to one of
        the two; None lists both. `project` narrows to one project. limit 0
        returns every match. Returns {"memories": [...]}, the same shape as
        memory_query, each memory with its `review` (verdict, rule, reason,
        rewrite, duplicate_of). A `verdict` that is not "reject" or "rewrite"
        returns {"error": "<why>"}."""
        if verdict is not None and verdict not in ("reject", "rewrite"):
            return {"error": f"verdict must be \"reject\" or \"rewrite\" (got {verdict!r})"}
        return {"memories": api.flagged(project=project, verdict=verdict, limit=limit)}

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
