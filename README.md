# agent-memory

![tests](https://github.com/kdop/agent-memory/actions/workflows/tests.yml/badge.svg)
![coverage](./coverage.svg)

A shared, **Postgres-backed persistent memory service for AI agents** — API-first. A
single async FastAPI service owns the database; agents reach it three ways — the
`memory-cli` command, the same HTTP API directly, and an MCP server — so they keep
continuity across sessions: what was done, decided, learned, instead of starting cold.
The **CLI and MCP are thin HTTP clients** (`ApiClient`, stdlib `urllib` only); only the
server touches Postgres, via SQLAlchemy 2.0 async (`pip install "agent-memory[server]"`
/ `[mcp]`).

> **Golden rule:** if you don't log it, it's gone next session.

## Architecture in one line

**Client (CLI / MCP) → HTTP → FastAPI service → Postgres.** The client never opens a
database; it resolves an endpoint and a bearer token and makes requests. The server is
the only thing with a `postgresql://` DSN.

## Quickstart

**1. Run Postgres** and create a database (any Postgres 14+ reachable by DSN).

**2. Install the server extra and create the schema** (Alembic owns it):

```bash
pip install -e ".[server]"        # add the [embed] extra to search by meaning: ".[server,embed]"
export AGENT_MEMORY_DB="postgresql://user:pass@localhost:5432/agent_memory"
alembic upgrade head
```

**3. Start the API** (needs a bearer token — the server fails closed without one):

```bash
AGENT_MEMORY_DB="$AGENT_MEMORY_DB" \
AGENT_MEMORY_API_TOKEN="$(openssl rand -hex 16)" \
AGENT_MEMORY_PORT=8099 \
python -m agent_memory.server           # binds 127.0.0.1:8099
```

**4. Point the CLI at it** and log a memory:

```bash
export PATH="$PWD:$PATH"          # from the repo root; or `pip install -e .` for the console script
alias memory="$PWD/memory-cli"
export AGENT_MEMORY_API="http://127.0.0.1:8099"     # this is also the built-in default
export AGENT_MEMORY_API_TOKEN="…"                   # same token the server was started with

memory add "Chose Postgres over SQLite" \
  --agent=my-agent --project=agent-memory --type=decision \
  --tags='[{"name":"design","description":"architecture choices"},{"name":"db"}]'
memory query --project=agent-memory --since-days 0
memory search "database" --project=agent-memory                    # by words (the default)
memory search "why we picked the database" --mode semantic        # by meaning; needs the [embed] extra
memory search "why we picked the database" --mode hybrid          # both lists, combined
```

Memories are attributed per agent (`--agent`), scoped by `--project`, classified by
`--type` (`decision | lesson | note | preference`), and tagged for retrieval. Tags are
structured objects — `--tags` takes a **JSON array** of `{"name", "description"}`, and a
new tag with no description defaults to its own name. Full command reference:
`memory --help`.

**Search** has three modes, `--mode keyword|semantic|hybrid`. `keyword` (the default)
finds memories that contain the words of the query and marks them in a snippet.
`semantic` finds memories that mean the same thing, even in other words: the server
turns each memory into a meaning vector with a small local model and compares the query
against those vectors. `hybrid` runs both and combines the two lists, so a memory found
both by words and by meaning comes first (how it is combined: [ARCHITECTURE.md](ARCHITECTURE.md)).
The last two need the `[embed]` extra on the server (`pip install -e ".[server,embed]"`).
Without it, `semantic` answers with an error that says so and `hybrid` falls back to
keyword. Two environment variables control the model, both read by the server at
start: `AGENT_MEMORY_EMBED_MODEL` (default `BAAI/bge-small-en-v1.5`; `off` turns it
off) and `AGENT_MEMORY_EMBED_CACHE` (where the model files are stored; default
`.cache/fastembed` under the repo root). `memory reindex` gives every memory that has
no vector, or a vector from another model, a vector from the current model. The server
also runs this once at start, so after installing the model, or switching to another,
a restart is enough.

**Adding** checks the entry. A new memory whose meaning is nearly the same as one
already in the same project (cosine 0.92 or above) is refused and nothing is stored:
the API answers `409` with the existing id, the CLI prints `✗ Duplicate of memory #<id>
(score 0.97). Use 'memory update <id>' or --force.` and exits with code 3, and the MCP
tool returns `{"error": "duplicate", "existing_id": <id>, "score": <score>}`. Update
the existing memory, or pass `--force` when the new one is meant as a separate record.
A server without the model has no vectors to compare, so it never refuses. Three
warnings can print after the id, one per line: `warning: short` (under 40 characters),
`warning: no-project` (no `--project`), and `warning: no-reasoning` (a `decision` or
`lesson` with no word that says why, such as "because" or "rejected"). Warnings never
block; the memory is stored either way.

**Review by a model.** The server can also have a model read each new memory and judge
it against the five rules in the skill. The model answers `approve`, `reject` with the
number of the rule the entry breaks (or the id of the memory it repeats), or `rewrite`
with a suggested text and suggested tags. `AGENT_MEMORY_REVIEW`, read by the server at
start, picks the mode: `off` (the default) asks no model; `warn` stores the memory,
answers, and asks the model afterwards, so the verdict is advice only; `enforce` asks
the model before storing, and a `reject` or `rewrite` refuses the write. The model runs
on any Ollama server: `AGENT_MEMORY_REVIEW_URL` is its address, for example
`http://192.168.1.20:11434`, `AGENT_MEMORY_REVIEW_MODEL` names the model to ask (default
`qwen3:14b`), and `AGENT_MEMORY_REVIEW_TIMEOUT` is how many seconds to wait for an answer
(default 30). In warn mode the verdict shows as a `review:` line on `show`, `query` and
`search`, for example `review: reject, rule 2: <reason>`; a rewrite adds the suggested
text under `suggested:` and a `suggested tags:` line. `memory review` lists the memories
with a reject or rewrite verdict, newest first (`--project`, `--verdict`, `--limit`,
`--all`); `memory review --missing` reviews the memories written while the model was off.
In enforce mode a reject or rewrite refuses the write and nothing is stored: the API
answers `422` with the verdict and the suggestion, the CLI prints `✗ Review: rewrite,
rule 3: <reason>`, the suggestion, and `Fix the entry, or pass --force to store it as
written.` and exits with code 4, and the MCP tool returns `{"error": "review", ...}`.
`--force` stores the entry anyway; the review then runs after the write, as in warn mode.
A model that is off, unreachable or slow never blocks a write: the memory is stored
without a verdict and the server logs one line.

- **[skills/memory/SKILL.md](skills/memory/SKILL.md)** — the logging protocol, as a
  Claude Code skill each project installs; see [Claude Code skill](#claude-code-skill)
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — schema, design decisions, programmatic access
- **[CLAUDE.md](CLAUDE.md)** — instructions for an agent working *on this tool*

## The three surfaces

| Surface | What it is | Extra |
|---|---|---|
| **API service** | Async FastAPI + SQLAlchemy 2.0 (asyncpg); the only thing that touches Postgres. `python -m agent_memory.server`. | `[server]` |
| **CLI client** | `memory-cli` — a presentation layer over `ApiClient`. Stdlib-only, never opens a DB. | none |
| **MCP client** | Same `ApiClient`, exposed as MCP tools over stdio. | `[mcp]` |
| **Dashboard** | Vue 3 + Quasar single-page app, served by the API service under `/app`. | `web/` (Node) |
| **Review model** | A model on an Ollama server (default `qwen3:14b`) that reads each new memory against the rules in the skill and answers approve, reject or rewrite. Off by default; `AGENT_MEMORY_REVIEW=warn` or `enforce` plus `AGENT_MEMORY_REVIEW_URL` turns it on. The call is plain `urllib`, so nothing extra is installed. | none |
| **Meaning vectors** | A small local model (`BAAI/bge-small-en-v1.5`, 384 numbers per vector) run inside the API service through fastembed. It serves `--mode semantic`, `--mode hybrid` and the duplicate check on add. Optional: `pip install -e ".[embed]"`; without it the service runs with no vectors. `AGENT_MEMORY_EMBED_MODEL=off` turns it off; `AGENT_MEMORY_EMBED_CACHE` sets where model files land (default `.cache/fastembed`). | `[embed]` |

### Endpoint & auth resolution (clients)

- **Endpoint:** `AGENT_MEMORY_API` env → config `api_url` (`memory config set api_url …`)
  → local default `http://127.0.0.1:8099`.
- **Token:** `AGENT_MEMORY_API_TOKEN` env → config `api_token`. Sent as
  `Authorization: Bearer …`. The server requires a token and fails closed without one;
  `GET /health` is the only unauthenticated route.

## MCP server

Agents can reach the memory system over **MCP**. Install the extra and register the
stdio server once — it's then available in every session, exposing tools
`memory_add/query/search/flagged/show/update/delete/tags/projects/stats` (`memory_search`
takes `mode`, `memory_add` takes `force` and returns the warnings, or `{"error": "review",
...}` when the review refuses the entry, and `memory_flagged` lists the memories the
review flagged):

```bash
pip install -e ".[mcp]"               # installs the `agent-memory-mcp` entry point
claude mcp add agent-memory -- agent-memory-mcp
```

Like the CLI, the MCP server is an `ApiClient` — it talks HTTP to the running FastAPI
service (`AGENT_MEMORY_API` / `api_url`), never a database. Run it directly for another
MCP client with `python -m agent_memory.mcp_server` (stdio).

## Claude Code skill

`skills/memory/SKILL.md` packages the protocol as a Claude Code skill, and this repo is
a plugin marketplace for it (`.claude-plugin/`). It is deliberately **per project, not
global**: a project opts in by installing the plugin at project scope, which records the
dependency in its `.claude/settings.json` — commit that, the same way you pin any other
dependency. Nothing lives in `~/.claude` and nothing points into this checkout.

```bash
cd <project>
claude plugin marketplace add kdop/agent-memory
claude plugin install memory@agent-memory --scope project
```

It then loads on demand in that project — when the task matches its description or via
`/memory`. To pick up protocol changes: `claude plugin update memory@agent-memory`, or
turn on auto-update for the marketplace in the `/plugin` Marketplaces tab.

### Changing the skill

`claude plugin update` only notices a new **version number** in
`.claude-plugin/plugin.json`, never new file contents. A skill edit pushed without a bump
never reaches the projects that installed it.

A pre-commit hook in `.githooks/` bumps the patch version for you whenever a commit
touches `skills/` or `.claude-plugin/`. Git only runs hooks from `.git/hooks/`, which is
not committed, so the hook has to be switched on **once per clone**:

```bash
git config core.hooksPath .githooks
```

Without that line the hook file is just sitting in the repo and git ignores it. The
`plugin-version` GitHub workflow is the safety net: it fails a pull request that changes
the plugin without a bump.

## Dashboard

A browser UI for the same API: search and filter memories, edit them in place, and
manage tags (rename, merge, detach). Built with Vue 3 and Quasar; the API service serves
the built assets under `/app`, so it needs no separate host. Log in once with the bearer
token. See [web/README.md](web/README.md) for the dev setup (runs against an in-browser
mock, no backend needed) and the build.

![dashboard](docs/dashboard.png)

## PostgreSQL

Postgres is the only backend. The server reads its DSN from `AGENT_MEMORY_DB`
(`postgresql://…`, normalized to the asyncpg driver internally); the schema is owned by
**Alembic** (`alembic upgrade head`) with the SQLAlchemy models as the source of truth.
The database is **live and shared** across all agent sessions — back it up out-of-band.
Clients never see the DSN; they only ever talk to the API.

### Working on a copy

Never develop against the live database. `scripts/db_copy.sh` gives you a copy in a
podman container named `memcopy` (Postgres 16 on `127.0.0.1:5434`, data in the volume
`memcopy-data`). `up` starts it, `load <source-dsn>` dumps the source into it and
prints the row counts of `memories`, `tags` and `memory_tags` from both sides (exit 1
if they differ), `verify <source-dsn>` repeats the count check, and `down` removes the
container and the volume. The source is only ever read. Point `AGENT_MEMORY_DB` at the
DSN the script prints and work there.

```bash
./scripts/db_copy.sh up
./scripts/db_copy.sh load "$AGENT_MEMORY_DB"      # the live DSN, read only
./scripts/db_copy.sh down
```

### Replaying the write checks

Before a write check goes live, see what it would have done to the entries already
there. `scripts/replay_write_checks.py` walks the copy's memories in the order they
were written and runs the duplicate check and the warnings on each one as if it were
new. It prints one line per memory (id, date, project, `refuse #<id> <score>` or `ok`,
then the warnings) and ends with the totals. It only reads: vectors are computed on
the fly with the same model the server uses (the `[embed]` extra), and it refuses any
DSN whose host is not `localhost` or `127.0.0.1`. `--since` and `--until` narrow the
report to a range of days; `--embedder fake` swaps in a hash-based stand-in for a
quick check of the script itself.

```bash
python scripts/replay_write_checks.py --dsn postgresql://memory:memory@127.0.0.1:5434/memory
python scripts/replay_write_checks.py --dsn ... --since 2026-09-01 --until 2026-09-30
```
