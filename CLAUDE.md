# CLAUDE.md — working on agent-memory

Persona: `franky` (served by the `personas` MCP - ignore if you don't have this MCP available)

This repo **is the memory system** — the `memory-cli` client, the FastAPI service that
owns the data, and the shared Postgres DB behind it that gives every agent session
continuity. You're the maintainer *and* a user, so changes here affect every other
project that logs to this system.

> Postgres is the only backend. There is no SQLite path left in the code — don't add
> one back.

## Where things are

- `README.md` — install, run, the three surfaces (CLI, API, MCP), the dashboard.
- `ARCHITECTURE.md` — package layout, the client/server seam, schema, design decisions.
- `skills/memory/SKILL.md` — the logging protocol, shipped as the `memory` plugin from
  this repo's marketplace (`.claude-plugin/`). This repo links it at
  `.claude/skills/memory/SKILL.md`.
- Command reference is `memory --help`, not markdown.
- **After cloning, run `git config core.hooksPath .githooks`.** The pre-commit hook bumps
  the plugin version whenever the skill changes; without a bump, installed projects
  never see the change.

## Rules

1. **The DB is live and shared.** Back up (`pg_dump`) before any schema change or
   destructive op; verify row counts before/after. Never experiment against it — point
   at a temporary copy (`scripts/db_copy.sh`).
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
   `--project=agent-memory`. The DB is the single source of truth for memory.

## Logging from this repo

This repo logs to the memory system like any other project, under the same rules —
what to log and what to skip is in `skills/memory/SKILL.md`, not here.
