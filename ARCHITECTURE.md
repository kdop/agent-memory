# Architecture — Memory CLI

How `memory-cli` and its SQLite store are built and why. For usage, see
[README.md](README.md); for the agent logging protocol, see [MEMORY.md](MEMORY.md).

## Overview

A single-file Python 3 CLI over a relational SQLite database. It gives AI agents a
persistent, queryable, full-text-searchable timeline across sessions — auto-timestamped,
tagged, scoped by project and agent. Stdlib only; no third-party dependencies.

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
| **Single Python script** | Copy one file, no `pip install`, runs on any Python 3.7+ | Library/package (heavier deploy; can evolve to one later) |

Tags started as a JSON column and were migrated to the relational schema; old DBs are
auto-detected and migrated on first run, no user intervention.

## Database schema

```sql
-- Main memories table
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME DEFAULT (datetime('now', 'localtime')),
    agent TEXT NOT NULL,
    project TEXT,
    content TEXT NOT NULL,
    type TEXT
);

-- Canonical tag names (case-insensitive unique)
CREATE TABLE tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL COLLATE NOCASE
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

**Add** — insert into `memories` → for each tag `INSERT OR IGNORE` into `tags`, link in
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

The CLI is the supported interface, but the DB is a plain SQLite file you can read directly.
The CLI resolves the path `AGENT_MEMORY_DB` → stored `db_path` (`memory-cli config set
db_path …`) → default `~/.local/share/agent-memory/memory.db`. A simple reader that honors
the env override and the default:

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
- **Limits:** no built-in sync (rsync/Syncthing with WAL), keyword not semantic search
  (FTS5 is lexical — tag well), CLI only (browse with `sqlite-web`/Datasette).
- **Principles:** simple over complex, fast over fancy, queryable over readable,
  agent-first. Markdown for prose; Memory CLI for the operational timeline.
