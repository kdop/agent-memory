# Plan: Centralize agent-memory behind a hosted API + MCP

> Living doc, kept lean. `git log -p -- PLAN.md` is the iteration history.
> Notable decisions also logged to the memory DB (`--type=decision --tags=plan,cloud`).

## Why

`memory-cli` is a single-file, stdlib-only CLI over a **live, shared SQLite DB**
(`~/.local/share/agent-memory/memory.db`). Phase 2 centralizes it behind an HTTP API so every
agent/machine shares one store and connects over **MCP** instead of copying a CLI around.
Executes epic #8 Phase 2 (#6 ApiStore, #7 cloud service + remote MCP).

## Decisions

- **Stack:** FastAPI + uvicorn (reuse the existing `MemoryStore`/`SqliteStore` layer).
- **DB:** SQLite first (reuse the live DB); **Postgres pluggable** later (Ticket F).
- **Surfaces:** MCP replaces the Skill; CLI kept but repointed via `ApiStore`; Skill discarded.
- **Exposure:** Local + LAN, **bearer-token auth**. No public TLS this phase.
- **Packaging:** Storage + config move out of the single script into an importable
  `agent_memory` package (reverses the #2/#8 single-file guideline). `memory-cli` command name
  + `memory` alias preserved (rule #5). Client surface (store/CLI/ApiStore) stays
  **stdlib-only** (`urllib`); only `[server]`/`[mcp]` extras pull deps — scopes rule #3 to the client.
- **`get_store()` precedence (unchanged):** `AGENT_MEMORY_API` → `AGENT_MEMORY_DB` → config
  `db_path` → XDG default.

## Testing

Plain **parametrized pytest** (no Gherkin). One `MemoryDriver` protocol
(`add/query/search/show/update/delete/tags/projects/stats`) with `CliDriver` (subprocess),
`ApiDriver` (httpx), `McpDriver` (MCP client); a `driver` fixture parametrizes the same test
functions over `{cli, api, mcp}` — assert behavior once, prove it on every surface (the
anti-drift guard). Dev deps: `pytest`, `httpx`. **Live DB never touched** — every driver runs
against a scratch `AGENT_MEMORY_DB` / a test server over one (rule #1).

## Target structure

```
src/agent_memory/{config,store,cli}.py     # moved from memory-cli; store.py gains ApiStore
src/agent_memory/server/{app,auth,schemas,__main__}.py   # FastAPI
src/agent_memory/mcp_server.py             # MCP, wraps get_store()
memory-cli                                 # thin shim -> agent_memory.cli:main
tests/{drivers.py,conftest.py,test_*.py}   # parametrized pytest over the 3 drivers
pyproject.toml                             # package + console_script; extras [server] [mcp] [dev]
```

## Tickets (branch + PR each)

- **A0 — pytest cross-surface harness + migrate the 41 characterization tests.** Stand up
  pytest + `MemoryDriver`/`CliDriver`; port `tests/test_memory_cli.py` to parametrized pytest;
  prove green against **today's** single-file CLI; delete the old unittest file after parity.
  *Accept:* `pytest` green vs the unchanged script; behavior parity with the retired 41 tests.
- **A — Extract `agent_memory` package (keystone).** Move store/config/cli into the package;
  `memory-cli` becomes a shim; switch pyproject `script-files` → package + `console_scripts`.
  *Accept:* A0 suite stays green via `CliDriver`; `memory` alias + path still work on the live DB.
- **B — FastAPI service over the store (SQLite) + bearer auth.** Routes mirroring `MemoryStore`:
  `POST /memories`, `GET /memories` (today/yesterday/since/until/project/agent/tag/type/limit),
  `GET /memories/search?q=` (+snippet), `GET /memories/{id}`, `PATCH /memories/{id}`,
  `DELETE /memories?ids=`, `GET /tags|/projects|/stats`, `GET /health` (no auth). Token from
  `AGENT_MEMORY_API_TOKEN`; agent attribution via the `agent` field. App calls `get_store()`.
  *Accept:* runs via `python -m agent_memory.server`; same tests pass through `ApiDriver`; 401 on bad token.
- **C — `ApiStore(MemoryStore)` over HTTP (urllib) + `get_store()` selection (#6).** Resolves
  `AGENT_MEMORY_API` (url+token / config) → `ApiStore`, else `SqliteStore`. *Accept:* with it
  set, every CLI command hits the service identically; A0 tests pass through `CliDriver`-vs-API.
- **D — MCP server + discard the Skill (#7 partial).** `mcp_server.py` tools
  `memory_add/query/search/show/update/delete/tags/projects/stats`, wrapping `get_store()`
  (SQLite local / API when `AGENT_MEMORY_API` set). stdio transport; streamable-http noted as
  follow-up. Remove `skills/memory/`, the `.claude` skill symlink, skill doc refs. *Accept:*
  tests pass through `McpDriver`.
- **E — Docs + packaging extras + run scripts.** Update ARCHITECTURE/MEMORY/README/CLAUDE for
  the package layout, server/mcp run commands, revised rules. Fix stale `install.sh`. Log
  user-visible changes to the DB as `--type=decision` (no CHANGELOG, rule #4).

### Phase 2b (later)
- **F — `PostgresStore` (tsvector/GIN) + SQLite→Postgres migration.** Config/env selects engine;
  `psycopg[binary]` in `[server]`. Handles the FTS5-vs-Postgres-FTS divergence behind `MemoryStore`.

## Reuse (don't rewrite)
- `MemoryStore` + `SqliteStore` (`memory-cli:68-499`), `get_store()` (`:496`) — moved verbatim.
- Config/path resolution (`memory-cli:24-52`), `get_agent_name` (`:59`).
- `tests/test_memory_cli.py` subprocess+scratch-DB pattern → reused by `CliDriver`, then retired.

## Verify (scratch DB only)
1. `export AGENT_MEMORY_DB=/tmp/mem-scratch.db`
2. `pytest` green across wired drivers (`-k cli`, `-k api`, `-k mcp`).
3. `python -m agent_memory.server` (token `dev`): `curl /health`; authed POST works; bad token → 401.
4. `AGENT_MEMORY_API=http://localhost:8000 AGENT_MEMORY_API_TOKEN=dev memory query` matches direct SQLite.
5. MCP: register `mcp_server.py` (stdio), exercise `memory_add`/`memory_search`.
6. Back up the live DB + verify row counts before pointing the server at it for real (rule #1).
