# agent-memory

![tests](https://github.com/kdop/agent-memory/actions/workflows/tests.yml/badge.svg)
![coverage](./coverage.svg)

A shared, **Postgres-backed persistent memory service for AI agents** — API-first. A
single async FastAPI service owns the database; agents reach it three ways — the
`memory-cli` command, the same HTTP API directly, and an MCP server — so they keep
continuity across sessions: what was done, decided, learned, instead of starting cold.
The **CLI and MCP are thin HTTP clients** (`ApiClient`, stdlib `urllib` only); only the
server touches Postgres, via SQLAlchemy 2.0 async (`pip install "agent-memory[server]"`
/ `[mcp]`).

> **Golden rule:** if you don't log it, it's gone next session.

## Architecture in one line

**Client (CLI / MCP) → HTTP → FastAPI service → Postgres.** The client never opens a
database; it resolves an endpoint and a bearer token and makes requests. The server is
the only thing with a `postgresql://` DSN.

## Quickstart

**1. Run Postgres** and create a database (any Postgres 14+ reachable by DSN).

**2. Install the server extra and create the schema** (Alembic owns it):

```bash
pip install -e ".[server]"
export AGENT_MEMORY_DB="postgresql://user:pass@localhost:5432/agent_memory"
alembic upgrade head
```

**3. Start the API** (needs a bearer token — the server fails closed without one):

```bash
AGENT_MEMORY_DB="$AGENT_MEMORY_DB" \
AGENT_MEMORY_API_TOKEN="$(openssl rand -hex 16)" \
AGENT_MEMORY_PORT=8099 \
python -m agent_memory.server           # binds 127.0.0.1:8099
```

**4. Point the CLI at it** and log a memory:

```bash
export PATH="$PWD:$PATH"          # from the repo root; or `pip install -e .` for the console script
alias memory="$PWD/memory-cli"
export AGENT_MEMORY_API="http://127.0.0.1:8099"     # this is also the built-in default
export AGENT_MEMORY_API_TOKEN="…"                   # same token the server was started with

memory add "Chose Postgres over SQLite" \
  --agent=my-agent --project=agent-memory --type=decision \
  --tags='[{"name":"design","description":"architecture choices"},{"name":"db"}]'
memory query --project=agent-memory --since-days 0
memory search "database" --project=agent-memory
```

Memories are attributed per agent (`--agent`), scoped by `--project`, classified by
`--type` (`decision | lesson | note | preference`), and tagged for retrieval. Tags are
structured objects — `--tags` takes a **JSON array** of `{"name", "description"}`, and a
new tag with no description defaults to its own name. Full command reference:
`memory --help`.

- **[MEMORY.md](MEMORY.md)** — the logging protocol (imported by other repos)
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — schema, design decisions, programmatic access
- **[CLAUDE.md](CLAUDE.md)** — instructions for an agent working *on this tool*

## The three surfaces

| Surface | What it is | Extra |
|---|---|---|
| **API service** | Async FastAPI + SQLAlchemy 2.0 (asyncpg); the only thing that touches Postgres. `python -m agent_memory.server`. | `[server]` |
| **CLI client** | `memory-cli` — a presentation layer over `ApiClient`. Stdlib-only, never opens a DB. | none |
| **MCP client** | Same `ApiClient`, exposed as MCP tools over stdio. | `[mcp]` |
| **Dashboard** | Vue 3 + Quasar single-page app, served by the API service under `/app`. | `web/` (Node) |

### Endpoint & auth resolution (clients)

- **Endpoint:** `AGENT_MEMORY_API` env → config `api_url` (`memory config set api_url …`)
  → local default `http://127.0.0.1:8099`.
- **Token:** `AGENT_MEMORY_API_TOKEN` env → config `api_token`. Sent as
  `Authorization: Bearer …`. The server requires a token and fails closed without one;
  `GET /health` is the only unauthenticated route.

## MCP server

Agents can reach the memory system over **MCP**. Install the extra and register the
stdio server once — it's then available in every session, exposing tools
`memory_add/query/search/show/update/delete/tags/projects/stats`:

```bash
pip install -e ".[mcp]"               # installs the `agent-memory-mcp` entry point
claude mcp add agent-memory -- agent-memory-mcp
```

Like the CLI, the MCP server is an `ApiClient` — it talks HTTP to the running FastAPI
service (`AGENT_MEMORY_API` / `api_url`), never a database. Run it directly for another
MCP client with `python -m agent_memory.mcp_server` (stdio).

## Dashboard

A browser UI for the same API: search and filter memories, edit them in place, and
manage tags (rename, merge, detach). Built with Vue 3 and Quasar; the API service serves
the built assets under `/app`, so it needs no separate host. Log in once with the bearer
token. See [web/README.md](web/README.md) for the dev setup (runs against an in-browser
mock, no backend needed) and the build.

![dashboard](docs/dashboard.png)

## PostgreSQL

Postgres is the only backend. The server reads its DSN from `AGENT_MEMORY_DB`
(`postgresql://…`, normalized to the asyncpg driver internally); the schema is owned by
**Alembic** (`alembic upgrade head`) with the SQLAlchemy models as the source of truth.
The database is **live and shared** across all agent sessions — back it up out-of-band.
Clients never see the DSN; they only ever talk to the API.
