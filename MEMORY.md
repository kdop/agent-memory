# MEMORY.md — agent memory protocol

Canonical protocol for the persistent memory system. `memory-cli` is a thin HTTP client
for the shared **memory API service** (Postgres-backed); the same service is reachable
via the HTTP API directly and the MCP server — see [README.md](README.md). **Other repos
import this file** instead of copying it:

    @~/workspace/agent-memory/MEMORY.md

Continuity across sessions: what you did, decided, learned. **Golden rule — if you
don't log it, it's gone next session.** Scope everything with `--project=<name>`.

## Usage

Invoke as `memory` (alias) or `memory-cli`. It talks to a running API server — it never
opens a database. The endpoint resolves `AGENT_MEMORY_API` env → stored `api_url`
(`memory config set api_url <url>`) → local default `http://127.0.0.1:8099`; the bearer
token resolves `AGENT_MEMORY_API_TOKEN` env → stored `api_token`. **A reachable server is
required** — if none is running the CLI prints how to start one
(`python -m agent_memory.server`) rather than a traceback. Attribute entries with
`--agent=<you>`.

    # Add — AS YOU WORK, not at session end
    # --tags is a JSON array of {"name":..., "description":...}; description is
    # optional — a brand-new tag with none just defaults to its own name.
    memory add "Implemented auth" --agent=<you> --project=<name> --type=note \
      --tags='[{"name":"auth","description":"authentication"},{"name":"feature"}]'

    # Query — filtered timeline
    memory query --project=<name> --limit=10
    memory query --project=<name> --since-days 7    # rolling window: since N days ago through now (0=today); also --since=, --until=
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

- **Types (enforced by the API — anything else is rejected with 422):**
  `decision` (architecture, tooling, judgment calls with rejected alternatives),
  `lesson` (mistakes, insights), `preference` (standing behavioral rules —
  corrections or confirmations about how to work), `note` (everything else:
  changes, refactors, status updates, plain work log). Blank/omitted is also
  valid (no type set).
- **Tag liberally** — tags are what make future searches land. A tag carries an
  optional descriptor (`{"name":..., "description":...}`); give one the first time
  you use a new tag if you can, but it's never required — an undescribed new tag
  just defaults to its own name.
- **Log:** decisions, bug fixes, features, and non-obvious things you learn or get
  wrong — as they happen, not at the end.
- **Start of session:** `memory query --project=<name> --limit=10` (empty is fine for
  new projects), then `memory search "<topic>"` for specifics.
