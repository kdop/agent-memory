# Architecture — agent-memory

How the `agent_memory` package and its SQLite store are built and why. For usage, see
[README.md](README.md); for the agent logging protocol, see [MEMORY.md](MEMORY.md).

## Overview

A relational SQLite store behind a `MemoryStore` interface, exposed through three
surfaces over one codebase: the `memory-cli` command, an HTTP API (FastAPI), and an MCP
server. It gives AI agents a persistent, queryable, full-text-searchable timeline across
sessions — auto-timestamped, tagged, scoped by project and agent. The **client surface**
(CLI + the urllib `ApiStore`) is stdlib-only; the API and MCP servers are opt-in extras.

## Package layout

```
src/agent_memory/
  config.py            # config + DB-path / agent-name resolution
  store.py             # MemoryStore (ABC), SqliteStore, ApiStore, get_store()
  pg_store.py          # PostgresStore + SQLite→Postgres migration — [postgres] extra
  cli.py               # memory-cli command (argparse); presentation only
  server/              # FastAPI service (app/auth/schemas/__main__) — [server] extra
  mcp_server.py        # FastMCP server wrapping get_store() — [mcp] extra
memory-cli             # thin shim on PATH -> agent_memory.cli:main (rule #5)
```

`get_store()` is the seam. Precedence: `AGENT_MEMORY_API` (env, else config `api_url`)
→ `ApiStore` (HTTP); otherwise the DB target (`AGENT_MEMORY_DB` → config `db_path` → XDG
default) — a `postgresql://` DSN → `PostgresStore`, anything else → `SqliteStore`. Every
surface and engine goes through the same `MemoryStore` contract, so behavior can't drift:
the cross-surface suite asserts the CLI, API, MCP, and Postgres all produce identical
results.

## Design goals

1. **Persistent continuity** — solve the fresh-slate problem; remember across sessions.
2. **Queryable timeline** — structured filters (date, tag, project, type), not grep.
3. **Fast at scale** — stays fast at 100k+ memories via proper indexing.
4. **Multi-agent shared memory** — agent-a, Clu, etc. share one DB; cross-agent continuity.
5. **Evolution-friendly** — schema can grow without breaking changes (auto-migration).

## Key decisions

| Decision | Why | Rejected |
|---|---|---|
| **SQLite over files** | Structured queries, FTS5, ACID, single-file backup, zero deps | Markdown (not queryable), Postgres (needs a server), JSON (no index/FTS) |
| **Relational tags** (3 tables: `memories`, `tags`, `memory_tags`) | Indexed tag queries, case-insensitive, global rename, analytics | JSON array (slow `LIKE`, case-sensitive), generated column (can't rename globally) |
| **FTS5 for search** | Built-in, ranked, snippet highlighting, trigger-synced | `LIKE` (slow, no ranking), external engine (overkill) |
| **`agent_memory` package** | One store core behind CLI + API + MCP; reusable, testable across surfaces | Single-file script (couldn't host the API/MCP surfaces; `memory-cli` is now a shim onto the package) |
| **Reuse `SqliteStore`, no ORM** | FTS5 search is hand-SQL either way; keeps the client stdlib-only and the characterization tests valid | SQLAlchemy (rewrite of working storage; an ORM buys nothing for the FTS query) |

`memory-cli` stays on PATH as a thin shim so the command name and `memory` alias are
unchanged despite the package split.

## Database schema

```sqlite
-- Main memories table
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME DEFAULT (datetime('now', 'localtime')),
    agent TEXT NOT NULL,
    project TEXT,
    content TEXT NOT NULL,
    type TEXT
);

-- Canonical tag names (case-insensitive unique) + a required descriptor
CREATE TABLE tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL COLLATE NOCASE,
    description TEXT NOT NULL
);

-- Many-to-many junction
CREATE TABLE memory_tags (
    memory_id INTEGER NOT NULL,
    tag_id INTEGER NOT NULL,
    PRIMARY KEY (memory_id, tag_id),
    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
);

-- Full-text search over content
CREATE VIRTUAL TABLE memories_fts USING fts5(
    content, content=memories, content_rowid=id
);
```

**Indexes:** `timestamp`, `project`, `agent`, `type` on `memories`; both columns of
`memory_tags`. **Triggers** keep `memories_fts` in sync on every INSERT / UPDATE / DELETE.

## Data flow

**Add** — insert into `memories` → for each tag entry (`{"name": ..., "description":
...}`; description optional) resolve/create the tag — reusing an existing one
(updating its descriptor only if a new one is given), or creating a brand-new one
(auto-defaulting its descriptor to its own name if none is given) — link in
`memory_tags` → FTS trigger fires automatically. **Query** — build a `WHERE` clause from
filters, JOIN `memory_tags`/`tags` when a tag filter is present, `ORDER BY timestamp DESC`.
**Search** — `MATCH` against `memories_fts`, JOIN back to `memories`, apply extra filters,
`ORDER BY rank`, return `snippet()`-highlighted excerpts.

## Performance & concurrency

All common queries are indexed (`O(log n)`); adding a memory is `O(1)` plus `O(t)` for `t`
tags. Storage is ~8–10 MB per 10k memories (FTS index ≈ 1.5× content size) and scales
linearly. The DB runs in **WAL mode** (`PRAGMA journal_mode=WAL`) so readers don't block
the writer — multiple agents can query while one writes. On networked filesystems (NFS/SMB)
SQLite locking is less reliable; keep the DB on local disk.

If queries slow down past ~100k rows: `ANALYZE;`, inspect with `EXPLAIN QUERY PLAN`, and add
a covering index (e.g. `CREATE INDEX idx_project_timestamp ON memories(project, timestamp DESC)`).
Run `VACUUM;` periodically on DBs with many deletes.

## Programmatic access

Three supported entry points beyond the CLI:

- **The package:** `from agent_memory.store import get_store` → a `MemoryStore` honoring
  the same `AGENT_MEMORY_API`/`AGENT_MEMORY_DB`/config/XDG precedence as the CLI.
- **The HTTP API:** `python -m agent_memory.server` (needs `[server]`); routes mirror the
  store 1:1, bearer auth via `AGENT_MEMORY_API_TOKEN`, `GET /health` is unauthenticated.
- **The MCP server:** `python -m agent_memory.mcp_server` (needs `[mcp]`); tools
  `memory_add/query/search/show/update/delete/tags/projects/stats` over stdio.

The DB is also a plain SQLite file you can read directly. The path resolves
`AGENT_MEMORY_DB` → stored `db_path` (`memory-cli config set db_path …`) → default
`~/.local/share/agent-memory/memory.db`. A simple reader that honors the env override and
the default:

```python
import os, sqlite3
from pathlib import Path

db = Path(os.environ.get("AGENT_MEMORY_DB",
                         Path.home() / ".local/share/agent-memory/memory.db"))
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row

# Always use parameterized queries (? placeholders) — never string interpolation.
rows = conn.execute("""
    SELECT DISTINCT m.id, m.timestamp, m.content
    FROM memories m
    JOIN memory_tags mt ON m.id = mt.memory_id
    JOIN tags t ON mt.tag_id = t.id
    WHERE m.project = ? AND t.name = ? COLLATE NOCASE
    ORDER BY m.timestamp DESC LIMIT ?
""", ("project-a", "bugfix", 5)).fetchall()
```

Wrap multi-row writes in a single transaction; the FTS triggers handle search-index sync.

## Notes

- **Extensible:** the relational schema grows without breaking data (tag metadata,
  hierarchies, memory links, a `schema_version` table) — add when a real need shows up.
- **Security:** plaintext file, user-only perms (`chmod 600`); use filesystem encryption
  for sensitive data. SQL injection is a non-issue — all queries are parameterized.
- **Postgres engine:** set the DB target to a `postgresql://` DSN to use `PostgresStore`
  (needs the `[postgres]` extra). It mirrors the schema with a generated `tsvector` column
  + GIN index and `ts_headline` in place of FTS5 — kept behind the same `MemoryStore`, so
  every surface works identically. Move an existing SQLite DB over with
  `memory-cli migrate-to-postgres <dsn>` (preserves ids, idempotent).
- **Limits:** no built-in sync (rsync/Syncthing with WAL), keyword not semantic search
  (FTS is lexical — tag well). Browse the raw SQLite DB with `sqlite-web`/Datasette.
- **Principles:** simple over complex, fast over fancy, queryable over readable,
  agent-first. Markdown for prose; Memory CLI for the operational timeline.
