# MEMORY.md — agent memory protocol

Canonical protocol for the persistent memory system (the `memory-cli` command over the
shared SQLite DB; the same store is also reachable via the HTTP API and the MCP server —
see [README.md](README.md)). **Other repos import this file** instead of copying it:

    @~/workspace/agent-memory/MEMORY.md

Continuity across sessions: what you did, decided, learned. **Golden rule — if you
don't log it, it's gone next session.** Scope everything with `--project=<name>`.

## Usage

Invoke as `memory` (alias) or `memory-cli`. DB path resolves: `AGENT_MEMORY_DB` env →
stored `db_path` (`memory-cli config set db_path <path>`) → default
`~/.local/share/agent-memory/memory.db`. Attribute entries with `--agent=<you>`.

    # Add — AS YOU WORK, not at session end
    # --tags is a JSON array of {"name":..., "description":...}; description is
    # optional — a brand-new tag with none just defaults to its own name.
    memory add "Implemented auth" --agent=<you> --project=<name> --type=code \
      --tags='[{"name":"auth","description":"authentication"},{"name":"feature"}]'

    # Query — filtered timeline
    memory query --project=<name> --limit=10
    memory query --project=<name> --since-days 0    # 0=today, 1=yesterday; also --since=, --until=
    memory query --project=<name> --tag=bugfix --type=decision

    # Search — full text
    memory search "auth flow" --project=<name>      # --tag=, --since= optional

    # By ID
    memory show 42
    memory update 42 --content "fixed" --add-tags='[{"name":"important"}]' --remove-tags draft
    memory delete 49 --yes              # no --yes = dry run; accepts multiple IDs

    # Overview
    memory tags ; memory projects ; memory stats

## Protocol

- **Types:** `code` (changes, refactors), `decision` (architecture, tooling),
  `lesson` (mistakes, insights), `note` (everything else).
- **Tag liberally** — tags are what make future searches land. A tag carries an
  optional descriptor (`{"name":..., "description":...}`); give one the first time
  you use a new tag if you can, but it's never required — an undescribed new tag
  just defaults to its own name.
- **Log:** decisions, bug fixes, features, and non-obvious things you learn or get
  wrong — as they happen, not at the end.
- **Start of session:** `memory query --project=<name> --limit=10` (empty is fine for
  new projects), then `memory search "<topic>"` for specifics.
