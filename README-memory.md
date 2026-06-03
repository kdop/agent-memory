# Memory CLI - Shared Memory System

SQLite-based memory system for AI agents (Clu, agent-a, etc.)

## 🧠 Philosophy: Docs vs Memory

### When to Use Markdown Documentation

**Purpose:** What the project **IS**

- ✅ Project fundamentals (README.md)
- ✅ Architecture decisions (ARCHITECTURE.md)
- ✅ Design patterns and rationale
- ✅ Tech stack (as documentation)
- ✅ Setup instructions
- ✅ API contracts

**Why Markdown:**
- Lives WITH the project (git-tracked)
- Human-readable in file browser
- Version controlled (diffs, history)
- Standard documentation format
- Can reference from code/README
- Agents read on session start

**Example:**
```markdown
# ARCHITECTURE.md

## Database Choice

**Decision:** PostgreSQL over MySQL

**Rationale:**
- Better JSON support
- ACID compliance
- Strong community
```

### When to Use Memory Database

**Purpose:** What you **DID**

- ✅ Timeline of work ("Fixed bug X yesterday")
- ✅ Implementation events ("Deployed feature Y")
- ✅ Learnings ("This approach didn't work")
- ✅ Context ("Still debugging auth issue")
- ✅ Cross-project queries ("All my auth work")

**Why SQLite:**
- Queryable ("show me all auth work from last week")
- Time-based ("what happened yesterday?")
- Cross-project ("all database decisions across projects")
- Tagged/searchable
- Operational timeline

**Example:**
```bash
memory add "Implemented PostgreSQL schema for users table" \
  --project=project-a \
  --tags=database,implementation,schema \
  --type=code
```

### Hybrid Workflow

**When you make an architectural decision:**

1. **Document in ARCHITECTURE.md** (the decision itself)
   ```markdown
   ## Authentication Strategy
   **Decision:** JWT with refresh tokens
   **Date:** 2026-05-15
   **Rationale:** ...
   ```

2. **Log implementation in memory** (when you did it)
   ```bash
   memory add "Implemented JWT auth with refresh token rotation" \
     --project=project-a \
     --tags=auth,security,jwt \
     --type=code
   ```

**When you learn something:**

- **Big lesson, reusable:** Update ARCHITECTURE.md or create a LESSONS.md
- **Specific context:** Log in memory with `--type=lesson`

**When you fix a bug:**

- **Always log in memory** (searchable timeline)
- **If it reveals design flaw:** Update ARCHITECTURE.md

### Quick Decision Guide

| Question | Answer | Where |
|----------|--------|-------|
| What is this project? | README.md | Markdown |
| How does it work? | ARCHITECTURE.md | Markdown |
| What did we do yesterday? | Timeline | Memory DB |
| Why did we choose Redis? | Design decision | ARCHITECTURE.md |
| When did we implement Redis? | Implementation event | Memory DB |
| What's our auth strategy? | Documentation | ARCHITECTURE.md |
| Fixed auth bug last week? | Timeline search | Memory DB |

---

## Quick Start

```bash
# Add a memory
memory add "Fixed login bug" \
  --project=project-a \
  --agent=agent-a \
  --tags=bugfix,auth \
  --type=code

# Query today's memories
memory query --today

# Search across all memories
memory search "what were we doing on the CV"

# List tags
memory tags

# Show stats
memory stats

# Fix a typo in memory #42
memory update 42 --content "corrected text"

# Delete obsolete memories (dry-run first)
memory delete 10 11 12
memory delete 10 11 12 --yes
```

## Quick Reference

### Available Filters

| Filter | Query | Search | Values |
|--------|-------|--------|--------|
| `--today` | ✅ | ❌ | (flag) |
| `--yesterday` | ✅ | ❌ | (flag) |
| `--since` | ✅ | ✅ | YYYY-MM-DD |
| `--until` | ✅ | ❌ | YYYY-MM-DD |
| `--project` | ✅ | ✅ | project name |
| `--agent` | ✅ | ✅ | clu, agent-a, etc. |
| `--tag` | ✅ | ✅ | tag name (case-insensitive) |
| `--type` | ✅ | ❌ | decision, code, lesson, note |
| `--limit` | ✅ | ✅ | number |

### Common Patterns

```bash
# Time-based
memory query --today
memory query --yesterday  
memory query --since=2026-05-01
memory query --since=2026-05-01 --until=2026-05-15

# Tag-based
memory query --tag=auth
memory query --tag=bugfix --limit=5

# Project-based
memory query --project=project-a
memory query --project=project-a --today

# Type-based
memory query --type=decision
memory query --type=code --since=2026-05-01

# Combined
memory query --project=project-a --tag=auth --since=2026-05-08
memory search "implementation" --project=project-a --tag=feature
```

---

## Commands

### Add
```bash
memory add <content> [options]

Options:
  --project <name>         Project name (e.g., project-a)
  --agent <name>           Agent name (auto-detected from $AGENT_NAME)
  --tags <comma,separated> Tags for categorization
  --type <type>            decision|code|lesson|note
```

### Query
```bash
memory query [filters]

Filters:
  --today                  Today's memories
  --yesterday              Yesterday's memories
  --since YYYY-MM-DD       Since date (inclusive)
  --until YYYY-MM-DD       Until date (inclusive)
  --project <name>         Filter by project
  --agent <name>           Filter by agent (clu, agent-a, etc.)
  --tag <tag>              Filter by tag (case-insensitive)
  --type <type>            Filter by type (decision|code|lesson|note)
  --limit <n>              Limit results (default: all)

All filters can be combined!
```

### Search
```bash
memory search <query> [filters]

Full-text search with SQLite FTS5.

Filters:
  --project <name>         Filter by project
  --agent <name>           Filter by agent
  --tag <tag>              Filter by tag
  --since YYYY-MM-DD       Since date
  --limit <n>              Limit results (default: 20)

Search uses FTS5 for fast full-text matching with snippet highlighting.
```

### Show
```bash
memory show <id>         # Show one memory in full by ID
```

### Update
```bash
memory update <id> [options]

Options:
  --content <text>           Replace content (use '-' for stdin)
  --content-file <path>      Read new content from file
  --project <name>           Set project (empty string clears)
  --type <type>              Set type (empty string clears)
  --set-tags <a,b,c>         Replace all tags
  --add-tags <a,b,c>         Add tags (idempotent)
  --remove-tags <a,b,c>      Remove tags
```

### Delete
```bash
memory delete <id> [<id> ...] [--yes]

Without --yes runs as a dry-run: prints what would be deleted.
With --yes (or -y) actually deletes. Cascade-safe (tags + FTS index).
```

### Other Commands
```bash
memory tags              # List all tags with counts
memory projects          # List all projects
memory stats             # Show statistics
```

## Architecture

- **Database:** `~/workspace/agents/memory.db` (SQLite with FTS5)
- **Schema:** Full relational design (3 tables)
  ```sql
  -- Main memories table
  memories (
    id INTEGER PRIMARY KEY,
    timestamp DATETIME (auto, local timezone),
    agent TEXT (e.g., 'clu', 'agent-a'),
    project TEXT (e.g., 'project-a', null for general),
    content TEXT (full-text indexed via FTS5),
    type TEXT (decision|code|lesson|note)
  )
  
  -- Tags table (canonical tag names)
  tags (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE (case-insensitive)
  )
  
  -- Many-to-many junction table
  memory_tags (
    memory_id INTEGER → memories.id,
    tag_id INTEGER → tags.id,
    PRIMARY KEY (memory_id, tag_id)
  )
  ```

**Why relational?**
- ✅ Properly indexed (fast tag queries)
- ✅ Case-insensitive tag matching (no "auth" vs "Auth" duplicates)
- ✅ Tag autocomplete/suggestions
- ✅ Global tag rename capability
- ✅ Tag usage analytics
- ✅ Scales to 100k+ memories

## Reading Strategy for Agents

### Session Start (agent-a example)

```bash
# 1. Read project fundamentals (Markdown)
read ./README.md
read ./ARCHITECTURE.md

# 2. Check recent work (Memory DB)
memory query --project=project-a --limit=10

# 3. Search for specific context if needed
memory search "authentication" --project=project-a
```

**Why this order:**
- Markdown gives you **what the project is**
- Memory gives you **what happened recently**
- Search gives you **specific context**

---

## Usage from Agents

### Clu (OpenClaw)
```bash
# Set agent name in environment (optional)
export AGENT_NAME=clu

# Log decisions, events, learnings
memory add "Scheduled daily backup cron" \
  --type=decision \
  --tags=infrastructure,automation
```

### agent-a (Claude CLI)
```bash
# Set agent name
export AGENT_NAME=agent-a

# In project-a project
cd ~/workspace/project-a

# Log code work
memory add "Refactored auth module to use JWT" \
  --project=project-a \
  --tags=auth,refactor \
  --type=code

# Query project history
memory query --project=project-a --limit=10

# Search for context
memory search "authentication changes"
```

## Shared Memory Protocol

Both agents write to the same database:
- Auto-timestamped entries
- Tag-based organization
- Project-scoped memories
- Full-text search across all content

When either agent asks "what's the status?" - both can query the same memory.

## Benefits over Markdown

✅ Auto-timestamping  
✅ Structured queries ("show me CV work from last week")  
✅ Tag-based organization  
✅ No file collision between agents  
✅ Fast full-text search (FTS5)  
✅ Can evolve to HTTP API later  
✅ Atomic writes, no race conditions  

## Installation

The tool is already at `~/workspace/agents/memory-cli` (executable Python script).

Optional: Add to PATH for easier access:
```bash
# Add to ~/.bashrc or ~/.zshrc
export PATH="$HOME/workspace/agents:$PATH"
```

Then you can just use `memory` instead of `~/workspace/agents/memory-cli`.

## Examples

### Adding Memories

```bash
# Simple note
memory add "Fixed login timeout issue"

# With full metadata
memory add "Implemented new feature X" \
  --project=project-a \
  --tags=feature,backend \
  --type=code

memory add "Decided to use Redis for caching" \
  --project=project-a \
  --tags=architecture,redis,caching \
  --type=decision

memory add "Learned: Always validate input before DB insert" \
  --project=project-a \
  --tags=security,lesson,validation \
  --type=lesson
```

### Time-Based Queries

```bash
# Today's work
memory query --today

# Yesterday's work
memory query --yesterday

# Last week (since date)
memory query --since=2026-05-08

# Date range
memory query --since=2026-05-01 --until=2026-05-15

# Today's work on specific project
memory query --project=project-a --today
```

### Tag-Based Queries

```bash
# All architecture decisions
memory query --tag=architecture

# All bugfixes (case-insensitive)
memory query --tag=BugFix

# Architecture decisions on project-a project
memory query --project=project-a --tag=architecture

# Recent auth work
memory query --tag=auth --limit=5
```

### Type-Based Queries

```bash
# All design decisions
memory query --type=decision

# All code implementations
memory query --type=code

# All lessons learned
memory query --type=lesson

# Decisions from last month
memory query --type=decision --since=2026-04-01
```

### Combined Filters

```bash
# Auth work from last week
memory query --tag=auth --since=2026-05-08 --limit=10

# project-a architecture decisions
memory query --project=project-a --type=decision --tag=architecture

# agent-a's recent bugfixes
memory query --agent=agent-a --tag=bugfix --limit=5

# Today's code work on project-a
memory query --project=project-a --type=code --today
```

### Full-Text Search

```bash
# Simple search
memory search "authentication"

# Search with project filter
memory search "authentication" --project=project-a

# Search with tag filter
memory search "caching decision" --tag=architecture

# Search recent memories
memory search "bug fix" --since=2026-05-01

# Search with multiple filters
memory search "redis implementation" --project=project-a --tag=caching --limit=3
```

### Agent-Specific Queries

```bash
# What did agent-a do yesterday?
memory query --agent=agent-a --yesterday

# What did Clu do today?
memory query --agent=clu --today

# Cross-agent: all auth work
memory query --tag=auth  # both agents
```

### Project Workflow

```bash
# Start of day: what happened yesterday?
memory query --project=project-a --yesterday

# During work: log as you go
memory add "Refactored auth module" \
  --project=project-a \
  --tags=refactor,auth \
  --type=code

# End of day: review
memory query --project=project-a --today

# Weekly review: all decisions
memory query --project=project-a --type=decision --since=2026-05-08
```

## Integration with CLAUDE.md

Update your project's CLAUDE.md to use `memory` commands instead of writing markdown files.

See `~/workspace/project-a/CLAUDE.md` for the updated workflow.
