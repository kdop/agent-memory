# MEMORY.md — agent memory protocol

Canonical protocol for the persistent memory system (`memory-cli` + the shared SQLite
DB). **Other repos import this file** instead of copying it:

    @~/workspace/agent-memory/MEMORY.md

Continuity across sessions: what you did, decided, learned. **Golden rule — if you
don't log it, it's gone next session.** Scope everything with `--project=<name>`.

## Usage

Invoke as `memory` (alias) or `memory-cli`. DB path resolves from `AGENT_MEMORY_DB`
(default `~/workspace/agent-memory/memory.db`). Attribute entries with `--agent=<you>`.

    # Add — AS YOU WORK, not at session end
    memory add "Implemented auth" --agent=<you> --project=<name> --tags=auth,feature --type=code

    # Query — filtered timeline
    memory query --project=<name> --limit=10
    memory query --project=<name> --today           # also --yesterday, --since=, --until=
    memory query --project=<name> --tag=bugfix --type=decision

    # Search — full text
    memory search "auth flow" --project=<name>      # --tag=, --since= optional

    # By ID
    memory show 42
    memory update 42 --content "fixed" --add-tags important --remove-tags draft
    memory delete 49 --yes              # no --yes = dry run; accepts multiple IDs

    # Overview
    memory tags ; memory projects ; memory stats

## Protocol

- **Types:** `code` (changes, refactors), `decision` (architecture, tooling),
  `lesson` (mistakes, insights), `note` (everything else).
- **Tag liberally** — tags are what make future searches land.
- **Log:** decisions, bug fixes, features, and non-obvious things you learn or get
  wrong — as they happen, not at the end.
- **Start of session:** `memory query --project=<name> --limit=10` (empty is fine for
  new projects), then `memory search "<topic>"` for specifics.
