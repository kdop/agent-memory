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
memory search "<topic>" --project=<name> --mode hybrid     # specifics, by words and by meaning
```

`<name>` is the repo / project you're working in. Also read your own standing rules:
`memory query --project=<agent-name> --limit=10`.

## Log as you work — not at the end

```bash
memory add "<content>" --agent=<you> --project=<name> --type=<type> \
  --tags='[{"name":"auth","description":"authentication"},{"name":"feature"}]'
```

- **Types** (the API accepts only these; anything else is a 422). The type says how
  to act on a memory, when you write it and when you read it back:

  | Type | What it is | How to act on it |
  |---|---|---|
  | `constraint` | A hard rule ("remote jobs only"). | Never break it. If a task conflicts with it, stop and ask. |
  | `preference` | A soft rule: how the user likes things done. | Follow it by default. If you trade it off, say so and why. |
  | `decision` | A settled choice, with the options turned down. | Don't reopen it without new information. |
  | `lesson` | A past cause and effect: a mistake, an insight. | Check it before doing similar work. |
  | `note` | Reference: anything else worth keeping. | No weight on how you act. |

  Rule 3 (the why) is expected for decisions, lessons and constraints; preferences and
  notes are exempt. Omitting `--type` is valid.
- **Tags** are a JSON array of `{"name", "description"}`; `description` is optional
  and a new tag without one defaults to its own name. Tag liberally — tags are what
  make future searches land.
- **Log:** what will still matter in a later session — decisions with their reasoning
  and the alternatives rejected, lessons with their cause, hard rules the user sets,
  stated preferences, a deliberate non-action, an environment gotcha. Log as it
  happens, not at the end.
- **Skip:** diary entries. Before adding, ask: could a future session reconstruct this
  from `git log`? If yes, skip it. "Did X, shipped Y" with no reasoning is git's job.
  The what is usually in git; the why is not, and the why is what to write down.
- **Warnings:** `add` may print `warning: short`, `warning: no-project` or
  `warning: no-reasoning` after the id. The memory is stored; the line says what the
  entry lacks (under 40 characters, no `--project`, a decision, lesson or constraint
  with no why). If the warning is right, fix the entry with `memory update <id>`.
- **Duplicate refusal:** `add` refuses an entry whose meaning is nearly the same as one
  already in the project. Nothing is stored; the message names the existing id and the
  exit code is 3. Update that memory instead. Use `--force` only when the new entry is
  a deliberate separate record, for example a reversal of a decision.
- **Review:** a `review:` line under a memory (on `show`, `query` or `search`) is a
  model's verdict on it against the rules above: `reject` names the rule it breaks
  (anything git holds or a project's own files should hold is rule 4); `improve` says
  the entry is worth keeping but needs one thing: `reason` (the why, for a decision,
  lesson or constraint only), `clarity`, `detail` or `scope`; `rewrite` is only for a
  repeat that adds more, with the merged text under `suggested:`. When the server is
  set to refuse, a refused `add` prints the verdict, stores nothing and exits with
  code 4; an `improve` prints "Low value memory, retry with more context or skip" and
  what is missing: add it and try again, or drop the entry. When the verdict is
  right, fix the entry and add it again; fixing a stored one with `memory update`
  puts it back to unverified for the model to check again. When you believe the entry is right as written, ask the user and pass `--force`
  only when the user says yes; never on your own. The header line of every memory says its status (`unverified` until
  the model has checked it, `verified` when it approved, `flagged` when it did not), and
  only verified memories count as reference when a new entry is checked. A memory that
  reverses an older one is stored with `supersedes #<old id>` in its header, the old one
  shows `superseded by #<new id>`, and `--current` on `query` and `search` hides the
  superseded ones.
- An agent's own cross-project working rules go under `--project=<agent-name>`.
- Never write memories to Claude Code's md-file memory (`~/.claude/.../memory/*.md`):
  it is neither shared nor checked in. The DB is the single source of truth.

## Look things up

```bash
memory query --project=<name> --since-days 7            # rolling window (0=today); also --since/--until YYYY-MM-DD
memory query --project=<name> --tag=bugfix --type=decision
memory search "auth flow" --project=<name> --mode hybrid   # by words and by meaning; --tag=, --since= optional
memory search "auth flow" --project=<name>                # by words only (the default)
memory show 42
memory tags ; memory projects ; memory stats
```

`query` returns up to 100 by default, `search` 20, and the footer says when more exist; `--all` (or `--limit 0`) returns every match.

## Fix or remove

```bash
memory update 42 --content "…" --add-tags='[{"name":"important"}]' --remove-tags draft
memory delete 49 --yes          # without --yes it's a dry run; takes multiple IDs
```

When a logged decision is reversed, add a new `decision` that says so and why, rather
than editing history — the timeline is the point. If that add is refused as a
duplicate, `--force` is the right call here. When the review answers `rewrite,
duplicate of #<id>`, the suggested text is that memory plus what you added: apply it
with `memory update <id> --content "…"` instead of adding a new memory.

Full command reference: `memory --help`.
