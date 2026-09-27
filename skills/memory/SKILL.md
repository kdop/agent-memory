---
name: memory
description: |
  Persistent cross-session memory for agents via `memory-cli` (the agent-memory service).
  Use at the start of every session to load context for the current project, while
  working to log decisions, lessons, preferences and notes as they happen, and to query,
  search, update or delete past memories, or when the user says "remember", "log this",
  "what did we decide", or asks what happened in a project.
  Rules. Numbers are permanent ids: a deleted rule's number is never reused.
  1. Log only what will still matter in later sessions: durable decisions, lessons, preferences and facts.
  2. Do not log diary-style entries of what was done.
  3. Every entry carries the why, not just the what: the reasoning behind a decision, the alternatives rejected, the cause of a lesson.
  4. Never write down anything that can be retrieved from git history.
  5. Project facts and data go in the project's own folder, tracked by git, not in the memory DB. Write them the way that project's user has instructed. With no instruction, write them at the project's top level in a folder named `notes/`, one markdown file per ISO week named `<year>_<week>.md` (for example `2026_39.md`).
---

# Agent memory

A shared, Postgres-backed memory service that gives every session continuity: what was
done, decided and learned, across projects and agents. **Golden rule — if you don't log
it, it's gone next session.** Scope everything with `--project=<name>`.

`memory-cli` (alias `memory`) is a thin client over the running API; it needs
`AGENT_MEMORY_API` / `AGENT_MEMORY_API_TOKEN` (or the stored `api_url` / `api_token`).
If the CLI reports the API as unreachable, say so and tell the user how it printed to
start it — don't fall back to md-file memory. The same tools exist over MCP
(`memory_add/query/search/show/update/delete/tags/projects/stats`) if the
`agent-memory` MCP server is registered; prefer whichever is available, they are the
same API.

## Start of session

```bash
memory query --project=<name> --limit=10     # recent timeline; empty is fine for new projects
memory search "<topic>" --project=<name>     # specifics, full text
```

`<name>` is the repo / project you're working in. Also read your own standing rules:
`memory query --project=<agent-name> --limit=10`.

## Log as you work — not at the end

```bash
memory add "<content>" --agent=<you> --project=<name> --type=<type> \
  --tags='[{"name":"auth","description":"authentication"},{"name":"feature"}]'
```

- **Types** (enforced by the API, anything else is a 422): `decision` (architecture,
  tooling, judgment calls — include the rejected alternatives), `lesson` (mistakes,
  insights), `preference` (standing behavioral rules — user corrections or
  confirmations about how to work), `note` (everything else: changes, refactors,
  status, work log). Omitting `--type` is valid.
- **Tags** are a JSON array of `{"name", "description"}`; `description` is optional
  and a new tag without one defaults to its own name. Tag liberally — tags are what
  make future searches land.
- **Log:** what will still matter in a later session — decisions with their reasoning
  and the alternatives rejected, lessons with their cause, stated preferences, a
  deliberate non-action, an environment gotcha. Log as it happens, not at the end.
- **Skip:** diary entries. Before adding, ask: could a future session reconstruct this
  from `git log`? If yes, skip it. "Did X, shipped Y" with no reasoning is git's job.
  The what is usually in git; the why is not, and the why is what to write down.
- An agent's own cross-project working rules go under `--project=<agent-name>`.
- Never write memories to Claude Code's md-file memory (`~/.claude/.../memory/*.md`):
  it is neither shared nor checked in. The DB is the single source of truth.

## Look things up

```bash
memory query --project=<name> --since-days 7            # rolling window (0=today); also --since/--until YYYY-MM-DD
memory query --project=<name> --tag=bugfix --type=decision
memory search "auth flow" --project=<name>              # --tag=, --since= optional
memory show 42
memory tags ; memory projects ; memory stats
```

`query` returns up to 100 by default, `search` 20; raise with `--limit`.

## Fix or remove

```bash
memory update 42 --content "…" --add-tags='[{"name":"important"}]' --remove-tags draft
memory delete 49 --yes          # without --yes it's a dry run; takes multiple IDs
```

When a logged decision is reversed, add a new `decision` that says so and why, rather
than editing history — the timeline is the point.

Full command reference: `memory --help`.
