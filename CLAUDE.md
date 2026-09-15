# CLAUDE.md — working on agent-memory

This repo **is the memory system** — the `memory-cli` client, the FastAPI service that
owns the data, and the shared Postgres DB behind it that gives every agent session
continuity. You're the maintainer *and* a user, so changes here affect every other
project that logs to this system.

> Postgres is the only backend. There is no SQLite path left in the code — don't add
> one back.

## The tool

- `agent_memory` — Python package (`src/`). **API-first, Postgres-only.** One async
  FastAPI service (`server/`, `[server]` extra) is the only thing that touches the DB;
  the `memory-cli` command (`cli.py`) and the MCP server (`mcp_server.py`, `[mcp]` extra)
  are thin HTTP clients over `ApiClient` (`client.py`). `memory-cli` on PATH is a shim
  onto `agent_memory.cli:main`. **Client surface (CLI + MCP + urllib `ApiClient`) is
  stdlib-only.**
- `client.py`/`config.py` are the client seam: they resolve an **endpoint + token**
  (`AGENT_MEMORY_API` env → config `api_url` → `http://127.0.0.1:8099`;
  `AGENT_MEMORY_API_TOKEN` env → config `api_token`) — never a DB. The server resolves the
  DSN from `AGENT_MEMORY_DB` (a `postgresql://` URL). The Pydantic contract is identical
  across CLI, API, and MCP (anti-drift).
- **Server stack:** SQLAlchemy 2.0 async + asyncpg; `server/models.py` is the schema
  source of truth; **Alembic** owns DDL (`alembic upgrade head`). FTS is a generated
  `content_tsv` tsvector + GIN index.
- Run the server: `AGENT_MEMORY_DB=postgresql://… AGENT_MEMORY_API_TOKEN=… python -m
  agent_memory.server` (fails closed without a token; host/port via
  `AGENT_MEMORY_HOST`/`AGENT_MEMORY_PORT`).
- Docs: `MEMORY.md` (logging protocol — imported by other repos), `ARCHITECTURE.md`
  (layout + schema + design). Command reference is `memory --help`, not markdown.

## Rules

1. **The DB is live and shared.** Back up (`pg_dump`) before any schema change or
   destructive op; verify row counts before/after. Never experiment against it — point
   at a scratch database.
2. **Never hardcode the DB target.** The **server** resolves it from `AGENT_MEMORY_DB`
   (a `postgresql://` DSN, normalized to asyncpg); the **client** never sees a DB — it
   resolves `AGENT_MEMORY_API` → config `api_url` → local default. Keep those resolution
   orders intact.
3. **Client stays stdlib-only.** `config.py`/`client.py`/`cli.py`/`mcp_server.py` (the
   client half of it) use no third-party deps — portability + fast CLI cold-start is the
   point. Server deps (FastAPI, SQLAlchemy, asyncpg, Alembic) live only in the `[server]`
   extra; MCP in `[mcp]`.
4. **No CHANGELOG.** Log user-visible changes to the memory system itself
   (`--type=decision`, tagged). That *is* the history.
5. **Don't rename or relocate `memory-cli` or its `memory` alias** — other repos
   reference it by path. It stays a shim onto `agent_memory.cli:main`.
6. **Memories live in the DB, never in md files.** Never write to Claude Code's
   md-file memory (`~/.claude/.../memory/*.md`). An agent's own working rules go in the
   DB under `--project=<agent-name>` (cross-project preferences); project work under
   `--project=agent-memory`. The DB is the single source of truth for memory — dogfood it.

## Dogfood it

Do **not** log "did X, shipped Y" narrative here — git/PR history already is that
record. Before adding an entry, ask: could a future session reconstruct
this from `git log`? If yes, skip it. Reserve entries here for durable preferences,
standing rules, and facts *not* reconstructable from git — a deliberate non-action, an
environment gotcha, a stated user preference. Full protocol in `MEMORY.md`.
