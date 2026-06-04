"""Storage layer.

Backend-agnostic memory operations behind a `MemoryStore` interface. `SqliteStore`
talks to SQLite directly; `ApiStore` talks to the FastAPI service over HTTP (#6).
`get_store()` selects between them. All methods return plain data (ids, dicts,
lists) in the same shapes regardless of backend; presentation lives in the CLI.
"""

import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path

from .config import load_config, resolve_db_path


class MemoryStore(ABC):
    """Backend-agnostic memory operations. Methods return plain data (ids, dicts,
    lists); all presentation lives in the CLI layer. SqliteStore implements this
    today; a remote ApiStore will implement the same contract later (#6)."""

    @abstractmethod
    def initialize(self): ...
    @abstractmethod
    def add(self, content, agent, project, tags, mtype): ...
    @abstractmethod
    def query(self, *, today=False, yesterday=False, since=None, until=None,
              project=None, agent=None, tag=None, mtype=None, limit=None): ...
    @abstractmethod
    def search(self, text, *, project=None, agent=None, since=None, tag=None, limit=None): ...
    @abstractmethod
    def get(self, mid): ...
    @abstractmethod
    def update(self, mid, *, new_content=None, project=None, mtype=None,
               set_tags=None, add_tags=None, remove_tags=None): ...
    @abstractmethod
    def get_many(self, ids): ...
    @abstractmethod
    def delete(self, ids): ...
    @abstractmethod
    def list_tags(self): ...
    @abstractmethod
    def list_projects(self): ...
    @abstractmethod
    def stats(self): ...


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

    def _migrate_json_tags(self):
        """Migrate old JSON tags column to relational tables"""
        conn = self._connect()
        columns = [row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()]
        if 'tags' not in columns:
            conn.close()
            return False  # Nothing to migrate

        print("🔄 Migrating JSON tags to relational schema...")
        rows = conn.execute("SELECT id, tags FROM memories WHERE tags IS NOT NULL").fetchall()
        migrated = 0
        for memory_id, tags_json in rows:
            try:
                for tag_name in json.loads(tags_json):
                    conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag_name,))
                    tag_id = conn.execute(
                        "SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (tag_name,)
                    ).fetchone()[0]
                    conn.execute(
                        "INSERT OR IGNORE INTO memory_tags (memory_id, tag_id) VALUES (?, ?)",
                        (memory_id, tag_id)
                    )
                migrated += 1
            except (json.JSONDecodeError, TypeError):
                continue
        conn.commit()
        print("🗑️  Removing old JSON tags column...")
        conn.execute("ALTER TABLE memories DROP COLUMN tags")
        conn.commit()
        conn.close()
        print(f"✓ Migrated {migrated} memories")
        return True

    def _migrate_fts_triggers(self, conn):
        """Replace the legacy unsafe FTS sync triggers (direct DELETE/UPDATE on the
        external-content FTS table) with the documented 'delete'-command pattern, and
        rebuild the index in case it was already corrupted. Idempotent. See #10."""
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='memories_au'"
        ).fetchone()
        if not row or not row[0]:
            return False  # no FTS trigger yet (fresh DB gets the correct ones from init)
        if "INSERT INTO memories_fts(memories_fts" in row[0]:
            return False  # already on the corrected pattern
        conn.executescript("""
            DROP TRIGGER IF EXISTS memories_ad;
            DROP TRIGGER IF EXISTS memories_au;
            CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.id, old.content);
            END;
            CREATE TRIGGER memories_au AFTER UPDATE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.id, old.content);
                INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
            END;
            INSERT INTO memories_fts(memories_fts) VALUES('rebuild');
        """)
        conn.commit()
        return True

    def initialize(self):
        """Create/upgrade the DB as needed. Returns a list of status messages to show."""
        msgs = []
        if not self.db_path.exists():
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = self._connect()
            self._init_schema(conn)
            conn.close()
            msgs.append(f"✓ Initialized memory database at {self.db_path}")
        else:
            conn = self._connect()
            has_tags_table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='tags'"
            ).fetchone() is not None
            conn.close()

            if not has_tags_table:
                conn = self._connect()
                self._init_schema(conn)
                conn.close()
                if self._migrate_json_tags():
                    msgs.append("✓ Migration complete - now using relational schema")

            conn = self._connect()
            if self._migrate_fts_triggers(conn):
                msgs.append("✓ Upgraded FTS triggers and rebuilt search index")
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

    def query(self, *, today=False, yesterday=False, since=None, until=None,
              project=None, agent=None, tag=None, mtype=None, limit=None):
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        where, params, joins = [], [], []

        if today:
            where.append("DATE(m.timestamp, 'localtime') = DATE('now', 'localtime')")
        elif yesterday:
            where.append("DATE(m.timestamp, 'localtime') = DATE('now', '-1 day', 'localtime')")
        elif since:
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


class ApiStore(MemoryStore):
    """MemoryStore backed by the FastAPI service over HTTP (stdlib urllib only, so
    the client surface stays dependency-free). Translates each operation to a route
    and parses the JSON back into the exact shapes SqliteStore returns, so the CLI
    presentation layer is identical whether it talks to SQLite or the service."""

    # Remote: there is no local DB file to guard/create (see cli.ensure_db_or_confirm).
    db_path = None

    def __init__(self, base_url, token=None, timeout=30):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    # ---- HTTP plumbing ---------------------------------------------------
    def _call(self, method, path, *, params=None, body=None):
        """Returns (status_code, parsed_json|None). Raises on transport errors and
        unexpected HTTP statuses; 404 is returned to the caller to handle."""
        url = self.base_url + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean, doseq=True)
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return 404, None
            detail = e.read().decode(errors="replace")
            raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {detail}") from e

    # ---- operations ------------------------------------------------------
    def initialize(self):
        return []  # the server owns schema creation/migration.

    def add(self, content, agent, project, tags, mtype):
        _, data = self._call("POST", "/memories", body={
            "content": content, "agent": agent, "project": project,
            "tags": list(tags), "type": mtype,
        })
        return data["id"]

    def query(self, *, today=False, yesterday=False, since=None, until=None,
              project=None, agent=None, tag=None, mtype=None, limit=None):
        params = {
            "today": "true" if today else None,
            "yesterday": "true" if yesterday else None,
            "since": since, "until": until, "project": project,
            "agent": agent, "tag": tag, "type": mtype, "limit": limit,
        }
        _, data = self._call("GET", "/memories", params=params)
        return data or []

    def search(self, text, *, project=None, agent=None, since=None, tag=None, limit=None):
        params = {"q": text, "project": project, "agent": agent,
                  "since": since, "tag": tag, "limit": limit}
        _, data = self._call("GET", "/memories/search", params=params)
        return data or []

    def get(self, mid):
        status, data = self._call("GET", f"/memories/{mid}")
        return None if status == 404 else data

    def update(self, mid, *, new_content=None, project=None, mtype=None,
               set_tags=None, add_tags=None, remove_tags=None):
        body = {}
        if new_content is not None:
            body["content"] = new_content
        if project is not None:
            body["project"] = project
        if mtype is not None:
            body["type"] = mtype
        if set_tags is not None:
            body["set_tags"] = set_tags
        if add_tags is not None:
            body["add_tags"] = add_tags
        if remove_tags is not None:
            body["remove_tags"] = remove_tags
        status, data = self._call("PATCH", f"/memories/{mid}", body=body)
        return None if status == 404 else data["changes"]

    def get_many(self, ids):
        # No bulk endpoint; fetch each (delete previews a handful of ids).
        out = []
        for i in ids:
            row = self.get(i)
            if row is not None:
                out.append(row)
        return out

    def delete(self, ids):
        self._call("DELETE", "/memories", params={"ids": list(ids)})

    def list_tags(self):
        _, data = self._call("GET", "/tags")
        return [(t["name"], t["count"]) for t in (data or [])]

    def list_projects(self):
        _, data = self._call("GET", "/projects")
        return [(p["project"], p["count"]) for p in (data or [])]

    def stats(self):
        _, data = self._call("GET", "/stats")
        return data


def get_store():
    """Resolve the active MemoryStore backend.

    Precedence: AGENT_MEMORY_API (env, else config `api_url`) selects the remote
    ApiStore; otherwise SQLite at the resolved path (AGENT_MEMORY_DB → config
    `db_path` → XDG default). The API token comes from AGENT_MEMORY_API_TOKEN,
    else config `api_token`."""
    api = os.environ.get("AGENT_MEMORY_API") or load_config().get("api_url")
    if api:
        token = os.environ.get("AGENT_MEMORY_API_TOKEN") or load_config().get("api_token")
        return ApiStore(api, token)
    return SqliteStore(resolve_db_path())
