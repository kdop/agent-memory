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
