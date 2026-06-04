# agent-memory

A shared, SQLite-backed **persistent memory system for AI agents** — a single-file
Python 3 CLI (`memory-cli`, stdlib only) over a relational SQLite DB. Gives CLI agents
continuity across sessions: what was done, decided, learned, instead of starting cold.

> **Golden rule:** if you don't log it, it's gone next session.

```bash
export PATH="$HOME/workspace/agent-memory:$PATH"
alias memory="$HOME/workspace/agent-memory/memory-cli"

memory add "Chose SQLite over flat files" \
  --agent=agent-a --project=agent-memory --tags=design --type=decision
memory query --project=agent-memory --today
memory search "database" --project=agent-memory
```

Memories are attributed per agent (`--agent`), scoped by `--project`, classified by
`--type` (`code | decision | lesson | note`), and tagged for retrieval. Full command
reference: `memory --help`.

- **[MEMORY.md](MEMORY.md)** — the logging protocol (imported by other repos)
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — schema, design decisions, programmatic access
- **[CLAUDE.md](CLAUDE.md)** — instructions for an agent working *on this tool*

The DB at `~/workspace/agent-memory/memory.db` (override with `AGENT_MEMORY_DB`) is
**live and shared** across all agent sessions. It's git-ignored — data, not source —
so back it up out-of-band.
