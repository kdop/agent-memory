# CLAUDE.md — working on agent-memory

You are **agent-a**. At session start, load your identity from the `personas` MCP
server: call `personas.show_persona` with `name: "agent-a"` and adopt it.

This repo **is the memory system** — the `memory-cli` tool plus the live SQLite DB
that gives every agent session continuity. You're the maintainer *and* a user, so
changes here affect every other project (project-a, …) that logs to this DB.

## The tool

- `memory-cli` — single-file Python 3 CLI, **stdlib only** (`sqlite3`, `argparse`,
  `json`, `pathlib`). No deps, no venv.
- `memory.db` — relational SQLite store at `~/workspace/agent-memory/memory.db`
  (override with `AGENT_MEMORY_DB`). **Live and shared across all agents.** Git-ignored.
- Docs: `MEMORY.md` (logging protocol — imported by other repos), `ARCHITECTURE.md`
  (schema + design). Command reference is `memory --help`, not markdown.

## Rules

1. **The DB is live and shared.** Back up `memory.db` before any schema change or
   destructive op; verify row counts before/after. Never experiment against it — point
   `AGENT_MEMORY_DB` at a scratch file.
2. **Never hardcode the DB path.** It resolves `AGENT_MEMORY_DB` → default above.
3. **Stdlib only.** No third-party deps — portability is the point.
4. **No CHANGELOG.** Log user-visible CLI changes to the DB itself
   (`--type=decision`, tagged). That *is* the history.
5. **Don't rename or relocate the tool or its `memory` alias** — other repos
   reference it by path.

## Dogfood it

Log your work here as it happens, under `--project=agent-memory --agent=agent-a`.
Full protocol in `MEMORY.md`.

    memory add "Repointed DB_PATH to AGENT_MEMORY_DB-overridable" \
      --agent=agent-a --project=agent-memory --tags=cli,migration --type=decision
