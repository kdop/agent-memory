"""SQLite backend.

`SqliteStore` implements the backend-agnostic `MemoryStore` contract (defined in
`store.py`) against a local SQLite file — it owns the schema, migrations, and all
SQL. Kept in its own module (mirroring `pg_store.py`) so `store.py` stays the thin
seam that only defines the contract, the remote `ApiStore`, and `get_store()`.

Stdlib-only (rule #3): the client surface must stay dependency-free.
"""

import sqlite3
from pathlib import Path

from .store import MemoryStore, since_days_window


class SqliteStore(MemoryStore):
    """SQLite-backed MemoryStore. Owns the schema, migrations, and all SQL."""

    def __init__(self, db_path):
        self.db_path = Path(db_path)

    # ---- connections / schema -------------------------------------------
    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _init_schema(self, conn):
        conn.execute("PRAGMA journal_mode=WAL")  # Better concurrency
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT (datetime('now', 'localtime')),
                agent TEXT NOT NULL,
                project TEXT,
                content TEXT NOT NULL,
                type TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL COLLATE NOCASE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_tags (
                memory_id INTEGER NOT NULL,
                tag_id INTEGER NOT NULL,
                PRIMARY KEY (memory_id, tag_id),
                FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE,
                FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                content,
                content=memories,
                content_rowid=id
            )
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
            END
        """)
        # External-content FTS5 tables must be synced via the special 'delete'
        # command — a naive DELETE/UPDATE on the FTS table corrupts the index (#10).
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.id, old.content);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.id, old.content);
                INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
            END
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON memories(timestamp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_project ON memories(project)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent ON memories(agent)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_type ON memories(type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_tags_memory ON memory_tags(memory_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_tags_tag ON memory_tags(tag_id)")
        conn.commit()

    def initialize(self):
        """Create the DB and schema if missing. Returns a list of status messages."""
        msgs = []
        if not self.db_path.exists():
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = self._connect()
            self._init_schema(conn)
            conn.close()
            msgs.append(f"✓ Initialized memory database at {self.db_path}")
        else:
            # Existing file with no schema yet (edge case) still gets the tables;
            # _init_schema is idempotent (all CREATE ... IF NOT EXISTS).
            conn = self._connect()
            has_tags_table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='tags'"
            ).fetchone() is not None
            if not has_tags_table:
                self._init_schema(conn)
            conn.close()
        return msgs

    # ---- internal helpers -----------------------------------------------
    def _get_or_create_tag(self, conn, tag_name):
        row = conn.execute(
            "SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (tag_name,)
        ).fetchone()
        if row:
            return row[0]
        return conn.execute("INSERT INTO tags (name) VALUES (?)", (tag_name,)).lastrowid

    def _tags_for(self, conn, memory_id):
        return [r[0] for r in conn.execute("""
            SELECT t.name
            FROM tags t
            JOIN memory_tags mt ON t.id = mt.tag_id
            WHERE mt.memory_id = ?
            ORDER BY t.name
        """, (memory_id,)).fetchall()]

    # ---- operations ------------------------------------------------------
    def add(self, content, agent, project, tags, mtype):
        conn = self._connect()
        mid = conn.execute(
            "INSERT INTO memories (agent, project, content, type) VALUES (?, ?, ?, ?)",
            (agent, project, content, mtype)
        ).lastrowid
        for tag_name in tags:
            tag_id = self._get_or_create_tag(conn, tag_name)
            conn.execute("INSERT INTO memory_tags (memory_id, tag_id) VALUES (?, ?)", (mid, tag_id))
        conn.commit()
        conn.close()
        return mid

    def query(self, *, since_days=None, since=None, until=None,
              project=None, agent=None, tag=None, mtype=None, limit=None):
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        where, params, joins = [], [], []

        if since_days is not None:
            since, until = since_days_window(since_days)  # overrides any user since/until
        if since:
            where.append("m.timestamp >= ?"); params.append(since)
        if until:
            where.append("m.timestamp <= ?"); params.append(until)
        if project:
            where.append("m.project = ?"); params.append(project)
        if agent:
            where.append("m.agent = ?"); params.append(agent)
        if mtype:
            where.append("m.type = ?"); params.append(mtype)
        if tag:
            joins.append("JOIN memory_tags mt ON m.id = mt.memory_id")
            joins.append("JOIN tags t ON mt.tag_id = t.id")
            where.append("t.name = ? COLLATE NOCASE"); params.append(tag)

        where_sql = " AND ".join(where) if where else "1=1"
        join_sql = " ".join(joins)
        limit_sql = f"LIMIT {int(limit)}" if limit else ""
        rows = conn.execute(f"""
            SELECT DISTINCT m.id, m.timestamp, m.agent, m.project, m.content, m.type
            FROM memories m
            {join_sql}
            WHERE {where_sql}
            ORDER BY m.timestamp DESC
            {limit_sql}
        """, params).fetchall()
        out = [self._with_tags(conn, r) for r in rows]
        conn.close()
        return out

    def search(self, text, *, project=None, agent=None, since=None, tag=None, limit=None):
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        where = ["m.id = memories_fts.rowid", "memories_fts MATCH ?"]
        params = [text]
        joins = []
        if project:
            where.append("m.project = ?"); params.append(project)
        if agent:
            where.append("m.agent = ?"); params.append(agent)
        if since:
            where.append("m.timestamp >= ?"); params.append(since)
        if tag:
            joins.append("JOIN memory_tags mt ON m.id = mt.memory_id")
            joins.append("JOIN tags t ON mt.tag_id = t.id")
            where.append("t.name = ? COLLATE NOCASE"); params.append(tag)

        where_sql = " AND ".join(where)
        join_sql = " ".join(joins)
        limit_sql = f"LIMIT {int(limit)}" if limit else ""
        rows = conn.execute(f"""
            SELECT DISTINCT m.id, m.timestamp, m.agent, m.project, m.content, m.type,
                   snippet(memories_fts, -1, '→ ', ' ←', '...', 32) as snippet
            FROM memories_fts
            JOIN memories m ON m.id = memories_fts.rowid
            {join_sql}
            WHERE {where_sql}
            ORDER BY rank
            {limit_sql}
        """, params).fetchall()
        out = []
        for r in rows:
            d = self._with_tags(conn, r)
            d["snippet"] = r["snippet"]
            out.append(d)
        conn.close()
        return out

    def get(self, mid):
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, timestamp, agent, project, content, type FROM memories WHERE id = ?",
            (mid,)
        ).fetchone()
        out = self._with_tags(conn, row) if row else None
        conn.close()
        return out

    def update(self, mid, *, new_content=None, project=None, mtype=None,
               set_tags=None, add_tags=None, remove_tags=None):
        """Apply field updates; returns a list of human-readable change descriptions
        (empty if nothing changed), or None if the memory does not exist."""
        conn = self._connect()
        conn.execute("PRAGMA foreign_keys = ON")
        row = conn.execute("SELECT content FROM memories WHERE id = ?", (mid,)).fetchone()
        if not row:
            conn.close()
            return None
        existing_content = row[0]
        changes = []

        if new_content is not None and new_content != existing_content:
            conn.execute("UPDATE memories SET content = ? WHERE id = ?", (new_content, mid))
            changes.append("content")
        if project is not None:
            project_val = project if project != "" else None
            conn.execute("UPDATE memories SET project = ? WHERE id = ?", (project_val, mid))
            changes.append(f"project → {project_val}")
        if mtype is not None:
            type_val = mtype if mtype != "" else None
            conn.execute("UPDATE memories SET type = ? WHERE id = ?", (type_val, mid))
            changes.append(f"type → {type_val}")
        if set_tags is not None:
            conn.execute("DELETE FROM memory_tags WHERE memory_id = ?", (mid,))
            new_tags = [t.strip() for t in set_tags.split(",") if t.strip()]
            for tag_name in new_tags:
                tag_id = self._get_or_create_tag(conn, tag_name)
                conn.execute("INSERT INTO memory_tags (memory_id, tag_id) VALUES (?, ?)", (mid, tag_id))
            changes.append(f"tags set to: {', '.join(new_tags) if new_tags else '(none)'}")
        if add_tags:
            added = [t.strip() for t in add_tags.split(",") if t.strip()]
            for tag_name in added:
                tag_id = self._get_or_create_tag(conn, tag_name)
                conn.execute("INSERT OR IGNORE INTO memory_tags (memory_id, tag_id) VALUES (?, ?)", (mid, tag_id))
            changes.append(f"+tags: {', '.join(added)}")
        if remove_tags:
            removed = [t.strip() for t in remove_tags.split(",") if t.strip()]
            for tag_name in removed:
                conn.execute("""
                    DELETE FROM memory_tags
                    WHERE memory_id = ?
                      AND tag_id IN (SELECT id FROM tags WHERE name = ? COLLATE NOCASE)
                """, (mid, tag_name))
            changes.append(f"-tags: {', '.join(removed)}")

        if changes:
            conn.commit()
        conn.close()
        return changes

    def get_many(self, ids):
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT id, agent, project, type, content FROM memories WHERE id IN ({placeholders})",
            ids
        ).fetchall()
        out = [dict(r) for r in rows]
        conn.close()
        return out

    def delete(self, ids):
        conn = self._connect()
        conn.execute("PRAGMA foreign_keys = ON")
        placeholders = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", ids)
        conn.commit()
        conn.close()

    def list_tags(self):
        conn = self._connect()
        rows = conn.execute("""
            SELECT t.name, COUNT(mt.memory_id) as count
            FROM tags t
            LEFT JOIN memory_tags mt ON t.id = mt.tag_id
            GROUP BY t.id, t.name
            ORDER BY count DESC, t.name
        """).fetchall()
        conn.close()
        return rows

    def list_projects(self):
        conn = self._connect()
        rows = conn.execute("""
            SELECT project, COUNT(*) as count
            FROM memories
            WHERE project IS NOT NULL
            GROUP BY project
            ORDER BY count DESC
        """).fetchall()
        conn.close()
        return rows

    def stats(self):
        conn = self._connect()
        s = {
            "total": conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0],
            "agents": conn.execute("SELECT COUNT(DISTINCT agent) FROM memories").fetchone()[0],
            "projects": conn.execute("SELECT COUNT(DISTINCT project) FROM memories WHERE project IS NOT NULL").fetchone()[0],
            "tags": conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0],
            "today": conn.execute("SELECT COUNT(*) FROM memories WHERE DATE(timestamp, 'localtime') = DATE('now', 'localtime')").fetchone()[0],
            "week": conn.execute("SELECT COUNT(*) FROM memories WHERE timestamp >= DATE('now', '-7 days')").fetchone()[0],
            "oldest": conn.execute("SELECT MIN(timestamp) FROM memories").fetchone()[0],
            "newest": conn.execute("SELECT MAX(timestamp) FROM memories").fetchone()[0],
        }
        conn.close()
        return s

    def _with_tags(self, conn, row):
        d = dict(row)
        d["tags"] = self._tags_for(conn, row["id"])
        return d
