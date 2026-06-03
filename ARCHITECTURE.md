# Memory CLI - Architecture

**Version:** 1.0  
**Last Updated:** 2026-05-15

---

## Overview

Memory CLI is a SQLite-based operational memory system for AI agents. It provides persistent, queryable, and searchable memory across sessions with auto-timestamping, tagging, and full-text search capabilities.

## Design Goals

### 1. **Persistent Continuity**
Agents need to remember context across sessions. Memory CLI solves the "fresh slate" problem where each new session starts with zero context.

### 2. **Queryable Timeline**
Unlike markdown files, we need structured queries:
- "What did I do yesterday?"
- "Show me all authentication work from last week"
- "Find all bugfixes tagged with 'urgent'"

### 3. **Fast at Scale**
System must remain fast with 100k+ memories. Proper indexing and relational design ensure this.

### 4. **Multi-Agent Shared Memory**
Multiple agents (Clu, agent-a, etc.) can share the same memory database, enabling cross-agent continuity.

### 5. **Evolution-Friendly**
Schema can evolve (add features like tag descriptions, hierarchies, analytics) without breaking changes.

---

## Architecture Decisions

### Decision 1: SQLite over Files

**Decision:** Use SQLite instead of markdown files

**Rationale:**
- ✅ Structured queries with filters (date, tag, project, type)
- ✅ Full-text search (FTS5) built-in
- ✅ ACID transactions (no file collisions)
- ✅ Indexing for fast queries
- ✅ Single file (easy backup/sync)
- ✅ No external dependencies (SQLite built into Python)

**Alternatives Considered:**
- Markdown files: Simple but not queryable, no indexing, collision risk
- PostgreSQL: Overkill, requires server, more setup
- JSON files: Better than markdown, but no indexing or FTS

---

### Decision 2: Relational Schema for Tags

**Decision:** 3-table normalized schema (memories, tags, memory_tags)

**Rationale:**
- ✅ Properly indexed tag queries (instant even with 100k memories)
- ✅ Case-insensitive tag matching (no "auth" vs "Auth" duplicates)
- ✅ Tag autocomplete (all tags in one table)
- ✅ Global tag rename (single UPDATE)
- ✅ Tag analytics (usage counts, trends)
- ✅ Future evolution (tag metadata: descriptions, categories, colors)

**Alternatives Considered:**
- JSON array: Simple, but slow (LIKE '%tag%'), no proper indexing, case-sensitive
- Generated column: Better than JSON, but still limited (can't rename tags globally)

**Migration Path:**
Initial version used JSON tags. Migration to relational was seamless (auto-detected and migrated on first run).

---

### Decision 3: FTS5 for Full-Text Search

**Decision:** SQLite FTS5 virtual table for content search

**Rationale:**
- ✅ Fast full-text search built into SQLite
- ✅ Snippet highlighting ("→ match ←")
- ✅ Ranking (most relevant first)
- ✅ No external dependencies
- ✅ Automatic sync via triggers

**Alternatives Considered:**
- LIKE queries: Too slow, no ranking
- External search engine: Overkill, adds complexity
- FTS4: Older, FTS5 is better and recommended

---

### Decision 4: Python CLI Tool

**Decision:** Single Python script (memory-cli) instead of library

**Rationale:**
- ✅ Simple deployment (just copy one file)
- ✅ No pip install needed
- ✅ Works anywhere Python 3.7+ exists
- ✅ Can evolve to package later if needed

**Why Python:**
- ✅ Standard on most systems
- ✅ Built-in sqlite3 module
- ✅ Good CLI argument parsing (argparse)
- ✅ Readable, maintainable code

---

## Database Schema

### Tables

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

-- Tags table (canonical tag names)
CREATE TABLE tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL COLLATE NOCASE
);

-- Many-to-many junction table
CREATE TABLE memory_tags (
    memory_id INTEGER NOT NULL,
    tag_id INTEGER NOT NULL,
    PRIMARY KEY (memory_id, tag_id),
    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
);

-- FTS5 virtual table for full-text search
CREATE VIRTUAL TABLE memories_fts USING fts5(
    content,
    content=memories,
    content_rowid=id
);
```

### Indexes

```sql
-- Performance indexes
CREATE INDEX idx_timestamp ON memories(timestamp);
CREATE INDEX idx_project ON memories(project);
CREATE INDEX idx_agent ON memories(agent);
CREATE INDEX idx_type ON memories(type);
CREATE INDEX idx_memory_tags_memory ON memory_tags(memory_id);
CREATE INDEX idx_memory_tags_tag ON memory_tags(tag_id);
```

### Triggers

```sql
-- Keep FTS5 in sync automatically
CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
    DELETE FROM memories_fts WHERE rowid = old.id;
END;

CREATE TRIGGER memories_au AFTER UPDATE ON memories BEGIN
    UPDATE memories_fts SET content = new.content WHERE rowid = new.id;
END;
```

---

## Data Flow

### Adding a Memory

```
1. User: memory add "content" --project=project-a --tags=auth,bugfix --type=code
2. Parse arguments → content, metadata, tags
3. Insert into memories table → get memory_id
4. For each tag:
   a. INSERT OR IGNORE into tags (creates if new)
   b. Get tag_id
   c. Insert into memory_tags (links memory ↔ tag)
5. Trigger automatically inserts into memories_fts
6. Return: "✓ Memory #123 added"
```

### Querying Memories

```
1. User: memory query --project=project-a --tag=auth --since=2026-05-01
2. Build WHERE clause with filters
3. If tag filter: JOIN memory_tags + tags tables
4. Execute SQL with proper ORDER BY timestamp DESC
5. For each row:
   a. Fetch tags from memory_tags JOIN tags
   b. Format output with emoji, timestamp, metadata
6. Print results
```

### Full-Text Search

```
1. User: memory search "authentication" --project=project-a --tag=auth
2. Build FTS5 MATCH query
3. JOIN memories_fts with memories table
4. Add additional filters (project, tag, since, etc.)
5. Use snippet() for highlighted excerpts
6. ORDER BY rank (most relevant first)
7. Print results with highlighted matches
```

---

## Performance Characteristics

### Query Performance

| Operation | Complexity | Notes |
|-----------|------------|-------|
| Add memory | O(1) | + O(t) for t tags |
| Query by date | O(log n) | Indexed on timestamp |
| Query by project | O(log n) | Indexed on project |
| Query by tag | O(log n) | Indexed JOIN |
| Query by type | O(log n) | Indexed on type |
| Full-text search | O(log n) | FTS5 index |
| Combined filters | O(log n) | Multiple indexes |

### Storage

- **Memories:** ~100-500 bytes per entry (depends on content length)
- **Tags:** ~20-50 bytes per tag
- **memory_tags:** 16 bytes per link (two integers)
- **FTS5 index:** ~1.5x content size
- **Indexes:** ~5-10% of table size

**Example:**
- 10,000 memories @ 300 bytes avg = ~3 MB
- 500 tags @ 30 bytes = ~15 KB
- 30,000 tag links @ 16 bytes = ~480 KB
- FTS5 index = ~4.5 MB
- **Total: ~8-10 MB for 10k memories**

Scales linearly. 100k memories ≈ 80-100 MB.

---

## Concurrency

### WAL Mode

```python
conn.execute("PRAGMA journal_mode=WAL")
```

**Write-Ahead Logging (WAL):**
- ✅ Better concurrency (readers don't block writers)
- ✅ Faster writes
- ✅ More robust (less corruption risk)

**Implication:**
Multiple agents can query while one is writing. No lock contention.

### File Locking

SQLite uses file locks for isolation. On shared filesystems (NFS, SMB), locking may be less reliable. For those cases, consider:
- SQLite on local disk only
- Network-accessible SQLite server (e.g., via Litestream)

---

## Extensibility Points

### Future Enhancements

**1. Tag Metadata**
```sql
ALTER TABLE tags ADD COLUMN description TEXT;
ALTER TABLE tags ADD COLUMN category TEXT;
ALTER TABLE tags ADD COLUMN color TEXT;
ALTER TABLE tags ADD COLUMN emoji TEXT;
```

**2. Tag Hierarchies**
```sql
CREATE TABLE tag_relations (
    parent_id INTEGER REFERENCES tags(id),
    child_id INTEGER REFERENCES tags(id)
);
```

**3. Memory Relationships**
```sql
CREATE TABLE memory_links (
    from_id INTEGER REFERENCES memories(id),
    to_id INTEGER REFERENCES memories(id),
    relationship TEXT  -- 'follows', 'related', 'fixes', etc.
);
```

**4. Attachments**
```sql
CREATE TABLE attachments (
    id INTEGER PRIMARY KEY,
    memory_id INTEGER REFERENCES memories(id),
    filename TEXT,
    content BLOB,
    mime_type TEXT
);
```

**5. Agent Metadata**
```sql
CREATE TABLE agents (
    id TEXT PRIMARY KEY,
    name TEXT,
    description TEXT,
    created_at DATETIME
);
```

**6. Analytics**
```sql
-- Views for common analytics
CREATE VIEW tag_usage_last_7d AS
SELECT t.name, COUNT(*) as count
FROM tags t
JOIN memory_tags mt ON t.id = mt.tag_id
JOIN memories m ON mt.memory_id = m.id
WHERE m.timestamp >= DATE('now', '-7 days')
GROUP BY t.name
ORDER BY count DESC;
```

---

## Security Considerations

### Data Storage

- **Location:** `~/workspace/agents/memory.db`
- **Permissions:** User-only read/write (chmod 600)
- **No encryption:** Data stored in plaintext
  - If sensitive: use filesystem encryption (LUKS, FileVault, BitLocker)
  - Future: SQLCipher for encrypted database

### SQL Injection

**Not a concern:**
- All queries use parameterized statements (`?` placeholders)
- No dynamic SQL construction from user input
- argparse validates input types

### Access Control

**Currently:** Filesystem permissions only

**Future considerations:**
- Per-agent access control
- Per-project visibility rules
- Audit log of who accessed what

---

## Migration Strategy

### Schema Versioning

**Current approach:** Detect missing tables/columns and create/migrate

**Future approach:**
```sql
CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY,
    applied_at DATETIME DEFAULT (datetime('now'))
);

-- Migration scripts in memory-cli:
migrations = {
    1: migrate_to_relational_tags,
    2: add_tag_metadata,
    3: add_memory_relationships,
}
```

### Backward Compatibility

**Philosophy:**
- New features should not break old workflows
- Migrations should be automatic and transparent
- Old data should remain accessible

**Example:**
When we migrated JSON tags → relational, old databases were auto-detected and migrated on first run. No user intervention needed.

---

## Testing Strategy

### Current State

**Manual testing:**
- All commands tested during development
- Example data created and queried
- Edge cases verified (empty results, missing filters, etc.)

### Future Testing

**Unit tests:**
```python
# test_memory_cli.py
def test_add_memory():
    # Insert test memory
    # Verify it appears in query
    # Check tags are linked correctly
    
def test_case_insensitive_tags():
    # Add memory with "Auth" tag
    # Add memory with "auth" tag
    # Verify only one tag exists in tags table
    
def test_date_filters():
    # Add memories on different dates
    # Verify --today, --yesterday, --since work correctly
```

**Integration tests:**
```bash
#!/bin/bash
# test_integration.sh

# Clean test DB
rm -f test.db

# Add test data
memory add "Test 1" --project=test --tags=tag1
memory add "Test 2" --project=test --tags=tag2

# Query and verify
count=$(memory query --project=test | grep "Found" | awk '{print $2}')
[ "$count" -eq "2" ] || exit 1
```

---

## Deployment

### Installation

**Current:**
1. Copy `memory-cli` to `~/workspace/agents/`
2. `chmod +x memory-cli`
3. Optionally: Add to PATH

**Future (pip package):**
```bash
pip install memory-cli
```

### Updates

**Current:**
Replace `memory-cli` file. Database auto-migrates if schema changed.

**Future:**
```bash
pip install --upgrade memory-cli
memory migrate  # explicit migration command
```

---

## Performance Tuning

### Query Optimization

**Current optimizations:**
- All common queries use indexes
- JOINs are on indexed columns
- FTS5 for full-text search (fast)
- LIMIT clauses prevent large result sets

**If queries get slow (>100k memories):**

```sql
-- Analyze tables for query planner
ANALYZE;

-- Check query plan
EXPLAIN QUERY PLAN
SELECT * FROM memories WHERE project = 'project-a';

-- Add covering index if needed
CREATE INDEX idx_project_timestamp ON memories(project, timestamp DESC);
```

### Database Maintenance

```bash
# Vacuum (reclaim space, optimize)
sqlite3 ~/workspace/agents/memory.db "VACUUM;"

# Analyze (update statistics)
sqlite3 ~/workspace/agents/memory.db "ANALYZE;"
```

**Recommended:** Monthly VACUUM for databases with many deletes.

---

## Monitoring

### Metrics to Track

```bash
# Database size
ls -lh ~/workspace/agents/memory.db

# Memory count
memory stats

# Most used tags
memory tags | head -10

# Memories per project
memory projects
```

### Health Checks

```bash
# Database integrity
sqlite3 ~/workspace/agents/memory.db "PRAGMA integrity_check;"

# Table info
sqlite3 ~/workspace/agents/memory.db ".schema"

# Index usage
sqlite3 ~/workspace/agents/memory.db "SELECT * FROM sqlite_stat1;"
```

---

## Comparison with Alternatives

### vs. Markdown Files

| Feature | Memory CLI | Markdown |
|---------|------------|----------|
| Queryable | ✅ | ❌ |
| Indexed | ✅ | ❌ |
| Auto-timestamp | ✅ | ❌ |
| Tag search | ✅ (fast) | ❌ |
| Date range | ✅ (fast) | ❌ |
| Full-text search | ✅ (FTS5) | ⚠️ (grep) |
| Git-friendly | ❌ | ✅ |
| Human-readable | ❌ | ✅ |
| Collision-safe | ✅ | ❌ |

**Use markdown for:** Project documentation, design decisions  
**Use Memory CLI for:** Operational timeline, queryable history

### vs. Notion/Obsidian

| Feature | Memory CLI | Notion | Obsidian |
|---------|------------|--------|----------|
| CLI access | ✅ | ❌ | ⚠️ |
| Offline | ✅ | ❌ | ✅ |
| Fast queries | ✅ | ⚠️ | ⚠️ |
| Agent-friendly | ✅ | ❌ | ⚠️ |
| Structured data | ✅ | ✅ | ❌ |
| UI | ❌ | ✅ | ✅ |
| Backlinks | ❌ | ✅ | ✅ |

**Use Memory CLI for:** Agent memory, structured logs  
**Use Notion/Obsidian for:** Human knowledge base, rich content

---

## Known Limitations

### 1. **No Built-in Sync**
Database is local. For multi-machine sync:
- Use Syncthing / Dropbox / rsync
- Be aware of SQLite + cloud sync issues (use WAL mode)

### 2. **No Web UI**
CLI-only. For web access:
- Future: Build web interface
- Workaround: Use sqlite-web or Datasette

### 3. **No Semantic Search**
FTS5 is keyword-based, not semantic.
- Future: Add embeddings + vector search
- Workaround: Tag well, use descriptive content

### 4. **No Collaboration Features**
Single-user, file-based.
- Future: Multi-user server mode
- Workaround: Shared filesystem + agent-specific filters

---

## Design Principles

1. **Simple over complex:** SQLite file, not client-server DB
2. **Fast over fancy:** Proper indexes, not complex features
3. **Queryable over readable:** Structured data, not prose
4. **Evolution over perfection:** Start simple, add features when needed
5. **Agent-first:** Designed for AI agents, not just humans

---

## References

- [SQLite Documentation](https://sqlite.org/docs.html)
- [SQLite FTS5](https://sqlite.org/fts5.html)
- [SQLite WAL Mode](https://sqlite.org/wal.html)
- [Database Normalization](https://en.wikipedia.org/wiki/Database_normalization)

---

_This document describes the technical architecture. For usage, see USER-GUIDE.md._
