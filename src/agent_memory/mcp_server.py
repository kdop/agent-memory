"""MCP server exposing the memory API as tools.

Mirrors the CLI surface — memory_add/query/search/flagged/show/restore/update/delete/
tags/projects/stats — each wrapping an `ApiClient` (talks HTTP to the FastAPI service, exactly like
the CLI). stdio transport. Lives in the [mcp] extra.

Every tool returns a single JSON object (lists wrapped under a key) because FastMCP
emits one content block per item for a bare-list return — a wrapper keeps each result
in one parseable block.
"""

import logging
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .client import REVIEW_STATUSES, ApiClient, ApiRefused, DuplicateMemory, ReviewRefused
from .config import get_agent_name


def _bad_status(status):
    """The error result for a `status` that is not one of the three, or None
    when it is fine."""
    if status is None or status in REVIEW_STATUSES:
        return None
    return {"error": f"status must be one of {', '.join(REVIEW_STATUSES)} (got {status!r})"}


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
        Update that memory instead, or pass force=true to store this one anyway.
        A server whose review model must approve each entry may refuse it too:
        then nothing is stored and the result is {"error": "review", "verdict":
        "reject" | "rewrite", "rule": <number or null>, "explanation": <why>,
        "rewrite": <suggested text or null>, "tags": [<suggested tag names>],
        "duplicate_of": <id or null>}. Fix the entry (the rewrite is a
        suggestion to check, not to paste), or pass force=true to store it as
        written. A "rewrite" with "duplicate_of" set is a merge: the entry
        repeats that memory and adds to it, and "rewrite" is the merged
        text; apply it to that memory with memory_update instead of adding.
        A stored memory that reverses or replaces an older one carries the
        old id as `supersedes`, set by the review model, never by the
        caller; the old one then carries `superseded_by`."""
        try:
            mid, warnings = api.add_with_warnings(
                content, agent or get_agent_name(), project, tags or [], type, force=force)
        except DuplicateMemory as e:
            return {"error": "duplicate", "existing_id": e.existing_id, "score": e.score}
        except ReviewRefused as e:
            return {"error": "review", "verdict": e.verdict, "rule": e.rule,
                    "explanation": e.explanation, "rewrite": e.rewrite, "tags": e.tags,
                    "duplicate_of": e.duplicate_of}
        return {"id": mid, "warnings": warnings}

    @mcp.tool()
    def memory_query(since_days: Optional[int] = None,
                     since: Optional[str] = None, until: Optional[str] = None,
                     project: Optional[str] = None, agent: Optional[str] = None,
                     tag: Optional[str] = None, type: Optional[str] = None,
                     status: Optional[str] = None, current: bool = False,
                     archived: bool = False, limit: Optional[int] = None) -> dict:
        """Query memories by time/project/agent/tag/type/status. since_days is a
        rolling window: everything since the start of the day N days ago (0=today,
        7=past week). `status` keeps to one review status: "unverified" (the
        review model has not checked the memory), "verified" (approved) or
        "flagged" (rejected, or a rewrite suggested); every memory carries its
        own as `review_status`. `current=true` hides the memories a newer one
        supersedes (those with a `superseded_by`); by default every memory is
        returned. Archived memories (see memory_restore) are left out;
        `archived=true` lists only them. limit defaults to 100 on the server; pass 0 for
        every match. Returns {"memories": [...]}; a `status` that is not one of
        the three returns {"error": "<why>"}."""
        bad = _bad_status(status)
        if bad is not None:
            return bad
        return {"memories": api.query(
            since_days=since_days, since=since, until=until, project=project,
            agent=agent, tag=tag, mtype=type, status=status, current=current,
            archived=archived, limit=limit)}

    @mcp.tool()
    def memory_search(q: str, project: Optional[str] = None, agent: Optional[str] = None,
                      since: Optional[str] = None, tag: Optional[str] = None,
                      limit: int = 20, mode: str = "keyword", current: bool = False,
                      archived: bool = False) -> dict:
        """Search memories. `mode` picks how to match: "keyword" finds memories
        that contain the words in `q`; "semantic" finds memories that mean the
        same thing as `q`, even in other words, and needs the embedding model on
        the server; "hybrid" combines both. `current=true` hides the memories a
        newer one supersedes; by default every match is returned.
        `archived=true` searches only the archived memories, which are left
        out otherwise. limit 0 returns every match. Returns
        {"memories": [...]}, best match first, each with a `score` and, for
        keyword mode, a `snippet` with the matches marked. When the server cannot
        serve the mode it returns {"error": "<why>"}."""
        try:
            rows = api.search(q, project=project, agent=agent, since=since, tag=tag,
                              limit=limit, mode=mode, current=current, archived=archived)
        except ApiRefused as e:
            return {"error": str(e)}
        return {"memories": rows}

    @mcp.tool()
    def memory_flagged(project: Optional[str] = None, verdict: Optional[str] = None,
                       status: Optional[str] = None, limit: int = 20,
                       archived: bool = False) -> dict:
        """List the memories the review model flagged: those whose review says
        "reject" or "rewrite", newest review first. `verdict` narrows to one of
        the two; None lists both. `status` lists the memories with that review
        status instead ("unverified", "verified" or "flagged"), so
        status="unverified" gives the memories the model has not checked yet.
        `project` narrows to one project. Archived memories are left out;
        `archived=true` lists only them. limit 0 returns every match. Returns
        {"memories": [...]}, the same shape as memory_query, each memory with
        its `review` (verdict, rule, reason, rewrite, duplicate_of) and its
        `review_status`. A `verdict` that is not "reject" or "rewrite", or a
        `status` that is not one of the three, returns {"error": "<why>"}."""
        if verdict is not None and verdict not in ("reject", "rewrite"):
            return {"error": f"verdict must be \"reject\" or \"rewrite\" (got {verdict!r})"}
        bad = _bad_status(status)
        if bad is not None:
            return bad
        return {"memories": api.flagged(project=project, verdict=verdict, status=status,
                                        limit=limit, archived=archived)}

    @mcp.tool()
    def memory_show(id: int, reviews: bool = False) -> dict:
        """Fetch one memory by id. Returns {"memory": {...} | null}. With
        `reviews=true` it adds "reviews": the memory's review history, every
        verdict the model gave on it, newest first, each with `created_at`
        (the memory's own `review` is only the newest); [] when it has not
        been reviewed, null when there is no such memory."""
        out = {"memory": api.get(id)}
        if reviews:
            out["reviews"] = api.reviews(id)
        return out

    @mcp.tool()
    def memory_restore(id: int) -> dict:
        """Bring an archived memory back into view. The review archives a
        memory it rejects as a diary line (rule 2) or as something git holds
        (rule 4), and the older memory when a newer one repeats it and adds
        more; an archived memory carries `archived_at`, is hidden from every
        listing and search, and is deleted after a set number of days unless
        restored. Returns {"found": bool, "restored": bool}: `restored` is
        false when the memory was not archived (nothing changed)."""
        restored = api.restore(id)
        if restored is None:
            return {"found": False, "restored": False}
        return {"found": True, "restored": restored}

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
