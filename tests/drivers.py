"""Cross-surface test drivers.

One `MemoryDriver` contract, implemented per surface, so the same behavioral
tests run against every surface and prove they don't drift (PLAN.md → Testing).

- `CliDriver`  — subprocess against `memory-cli` (this ticket, A0).
- `ApiDriver`  — httpx over the FastAPI service        (Ticket B).
- `McpDriver`  — MCP client                            (Ticket D).

Drivers return plain structured data (`Memory`, lists, dicts) — never raw CLI
chrome. Surface-specific output/exit-code/format assertions live in
`test_cli_surface.py`, not here. Every driver runs against a scratch
`AGENT_MEMORY_DB` (rule #1: the live DB is never touched).
"""

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

MEMORY_CLI = Path(__file__).resolve().parent.parent / "memory-cli"

# Lines a CLI block can start with that are NOT content.
_SEP = re.compile(r"^[─━]{10,}$")


@dataclass
class Memory:
    id: int
    agent: str | None = None
    project: str | None = None
    type: str | None = None
    tags: list = field(default_factory=list)
    content: str = ""
    snippet: str | None = None


class CliDriver:
    """Drives the CLI as a subprocess and parses its output back into data."""

    name = "cli"

    def __init__(self, db_path, agent="tester", extra_env=None):
        self.db_path = Path(db_path)
        self.agent = agent
        self.extra_env = extra_env or {}

    # ---- plumbing --------------------------------------------------------
    def _env(self):
        env = {**os.environ, "AGENT_NAME": self.agent}
        env["AGENT_MEMORY_DB"] = str(self.db_path)
        env.update(self.extra_env)
        return env

    def raw(self, *args, stdin=None):
        """Run the CLI verbatim; returns the CompletedProcess (for surface tests)."""
        return subprocess.run(
            [sys.executable, str(MEMORY_CLI), *args],
            env=self._env(), input=stdin, capture_output=True, text=True,
        )

    def initialize(self):
        # Prime the scratch DB so the one-time "Initialized…" banner doesn't bleed
        # into later assertions. --yes auto-creates (no TTY in a subprocess).
        self.raw("--yes", "stats")

    # ---- semantic operations --------------------------------------------
    def add(self, content, *, project=None, tags=None, type=None, agent=None):
        args = ["add", content]
        if project:
            args += ["--project", project]
        if tags:
            args += ["--tags", tags if isinstance(tags, str) else ",".join(tags)]
        if type:
            args += ["--type", type]
        if agent:
            args += ["--agent", agent]
        m = re.search(r"Memory #(\d+) added", self.raw(*args).stdout)
        return int(m.group(1)) if m else None

    def query(self, **filters):
        args = ["query"]
        for flag in ("today", "yesterday"):
            if filters.get(flag):
                args.append(f"--{flag}")
        for flag in ("since", "until", "project", "agent", "tag", "type"):
            if filters.get(flag):
                args += [f"--{flag}", str(filters[flag])]
        if filters.get("limit") is not None:
            args += ["--limit", str(filters["limit"])]
        return self._parse_memories(self.raw(*args).stdout)

    def search(self, text, **filters):
        args = ["search", text]
        for flag in ("project", "agent", "since", "tag"):
            if filters.get(flag):
                args += [f"--{flag}", str(filters[flag])]
        if filters.get("limit") is not None:
            args += ["--limit", str(filters["limit"])]
        out = self.raw(*args).stdout
        if "No memories found for" in out:
            return []
        return self._parse_memories(out, snippet=True)

    def get(self, mid):
        proc = self.raw("show", str(mid))
        if proc.returncode != 0 or "not found" in proc.stdout:
            return None
        mems = self._parse_memories(proc.stdout)
        return mems[0] if mems else None

    def update(self, mid, *, content=None, project=None, type=None,
               set_tags=None, add_tags=None, remove_tags=None, stdin=None):
        """Returns 'updated' | 'nochange' | None (not found)."""
        args = ["update", str(mid)]
        if content is not None:
            args += ["--content", content]
        if project is not None:
            args += ["--project", project]
        if type is not None:
            args += ["--type", type]
        if set_tags is not None:
            args += ["--set-tags", set_tags]
        if add_tags:
            args += ["--add-tags", add_tags]
        if remove_tags:
            args += ["--remove-tags", remove_tags]
        proc = self.raw(*args, stdin=stdin)
        if proc.returncode != 0 or "not found" in proc.stdout:
            return None
        return "updated" if "updated" in proc.stdout else "nochange"

    def delete(self, *ids):
        """Actually deletes (the CLI dry-run is a surface concern). Returns count removed."""
        proc = self.raw("delete", *[str(i) for i in ids], "--yes")
        m = re.search(r"Deleted (\d+)", proc.stdout)
        return int(m.group(1)) if m else 0

    def tags(self):
        out = self.raw("tags").stdout
        if "No tags found" in out:
            return []
        res = []
        for line in out.splitlines():
            m = re.match(r"\s+(\S+)\s+\((\d+)\)\s*$", line)
            if m:
                res.append((m.group(1), int(m.group(2))))
        return res

    def projects(self):
        out = self.raw("projects").stdout
        if "No projects found" in out:
            return []
        res = []
        for line in out.splitlines():
            m = re.match(r"\s+(\S+)\s+\((\d+) memories\)\s*$", line)
            if m:
                res.append((m.group(1), int(m.group(2))))
        return res

    def stats(self):
        out = self.raw("stats").stdout
        d = {}
        for key, label in (("total", "Total memories"), ("agents", "Agents"),
                           ("projects", "Projects"), ("tags", "Tags"),
                           ("today", "Today"), ("week", "Last 7 days")):
            m = re.search(rf"{re.escape(label)}:\s+(\d+)", out)
            if m:
                d[key] = int(m.group(1))
        return d

    # ---- output parsing --------------------------------------------------
    def _parse_memories(self, text, snippet=False):
        if "No memories found" in text:
            return []
        parts = re.split(r"━+ #(\d+)[^\n]*\n", text)
        out = []
        it = iter(parts[1:])  # parts[0] is the preamble before the first block
        for mid, body in zip(it, it):
            out.append(self._parse_block(int(mid), body, snippet=snippet))
        return out

    def _parse_block(self, mid, body, snippet=False):
        agent = project = type_ = None
        tags = []
        collected = []
        for line in body.split("\n"):
            if line.startswith("🕒"):
                continue
            if line.startswith("👤"):
                meta = line[len("👤"):].strip()
                tm = re.search(r"\[([^\]]+)\]\s*$", meta)
                if tm:
                    type_ = tm.group(1)
                    meta = meta[:tm.start()].strip()
                if " @ " in meta:
                    agent, project = (p.strip() for p in meta.split(" @ ", 1))
                else:
                    agent = meta.strip()
                continue
            if line.lstrip().startswith("🏷"):
                tagstr = re.sub(r"^[^\w]+", "", line.strip())
                tags = [t.strip() for t in tagstr.split(",") if t.strip()]
                continue
            if _SEP.match(line.strip()) or line.startswith("Found "):
                break
            collected.append(line)
        text_body = "\n".join(collected).strip()
        return Memory(
            id=mid, agent=agent, project=project, type=type_, tags=tags,
            content="" if snippet else text_body,
            snippet=text_body if snippet else None,
        )
