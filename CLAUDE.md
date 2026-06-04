# CLAUDE.md — working on agent-memory

You are **agent-a**. At session start, load your identity from the `personas` MCP
server: call `personas.show_persona` with `name: "agent-a"` and adopt it.

This repo **is the memory system** — the `memory-cli` tool plus the live SQLite DB
that gives every agent session continuity. You're the maintainer *and* a user, so
changes here affect every other project (project-a, …) that logs to this DB.

## The tool

- `agent_memory` — Python package (`src/`) with one store core behind three surfaces:
  the `memory-cli` command (`cli.py`), an HTTP API (`server/`, `[server]` extra), and an
  MCP server (`mcp_server.py`, `[mcp]` extra). `memory-cli` on PATH is a thin shim onto
  `agent_memory.cli:main`. **Client surface (CLI + urllib `ApiStore`) is stdlib-only.**
- `store.py` is the seam: `get_store()` returns `ApiStore` when `AGENT_MEMORY_API` is set,
  else `SqliteStore`. The store contract is identical across surfaces (anti-drift).
- `memory.db` — relational SQLite store; path resolves `AGENT_MEMORY_DB` env → stored
  `db_path` (`memory-cli config set db_path …`) → default `~/.local/share/agent-memory/memory.db`.
  **Live and shared across all agents.** Git-ignored.
- Docs: `MEMORY.md` (logging protocol — imported by other repos), `ARCHITECTURE.md`
  (layout + schema + design). Command reference is `memory --help`, not markdown.

## Rules

1. **The DB is live and shared.** Back up `memory.db` before any schema change or
   destructive op; verify row counts before/after. Never experiment against it — point
   `AGENT_MEMORY_DB` at a scratch file.
2. **Never hardcode the DB path.** It resolves `AGENT_MEMORY_DB` → stored `db_path` →
   XDG default (see above). Keep that resolution order intact.
3. **Client stays stdlib-only.** `config.py`/`store.py`/`cli.py` use no third-party
   deps — portability is the point. Server/MCP deps are confined to the `[server]`/`[mcp]`
   extras (Phase 2 scoped the old "single-file, stdlib-only" rule to the client).
4. **No CHANGELOG.** Log user-visible changes to the DB itself
   (`--type=decision`, tagged). That *is* the history.
5. **Don't rename or relocate `memory-cli` or its `memory` alias** — other repos
   reference it by path. It stays a shim onto `agent_memory.cli:main`.

## Dogfood it

Log your work here as it happens, under `--project=agent-memory --agent=agent-a`.
Full protocol in `MEMORY.md`.

    memory add "Repointed DB_PATH to AGENT_MEMORY_DB-overridable" \
      --agent=agent-a --project=agent-memory --tags=cli,migration --type=decision
