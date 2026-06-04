# agent-memory

A shared, SQLite-backed **persistent memory system for AI agents** — a single-file
Python 3 CLI (`memory-cli`, stdlib only) over a relational SQLite DB. Gives CLI agents
continuity across sessions: what was done, decided, learned, instead of starting cold.

> **Golden rule:** if you don't log it, it's gone next session.

```bash
export PATH="$HOME/workspace/agent-memory:$PATH"
alias memory="$HOME/workspace/agent-memory/memory-cli"

memory add "Chose SQLite over flat files" \
  --agent=agent-a --project=agent-memory --tags=design --type=decision
memory query --project=agent-memory --today
memory search "database" --project=agent-memory
```

Memories are attributed per agent (`--agent`), scoped by `--project`, classified by
`--type` (`code | decision | lesson | note`), and tagged for retrieval. Full command
reference: `memory --help`.

- **[MEMORY.md](MEMORY.md)** — the logging protocol (imported by other repos)
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — schema, design decisions, programmatic access
- **[CLAUDE.md](CLAUDE.md)** — instructions for an agent working *on this tool*

## MCP server

Agents reach the memory system over **MCP** (replacing the former Claude Code skill).
Install the extra and register the stdio server once — it's then available in every
session, exposing tools `memory_add/query/search/show/update/delete/tags/projects/stats`:

```bash
pip install -e ".[mcp]"               # installs the `agent-memory-mcp` entry point
claude mcp add agent-memory -- agent-memory-mcp
```

The server wraps the same `get_store()` selection as the CLI, so it talks to local
SQLite by default, or to the HTTP service when `AGENT_MEMORY_API` is set. To run it
directly (e.g. for another MCP client): `python -m agent_memory.mcp_server` (stdio).

## HTTP service

`python -m agent_memory.server` starts a FastAPI service mirroring the CLI (needs the
`[server]` extra). Set `AGENT_MEMORY_API_TOKEN` for bearer auth; point clients at it
with `AGENT_MEMORY_API=http://host:8000`.

The DB location resolves, highest priority first: the `AGENT_MEMORY_DB` env var → a
stored `db_path` setting (`memory-cli config set db_path <path>`) → the default
`~/.local/share/agent-memory/memory.db`. It's **live and shared** across all agent
sessions, git-ignored (data, not source) — back it up out-of-band.
