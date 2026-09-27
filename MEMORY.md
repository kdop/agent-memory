# MEMORY.md — agent memory protocol

The protocol lives in **[`skills/memory/SKILL.md`](skills/memory/SKILL.md)**, packaged
as a Claude Code plugin so each project installs it explicitly, at project scope, with
no reference to this checkout:

```bash
cd <project>
claude plugin marketplace add kdop/agent-memory
claude plugin install memory@agent-memory --scope project
```

Commit the resulting `.claude/settings.json` change with the project. It loads on
demand there — when the task matches its description or via `/memory`.

This file is kept only so existing `@~/workspace/agent-memory/MEMORY.md` imports in other
repos' `CLAUDE.md` don't break silently; migrate those to the plugin and drop the
import.
