# MEMORY.md — agent memory protocol

The protocol lives in the top-level **[`SKILL.md`](SKILL.md)**, packaged as a Claude
Code skill so each project opts in explicitly, in its own tree, with no reference to
this checkout:

```bash
mkdir -p <project>/.claude/skills/memory
cp /path/to/agent-memory/SKILL.md <project>/.claude/skills/memory/
```

Commit the copy with the project. It loads on demand there — when the task matches its
description or via `/memory`.

This file is kept only so existing `@~/workspace/agent-memory/MEMORY.md` imports in other
repos' `CLAUDE.md` don't break silently; migrate those to a copied skill and drop the
import.
