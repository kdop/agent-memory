# API-first iteration — plan (2026-07-09)

Delta plan only: current state → new state. Tickets: #44–#47, #49–#51 (#48 deferred/closed).

## Current state

- Three surfaces (CLI, HTTP API, MCP) each resolve a store via `get_store()` and talk
  to it directly — bookkeeping (delete found/missing, list shapes, tag validation)
  duplicated per surface, and drift is already observable (CLI rejects empty tag
  names; API silently accepts them).
- Two engines: `SqliteStore` (default, live DB at `~/.local/share/agent-memory/memory.db`)
  and `PostgresStore` (DSN-selected). Store code owns DDL in `initialize()`.
- Sync psycopg, new connection per operation. No pooling. No migration tool.
- Client surface stdlib-only (rule #3).

## New state

- **API-first:** the FastAPI server (bearer-token auth) is the only process that
  touches a database. CLI and MCP are thin HTTP clients over `ApiStore` (urllib —
  stdlib rule preserved). All validation/bookkeeping enforced once, server-side.
- **Postgres-only:** SQLite dropped as an engine. Live SQLite data untouched until
  the final bulk import (#51).
- **SQLAlchemy 2.0 async + asyncpg + Alembic:** typed `Mapped[]` models are the
  schema source of truth; Alembic autogenerate for migrations; engine pool replaces
  connection-per-op; routes go `async def`. FTS queries remain `text()` islands.
  No SQLModel. Deps confined to `[server]` (absorbs `[postgres]`).
- Centralization (droplet/Tailscale) deferred — becomes a config change afterwards.

## Decisions

| Decision | Why | Rejected |
|---|---|---|
| Clients flip to API **before** the store rewrite | Async store then has exactly one consumer (`app.py`); MCP never needs async-ifying against the store | Store-first order |
| Alembic (supersedes dbmate pick) | With an ORM, models drive autogenerate; dbmate's edge was ORM-free plain SQL | dbmate, Atlas |
| SQLAlchemy 2.0 async, no SQLModel | Boring-standard FastAPI stack 2026; SQLModel lags core SA | raw asyncpg, SQLModel |
| Keep `memory-cli` name/alias + crisp offline error | Other repos (project-a) call it blind (rule #5); the error message is the contract | — |
| `config` + `migrate-to-postgres` keep direct access | Admin ops on the server's own store; can't go through HTTP | — |
| Data import LAST (#51), backup + count verification first | Rule #1; everything before it is reversible | migrate-early |

## Ticket order

1. **#44** Server ergonomics — config-driven host/port/token/DSN, zero-flag localhost
   default, systemd user unit example. *(Open Q: lifecycle — systemd unit proposed.)*
2. **#45 / #46** (parallel) CLI → pure API client / MCP attaches to API. Test harness:
   Cli/Mcp drivers run against an in-process API.
3. **#49** SQLAlchemy async store + Alembic baseline migration; routes async; pooling;
   store DDL removed (schema-presence check only); CI runs `alembic upgrade head`.
4. **#50** Cleanup: delete `sqlite_store.py` + sqlite test paths (keep stdlib `sqlite3`
   read inside `migrate-to-postgres` for #51).
5. **#47** Server-side normalization — tag validation once (422 on empty name), list
   endpoints return dicts, bulk `get_many` endpoint (kills N-GET delete preview).
6. **#51** Bulk-import live SQLite → PG: backup, copy preserving ids, verify counts/FTS/
   descriptions, cut over, retire SQLite file, update CLAUDE.md/MEMORY.md references.

## Acceptance (end state)

- Full suite green with CLI + MCP exercising HTTP only; engine contract covered by
  PG store tests (CI postgres service; documented podman one-liner locally).
- Fresh PG + `alembic upgrade head` → complete schema; no DDL in store code.
- Killing the API server mid-use → one-line actionable client error, exit 1.
- `memory query` returns full history from Postgres; SQLite file archived read-only.
