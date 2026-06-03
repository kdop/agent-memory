# Memory CLI - Documentation Index

Complete documentation for Memory CLI - persistent AI agent memory system.

---

## Quick Links

| Document | Purpose | Audience |
|----------|---------|----------|
| **[README-memory.md](./README-memory.md)** | Quick start & reference | Everyone |
| **[USER-GUIDE.md](./USER-GUIDE.md)** | Comprehensive usage guide | Users |
| **[ARCHITECTURE.md](./ARCHITECTURE.md)** | Technical design & internals | Developers |
| **[CONTRIBUTING.md](./CONTRIBUTING.md)** | Development guide | Contributors |
| **[API.md](./API.md)** | Programmatic access | Integrators |
| **[CHANGELOG.md](./CHANGELOG.md)** | Version history | Everyone |

---

## Documentation Overview

### For First-Time Users

**Start here:**
1. [README-memory.md](./README-memory.md) - Quick start (5 min)
2. [USER-GUIDE.md](./USER-GUIDE.md) - Full tutorial (30 min)

**Then try:**
```bash
# Your first memory
memory add "Learning memory-cli" --tags=learning

# Query it
memory query --tag=learning

# See stats
memory stats
```

---

### For Daily Users

**Quick reference:**
- [README-memory.md](./README-memory.md) - Command syntax
- [USER-GUIDE.md § Commands Reference](./USER-GUIDE.md#commands-reference) - All commands
- [USER-GUIDE.md § Workflows](./USER-GUIDE.md#workflows) - Daily/weekly workflows

**Common tasks:**
```bash
# Daily workflow
memory query --yesterday        # What happened yesterday?
memory query --today           # What did I do today?
memory add "..." --project=X   # Log as you work

# Weekly review
memory query --since=2026-05-08
memory query --type=decision --since=2026-05-08
```

---

### For Developers

**Architecture & design:**
- [ARCHITECTURE.md](./ARCHITECTURE.md) - Full technical design
  - Database schema
  - Design decisions
  - Performance characteristics
  - Extensibility points

**Contributing:**
- [CONTRIBUTING.md](./CONTRIBUTING.md) - Development guide
  - Code structure
  - Adding features
  - Testing
  - Code style

**Programmatic access:**
- [API.md](./API.md) - Python API & integrations
  - Direct SQLite access
  - Advanced queries
  - Bulk operations
  - Integration examples

---

### For Integrators

**Using from code:**
1. [API.md § Python API](./API.md#python-api-direct-sqlite) - Direct SQLite
2. [API.md § Integration Examples](./API.md#integration-examples) - Real examples
3. [ARCHITECTURE.md § Schema](./ARCHITECTURE.md#database-schema) - Database structure

**Future HTTP API:**
- [API.md § Future HTTP API](./API.md#future-http-api) - Planned features

---

## Documentation by Topic

### Installation & Setup

| Topic | Document | Section |
|-------|----------|---------|
| Quick install | README-memory.md | [Installation](./README-memory.md#installation) |
| PATH setup | USER-GUIDE.md | [Installation](./USER-GUIDE.md#installation) |
| Verify installation | USER-GUIDE.md | [Verify Installation](./USER-GUIDE.md#verify-installation) |

---

### Basic Usage

| Topic | Document | Section |
|-------|----------|---------|
| Adding memories | USER-GUIDE.md | [Your First Memory](./USER-GUIDE.md#your-first-memory) |
| Querying | USER-GUIDE.md | [Query Your Memories](./USER-GUIDE.md#query-your-memories) |
| Searching | USER-GUIDE.md | [Search](./USER-GUIDE.md#search) |
| Tags & projects | USER-GUIDE.md | [Core Concepts](./USER-GUIDE.md#core-concepts) |

---

### Advanced Features

| Topic | Document | Section |
|-------|----------|---------|
| Date ranges | USER-GUIDE.md | [Date Range Queries](./USER-GUIDE.md#date-range-queries) |
| Combined filters | USER-GUIDE.md | [Complex Tag Queries](./USER-GUIDE.md#complex-tag-queries) |
| Shell integration | USER-GUIDE.md | [Using Shell Pipelines](./USER-GUIDE.md#using-shell-pipelines) |
| Export/backup | USER-GUIDE.md | [Export and Backup](./USER-GUIDE.md#export-and-backup) |

---

### Workflows & Patterns

| Topic | Document | Section |
|-------|----------|---------|
| Daily workflow | USER-GUIDE.md | [Daily Development](./USER-GUIDE.md#daily-development-workflow) |
| Weekly review | USER-GUIDE.md | [Weekly Review](./USER-GUIDE.md#weekly-review-workflow) |
| Bug tracking | USER-GUIDE.md | [Bug Tracking](./USER-GUIDE.md#bug-tracking-workflow) |
| Multi-project | USER-GUIDE.md | [Multi-Project](./USER-GUIDE.md#multi-project-workflow) |
| Agent handoff | USER-GUIDE.md | [Agent Handoff](./USER-GUIDE.md#agent-handoff-workflow) |

---

### Technical Details

| Topic | Document | Section |
|-------|----------|---------|
| Database schema | ARCHITECTURE.md | [Database Schema](./ARCHITECTURE.md#database-schema) |
| Design decisions | ARCHITECTURE.md | [Architecture Decisions](./ARCHITECTURE.md#architecture-decisions) |
| Performance | ARCHITECTURE.md | [Performance Characteristics](./ARCHITECTURE.md#performance-characteristics) |
| Concurrency | ARCHITECTURE.md | [Concurrency](./ARCHITECTURE.md#concurrency) |
| Extensibility | ARCHITECTURE.md | [Extensibility Points](./ARCHITECTURE.md#extensibility-points) |

---

### Development

| Topic | Document | Section |
|-------|----------|---------|
| Code structure | CONTRIBUTING.md | [Code Structure](./CONTRIBUTING.md#code-structure) |
| Adding features | CONTRIBUTING.md | [Adding New Features](./CONTRIBUTING.md#adding-new-features) |
| Testing | CONTRIBUTING.md | [Testing](./CONTRIBUTING.md#testing) |
| Code style | CONTRIBUTING.md | [Code Style](./CONTRIBUTING.md#code-style) |

---

### Programmatic Access

| Topic | Document | Section |
|-------|----------|---------|
| Python API | API.md | [Python API](./API.md#python-api-direct-sqlite) |
| Basic operations | API.md | [Basic Operations](./API.md#basic-operations) |
| Advanced queries | API.md | [Advanced Queries](./API.md#advanced-queries) |
| Bulk operations | API.md | [Bulk Operations](./API.md#bulk-operations) |
| Integrations | API.md | [Integration Examples](./API.md#integration-examples) |

---

### Troubleshooting

| Topic | Document | Section |
|-------|----------|---------|
| Common issues | USER-GUIDE.md | [Troubleshooting](./USER-GUIDE.md#troubleshooting) |
| FAQ | USER-GUIDE.md | [FAQ](./USER-GUIDE.md#faq) |
| Performance tuning | ARCHITECTURE.md | [Performance Tuning](./ARCHITECTURE.md#performance-tuning) |

---

## Quick Reference Cards

### Essential Commands

```bash
# Add
memory add "<content>" --project=X --tags=a,b --type=code

# Query
memory query --today
memory query --project=X --tag=Y --since=YYYY-MM-DD

# Search
memory search "text" --project=X

# Inspect / mutate by ID
memory show <id>
memory update <id> --content "..." --add-tags a,b --remove-tags c
memory delete <id> [<id> ...] --yes   # without --yes runs as dry-run

# Explore
memory tags
memory projects
memory stats
```

### All Filters

| Filter | query | search | Values |
|--------|-------|--------|--------|
| `--today` | ✅ | ❌ | flag |
| `--yesterday` | ✅ | ❌ | flag |
| `--since` | ✅ | ✅ | YYYY-MM-DD |
| `--until` | ✅ | ❌ | YYYY-MM-DD |
| `--project` | ✅ | ✅ | name |
| `--agent` | ✅ | ✅ | name |
| `--tag` | ✅ | ✅ | name |
| `--type` | ✅ | ❌ | decision\|code\|lesson\|note |
| `--limit` | ✅ | ✅ | number |

### Memory Types

| Type | Use For | Example |
|------|---------|---------|
| `decision` | Architectural decisions | "Chose PostgreSQL for ACID compliance" |
| `code` | Implementation | "Implemented JWT auth" |
| `lesson` | Learnings | "Always validate input first" |
| `note` | General | "Project kickoff meeting" |

---

## Project Structure

```
~/workspace/agents/
├── memory-cli              # Main executable
├── memory                  # Symlink for convenience
├── memory.db               # SQLite database
│
├── INDEX.md               # This file (documentation index)
├── README-memory.md       # Quick start & reference
├── USER-GUIDE.md          # Comprehensive user guide
├── ARCHITECTURE.md        # Technical architecture
├── CONTRIBUTING.md        # Development guide
├── API.md                 # Programmatic access
├── CHANGELOG.md           # Version history
│
└── install.sh             # Setup script
```

---

## Version History

See [CHANGELOG.md](./CHANGELOG.md) for detailed version history.

**Current version:** 1.1 (2026-05-17)

---

## Getting Help

### Documentation

1. Check this INDEX for the right document
2. Read the relevant section
3. Try the examples
4. Check FAQ in USER-GUIDE.md

### Still stuck?

- Re-read ARCHITECTURE.md for technical details
- Check CONTRIBUTING.md for advanced topics
- Look at API.md for programmatic examples

### Found a bug?

1. Check if it's already documented in troubleshooting
2. Verify your command syntax (see USER-GUIDE.md)
3. Test with minimal example
4. Report with: command, expected behavior, actual behavior

### Want a feature?

1. Check ARCHITECTURE.md § Extensibility Points
2. See if it's mentioned in CONTRIBUTING.md
3. Consider implementing it (CONTRIBUTING.md has guides)
4. Submit feature request with use case

---

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md) for:
- Development setup
- Code structure
- Adding features
- Testing
- Code style
- Release process

**We welcome contributions!** 🙏

---

## License

[TBD - Add license information]

---

## Acknowledgments

Built for AI agents by AI agents. 🤖

Special thanks to:
- SQLite team (amazing embedded database)
- Python community (great stdlib)
- Early testers and contributors

---

**Questions? Start with [USER-GUIDE.md](./USER-GUIDE.md).**  
**Technical deep-dive? See [ARCHITECTURE.md](./ARCHITECTURE.md).**  
**Want to contribute? Check [CONTRIBUTING.md](./CONTRIBUTING.md).**
