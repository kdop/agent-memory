# agent-memory

A shared, SQLite-backed **persistent memory system for AI agents**. It gives a CLI
agent (e.g. agent-a on Claude Code, Clu on OpenClaw) continuity across sessions —
what was done, what was decided, what was learned — instead of starting cold every
time.

> **The golden rule:** if you don't log it, it's gone next session. Text > brain.

## What it is

- A single-file Python 3 CLI — `memory-cli` — with **no third-party dependencies**
  (stdlib `sqlite3`, `argparse`, `json`, `pathlib`).
- A relational SQLite database at `~/workspace/agent-memory/memory.db`
  (override with the `AGENT_MEMORY_DB` env var).
- Memories are attributed per agent via the `AGENT_NAME` env var, scoped by
  `--project`, classified by `--type`, and tagged for retrieval.

## Quick start

```bash
# 1. Put the CLI on your PATH and set your agent name (in your shell profile):
export PATH="$HOME/workspace/agent-memory:$PATH"
alias memory="$HOME/workspace/agent-memory/memory-cli"
export AGENT_NAME=agent-a

# 2. Use it as you work — not at session end:
memory add "Decided to repoint the CLI DB path via env var" \
  --project=agent-memory --tags=migration,cli --type=decision

memory query --project=agent-memory --today
memory search "database decision" --project=agent-memory
memory stats
```

## Command reference (short)

| Command | Purpose |
|---|---|
| `add "<text>" --project= --tags= --type=` | Record a memory (`type`: code \| decision \| lesson \| note) |
| `query --project= [--today\|--yesterday\|--since\|--until\|--tag\|--type\|--limit]` | Filtered timeline |
| `search "<text>" [--project= --tag= --since=]` | Full-text search |
| `show <id>` / `update <id> [--content\|--add-tags\|--remove-tags]` / `delete <id> [--yes]` | Inspect / edit / remove |
| `tags` / `projects` / `stats` | Overview |

## Documentation

| Doc | Contents |
|---|---|
| [README-memory.md](README-memory.md) | Full feature walk-through |
| [USER-GUIDE.md](USER-GUIDE.md) | Day-to-day usage patterns |
| [API.md](API.md) | Command/flag reference in detail |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Schema, design decisions, data model |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to change the tool safely |
| [CHANGELOG.md](CHANGELOG.md) | Version history |
| [INDEX.md](INDEX.md) | Index of all docs |
| [CLAUDE.md](CLAUDE.md) | Instructions for an agent working *on this project* |

## Note on the database

`memory.db` is the **live** memory shared across all agent sessions and projects.
It is intentionally **git-ignored** — it is data, not source. Migrate it carefully
(it's the one file every session depends on), and back it up out-of-band.
