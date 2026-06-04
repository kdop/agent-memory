---
name: memory
description: >-
  Persistent cross-session memory via the `memory-cli` tool. Use when the user wants to
  record/remember a decision, fact, bug, or lesson, or to recall what was previously done
  or decided — e.g. "remember that…", "log this", "note for later", "what did we decide
  about…", "what did I do yesterday", "have we hit this before". Also use proactively:
  log decisions/fixes/lessons as they happen, and recall recent context at the start of work.
---

# Memory

Durable memory across sessions, via the `memory-cli` command (on PATH once installed).

**Always call `memory-cli` directly — never the `memory` shell alias.** Aliases come from
interactive shell profiles, which the non-interactive shell you run commands in does not load.

## When to use it

- **Log as you work** (don't wait for session end): after a decision, a bug fix, a feature,
  or any non-obvious thing you learned or got wrong.
- **Recall at the start of work**: pull recent context for the project before diving in.

## Record

```bash
memory-cli add "<what happened, concretely>" --yes \
    --agent=<you> --project=<project> \
    --tags=<comma,separated> --type=<code|decision|lesson|note>
```

- `--yes` auto-creates the database on first use (an agent shell has no TTY to confirm at a prompt).
- `--agent` attributes the entry to you; `--project` scopes it.
- **Tag liberally** — tags are what make later searches land.
- **Types:** `code` (changes/refactors), `decision` (architecture/tooling), `lesson`
  (mistakes/insights), `note` (everything else).

## Recall

```bash
memory-cli query --project=<project> --limit=10     # recent timeline
memory-cli query --project=<project> --today        # also --yesterday, --since=, --until=, --tag=, --type=
memory-cli search "<topic or phrase>" --project=<project>   # full-text search
```

## Inspect / edit

```bash
memory-cli show <id>
memory-cli update <id> --content "…" --add-tags "…" --remove-tags "…"
memory-cli delete <id> --yes        # without --yes this is a dry run
```

## Overview

```bash
memory-cli tags ; memory-cli projects ; memory-cli stats
```

Full flag reference: `memory-cli --help`. Protocol details and rationale: `MEMORY.md` in the
agent-memory repo. Golden rule: **if you don't log it, it's gone next session.**
