"""PostgreSQL backend (Ticket F, Phase 2b).

`PostgresStore` implements the same `MemoryStore` contract as `SqliteStore`, behind
the same `get_store()` seam (selected when the DB target is a `postgresql://` DSN).
The one real divergence — full-text search — is handled here: a generated `tsvector`
column + GIN index replace SQLite's external-content FTS5, and `ts_headline` replaces
`snippet()`. Because the tsvector is a GENERATED column, it stays in sync on every
insert/update/delete with no triggers.

Lives in the `[postgres]` extra (psycopg); imported lazily so the stdlib client never
needs it. Also provides `migrate_sqlite_to_postgres()`.
"""

import sqlite3

import psycopg
from psycopg.rows import dict_row

from .store import MemoryStore, since_days_window

# Highlight markers match SqliteStore.search() so output is identical across engines.
_HEADLIGHT = "StartSel=→ , StopSel= ←, MaxWords=32, MinWords=1, ShortWord=0, HighlightAll=FALSE"


class PostgresStore(MemoryStore):
    """PostgreSQL-backed MemoryStore. Owns the schema and all SQL."""

    db_path = None  # not a local file; the missing-DB CLI guard does not apply.

    def __init__(self, dsn):
        self.dsn = dsn

    def _connect(self):
        return psycopg.connect(self.dsn)

    # ---- schema ----------------------------------------------------------
    def initialize(self):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id BIGSERIAL PRIMARY KEY,
                    timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),
                    agent TEXT NOT NULL,
                    project TEXT,
                    content TEXT NOT NULL,
                    type TEXT,
                    content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tags (
                    id BIGSERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT ''
                )
            """)
            # Pre-existing tags table without the required descriptor: add + backfill
            # each tag's own name as a placeholder descriptor (mirrors SqliteStore).
            has_desc = cur.execute("""
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'tags' AND column_name = 'description'
            """).fetchone() is not None
            if not has_desc:
                cur.execute("ALTER TABLE tags ADD COLUMN description TEXT NOT NULL DEFAULT ''")
                cur.execute("UPDATE tags SET description = name WHERE description = ''")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS memory_tags (
                    memory_id BIGINT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                    tag_id BIGINT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                    PRIMARY KEY (memory_id, tag_id)
                )
            """)
            # Case-insensitive uniqueness, mirroring SQLite's COLLATE NOCASE on tag names.
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tags_lower_name ON tags (lower(name))")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_content_tsv ON memories USING GIN (content_tsv)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON memories(timestamp)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_project ON memories(project)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_agent ON memories(agent)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_type ON memories(type)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_memory_tags_tag ON memory_tags(tag_id)")
        return []

    # ---- helpers ---------------------------------------------------------
    def _get_or_create_tag(self, cur, name, description=None):
        """Resolve a tag id. An existing tag is reused (descriptor updated only if a
        new non-empty one is given); a brand-new tag with no descriptor auto-defaults
        to its own name. Mirrors SqliteStore."""
        row = cur.execute(
            "SELECT id, description FROM tags WHERE lower(name) = lower(%s)", (name,)
        ).fetchone()
        if row:
            tag_id, existing = row
            if description and description != existing:
                cur.execute("UPDATE tags SET description = %s WHERE id = %s", (description, tag_id))
            return tag_id
        return cur.execute(
            "INSERT INTO tags (name, description) VALUES (%s, %s) RETURNING id",
            (name, description or name),
        ).fetchone()[0]

    @staticmethod
    def _tag_fields(item):
        if isinstance(item, dict):
            return item.get("name", "").strip(), (item.get("description") or None)
        return str(item).strip(), None

    def _tags_for(self, cur, memory_id):
        return [r[0] for r in cur.execute("""
            SELECT t.name FROM tags t
            JOIN memory_tags mt ON t.id = mt.tag_id
            WHERE mt.memory_id = %s ORDER BY t.name
        """, (memory_id,)).fetchall()]

    @staticmethod
    def _row_to_dict(row, tags):
        d = dict(row)
        ts = d.get("timestamp")
        if ts is not None:
            d["timestamp"] = ts.isoformat(sep=" ", timespec="seconds")
        d["tags"] = tags
        return d

    # ---- operations ------------------------------------------------------
    def add(self, content, agent, project, tags, mtype):
        with self._connect() as conn, conn.cursor() as cur:
            mid = cur.execute(
                "INSERT INTO memories (agent, project, content, type) VALUES (%s,%s,%s,%s) RETURNING id",
                (agent, project, content, mtype),
            ).fetchone()[0]
            for item in tags:
                name, description = self._tag_fields(item)
                if not name:
                    continue
                tag_id = self._get_or_create_tag(cur, name, description)
                cur.execute(
                    "INSERT INTO memory_tags (memory_id, tag_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                    (mid, tag_id),
                )
        return mid

    def query(self, *, since_days=None, since=None, until=None,
              project=None, agent=None, tag=None, mtype=None, limit=None):
        where, params, joins = [], [], []
        if since_days is not None:
            since, until = since_days_window(since_days)  # overrides any user since/until
        if since:
            where.append("m.timestamp >= %s"); params.append(since)
        if until:
            where.append("m.timestamp <= %s"); params.append(until)
        if project:
            where.append("m.project = %s"); params.append(project)
        if agent:
            where.append("m.agent = %s"); params.append(agent)
        if mtype:
            where.append("m.type = %s"); params.append(mtype)
        if tag:
            joins.append("JOIN memory_tags mt ON m.id = mt.memory_id")
            joins.append("JOIN tags t ON mt.tag_id = t.id")
            where.append("lower(t.name) = lower(%s)"); params.append(tag)

        where_sql = " AND ".join(where) if where else "TRUE"
        join_sql = " ".join(joins)
        limit_sql = f"LIMIT {int(limit)}" if limit else ""
        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                rows = cur.execute(f"""
                    SELECT DISTINCT m.id, m.timestamp, m.agent, m.project, m.content, m.type
                    FROM memories m {join_sql}
                    WHERE {where_sql}
                    ORDER BY m.timestamp DESC {limit_sql}
                """, params).fetchall()
            with conn.cursor() as tcur:
                return [self._row_to_dict(r, self._tags_for(tcur, r["id"])) for r in rows]

    def search(self, text, *, project=None, agent=None, since=None, tag=None, limit=None):
        where = ["m.content_tsv @@ plainto_tsquery('english', %s)"]
        params = [text]
        joins = []
        if project:
            where.append("m.project = %s"); params.append(project)
        if agent:
            where.append("m.agent = %s"); params.append(agent)
        if since:
            where.append("m.timestamp >= %s"); params.append(since)
        if tag:
            joins.append("JOIN memory_tags mt ON m.id = mt.memory_id")
            joins.append("JOIN tags t ON mt.tag_id = t.id")
            where.append("lower(t.name) = lower(%s)"); params.append(tag)

        where_sql = " AND ".join(where)
        join_sql = " ".join(joins)
        limit_sql = f"LIMIT {int(limit)}" if limit else ""
        # plainto_tsquery appears three times (snippet, filter, rank) — same bind each.
        bind = [text] + params + [text]
        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                rows = cur.execute(f"""
                    SELECT DISTINCT m.id, m.timestamp, m.agent, m.project, m.content, m.type,
                        ts_headline('english', m.content, plainto_tsquery('english', %s),
                                    '{_HEADLIGHT}') AS snippet,
                        ts_rank(m.content_tsv, plainto_tsquery('english', %s)) AS rank
                    FROM memories m {join_sql}
                    WHERE {where_sql}
                    ORDER BY rank DESC {limit_sql}
                """, bind).fetchall()
            with conn.cursor() as tcur:
                out = []
                for r in rows:
                    snippet = r.pop("snippet")
                    r.pop("rank", None)
                    d = self._row_to_dict(r, self._tags_for(tcur, r["id"]))
                    d["snippet"] = snippet
                    out.append(d)
                return out

    def get(self, mid):
        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                row = cur.execute(
                    "SELECT id, timestamp, agent, project, content, type FROM memories WHERE id = %s",
                    (mid,),
                ).fetchone()
            if not row:
                return None
            with conn.cursor() as tcur:
                return self._row_to_dict(row, self._tags_for(tcur, row["id"]))

    def update(self, mid, *, new_content=None, project=None, mtype=None,
               set_tags=None, add_tags=None, remove_tags=None):
        with self._connect() as conn, conn.cursor() as cur:
            row = cur.execute("SELECT content FROM memories WHERE id = %s", (mid,)).fetchone()
            if not row:
                return None
            existing = row[0]
            changes = []
            if new_content is not None and new_content != existing:
                cur.execute("UPDATE memories SET content = %s WHERE id = %s", (new_content, mid))
                changes.append("content")
            if project is not None:
                val = project if project != "" else None
                cur.execute("UPDATE memories SET project = %s WHERE id = %s", (val, mid))
                changes.append(f"project → {val}")
            if mtype is not None:
                val = mtype if mtype != "" else None
                cur.execute("UPDATE memories SET type = %s WHERE id = %s", (val, mid))
                changes.append(f"type → {val}")
            if set_tags is not None:
                cur.execute("DELETE FROM memory_tags WHERE memory_id = %s", (mid,))
                new_names = []
                for item in set_tags:
                    name, description = self._tag_fields(item)
                    if not name:
                        continue
                    tag_id = self._get_or_create_tag(cur, name, description)
                    cur.execute("INSERT INTO memory_tags (memory_id, tag_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (mid, tag_id))
                    new_names.append(name)
                changes.append(f"tags set to: {', '.join(new_names) if new_names else '(none)'}")
            if add_tags:
                added = []
                for item in add_tags:
                    name, description = self._tag_fields(item)
                    if not name:
                        continue
                    tag_id = self._get_or_create_tag(cur, name, description)
                    cur.execute(
                        "INSERT INTO memory_tags (memory_id, tag_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                        (mid, tag_id),
                    )
                    added.append(name)
                changes.append(f"+tags: {', '.join(added)}")
            if remove_tags:
                removed = [str(t).strip() for t in remove_tags if str(t).strip()]
                for name in removed:
                    cur.execute("""
                        DELETE FROM memory_tags
                        WHERE memory_id = %s
                          AND tag_id IN (SELECT id FROM tags WHERE lower(name) = lower(%s))
                    """, (mid, name))
                changes.append(f"-tags: {', '.join(removed)}")
            return changes

    def get_many(self, ids):
        if not ids:
            return []
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            return cur.execute(
                "SELECT id, agent, project, type, content FROM memories WHERE id = ANY(%s)",
                (list(ids),),
            ).fetchall()

    def delete(self, ids):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM memories WHERE id = ANY(%s)", (list(ids),))

    def list_tags(self):
        with self._connect() as conn, conn.cursor() as cur:
            return cur.execute("""
                SELECT t.name, COUNT(mt.memory_id) AS count, t.description
                FROM tags t LEFT JOIN memory_tags mt ON t.id = mt.tag_id
                GROUP BY t.id, t.name, t.description
                ORDER BY count DESC, t.name
            """).fetchall()

    def list_projects(self):
        with self._connect() as conn, conn.cursor() as cur:
            return cur.execute("""
                SELECT project, COUNT(*) AS count
                FROM memories WHERE project IS NOT NULL
                GROUP BY project ORDER BY count DESC
            """).fetchall()

    def stats(self):
        with self._connect() as conn, conn.cursor() as cur:
            def scalar(sql):
                return cur.execute(sql).fetchone()[0]
            oldest = scalar("SELECT MIN(timestamp) FROM memories")
            newest = scalar("SELECT MAX(timestamp) FROM memories")
            return {
                "total": scalar("SELECT COUNT(*) FROM memories"),
                "agents": scalar("SELECT COUNT(DISTINCT agent) FROM memories"),
                "projects": scalar("SELECT COUNT(DISTINCT project) FROM memories WHERE project IS NOT NULL"),
                "tags": scalar("SELECT COUNT(*) FROM tags"),
                "today": scalar("SELECT COUNT(*) FROM memories WHERE timestamp::date = current_date"),
                "week": scalar("SELECT COUNT(*) FROM memories WHERE timestamp >= current_date - 7"),
                "oldest": oldest.isoformat(sep=" ", timespec="seconds") if oldest else None,
                "newest": newest.isoformat(sep=" ", timespec="seconds") if newest else None,
            }


def migrate_sqlite_to_postgres(sqlite_path, dsn):
    """Copy a SQLite memory DB into Postgres, preserving ids. Idempotent (ON CONFLICT
    DO NOTHING). Returns {"memories": n, "tags": n}."""
    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    mems = src.execute(
        "SELECT id, timestamp, agent, project, content, type FROM memories ORDER BY id"
    ).fetchall()
    # Older source DBs may predate the required tag descriptor; fall back to the name.
    tag_cols = [r[1] for r in src.execute("PRAGMA table_info(tags)").fetchall()]
    if "description" in tag_cols:
        tags = src.execute("SELECT id, name, description FROM tags").fetchall()
    else:
        tags = src.execute("SELECT id, name, name AS description FROM tags").fetchall()
    links = src.execute("SELECT memory_id, tag_id FROM memory_tags").fetchall()
    src.close()

    PostgresStore(dsn).initialize()
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        for m in mems:
            cur.execute(
                "INSERT INTO memories (id, timestamp, agent, project, content, type) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                (m["id"], m["timestamp"], m["agent"], m["project"], m["content"], m["type"]),
            )
        for t in tags:
            cur.execute(
                "INSERT INTO tags (id, name, description) VALUES (%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                (t["id"], t["name"], t["description"]),
            )
        for link in links:
            cur.execute(
                "INSERT INTO memory_tags (memory_id, tag_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (link["memory_id"], link["tag_id"]),
            )
        # Advance the serial sequences past the copied ids.
        cur.execute("SELECT setval(pg_get_serial_sequence('memories','id'), "
                    "GREATEST(COALESCE((SELECT MAX(id) FROM memories), 1), 1))")
        cur.execute("SELECT setval(pg_get_serial_sequence('tags','id'), "
                    "GREATEST(COALESCE((SELECT MAX(id) FROM tags), 1), 1))")
    return {"memories": len(mems), "tags": len(tags)}
