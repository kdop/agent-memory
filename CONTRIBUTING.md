# Contributing to Memory CLI

Thank you for considering contributing to Memory CLI! This document provides guidelines for development, testing, and extending the tool.

---

## Development Setup

### Prerequisites

- Python 3.7+
- sqlite3 (built into Python)
- Basic understanding of SQL and SQLite

### Get Started

```bash
# Clone/navigate to the tool
cd ~/workspace/agents

# The tool is a single file
ls -la memory-cli

# Test it works
./memory-cli stats
```

---

## Code Structure

### Single-File Architecture

Memory CLI is intentionally a single Python file (`memory-cli`) for:
- ✅ Easy deployment (just copy one file)
- ✅ No dependencies to install
- ✅ Simple to understand and modify
- ✅ Easy to version and distribute

### File Organization

```python
# memory-cli structure

1. Imports (stdlib only)
2. Constants (DB_PATH)
3. Database initialization (init_db, migrate_json_tags)
4. Helper functions (get_agent_name, get_or_create_tag, get_memory_tags)
5. Command functions (add_memory, query_memories, search_memories, etc.)
6. Main CLI (argparse setup, command routing)
```

### Key Functions

| Function | Purpose |
|----------|---------|
| `init_db()` | Create tables, indexes, triggers if missing |
| `migrate_json_tags()` | Migrate old JSON tags to relational |
| `get_or_create_tag(conn, tag_name)` | Idempotent tag creation |
| `get_memory_tags(conn, memory_id)` | Fetch tags for display |
| `add_memory(args)` | Insert memory + tags |
| `query_memories(args)` | Query with filters |
| `search_memories(args)` | FTS5 search |
| `list_tags(args)` | Tag usage stats |
| `show_stats(args)` | Database statistics |

---

## Adding New Features

### 1. Add a New Command

**Example: Add `memory edit` command**

```python
# Step 1: Create command function
def edit_memory(args):
    """Edit an existing memory"""
    conn = sqlite3.connect(DB_PATH)
    
    # Fetch current memory
    cursor = conn.execute(
        "SELECT content FROM memories WHERE id = ?",
        (args.id,)
    )
    row = cursor.fetchone()
    
    if not row:
        print(f"Memory #{args.id} not found")
        return
    
    # Update memory
    conn.execute(
        "UPDATE memories SET content = ? WHERE id = ?",
        (args.new_content, args.id)
    )
    conn.commit()
    conn.close()
    
    print(f"✓ Memory #{args.id} updated")

# Step 2: Add to argparse (in main())
edit_parser = subparsers.add_parser("edit", help="Edit a memory")
edit_parser.add_argument("id", type=int, help="Memory ID")
edit_parser.add_argument("new_content", help="New content")
edit_parser.set_defaults(func=edit_memory)
```

---

### 2. Add a New Filter

**Example: Add `--agent-not` filter to exclude an agent**

```python
# In query_memories() function

# After existing agent filter:
if args.agent:
    where_clauses.append("m.agent = ?")
    params.append(args.agent)

# Add new filter:
if args.agent_not:
    where_clauses.append("m.agent != ?")
    params.append(args.agent_not)

# In main(), add to query_parser:
query_parser.add_argument("--agent-not", help="Exclude agent")
```

---

### 3. Add Schema Migration

**Example: Add `description` field to tags table**

```python
def migrate_add_tag_descriptions():
    """Add description column to tags table"""
    conn = sqlite3.connect(DB_PATH)
    
    # Check if column exists
    cursor = conn.execute("PRAGMA table_info(tags)")
    columns = [row[1] for row in cursor.fetchall()]
    
    if 'description' in columns:
        conn.close()
        return False  # Already migrated
    
    # Add column
    conn.execute("ALTER TABLE tags ADD COLUMN description TEXT")
    conn.commit()
    conn.close()
    
    return True

# Call in main() after init_db():
if migrate_add_tag_descriptions():
    print("✓ Migrated: Added tag descriptions")
```

---

### 4. Add Tag Metadata

**Example: Allow setting tag descriptions**

```python
# New command: memory tag-describe
def describe_tag(args):
    """Set description for a tag"""
    conn = sqlite3.connect(DB_PATH)
    
    # Find tag
    cursor = conn.execute(
        "SELECT id FROM tags WHERE name = ? COLLATE NOCASE",
        (args.tag,)
    )
    row = cursor.fetchone()
    
    if not row:
        print(f"Tag '{args.tag}' not found")
        conn.close()
        return
    
    # Update description
    conn.execute(
        "UPDATE tags SET description = ? WHERE id = ?",
        (args.description, row[0])
    )
    conn.commit()
    conn.close()
    
    print(f"✓ Tag '{args.tag}' description updated")

# Add to argparse
tag_desc_parser = subparsers.add_parser("tag-describe", help="Set tag description")
tag_desc_parser.add_argument("tag", help="Tag name")
tag_desc_parser.add_argument("description", help="Tag description")
tag_desc_parser.set_defaults(func=describe_tag)
```

---

### 5. Add Memory Relationships

**Example: Link related memories**

```python
# Schema (add in init_db):
conn.execute("""
    CREATE TABLE IF NOT EXISTS memory_links (
        from_id INTEGER REFERENCES memories(id) ON DELETE CASCADE,
        to_id INTEGER REFERENCES memories(id) ON DELETE CASCADE,
        relationship TEXT,
        PRIMARY KEY (from_id, to_id)
    )
""")

# Command: memory link
def link_memories(args):
    """Link two memories"""
    conn = sqlite3.connect(DB_PATH)
    
    conn.execute(
        "INSERT OR IGNORE INTO memory_links (from_id, to_id, relationship) VALUES (?, ?, ?)",
        (args.from_id, args.to_id, args.relationship)
    )
    conn.commit()
    conn.close()
    
    print(f"✓ Linked #{args.from_id} → #{args.to_id} ({args.relationship})")

# Add to argparse
link_parser = subparsers.add_parser("link", help="Link memories")
link_parser.add_argument("from_id", type=int, help="From memory ID")
link_parser.add_argument("to_id", type=int, help="To memory ID")
link_parser.add_argument("relationship", help="Relationship type (e.g., 'follows', 'fixes')")
link_parser.set_defaults(func=link_memories)
```

---

## Testing

### Manual Testing

```bash
# Test basic add
memory add "Test memory" --tags=test

# Verify it appears
memory query --tag=test

# Test filters
memory query --today
memory query --since=2026-05-01

# Test search
memory search "test"

# Clean up
sqlite3 ~/workspace/agents/memory.db "DELETE FROM memories WHERE content = 'Test memory'"
```

### Automated Testing (Future)

```python
# test_memory_cli.py
import unittest
import sqlite3
import tempfile
import os

class TestMemoryCLI(unittest.TestCase):
    def setUp(self):
        # Use temp database
        self.db_path = tempfile.mktemp(suffix='.db')
        os.environ['MEMORY_DB_PATH'] = self.db_path
    
    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
    
    def test_add_memory(self):
        # Add memory
        # Verify it's in database
        pass
    
    def test_case_insensitive_tags(self):
        # Add memory with "Auth" tag
        # Add memory with "auth" tag
        # Verify only one tag exists
        pass

if __name__ == '__main__':
    unittest.main()
```

---

## Code Style

### Python Style

Follow PEP 8 with these specifics:

```python
# Imports: stdlib only, grouped and sorted
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Constants: UPPER_CASE
DB_PATH = Path.home() / "workspace" / "agents" / "memory.db"

# Functions: snake_case
def get_memory_tags(conn, memory_id):
    pass

# Classes: PascalCase (if added in future)
class MemoryManager:
    pass

# SQL: Multi-line for clarity
cursor = conn.execute("""
    SELECT m.id, m.timestamp, m.content
    FROM memories m
    WHERE m.project = ?
    ORDER BY m.timestamp DESC
""", (project,))
```

### SQL Style

```sql
-- Tables: lowercase, plural
CREATE TABLE memories (...);
CREATE TABLE tags (...);

-- Columns: lowercase, snake_case
timestamp DATETIME
memory_id INTEGER
tag_name TEXT

-- Indexes: idx_<table>_<column>
CREATE INDEX idx_timestamp ON memories(timestamp);
CREATE INDEX idx_memory_tags_memory ON memory_tags(memory_id);
```

---

## Documentation

### Inline Comments

```python
# Good: Explain WHY, not WHAT
# Use case-insensitive collation to prevent duplicate tags
name TEXT UNIQUE NOT NULL COLLATE NOCASE

# Bad: Obvious
# Set name to tag name
name = tag_name
```

### Docstrings

```python
def get_or_create_tag(conn, tag_name):
    """
    Get tag_id, creating tag if it doesn't exist.
    
    Args:
        conn: SQLite connection
        tag_name: Tag name (case-insensitive)
    
    Returns:
        int: Tag ID
    """
    # Implementation
```

### User-Facing Docs

When adding features, update:
- **USER-GUIDE.md** - How to use it (examples, workflows)
- **README-memory.md** - Quick reference (add to command list)
- **ARCHITECTURE.md** - Technical details (schema changes, design decisions)

---

## Performance Considerations

### Indexing

Always index columns used in WHERE clauses:

```python
# Good: Indexed column
conn.execute("SELECT * FROM memories WHERE timestamp > ?", (date,))

# Bad: Un-indexed column (if not indexed)
conn.execute("SELECT * FROM memories WHERE some_new_column = ?", (value,))
# Fix: CREATE INDEX idx_some_new_column ON memories(some_new_column);
```

### JOINs

Keep JOINs on indexed columns:

```python
# Good: Indexed foreign keys
JOIN memory_tags mt ON m.id = mt.memory_id
JOIN tags t ON mt.tag_id = t.id

# Check indexes exist:
# CREATE INDEX idx_memory_tags_memory ON memory_tags(memory_id);
# CREATE INDEX idx_memory_tags_tag ON memory_tags(tag_id);
```

### LIMIT Clauses

Always provide LIMIT for unbounded queries:

```python
# Good: Bounded result set
memory query --limit=100

# Bad: Could return 100k rows
memory query  # No limit
```

---

## Security

### SQL Injection

Always use parameterized queries:

```python
# ✅ Good: Parameterized
conn.execute("SELECT * FROM memories WHERE project = ?", (project,))

# ❌ Bad: String formatting (SQL injection risk)
conn.execute(f"SELECT * FROM memories WHERE project = '{project}'")
```

### Input Validation

```python
# Validate types
if not isinstance(args.limit, int) or args.limit < 1:
    print("Error: limit must be a positive integer")
    return

# Validate dates
try:
    datetime.strptime(args.since, '%Y-%m-%d')
except ValueError:
    print("Error: since must be YYYY-MM-DD format")
    return
```

---

## Versioning

### Schema Versioning (Future)

```python
# In init_db(), track schema version
conn.execute("""
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY,
        applied_at DATETIME DEFAULT (datetime('now'))
    )
""")

# Migration system
MIGRATIONS = {
    1: migrate_to_relational_tags,
    2: add_tag_metadata,
    3: add_memory_relationships,
}

def get_schema_version(conn):
    cursor = conn.execute("SELECT MAX(version) FROM schema_version")
    row = cursor.fetchone()
    return row[0] if row[0] else 0

def apply_migrations(conn):
    current = get_schema_version(conn)
    for version in sorted(MIGRATIONS.keys()):
        if version > current:
            print(f"Applying migration {version}...")
            MIGRATIONS[version](conn)
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            conn.commit()
```

---

## Release Checklist

Before releasing a new version:

- [ ] Update version number in documentation
- [ ] Test all commands manually
- [ ] Run automated tests (when available)
- [ ] Check performance with large dataset (10k+ memories)
- [ ] Update CHANGELOG.md
- [ ] Update USER-GUIDE.md with new features
- [ ] Update ARCHITECTURE.md if schema changed
- [ ] Test migration from previous version
- [ ] Backup test database before migration
- [ ] Tag release in git

---

## Common Patterns

### Adding a Filter

```python
# 1. Add to argparse
parser.add_argument("--new-filter", help="Description")

# 2. Add to query building
if args.new_filter:
    where_clauses.append("m.column = ?")
    params.append(args.new_filter)

# 3. Document in USER-GUIDE.md
```

### Adding a Table

```python
# 1. Add in init_db()
conn.execute("""
    CREATE TABLE IF NOT EXISTS new_table (
        id INTEGER PRIMARY KEY,
        ...
    )
""")

# 2. Add indexes
conn.execute("CREATE INDEX IF NOT EXISTS idx_... ON new_table(...)")

# 3. Add foreign keys if needed
# 4. Document in ARCHITECTURE.md
```

### Adding Output Formatting

```python
# Keep emoji consistent:
# 🕒 = timestamp
# 👤 = agent
# 📂 = project
# 🏷️  = tags
# 📊 = stats
# ✓ = success
# 🔍 = search

# Example:
print(f"🕒 {timestamp}")
print(f"👤 {agent}")
```

---

## Questions?

For questions or suggestions:
- Open an issue on GitHub (when available)
- Check existing documentation
- Ask in the project chat
- Contact maintainers

---

## License

[TBD - Add license information]

---

**Thank you for contributing!** 🙏
