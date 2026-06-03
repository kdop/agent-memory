# CLAUDE.md — agent-memory project instructions

@AGENT.md

This project **is the memory system itself** — the `memory-cli` tool and the SQLite
database that give every agent session its continuity. You are working *on the tool*,
not just using it. That makes you both the maintainer and a user of the same system,
so changes here affect every other project (project-a, project-a2, …) that logs to it.

## 🧠 Memory System (you dogfood it)

Your memory lives in the **SQLite database** at `~/workspace/agent-memory/memory.db`
(override with `AGENT_MEMORY_DB`). Use the CLI as you work — log decisions, fixes, and
lessons *as they happen*, not at session end.

```bash
export AGENT_NAME=agent-a          # attribute work to you
memory add "Repointed DB_PATH to be AGENT_MEMORY_DB-overridable" \
  --project=agent-memory --tags=cli,migration --type=decision
memory query --project=agent-memory --limit=10
memory search "schema" --project=agent-memory
```

Log work on this project under **`--project=agent-memory`**. Types: `code`,
`decision`, `lesson`, `note`. Tag liberally.

## 🔧 What this project is

- **`memory-cli`** — a single-file Python 3 CLI, **stdlib only** (no `pip install`,
  no venv needed): `sqlite3`, `argparse`, `json`, `os`, `sys`, `datetime`, `pathlib`.
- **`memory.db`** — relational SQLite store. **Live and shared across all agents and
  projects.** Git-ignored (it's data, not source).
- Docs: `README.md` (entry), `README-memory.md`, `USER-GUIDE.md`, `API.md`,
  `ARCHITECTURE.md` (schema + design), `CONTRIBUTING.md`, `CHANGELOG.md`, `INDEX.md`.

## ⚠️ Operating rules (this project bites harder than most)

1. **The DB is live and shared.** Every other project depends on it. Before any
   schema change or destructive operation: **back up `memory.db` first**, verify on a
   copy, and confirm `stats`/row counts match before and after.
2. **Never hardcode the DB path again.** It resolves via `AGENT_MEMORY_DB` →
   default `~/workspace/agent-memory/memory.db`. Keep it that way.
3. **Stdlib only.** Don't add dependencies — portability across agent hosts is the
   point. If you think you need a package, reconsider.
4. **CHANGELOG discipline.** User-visible behavior changes get a `CHANGELOG.md`
   entry. Read `CONTRIBUTING.md` before changing the CLI.
5. **Test by copy, not in place.** Point `AGENT_MEMORY_DB` at a scratch file to
   exercise new behavior; never experiment against the live DB.
6. **Backward compatibility.** Other projects' `CLAUDE.md` reference this tool by
   path and the `memory` alias — don't rename or relocate without updating them.

## 📋 Session checklist

**Start:**
- [ ] Read persona: `~/workspace/agents/agent-a/{SOUL,IDENTITY,USER}.md`
- [ ] Read `README.md` + `ARCHITECTURE.md` (what the tool is and how the schema works)
- [ ] `memory query --project=agent-memory --limit=10` (recent work on the tool)

**During:**
- [ ] Log decisions/fixes as they happen, `--project=agent-memory`, tagged
- [ ] Back up the DB before anything destructive; test against a scratch DB

**End (optional):**
- [ ] CHANGELOG entry if behavior changed; session summary memory if significant

---

**Remember:** this is the tool that remembers everything else. Treat the live DB the
way you'd treat a production database — because for every other agent session, it is.
