# Architecture — agent-memory

How the `agent_memory` package is built and why. For usage, see [README.md](README.md);
for the agent logging protocol, see [skills/memory/SKILL.md](skills/memory/SKILL.md).

## Overview

**API-first, Postgres-only.** One async FastAPI service owns the database; everything
else is a client. The service is reachable three ways over one codebase — the
`memory-cli` command, the HTTP API directly, and an MCP server — giving AI agents a
persistent, queryable timeline across sessions, searchable by words and by meaning:
auto-timestamped, tagged, scoped by project and agent. The **client surface** (CLI + MCP, both the urllib
`ApiClient`) is stdlib-only and never opens a database; the server is an opt-in extra.

```
Client (CLI / MCP)  ──HTTP──▶  FastAPI service  ──asyncpg──▶  Postgres
  ApiClient (urllib)             async routes                 (schema by Alembic)
  stdlib-only                    → repository → SQLAlchemy 2.0 async
```

## Package layout

```
src/agent_memory/
  config.py            # endpoint/token resolution + agent-name; client settings (JSON)
  client.py            # ApiClient — stdlib urllib HTTP wrapper (the CLI+MCP share it)
  cli.py               # memory-cli command (argparse); presentation only
  mcp_server.py        # FastMCP server wrapping ApiClient — [mcp] extra
  server/              # the API service — [server] extra
    app.py             #   FastAPI app; thin async routes, one txn per request
    db.py              #   async engine/sessionmaker; DSN → asyncpg normalization
    models.py          #   SQLAlchemy 2.0 models — the schema source of truth
    repository.py      #   async data-access functions over an AsyncSession
    schemas.py         #   Pydantic v2 request/response models (the HTTP contract)
    auth.py            #   bearer-token guard (constant-time)
    embedding.py       #   Embedder interface; local fastembed model — [embed] extra, optional
    checks.py          #   the warnings on add (short, no-project, no-reasoning); never block
    review.py          #   the review of each new memory by a model on an Ollama server; off by default
    __main__.py        #   `python -m agent_memory.server` (uvicorn launcher)
alembic/               # migrations; env.py autogenerates from models.Base.metadata
memory-cli             # thin shim on PATH -> agent_memory.cli:main (rule #5)
```

## The seam

The client resolves an **endpoint + token**, never a DB target:

- Endpoint: `AGENT_MEMORY_API` env → config `api_url` → local default
  `http://127.0.0.1:8099`.
- Token: `AGENT_MEMORY_API_TOKEN` env → config `api_token` → sent as `Bearer`.

The server resolves a **DSN**: `AGENT_MEMORY_DB` (a `postgresql://` URL, normalized to
`postgresql+asyncpg://`). The CLI, MCP, HTTP API, and repository all round-trip through
the same Pydantic contract, so behaviour can't drift — the cross-surface suite asserts
CLI, API, and MCP produce identical results.

## Design goals

1. **Persistent continuity** — solve the fresh-slate problem; remember across sessions.
2. **Queryable timeline** — structured filters (date, tag, project, type), not grep.
3. **Fast at scale** — stays fast at 100k+ memories via proper indexing (GIN FTS).
4. **Multi-agent shared memory** — several agents share one DB; cross-agent continuity.
5. **Single writer of the schema** — Alembic owns DDL; the API owns all data access.

## Key decisions

| Decision | Why | Rejected |
|---|---|---|
| **API-first** — the service is the only DB client | One place holds credentials + connection pool; CLI/MCP stay stdlib-only and need nothing installed to reach a shared server | Every client opening its own DB (creds sprawl, no shared server, drift) |
| **Postgres-only** | Real concurrency, `tsvector`/GIN FTS, network-reachable, one deploy target | SQLite (single-file, no network, weaker FTS), a store abstraction over both (dead weight once there's one backend) |
| **SQLAlchemy 2.0 async + asyncpg** | Async all the way to the socket under FastAPI; models double as the Alembic source of truth | Raw asyncpg (hand-rolled schema/migrations), sync ORM (blocks the event loop) |
| **Alembic owns the schema** | Versioned, reviewable migrations; `alembic upgrade head` is the one setup step | Auto-`create_all` at boot (no history, unreviewable changes) |
| **Generated `tsvector` + GIN** | FTS kept in sync by Postgres itself — no triggers; ranked results + `ts_headline` snippets | `LIKE` (slow, no ranking), trigger-maintained column (more moving parts) |
| **Relational tags** (`memories`, `tags`, `memory_tags`) | Indexed tag queries, case-insensitive via `lower(name)`, per-tag descriptor | JSON array (slow, case-sensitive, no descriptor) |
| **Meaning vectors in a plain `REAL[]` column, compared in Python** | Nothing is installed on the database host, and at this row count a scan in Python is fast; switching to pgvector later is one migration | pgvector (an extension the host does not have), an outside service to make the vectors (memories are private; the model runs locally) |

`memory-cli` stays on PATH as a thin shim so the command name and `memory` alias are
unchanged despite the package split (now an entry point onto `agent_memory.cli:main`).

## Database schema

Postgres, created by `alembic upgrade head` (baseline
`alembic/versions/*_baseline_schema.py`). The SQLAlchemy models in `server/models.py`
are the source of truth; the effective DDL:

```sql
-- Memories. content_tsv is a stored, generated tsvector kept in sync by Postgres.
CREATE TABLE memories (
    id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    timestamp   TIMESTAMPTZ NOT NULL DEFAULT now(),
    agent       TEXT NOT NULL,
    project     TEXT,
    content     TEXT NOT NULL,
    type        TEXT,
    content_tsv TSVECTOR NOT NULL
                GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    -- The meaning vector of `content` and the name of the model that made it.
    -- Both NULL until a model has seen the row. Neither is ever returned by the API.
    embedding       REAL[],
    embedding_model TEXT
);
CREATE INDEX idx_content_tsv ON memories USING gin (content_tsv);
CREATE INDEX ix_memories_timestamp ON memories (timestamp);
CREATE INDEX ix_memories_agent     ON memories (agent);
CREATE INDEX ix_memories_project   ON memories (project);
CREATE INDEX ix_memories_type      ON memories (type);

-- Canonical tags: case-insensitive-unique name + a required descriptor.
CREATE TABLE tags (
    id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_tags_lower_name ON tags (lower(name));

-- Many-to-many junction (cascades on delete).
CREATE TABLE memory_tags (
    memory_id BIGINT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    tag_id    BIGINT NOT NULL REFERENCES tags(id)     ON DELETE CASCADE,
    PRIMARY KEY (memory_id, tag_id)
);
CREATE INDEX ix_memory_tags_tag_id ON memory_tags (tag_id);

-- What the review model said about a memory (Checks on write below). One row per
-- memory, replaced when it is reviewed again; the row goes with its memory.
CREATE TABLE memory_reviews (
    memory_id    BIGINT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    verdict      TEXT NOT NULL,       -- approve, reject or rewrite
    rule         INTEGER,             -- the rule the entry breaks; NULL on approve
    reason       TEXT NOT NULL,       -- one sentence
    rewrite      TEXT,                -- the suggested text, when the verdict is rewrite
    duplicate_of BIGINT REFERENCES memories(id) ON DELETE SET NULL,  -- the memory it repeats
    tags         TEXT[],              -- suggested tags for the rewrite; NULL when none
    model        TEXT NOT NULL,       -- the name of the model that answered
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

## Full-text search

`content_tsv` is a **stored generated column** (`to_tsvector('english', content)`) with a
**GIN index** — Postgres maintains it on every write, so there are no triggers. Search
(`repository.search`) builds a `plainto_tsquery`, filters with `content_tsv @@ query`,
orders by `ts_rank(content_tsv, query)`, and returns a `ts_headline` snippet whose
markers (`→ … ←`) match the old highlighter so clients render identically. All of it is
expressed with SQLAlchemy `func`, not string SQL.

## Search by meaning

Keyword search only finds rows that share words with the query. A question asked in
other words misses its answer. So each memory also gets a **meaning vector**: 384
numbers, made by the local model `BAAI/bge-small-en-v1.5` (through fastembed, the
`[embed]` extra), scaled to unit length. The vector is stored in `embedding` and the
model's name in `embedding_model`; `add` computes it, an update that changes the
content computes it again. Two texts that mean the same have vectors that point the
same way, so their cosine is close to 1.

**No pgvector.** The vector is a plain `REAL[]` column and the comparison runs in
Python on the server: `search_semantic` loads every row that passes the filters and has
a vector from the running model, scores each with `cosine`, sorts, cuts to the limit.
Nothing is installed on the database host, and at this row count a full scan in Python
takes a few milliseconds. If the table ever grows past that, switching to pgvector and
an index is one migration; nothing in the API changes.

**Combined search** (`mode=hybrid`, `search_hybrid`) runs the keyword search and the
search by meaning with the same filters and no limit, then merges the two ranked lists
by rank, not by score, because `ts_rank` and cosine live on different scales. A memory
at rank *r* in a list (the first hit is rank 1) adds `1 / (60 + r)` to its combined
score; a memory in both lists gets the sum of both. So a memory that is first in both
lists scores `2 / 61`, one that is first in one list only scores `1 / 61`, and a memory
found both ways always outranks one found one way at the same rank. The constant 60
(`RRF_K`) keeps the top ranks from crushing everything below them. Rows are sorted by
that score, ties by newest id, then cut to the limit. The snippet comes from the
keyword hit when there is one.

**Without the model** (`[embed]` not installed, or `AGENT_MEMORY_EMBED_MODEL=off`) the
server runs with a `NullEmbedder`: writes store no vector, `mode=semantic` answers 400
with the reason, `mode=hybrid` serves the keyword half and says so in the response
header `X-Search-Fallback: keyword`.

**Reindex** (`repository.reindex`, `POST /admin/reindex`, `memory reindex`) walks the
rows that have no vector, or one from another model, in batches of 64 and writes a
vector from the current model. The server runs it once at start, so the first start
after installing the model, or after a model change, fills the table. Only vectors
from the running model are ever compared, since another model's vector may not even
have the same length.

## Checks on write

`POST /memories` checks a new memory before it is stored.

- **Duplicate refusal.** `find_duplicate` embeds the new content and compares it, in
  Python, with every vector in the same project (a memory with no project is compared
  with the other memories that have none). If the best match scores cosine
  `DUPLICATE_THRESHOLD = 0.92` or above, the route answers `409` with
  `{"reason": "duplicate", "existing_id": <id>, "score": <cosine>}` and stores nothing.
  On unit vectors, 1.0 is the same text and 0.92 is a rewording. `?force=true` (CLI
  `--force`, MCP `force=true`) skips the check. Without the model there are no vectors
  to compare, so nothing is refused.
- **Warnings.** `checks.py` holds three rules as data: `short` (content under 40
  characters), `no-project` (no project given), `no-reasoning` (a `decision` or
  `lesson` whose text has none of the words that say why: because, since, reason, why,
  rejected, instead, so that, cause, trade-off, alternative). The names of the rules
  the entry breaks come back next to the new id. They never block; the memory is
  stored either way.

- **Review by a model.** `review.py` asks a model on an Ollama server what it thinks
  of the entry (`OllamaReviewer`: `POST /api/chat` through `urllib` in a thread, so no
  new package and the event loop stays free). The prompt holds the five rules from the
  skill, copied into `RULES` as data (a test reads the skill file and checks they still
  match; the server never reads that file), the new entry, the five memories of the
  same project closest to it by cosine over the stored vectors (`_nearest`; none when
  there are no vectors), and the ten tags in use closest to it in meaning
  (`tags_for_review`: the entry and each tag's `name: description` are embedded on the
  fly, never stored; without an embedding model the ten most used tags are offered).
  The model answers one JSON object with six keys: `verdict` (`approve`, `reject` or
  `rewrite`), `rule`, `reason`, `rewrite`, `duplicate_of` and `tags`. `parse_verdict`
  turns it into a `Verdict` and treats anything else as no answer: a missing key, a
  rule number that is not a rule, a `duplicate_of` the model was not shown; a tag not on
  the offered list is dropped, and tags on an approve or reject are dropped too. Four
  request settings matter: `think: false` and `format: json`, because otherwise the
  model thinks out loud and returns prose instead of JSON; `temperature: 0`, so the
  same entry gets the same verdict; and `num_ctx: 8192`, so the rules, the entry and
  five neighbours fit. The verdict is stored in `memory_reviews` (schema above), one
  row per memory; `set_review` replaces it on a re-review, and every read carries it
  as `review` next to the tags.

  `AGENT_MEMORY_REVIEW` picks how the review runs (`review_mode`, `make_reviewer`):
  `off` (the default, also for an unknown value or a missing `AGENT_MEMORY_REVIEW_URL`)
  gives a `NullReviewer` that is never called. In **warn** mode the add route stores the
  memory, the session commits, the answer goes out, and then a FastAPI background task
  runs the review: it reads the memory, its neighbours and the tags on offer in one
  session, closes it, calls the model in a thread, and writes the row in a second
  session, so no database connection sits idle while the model thinks. The writer
  never waits, and nothing is refused or changed because of a verdict. In **enforce**
  mode the route builds the entry from the request, finds its neighbours
  (`neighbours_for`, the same rules as `_nearest` for an entry that has no id yet) and
  the tags, and calls the model before storing, with the request's session open while
  the model thinks. A `reject` or `rewrite` answers `422` with
  `{"reason": "review", "verdict", "rule", "explanation", "rewrite", "tags", "duplicate_of"}`
  and stores nothing; an `approve` stores the memory and its row in one go, with no
  second call. The duplicate check comes first (`409` before any model call), and
  `?force=true` skips the review as it skips that check; the review then runs in the
  background as in warn mode, so the row is still written.

  **The fallback rule, in every mode:** a model that is off, unreachable, slow (past
  `AGENT_MEMORY_REVIEW_TIMEOUT`, default 30 s) or answering with something that is not
  the expected JSON never blocks a write. The memory is stored, no row is written, and
  the server logs one line. `POST /admin/review` (`memory review --missing`) reviews the
  memories that have no row yet, newest first, in the background, so memories written
  while the model was off or down get their verdict later; `POST /admin/review/{id}`
  reviews one memory now and replaces its row. `GET /memories/flagged`
  (`memory review`, MCP `memory_flagged`) lists the memories whose verdict is `reject`
  or `rewrite`, newest review first.

Before a check goes live, `scripts/replay_write_checks.py` runs the duplicate check and
the warnings over a copy of the database in write order and prints what each would
have said (see README).

## Data flow

**Add** — run the checks above (a duplicate is refused, warnings are collected, in
enforce mode the model reads the entry first), then insert a `Memory`; for each `{"name", "description"}` tag, reuse the existing
row (updating its descriptor only if a new non-blank one is given) or create it
(defaulting a new tag's descriptor to its own name), then link in `memory_tags`;
`content_tsv` is generated automatically. When the server has an embedding model, `add` also
stores the content's vector in `embedding` and the model's name in `embedding_model`;
an update that changes the content recomputes both, any other update leaves them alone.
With no model both stay NULL. In warn mode the review runs after the response and
writes its row to `memory_reviews`. **Query** — build a `SELECT` with
`selectinload(tags)` and `WHERE` clauses from the filters (date window, project, agent,
type, `tags.any(lower(name)=…)`), `ORDER BY timestamp DESC`. **Search** — `mode`
picks keyword search (Full-text search above), search by meaning, or the combined
search (Search by meaning above). One `AsyncSession` per request, committed if the
handler returns and rolled back if it raises.

## Concurrency & performance

The service holds one pooled `AsyncEngine` per process (`pool_size=10`,
`max_overflow=20`, `pool_pre_ping=True`) and Postgres handles concurrent readers/writers
natively — no WAL/NFS caveats. Every common filter column is indexed (`O(log n)`); FTS is
GIN-indexed. Add is `O(1)` plus `O(t)` for `t` tags. Scale past ~100k rows the usual
Postgres way: `ANALYZE`, `EXPLAIN`, add a covering index (e.g.
`CREATE INDEX ON memories (project, timestamp DESC)`).

## Programmatic access

Three ways in beyond the CLI:

- **The HTTP API** — routes mirror the operations 1:1: `POST /memories`,
  `GET /memories` (query), `GET /memories/search`, `GET /memories/{id}`,
  `GET /memories/bulk`, `GET /memories/flagged`, `PATCH /memories/{id}`,
  `DELETE /memories`, `GET /tags`, `GET /projects`, `GET /stats`, `POST /admin/reindex`,
  `POST /admin/review` (review the memories that have no verdict yet),
  `POST /admin/review/{id}` (review one memory now). Bearer auth via `AGENT_MEMORY_API_TOKEN`;
  `GET /health` is unauthenticated. Call it with any HTTP client, or reuse
  `agent_memory.client.ApiClient`.
- **The MCP server** — `python -m agent_memory.mcp_server` (needs `[mcp]`); tools
  `memory_add/query/search/flagged/show/update/delete/tags/projects/stats` over stdio,
  each a thin wrapper over `ApiClient`.
- **The repository** — for in-process server code/tests, `agent_memory.server.repository`
  is plain async functions over an `AsyncSession` (`add`, `find_duplicate`, `query`,
  `search`, `search_semantic`, `search_hybrid`, `get`, `update`, `get_many`, `delete`,
  `list_tags`, `list_projects`, `stats`, `reindex`, `review_input`, `neighbours_for`,
  `tags_for_review`, `set_review`, `flagged`, `count_flagged`, `without_review`).

A minimal client using the packaged wrapper:

```python
from agent_memory.client import ApiClient   # resolves AGENT_MEMORY_API / api_token

api = ApiClient()
mid = api.add("shipped the async rewrite", agent="my-agent", project="my-project",
              tags=[{"name": "release", "description": "ship events"}], mtype="decision")
rows = api.query(project="my-project", limit=5)
```

## Notes

- **Extensible:** the relational schema grows without breaking data (memory links,
  hierarchies, extra tag metadata) via new Alembic revisions — models are the source of
  truth, `alembic revision --autogenerate` diffs them.
- **Security:** bearer token on every data route, constant-time compare; the server
  fails closed with 503 if no token is configured. Put TLS in front for anything off
  localhost. SQL injection is a non-issue — all queries go through SQLAlchemy.
- **Limits:** search by meaning needs the `[embed]` extra on the server; without it
  only keyword search runs, which matches words, not meaning — tag well. Browse the raw
  DB with `psql` or any Postgres client.
- **Principles:** simple over complex, fast over fancy, queryable over readable,
  agent-first. Markdown for prose; the Memory API for the operational timeline.
