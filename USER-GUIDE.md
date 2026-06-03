# Memory CLI - User Guide

**Version:** 1.0  
**Last Updated:** 2026-05-15

Complete guide to using Memory CLI for persistent AI agent memory.

---

## Table of Contents

1. [Introduction](#introduction)
2. [Installation](#installation)
3. [Quick Start](#quick-start)
4. [Core Concepts](#core-concepts)
5. [Commands Reference](#commands-reference)
6. [Advanced Usage](#advanced-usage)
7. [Workflows](#workflows)
8. [Best Practices](#best-practices)
9. [Troubleshooting](#troubleshooting)
10. [FAQ](#faq)

---

## Introduction

### What is Memory CLI?

Memory CLI is a command-line tool for AI agents to maintain persistent, queryable memory across sessions. It solves the problem of agents starting each session with a "blank slate" by providing structured, searchable operational history.

### Why Use It?

**Without Memory CLI:**
```
Session 1: "I built authentication."
Session 2: "What authentication?" (no memory)
```

**With Memory CLI:**
```
Session 1: memory add "Built JWT auth" --project=myapp --tags=auth
Session 2: memory query --project=myapp --tag=auth
           → "Built JWT auth" (full context restored)
```

### When to Use It

✅ **Use Memory CLI for:**
- Operational timeline ("what did I do yesterday?")
- Implementation logs ("fixed bug X on date Y")
- Learnings ("this approach didn't work")
- Cross-project queries ("all my auth work")
- Agent continuity across sessions

❌ **Don't use Memory CLI for:**
- Project documentation (use README.md, ARCHITECTURE.md)
- Code snippets (use version control)
- Large binary data (use filesystem)
- Real-time collaboration (use chat/issues)

---

## Installation

### Prerequisites

- Python 3.7+ (built into most systems)
- No external dependencies required

### Quick Install

```bash
# 1. The tool is already at ~/workspace/agents/memory-cli
# 2. Make sure it's executable
chmod +x ~/workspace/agents/memory-cli

# 3. Add to PATH (optional, recommended)
echo 'export PATH="$HOME/workspace/agents:$PATH"' >> ~/.bashrc
source ~/.bashrc

# 4. Test
memory stats
```

### Verify Installation

```bash
$ memory stats
📊 Memory Statistics

Total memories:    0
Agents:            0
Projects:          0

Today:             0
Last 7 days:       0

Oldest:            N/A
Newest:            N/A
```

If you see this, you're good to go!

---

## Quick Start

### Your First Memory

```bash
# Add a simple memory
memory add "Learned how to use memory-cli"

# Add a memory with metadata
memory add "Built user authentication system" \
  --project=myapp \
  --agent=agent-a \
  --tags=auth,security \
  --type=code
```

### Query Your Memories

```bash
# See recent memories
memory query --limit=5

# See today's memories
memory query --today

# See all memories for a project
memory query --project=myapp
```

### Search

```bash
# Full-text search
memory search "authentication"

# Search within a project
memory search "authentication" --project=myapp
```

### Explore

```bash
# List all tags
memory tags

# List all projects
memory projects

# See statistics
memory stats
```

---

## Core Concepts

### Memories

A **memory** is a single entry with:
- **Content:** The actual memory text
- **Timestamp:** Auto-generated (local timezone)
- **Agent:** Who created it (e.g., "clu", "agent-a")
- **Project:** Which project it belongs to (optional)
- **Type:** Category (decision, code, lesson, note)
- **Tags:** Searchable labels (multiple allowed)

**Example:**
```
Memory #42
🕒 2026-05-15 14:30:15
👤 agent-a @ myapp [code]
🏷️  auth, security, jwt

Implemented JWT authentication with refresh token rotation
```

### Projects

A **project** is a namespace for organizing memories.

**Examples:**
- `myapp` - Your main application
- `project-a` - The project-a project
- `infrastructure` - DevOps work
- `learning` - Things you learned

**Queries:**
```bash
memory query --project=myapp
memory search "bug" --project=myapp
```

### Tags

**Tags** are labels for categorization and search.

**Examples:**
- `auth`, `security` - Feature areas
- `bugfix`, `feature` - Work types
- `urgent`, `important` - Priority
- `python`, `javascript` - Technologies
- `learning`, `mistake` - Meta-tags

**Features:**
- Case-insensitive (`auth` = `Auth` = `AUTH`)
- Multiple tags per memory
- Fast indexed queries
- Autocomplete-ready (all tags in one table)

**Queries:**
```bash
memory query --tag=auth
memory query --tag=bugfix --limit=5
memory search "implementation" --tag=auth
```

### Types

**Types** categorize the nature of the memory:

| Type | Use For | Example |
|------|---------|---------|
| `decision` | Architectural decisions | "Chose PostgreSQL over MySQL" |
| `code` | Implementation work | "Implemented user login" |
| `lesson` | Learnings, insights | "Always validate input first" |
| `note` | General notes, status | "Project kickoff meeting" |

**Queries:**
```bash
memory query --type=decision
memory query --type=lesson --since=2026-05-01
```

### Agents

**Agents** are who created the memory.

**Examples:**
- `clu` - OpenClaw agent
- `agent-a` - Claude CLI agent
- `your-name` - Manual entries

**Set your agent name:**
```bash
export AGENT_NAME=agent-a
# Add to ~/.bashrc to persist
```

**Queries:**
```bash
memory query --agent=agent-a --yesterday
memory query --agent=clu --project=myapp
```

---

## Commands Reference

### `memory add`

Add a new memory.

**Syntax:**
```bash
memory add "<content>" [options]
```

**Options:**
| Option | Type | Description |
|--------|------|-------------|
| `--project <name>` | string | Project name |
| `--agent <name>` | string | Agent name (default: $AGENT_NAME) |
| `--tags <tags>` | string | Comma-separated tags |
| `--type <type>` | string | decision, code, lesson, note |

**Examples:**
```bash
# Minimal
memory add "Fixed login bug"

# Full metadata
memory add "Implemented JWT authentication" \
  --project=myapp \
  --agent=agent-a \
  --tags=auth,security,jwt \
  --type=code

# Multiple tags
memory add "Learned about SQL injection" \
  --tags=security,learning,sql,vulnerability \
  --type=lesson

# Decision
memory add "Chose Redis for caching because of speed and simplicity" \
  --project=myapp \
  --tags=architecture,redis,caching \
  --type=decision
```

---

### `memory query`

Query memories with filters.

**Syntax:**
```bash
memory query [filters]
```

**Filters:**
| Filter | Type | Description |
|--------|------|-------------|
| `--today` | flag | Today's memories |
| `--yesterday` | flag | Yesterday's memories |
| `--since <date>` | YYYY-MM-DD | Since date (inclusive) |
| `--until <date>` | YYYY-MM-DD | Until date (inclusive) |
| `--project <name>` | string | Filter by project |
| `--agent <name>` | string | Filter by agent |
| `--tag <tag>` | string | Filter by tag (case-insensitive) |
| `--type <type>` | string | Filter by type |
| `--limit <n>` | number | Limit results |

**All filters can be combined!**

**Examples:**
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
memory query --project=myapp
memory query --project=myapp --today

# Type-based
memory query --type=decision
memory query --type=code --since=2026-05-01

# Agent-based
memory query --agent=agent-a --yesterday
memory query --agent=clu --project=myapp

# Combined
memory query --project=myapp --tag=auth --since=2026-05-01 --limit=5
memory query --type=decision --tag=architecture --yesterday
memory query --agent=agent-a --tag=bugfix --today
```

---

### `memory search`

Full-text search across memory content.

**Syntax:**
```bash
memory search "<query>" [filters]
```

**Filters:**
| Filter | Description |
|--------|-------------|
| `--project <name>` | Filter by project |
| `--agent <name>` | Filter by agent |
| `--tag <tag>` | Filter by tag |
| `--since <date>` | Since date |
| `--limit <n>` | Limit results (default: 20) |

**Features:**
- SQLite FTS5 full-text search
- Snippet highlighting (`→ match ←`)
- Ranked by relevance

**Examples:**
```bash
# Simple search
memory search "authentication"
memory search "bug fix"
memory search "decided to use"

# Search within project
memory search "authentication" --project=myapp

# Search with tag filter
memory search "implementation" --tag=auth

# Search recent memories
memory search "error" --since=2026-05-01

# Combined filters
memory search "redis caching" --project=myapp --tag=architecture --limit=3
```

---

### `memory tags`

List all tags with usage counts.

**Syntax:**
```bash
memory tags
```

**Output:**
```
🏷️  Tags:

  auth                 (15)
  bugfix               (12)
  feature              (10)
  security             (8)
  ...
```

**Use for:**
- See what tags exist (autocomplete reference)
- Find most-used tags
- Audit tag consistency

---

### `memory projects`

List all projects with memory counts.

**Syntax:**
```bash
memory projects
```

**Output:**
```
📂 Projects:

  myapp                (45 memories)
  project-a                (23 memories)
  infrastructure       (12 memories)
```

**Use for:**
- See all active projects
- Find which project has most memories

---

### `memory stats`

Show database statistics.

**Syntax:**
```bash
memory stats
```

**Output:**
```
📊 Memory Statistics

Total memories:    150
Agents:            2
Projects:          3
Tags:              25

Today:             8
Last 7 days:       42

Oldest:            2026-04-15 09:30:22
Newest:            2026-05-15 14:45:33
```

---

### `memory show`

Show one memory in full by ID.

**Syntax:**
```bash
memory show <id>
```

**Examples:**
```bash
memory show 42
```

Exits 1 if the ID doesn't exist.

---

### `memory update`

Update fields of an existing memory by ID. Content/project/type/tags can be changed independently or in one call (applied atomically).

**Syntax:**
```bash
memory update <id> [options]
```

**Options:**
| Option | Type | Description |
|--------|------|-------------|
| `--content <text>` | string | Replace content (use `-` to read from stdin) |
| `--content-file <path>` | string | Read new content from file |
| `--project <name>` | string | Set project (empty string clears) |
| `--type <type>` | string | Set type (empty string clears) |
| `--set-tags <a,b,c>` | string | Replace all tags |
| `--add-tags <a,b,c>` | string | Add tags (idempotent) |
| `--remove-tags <a,b,c>` | string | Remove tags |

`--content` and `--content-file` are mutually exclusive.

**Examples:**
```bash
# Fix a typo
memory update 42 --content "corrected text"

# Replace content from a file (useful for long edits)
memory update 42 --content-file /tmp/new-content.md

# Read from stdin
echo "new text" | memory update 42 --content -

# Add tags to an existing memory
memory update 42 --add-tags "important,review"

# Remove tags
memory update 42 --remove-tags "draft"

# Replace all tags
memory update 42 --set-tags "cv,lesson,important"

# Change multiple fields in one call (atomic)
memory update 42 --content "new content" --type lesson --add-tags "fixed"
```

The FTS index auto-updates via trigger when content changes.

---

### `memory delete`

Delete one or more memories by ID. Safe by default (dry-run unless `--yes`).

**Syntax:**
```bash
memory delete <id> [<id> ...] [--yes]
```

**Behavior:**
- Without `--yes` (or `-y`): prints what would be deleted, exits without changes.
- With `--yes`: deletes the memories. Cascade removes associated tags (via FK `ON DELETE CASCADE`) and FTS index entries (via trigger).

**Examples:**
```bash
# Dry-run — see what would be deleted
memory delete 49

# Actually delete
memory delete 49 --yes

# Delete multiple
memory delete 10 11 12 --yes

# Short flag
memory delete 49 -y
```

If any IDs don't exist, they're reported as missing and the rest are still processed.

---

## Advanced Usage

### Date Range Queries

```bash
# Last week
memory query --since=2026-05-08

# Specific week
memory query --since=2026-05-08 --until=2026-05-15

# Last month (approximate)
memory query --since=2026-04-15

# Today's work on specific project
memory query --project=myapp --today --limit=10
```

### Complex Tag Queries

```bash
# All security work
memory query --tag=security

# All urgent bugfixes
memory query --tag=urgent --tag=bugfix  # Note: This is OR, not AND currently

# Architecture decisions about databases
memory query --type=decision --tag=database
```

**Note:** Currently, multiple `--tag` filters aren't supported in a single command. Use search for complex queries:

```bash
memory search "auth security" --tag=urgent
```

### Combining Search and Filters

```bash
# Search for "performance" in myapp project, architecture decisions only
memory search "performance" --project=myapp --type=decision

# Recent errors with urgency
memory search "error exception" --tag=urgent --since=2026-05-10

# All authentication bugs from last week
memory search "authentication bug" --since=2026-05-08 --limit=10
```

### Using Shell Pipelines

```bash
# Count memories per project
memory projects | grep -oP '\(\K[0-9]+' | awk '{sum+=$1} END {print sum}'

# Extract all tags
memory tags | awk '{print $1}' | grep -v Tags

# Today's memory count
memory query --today | grep "Found" | awk '{print $2}'

# Most active day (requires jq)
memory query --limit=1000 | grep "🕒" | awk '{print $2}' | sort | uniq -c | sort -rn | head -1
```

### Export and Backup

```bash
# Backup database
cp ~/workspace/agents/memory.db ~/backups/memory-$(date +%Y%m%d).db

# Export to JSON (requires jq)
sqlite3 -json ~/workspace/agents/memory.db "SELECT * FROM memories" > memories.json

# Export to CSV
sqlite3 ~/workspace/agents/memory.db <<EOF
.mode csv
.headers on
.output memories.csv
SELECT * FROM memories;
.quit
EOF
```

---

## Workflows

### Daily Development Workflow

**Morning:**
```bash
# What happened yesterday?
memory query --yesterday

# What's on my plate for today? (check TODOs)
memory query --tag=todo --project=myapp
```

**During work:**
```bash
# Log as you go
memory add "Fixed null pointer exception in login handler" \
  --project=myapp \
  --tags=bugfix,auth \
  --type=code

memory add "Decided to use bcrypt for password hashing" \
  --project=myapp \
  --tags=security,decision,auth \
  --type=decision
```

**End of day:**
```bash
# Review what you did
memory query --today

# Add summary if significant
memory add "Completed auth module refactor, all tests passing" \
  --project=myapp \
  --tags=summary,milestone,auth \
  --type=note
```

---

### Weekly Review Workflow

```bash
# Last 7 days of work
memory query --since=$(date -d '7 days ago' +%Y-%m-%d)

# All decisions from last week
memory query --type=decision --since=$(date -d '7 days ago' +%Y-%m-%d)

# All lessons learned
memory query --type=lesson --since=$(date -d '7 days ago' +%Y-%m-%d)

# Most used tags this week
memory tags | head -10

# Tag TODO items for next week
memory add "TODO: Implement rate limiting" \
  --project=myapp \
  --tags=todo,security,next-week
```

---

### Bug Tracking Workflow

```bash
# Log bug discovery
memory add "Found: Login fails with special characters in password" \
  --project=myapp \
  --tags=bug,auth,urgent \
  --type=note

# Log investigation
memory add "Debugged password encoding issue - missing URL encoding" \
  --project=myapp \
  --tags=bug,auth,debugging \
  --type=note

# Log fix
memory add "Fixed password encoding bug with urllib.parse.quote" \
  --project=myapp \
  --tags=bugfix,auth \
  --type=code

# Later: Find all auth bugs
memory query --tag=bug --tag=auth
```

---

### Learning Workflow

```bash
# Log learnings as they happen
memory add "Learned: Always use parameterized queries to prevent SQL injection" \
  --tags=security,sql,learning \
  --type=lesson

memory add "Learned: Redis sorted sets are perfect for leaderboards" \
  --tags=redis,learning,data-structures \
  --type=lesson

# Later: Review all lessons
memory query --type=lesson

# Review lessons on specific topic
memory search "learned" --tag=security
```

---

### Multi-Project Workflow

```bash
# Work on project A
memory add "Built API endpoint for user registration" \
  --project=projectA \
  --tags=api,backend,users \
  --type=code

# Switch to project B
memory add "Deployed new feature to staging" \
  --project=projectB \
  --tags=deployment,staging \
  --type=note

# Later: What did I do across all projects today?
memory query --today

# What happened on project A this week?
memory query --project=projectA --since=$(date -d '7 days ago' +%Y-%m-%d)
```

---

### Agent Handoff Workflow

**Scenario:** Clu starts work, agent-a continues it.

**Clu:**
```bash
export AGENT_NAME=clu
memory add "Started authentication implementation, created user model" \
  --project=myapp \
  --agent=clu \
  --tags=auth,wip \
  --type=code
```

**agent-a (later session):**
```bash
export AGENT_NAME=agent-a

# Check what Clu did
memory query --project=myapp --agent=clu --limit=5

# Continue the work
memory add "Completed authentication - added JWT tokens and tests" \
  --project=myapp \
  --agent=agent-a \
  --tags=auth,complete \
  --type=code
```

**Anyone (status check):**
```bash
# What's the status of auth work?
memory query --project=myapp --tag=auth
```

---

## Best Practices

### 1. **Log During Work, Not After**

❌ **Bad:**
```bash
# At end of day: "What did I do today?"
# Try to remember and log it all
```

✅ **Good:**
```bash
# As you work:
memory add "Fixed login bug"
memory add "Refactored auth module"
memory add "Added tests for registration"
```

---

### 2. **Use Descriptive Content**

❌ **Bad:**
```bash
memory add "Fixed it" --tags=bugfix
```

✅ **Good:**
```bash
memory add "Fixed null pointer exception in login handler when password field is empty" \
  --project=myapp \
  --tags=bugfix,auth,validation \
  --type=code
```

---

### 3. **Tag Liberally**

❌ **Bad:**
```bash
memory add "Built user authentication" --tags=code
```

✅ **Good:**
```bash
memory add "Built user authentication" \
  --tags=auth,security,jwt,backend,api,users \
  --type=code
```

More tags = easier to find later.

---

### 4. **Use Consistent Tag Naming**

✅ **Good:**
```bash
--tags=auth,security,bugfix
--tags=auth,security,feature
--tags=auth,security,refactor
```

❌ **Bad:**
```bash
--tags=authentication,sec,bug-fix
--tags=Auth,Security,new-feature
--tags=auth,safety,refactoring
```

**Tip:** Run `memory tags` regularly to see existing tags and maintain consistency.

---

### 5. **Document Decisions with Rationale**

❌ **Bad:**
```bash
memory add "Using PostgreSQL" --type=decision
```

✅ **Good:**
```bash
memory add "Chose PostgreSQL over MySQL for better JSON support, ACID compliance, and active community" \
  --project=myapp \
  --tags=database,postgresql,architecture \
  --type=decision
```

---

### 6. **Capture Learnings and Mistakes**

```bash
# When you learn something
memory add "Learned: Always close database connections in finally blocks" \
  --tags=database,python,learning,best-practice \
  --type=lesson

# When you make a mistake
memory add "Mistake: Deployed to production without testing - caused 2h outage. Always test on staging first!" \
  --tags=deployment,mistake,learning,process \
  --type=lesson
```

Future-you will thank you.

---

### 7. **Use Types Appropriately**

| Type | When to Use |
|------|-------------|
| `decision` | Architectural decisions, tool choices |
| `code` | Implementation work, refactors, features |
| `lesson` | Learnings, insights, mistakes |
| `note` | Status updates, observations, TODOs |

---

### 8. **Project Scoping**

✅ **Do:**
- Use `--project=myapp` for project-specific work
- Use no project for general learnings/meta work

❌ **Don't:**
- Mix project names (`myapp`, `my-app`, `MyApp`)
- Use project for non-project work

---

### 9. **Regular Reviews**

**Daily:**
```bash
memory query --today
```

**Weekly:**
```bash
memory query --since=$(date -d '7 days ago' +%Y-%m-%d)
memory query --type=decision --since=$(date -d '7 days ago' +%Y-%m-%d)
memory query --type=lesson --since=$(date -d '7 days ago' +%Y-%m-%d)
```

**Monthly:**
```bash
memory query --since=$(date -d '30 days ago' +%Y-%m-%d) --limit=50
memory tags  # Review tag usage
```

---

## Troubleshooting

### "Command not found: memory"

**Solution:**
```bash
# Use full path
~/workspace/agents/memory-cli stats

# Or add to PATH
echo 'export PATH="$HOME/workspace/agents:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

---

### "No memories found" but you know they exist

**Check filters:**
```bash
# Remove all filters
memory query --limit=100

# Check specific project
memory query --project=yourproject
```

**Check date:**
```bash
# Today might be empty, try yesterday
memory query --yesterday

# Or broader date range
memory query --since=2026-05-01
```

---

### Case-sensitive tag not matching

Tags are case-insensitive. `--tag=auth` matches `Auth`, `AUTH`, `auth`.

If not matching:
```bash
# Check available tags
memory tags

# Check exact spelling
memory tags | grep -i auth
```

---

### Database corrupted

**Symptoms:** Errors like "database disk image is malformed"

**Solution:**
```bash
# Check integrity
sqlite3 ~/workspace/agents/memory.db "PRAGMA integrity_check;"

# If corrupted, restore from backup
cp ~/backups/memory-20260515.db ~/workspace/agents/memory.db

# If no backup, try to recover
sqlite3 ~/workspace/agents/memory.db ".recover" | sqlite3 ~/workspace/agents/memory-recovered.db
```

---

### Slow queries

**For large databases (>10k memories):**

```bash
# Analyze database
sqlite3 ~/workspace/agents/memory.db "ANALYZE;"

# Vacuum (reclaim space, optimize)
sqlite3 ~/workspace/agents/memory.db "VACUUM;"
```

**Still slow? Check query:**
```bash
# Use LIMIT
memory query --limit=100

# Use specific filters (faster)
memory query --project=myapp --since=2026-05-01
```

---

## FAQ

### Q: Can I use this on multiple machines?

**A:** Yes, sync `~/workspace/agents/memory.db` via:
- Syncthing (recommended)
- Dropbox / Google Drive (works, but beware of conflicts)
- Git (works, but binary diffs are useless)
- rsync (manual, but safe)

**Tip:** Use SQLite WAL mode (enabled by default) for better conflict handling.

---

### Q: Can multiple agents write at the same time?

**A:** Yes, SQLite WAL mode allows concurrent reads and one writer. For high concurrency, consider a server-based DB.

---

### Q: How do I delete memories?

**A:** Use `memory delete`:

```bash
# Dry-run — shows what would be deleted
memory delete 123

# Actually delete (with --yes / -y)
memory delete 123 --yes

# Delete several at once
memory delete 10 11 12 --yes
```

The CLI handles tag cleanup (via FK cascade) and FTS index sync (via trigger). For bulk deletes by project or other criteria, drop to SQL:

```bash
python3 -c "
import sqlite3
c = sqlite3.connect('/path/to/memory.db')
c.execute('PRAGMA foreign_keys = ON')
c.execute(\"DELETE FROM memories WHERE project = 'oldproject'\")
c.commit()
"
```

**Caution:** Deletes are permanent (unless you have backups).

### Q: How do I update or fix a memory?

**A:** Use `memory update`:

```bash
# Fix content
memory update 42 --content "corrected text"

# Adjust tags
memory update 42 --add-tags "important" --remove-tags "draft"

# Change multiple fields in one atomic call
memory update 42 --content "new text" --type lesson --set-tags "cv,fixed"
```

See `memory update --help` for all options.

---

### Q: Can I rename tags?

**A:** Yes, with SQL:

```bash
sqlite3 ~/workspace/agents/memory.db

UPDATE tags SET name = 'authentication' WHERE name = 'auth';

.quit
```

All memories with `auth` tag will now show `authentication`.

---

### Q: How big can the database get?

**A:** SQLite can handle databases up to 281 TB. Practically:
- 10k memories ≈ 10 MB
- 100k memories ≈ 100 MB
- 1M memories ≈ 1 GB

Performance stays good up to 100k+ memories with proper indexing.

---

### Q: Can I export to other formats?

**A:** Yes:

```bash
# JSON
sqlite3 -json ~/workspace/agents/memory.db "SELECT * FROM memories" > memories.json

# CSV
sqlite3 ~/workspace/agents/memory.db <<EOF
.mode csv
.output memories.csv
SELECT * FROM memories;
.quit
EOF

# Markdown (custom script)
memory query --limit=1000 > memories.txt
```

---

### Q: What if I want a web UI?

**A:** Use [Datasette](https://datasette.io/):

```bash
pip install datasette
datasette ~/workspace/agents/memory.db
```

Opens at http://localhost:8001 with full query UI.

---

### Q: Can I use this for personal notes, not just code?

**A:** Absolutely! It's generic.

```bash
memory add "Read 'Atomic Habits' - excellent book on behavior change" \
  --tags=reading,books,personal-development \
  --type=note

memory add "Gym: 5x5 squats at 100kg - new PR!" \
  --tags=fitness,gym,personal \
  --type=note
```

---

### Q: How do I see the raw SQL schema?

```bash
sqlite3 ~/workspace/agents/memory.db .schema
```

Or see [ARCHITECTURE.md](./ARCHITECTURE.md).

---

## Next Steps

- **Try it:** Add your first 10 memories
- **Review:** `memory query --today` at end of day
- **Refine:** Adjust tags and types as you go
- **Evolve:** Add features (see ARCHITECTURE.md)

**Happy remembering!** 🧠

---

_For technical details, see [ARCHITECTURE.md](./ARCHITECTURE.md)._  
_For quick reference, see [README-memory.md](./README-memory.md)._
