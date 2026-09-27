"""MCP-surface characterization — what only the MCP tools promise: the shape of
a tool result that is not a plain success. Cross-surface behavior lives in
test_behaviors.py (run against MCP via `McpDriver`).

Every test drives the real FastMCP server over an in-memory session, wrapping an
`ApiClient` pointed at the live server.
"""

import pytest

from drivers import McpDriver


@pytest.fixture
def mcp(live_server):
    url, token = live_server
    return McpDriver(url, token)


def _add(mcp, content, **kw):
    return mcp._call("memory_add", content=content, agent="tester", **kw)


# ── duplicate refusal ────────────────────────────────────────────────────────
def test_memory_add_duplicate_returns_error_shape(mcp):
    first = _add(mcp, "dup me", project="proj")
    assert set(first) == {"id", "warnings"}

    again = _add(mcp, "dup me", project="proj")
    assert set(again) == {"error", "existing_id", "score"}
    assert again["error"] == "duplicate"
    assert again["existing_id"] == first["id"]
    assert again["score"] == pytest.approx(1.0, abs=1e-5)
    assert len(mcp.query()) == 1   # nothing new stored


def test_memory_add_force_stores_the_duplicate(mcp):
    _add(mcp, "dup me", project="proj")
    forced = _add(mcp, "dup me", project="proj", force=True)
    assert set(forced) == {"id", "warnings"}
    assert forced["id"] == 2


# ── search mode ──────────────────────────────────────────────────────────────
def _seed_search(mcp):
    for content in ("the cat sat on the mat", "a dog in the yard",
                    "rain on the window", "coffee before code"):
        _add(mcp, content)


def test_memory_search_defaults_to_keyword(mcp):
    _seed_search(mcp)
    plain = mcp._call("memory_search", q="cat")
    keyword = mcp._call("memory_search", q="cat", mode="keyword")
    assert plain == keyword
    assert [m["id"] for m in plain["memories"]] == [1]
    assert plain["memories"][0]["score"] > 0
    assert "cat" in plain["memories"][0]["snippet"]


def test_memory_search_semantic_mode(mcp):
    _seed_search(mcp)
    data = mcp._call("memory_search", q="the cat sat on the mat", mode="semantic")
    hits = data["memories"]
    assert len(hits) == 4
    assert hits[0]["id"] == 1
    assert hits[0]["score"] == pytest.approx(1.0, abs=1e-6)
    assert hits[0]["snippet"] is None
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True)


def test_memory_search_hybrid_is_passed_through(mcp):
    # hybrid reaches the server and comes back fused: the exact text is found by
    # words and by meaning, so it ranks first with a score of 2/61.
    _add(mcp, "the cat sat on the mat")
    _add(mcp, "a dog in the yard")
    hits = mcp._call("memory_search", q="the cat sat on the mat", mode="hybrid")["memories"]
    assert hits[0]["id"] == 1
    assert hits[0]["score"] == pytest.approx(2 / 61, abs=1e-6)


def test_memory_search_docstring_explains_the_modes(mcp):
    import asyncio

    from mcp.shared.memory import create_connected_server_and_client_session as connect

    async def run():
        async with connect(mcp._mcp) as session:
            tools = (await session.list_tools()).tools
            return next(t for t in tools if t.name == "memory_search")

    tool = asyncio.run(run())
    assert tool.inputSchema["properties"]["mode"]["default"] == "keyword"
    for word in ("keyword", "semantic", "hybrid", "embedding model"):
        assert word in tool.description
