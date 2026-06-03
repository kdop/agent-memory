# MEMORY.md — agent memory usage protocol

The canonical protocol for using the persistent memory system (`memory-cli` + the
SQLite DB in this repo). **Other projects import this file** rather than carrying
their own copy:

```
@~/workspace/agent-memory/MEMORY.md
```

It gives you continuity across sessions — what you did, what was decided, what was
learned. Scope every command to your project with `--project=<name>`.

## Setup

```bash
export AGENT_NAME=<you>      # who is logging (the persona pointer; see AGENT.md)
# DB path resolves from AGENT_MEMORY_DB, default ~/workspace/agent-memory/memory.db
```

Invoke as `memory` (alias) or `memory-cli` (on PATH).

## Commands

```bash
# Add — do this AS YOU WORK, not at session end
memory add "Implemented user authentication" --project=<name> --tags=auth,feature --type=code
memory add "Chose PostgreSQL over MySQL" --project=<name> --tags=database --type=decision

# Query — timeline, filtered
memory query --project=<name> --limit=10
memory query --project=<name> --today           # also: --yesterday
memory query --project=<name> --since=2026-05-01 --until=2026-05-15
memory query --project=<name> --tag=bugfix       # --tag=auth across all projects
memory query --project=<name> --type=decision

# Search — full text (optionally filtered)
memory search "what were we doing with auth" --project=<name>
memory search "redis" --since=2026-05-01

# Inspect / mutate by ID
memory show 42
memory update 42 --content "corrected text"
memory update 42 --add-tags "important" --remove-tags "draft"
memory delete 49 --yes            # without --yes = dry run; accepts multiple IDs

# Overview
memory tags ; memory projects ; memory stats
```

## When to log (during work, not at the end)

- ✅ After architectural decisions
- ✅ After fixing bugs (what was broken, how you fixed it)
- ✅ After implementing features
- ✅ When you learn something non-obvious about the codebase
- ✅ When you make a mistake (so future-you doesn't repeat it)

## Memory types

- `--type=code` — code changes, implementations, refactors
- `--type=decision` — architectural decisions, tool choices
- `--type=lesson` — lessons learned, mistakes, insights
- `--type=note` — general notes, observations

## Tags

Use them liberally — tags are what make future searches land: `--tags=auth,bugfix,api`.

## Protocol

- **Start:** `memory query --project=<name> --limit=10` (new projects may be empty — fine), then `memory search "<topic>"` for specific context.
- **During:** log decisions and changes as they happen, tagged.
- **End (optional):** a summary memory if the session was significant.

---

**The golden rule:** if you don't log it, it's gone next session. Text > brain. 📝
