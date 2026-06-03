# Memory CLI - API Reference

Programmatic access to Memory CLI from Python scripts, other tools, or future HTTP API.

---

## Python API (Direct SQLite)

For advanced use cases, you can access the SQLite database directly from Python.

### Setup

```python
import sqlite3
from pathlib import Path
from datetime import datetime

DB_PATH = Path.home() / "workspace" / "agents" / "memory.db"

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row  # Access by column name
```

---

## Basic Operations

### Add a Memory

```python
def add_memory(content, agent="script", project=None, tags=None, mem_type=None):
    """Add a memory programmatically"""
    conn = sqlite3.connect(DB_PATH)
    
    # Insert memory
    cursor = conn.execute("""
        INSERT INTO memories (agent, project, content, type)
        VALUES (?, ?, ?, ?)
    """, (agent, project, content, mem_type))
    
    memory_id = cursor.lastrowid
    
    # Add tags
    if tags:
        for tag_name in tags:
            # Get or create tag
            cursor = conn.execute(
                "SELECT id FROM tags WHERE name = ? COLLATE NOCASE",
                (tag_name,)
            )
            row = cursor.fetchone()
            
            if row:
                tag_id = row[0]
            else:
                cursor = conn.execute("INSERT INTO tags (name) VALUES (?)", (tag_name,))
                tag_id = cursor.lastrowid
            
            # Link memory to tag
            conn.execute(
                "INSERT INTO memory_tags (memory_id, tag_id) VALUES (?, ?)",
                (memory_id, tag_id)
            )
    
    conn.commit()
    conn.close()
    
    return memory_id

# Usage
memory_id = add_memory(
    "Automated backup completed",
    agent="backup-script",
    project="infrastructure",
    tags=["backup", "automated", "success"],
    mem_type="note"
)
print(f"Memory #{memory_id} added")
```

---

### Query Memories

```python
def query_memories(project=None, tag=None, since=None, limit=10):
    """Query memories with filters"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    
    where_clauses = []
    params = []
    joins = []
    
    if project:
        where_clauses.append("m.project = ?")
        params.append(project)
    
    if tag:
        joins.append("JOIN memory_tags mt ON m.id = mt.memory_id")
        joins.append("JOIN tags t ON mt.tag_id = t.id")
        where_clauses.append("t.name = ? COLLATE NOCASE")
        params.append(tag)
    
    if since:
        where_clauses.append("m.timestamp >= ?")
        params.append(since)
    
    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
    join_sql = " ".join(joins)
    
    query = f"""
        SELECT DISTINCT m.id, m.timestamp, m.agent, m.project, m.content, m.type
        FROM memories m
        {join_sql}
        WHERE {where_sql}
        ORDER BY m.timestamp DESC
        LIMIT ?
    """
    
    params.append(limit)
    
    cursor = conn.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    
    return [dict(row) for row in rows]

# Usage
memories = query_memories(project="myapp", tag="bugfix", limit=5)
for mem in memories:
    print(f"{mem['timestamp']}: {mem['content']}")
```

---

### Search Memories

```python
def search_memories(query_text, project=None, limit=20):
    """Full-text search"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    
    where_clauses = ["m.id = memories_fts.rowid", "memories_fts MATCH ?"]
    params = [query_text]
    
    if project:
        where_clauses.append("m.project = ?")
        params.append(project)
    
    where_sql = " AND ".join(where_clauses)
    
    query = f"""
        SELECT m.id, m.timestamp, m.agent, m.project, m.content, m.type,
               snippet(memories_fts, -1, '→ ', ' ←', '...', 32) as snippet
        FROM memories_fts
        JOIN memories m ON m.id = memories_fts.rowid
        WHERE {where_sql}
        ORDER BY rank
        LIMIT ?
    """
    
    params.append(limit)
    
    cursor = conn.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    
    return [dict(row) for row in rows]

# Usage
results = search_memories("authentication bug", project="myapp")
for result in results:
    print(f"{result['timestamp']}: {result['snippet']}")
```

---

### Get Memory Tags

```python
def get_memory_tags(memory_id):
    """Get tags for a memory"""
    conn = sqlite3.connect(DB_PATH)
    
    cursor = conn.execute("""
        SELECT t.name
        FROM tags t
        JOIN memory_tags mt ON t.id = mt.tag_id
        WHERE mt.memory_id = ?
        ORDER BY t.name
    """, (memory_id,))
    
    tags = [row[0] for row in cursor.fetchall()]
    conn.close()
    
    return tags

# Usage
tags = get_memory_tags(42)
print(f"Tags: {', '.join(tags)}")
```

---

### Get Statistics

```python
def get_stats():
    """Get database statistics"""
    conn = sqlite3.connect(DB_PATH)
    
    stats = {}
    
    stats['total'] = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    stats['agents'] = conn.execute("SELECT COUNT(DISTINCT agent) FROM memories").fetchone()[0]
    stats['projects'] = conn.execute("SELECT COUNT(DISTINCT project) FROM memories WHERE project IS NOT NULL").fetchone()[0]
    stats['tags'] = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
    
    stats['today'] = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE DATE(timestamp, 'localtime') = DATE('now', 'localtime')"
    ).fetchone()[0]
    
    stats['last_7_days'] = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE timestamp >= DATE('now', '-7 days')"
    ).fetchone()[0]
    
    oldest = conn.execute("SELECT MIN(timestamp) FROM memories").fetchone()[0]
    newest = conn.execute("SELECT MAX(timestamp) FROM memories").fetchone()[0]
    
    stats['oldest'] = oldest
    stats['newest'] = newest
    
    conn.close()
    
    return stats

# Usage
stats = get_stats()
print(f"Total memories: {stats['total']}")
print(f"Today: {stats['today']}")
```

---

## Advanced Queries

### Memories by Tag Usage

```python
def most_tagged_memories(tag, limit=10):
    """Get memories with most tags, filtered by a specific tag"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    
    cursor = conn.execute("""
        SELECT m.id, m.content, COUNT(mt.tag_id) as tag_count
        FROM memories m
        JOIN memory_tags mt ON m.id = mt.memory_id
        JOIN tags t ON mt.tag_id = t.id
        WHERE t.name = ? COLLATE NOCASE
        GROUP BY m.id
        ORDER BY tag_count DESC
        LIMIT ?
    """, (tag, limit))
    
    rows = cursor.fetchall()
    conn.close()
    
    return [dict(row) for row in rows]
```

---

### Tag Co-occurrence

```python
def tag_cooccurrence(tag, limit=10):
    """Find tags that frequently appear with a given tag"""
    conn = sqlite3.connect(DB_PATH)
    
    cursor = conn.execute("""
        SELECT t2.name, COUNT(*) as count
        FROM memory_tags mt1
        JOIN tags t1 ON mt1.tag_id = t1.id
        JOIN memory_tags mt2 ON mt1.memory_id = mt2.memory_id
        JOIN tags t2 ON mt2.tag_id = t2.id
        WHERE t1.name = ? COLLATE NOCASE AND t1.id != t2.id
        GROUP BY t2.name
        ORDER BY count DESC
        LIMIT ?
    """, (tag, limit))
    
    rows = cursor.fetchall()
    conn.close()
    
    return [(row[0], row[1]) for row in rows]

# Usage
related = tag_cooccurrence("auth")
print("Tags that often appear with 'auth':")
for tag, count in related:
    print(f"  {tag}: {count} times")
```

---

### Trending Tags (Last 7 Days)

```python
def trending_tags(limit=10):
    """Get most-used tags in last 7 days"""
    conn = sqlite3.connect(DB_PATH)
    
    cursor = conn.execute("""
        SELECT t.name, COUNT(*) as count
        FROM tags t
        JOIN memory_tags mt ON t.id = mt.tag_id
        JOIN memories m ON mt.memory_id = m.id
        WHERE m.timestamp >= DATE('now', '-7 days')
        GROUP BY t.name
        ORDER BY count DESC
        LIMIT ?
    """, (limit,))
    
    rows = cursor.fetchall()
    conn.close()
    
    return [(row[0], row[1]) for row in rows]

# Usage
trending = trending_tags(10)
print("Trending tags (last 7 days):")
for tag, count in trending:
    print(f"  {tag}: {count}")
```

---

### Agent Activity

```python
def agent_activity(agent, days=7):
    """Get activity stats for an agent"""
    conn = sqlite3.connect(DB_PATH)
    
    stats = {}
    
    # Total memories
    stats['total'] = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE agent = ?",
        (agent,)
    ).fetchone()[0]
    
    # Recent activity
    stats['last_n_days'] = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE agent = ? AND timestamp >= DATE('now', ? || ' days')",
        (agent, f'-{days}')
    ).fetchone()[0]
    
    # Most used tags
    cursor = conn.execute("""
        SELECT t.name, COUNT(*) as count
        FROM memories m
        JOIN memory_tags mt ON m.id = mt.memory_id
        JOIN tags t ON mt.tag_id = t.id
        WHERE m.agent = ?
        GROUP BY t.name
        ORDER BY count DESC
        LIMIT 5
    """, (agent,))
    
    stats['top_tags'] = [(row[0], row[1]) for row in cursor.fetchall()]
    
    # Projects
    cursor = conn.execute("""
        SELECT project, COUNT(*) as count
        FROM memories
        WHERE agent = ? AND project IS NOT NULL
        GROUP BY project
        ORDER BY count DESC
    """, (agent,))
    
    stats['projects'] = [(row[0], row[1]) for row in cursor.fetchall()]
    
    conn.close()
    
    return stats

# Usage
activity = agent_activity("agent-a", days=7)
print(f"agent-a's activity (last 7 days): {activity['last_n_days']} memories")
print(f"Top tags: {activity['top_tags']}")
```

---

## Bulk Operations

### Bulk Import

```python
def bulk_import(memories):
    """
    Import multiple memories at once
    
    Args:
        memories: List of dicts with keys: content, agent, project, tags, type
    """
    conn = sqlite3.connect(DB_PATH)
    
    for mem in memories:
        # Insert memory
        cursor = conn.execute("""
            INSERT INTO memories (agent, project, content, type)
            VALUES (?, ?, ?, ?)
        """, (
            mem.get('agent', 'import'),
            mem.get('project'),
            mem['content'],
            mem.get('type')
        ))
        
        memory_id = cursor.lastrowid
        
        # Add tags
        if 'tags' in mem:
            for tag_name in mem['tags']:
                # Get or create tag
                cursor = conn.execute(
                    "SELECT id FROM tags WHERE name = ? COLLATE NOCASE",
                    (tag_name,)
                )
                row = cursor.fetchone()
                
                if row:
                    tag_id = row[0]
                else:
                    cursor = conn.execute("INSERT INTO tags (name) VALUES (?)", (tag_name,))
                    tag_id = cursor.lastrowid
                
                # Link
                conn.execute(
                    "INSERT INTO memory_tags (memory_id, tag_id) VALUES (?, ?)",
                    (memory_id, tag_id)
                )
    
    conn.commit()
    conn.close()

# Usage
memories_to_import = [
    {
        'content': 'Memory 1',
        'agent': 'importer',
        'project': 'myapp',
        'tags': ['import', 'test'],
        'type': 'note'
    },
    {
        'content': 'Memory 2',
        'agent': 'importer',
        'project': 'myapp',
        'tags': ['import', 'test'],
        'type': 'note'
    },
]

bulk_import(memories_to_import)
```

---

### Export to JSON

```python
import json

def export_to_json(filename, project=None):
    """Export memories to JSON"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    
    where = "WHERE project = ?" if project else ""
    params = [project] if project else []
    
    cursor = conn.execute(f"SELECT * FROM memories {where}", params)
    memories = []
    
    for row in cursor.fetchall():
        mem = dict(row)
        # Get tags
        mem['tags'] = get_memory_tags(mem['id'])
        memories.append(mem)
    
    conn.close()
    
    with open(filename, 'w') as f:
        json.dump(memories, f, indent=2, default=str)
    
    return len(memories)

# Usage
count = export_to_json('memories.json', project='myapp')
print(f"Exported {count} memories")
```

---

## Integration Examples

### Cron Job Integration

```python
#!/usr/bin/env python3
"""
Daily backup status logger
Run via cron: 0 2 * * * /path/to/backup_logger.py
"""

import subprocess
from pathlib import Path
import sys

sys.path.append(str(Path.home() / "workspace" / "agents"))
from memory_api import add_memory

# Run backup
result = subprocess.run(['rsync', '-av', '/data', '/backup'], capture_output=True)

if result.returncode == 0:
    add_memory(
        f"Daily backup completed successfully. {result.stdout.decode()[:100]}",
        agent="backup-cron",
        project="infrastructure",
        tags=["backup", "automated", "success"],
        mem_type="note"
    )
else:
    add_memory(
        f"Daily backup FAILED! Error: {result.stderr.decode()[:200]}",
        agent="backup-cron",
        project="infrastructure",
        tags=["backup", "automated", "failure", "urgent"],
        mem_type="note"
    )
```

---

### Git Hook Integration

```python
#!/usr/bin/env python3
"""
Post-commit hook to log commits to memory
Save to: .git/hooks/post-commit
"""

import subprocess
import sys
from pathlib import Path

sys.path.append(str(Path.home() / "workspace" / "agents"))
from memory_api import add_memory

# Get commit message
commit_msg = subprocess.check_output(['git', 'log', '-1', '--pretty=%B']).decode().strip()

# Get project name from repo
project = Path.cwd().name

add_memory(
    f"Committed: {commit_msg}",
    agent="git-hook",
    project=project,
    tags=["git", "commit", "automated"],
    mem_type="code"
)
```

---

### Monitoring Integration

```python
"""
Log application errors to memory
"""

def log_error_to_memory(error_msg, context=None):
    """Log application error"""
    from memory_api import add_memory
    
    content = f"Error: {error_msg}"
    if context:
        content += f"\nContext: {context}"
    
    add_memory(
        content,
        agent="app-monitor",
        project="myapp",
        tags=["error", "monitoring", "urgent"],
        mem_type="note"
    )

# Usage in application
try:
    # some code
    raise ValueError("Something went wrong")
except Exception as e:
    log_error_to_memory(str(e), context="user_login_handler")
    # ... handle error
```

---

## Future: HTTP API

**Planned features:**

```bash
# Start server
memory serve --port 8080

# REST endpoints
GET    /memories?project=myapp&tag=auth&limit=10
POST   /memories
PUT    /memories/:id
DELETE /memories/:id

GET    /search?q=authentication&project=myapp
GET    /tags
GET    /projects
GET    /stats

# WebSocket for real-time updates
WS     /ws
```

**Client example:**

```python
import requests

# Add memory via HTTP
response = requests.post('http://localhost:8080/memories', json={
    'content': 'Deployed to production',
    'agent': 'deploy-script',
    'project': 'myapp',
    'tags': ['deployment', 'production'],
    'type': 'note'
})

memory_id = response.json()['id']

# Query memories
response = requests.get('http://localhost:8080/memories', params={
    'project': 'myapp',
    'tag': 'deployment',
    'limit': 10
})

memories = response.json()['memories']
```

---

## Best Practices for Programmatic Use

### 1. **Connection Management**

```python
# Good: Use context manager (when available)
with sqlite3.connect(DB_PATH) as conn:
    conn.execute("...")

# Or: Close explicitly
conn = sqlite3.connect(DB_PATH)
try:
    conn.execute("...")
    conn.commit()
finally:
    conn.close()
```

---

### 2. **Error Handling**

```python
import sqlite3

try:
    add_memory("Test", tags=["test"])
except sqlite3.IntegrityError as e:
    print(f"Database integrity error: {e}")
except sqlite3.OperationalError as e:
    print(f"Database operation failed: {e}")
except Exception as e:
    print(f"Unexpected error: {e}")
```

---

### 3. **Batch Operations**

```python
# Good: Single transaction for multiple inserts
conn = sqlite3.connect(DB_PATH)
try:
    for item in items:
        conn.execute("INSERT ...", item)
    conn.commit()
finally:
    conn.close()

# Bad: Commit after each insert
for item in items:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT ...", item)
    conn.commit()
    conn.close()
```

---

### 4. **Parameterized Queries**

```python
# Good: Safe from SQL injection
conn.execute("SELECT * FROM memories WHERE project = ?", (project,))

# Bad: SQL injection risk
conn.execute(f"SELECT * FROM memories WHERE project = '{project}'")
```

---

## References

- [SQLite Python Documentation](https://docs.python.org/3/library/sqlite3.html)
- [Memory CLI Architecture](./ARCHITECTURE.md)
- [Memory CLI User Guide](./USER-GUIDE.md)

---

_For CLI usage, see [USER-GUIDE.md](./USER-GUIDE.md)._  
_For contributing, see [CONTRIBUTING.md](./CONTRIBUTING.md)._
