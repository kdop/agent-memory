"""Helpers for driving the CLI in tests, and `recall_at` for the search
quality tests.

`CliDriver` runs the real `memory-cli` as a subprocess against a live
server, steered by `AGENT_MEMORY_API` / `AGENT_MEMORY_API_TOKEN`, so it runs
the real remote path (ApiClient over HTTP). `raw` gives the finished process
for tests of the output itself; `add`, `get`, `query` and `search` parse the
output back into `Memory` records, so a test can read the status and the
supersedes links out of the headers.
"""

# Keep the PEP 604 (`str | None`) annotations below evaluable as strings so the
# harness imports on every supported runtime, not just 3.10+.
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

MEMORY_CLI = Path(__file__).resolve().parent.parent / "memory-cli"


def _normalize_tags(tags):
    """Canonicalize a driver-level `tags`/`set_tags`/`add_tags` argument into the
    wire shape every surface expects: a list of {"name": ..., "description": ...}
    dicts (description omitted unless given). Accepts, for test convenience: None,
    a comma-separated string of bare names, a list of bare names, or already-shaped
    dicts (mixed freely)."""
    if tags is None:
        return None
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    return [t if isinstance(t, dict) else {"name": t} for t in tags]


def recall_at(k, results, expected):
    """Share of the expected ids found in the top `k` results.

    `results` is a ranked list of memory ids, or of objects with an `id` field
    (a `Memory`, or a dict from the repository). `expected` is the set of ids
    that answer the question. Returns a float in [0, 1]: 1.0 when every expected
    id is in the top k, 0.0 when none is. An empty `expected` is a data error."""
    expected = set(expected)
    if not expected:
        raise ValueError("recall_at needs at least one expected id")
    top = []
    for r in results[:k]:
        if isinstance(r, dict):
            top.append(r["id"])
        else:
            top.append(getattr(r, "id", r))
    return len(expected & set(top)) / len(expected)


def _cli_launcher():
    """Interpreter prefix for launching the CLI subprocess. Under coverage
    (``AGENT_MEMORY_COV`` set) run the CLI under ``coverage run --parallel-mode``
    so the subprocess's lines are recorded; otherwise just the interpreter."""
    if os.environ.get("AGENT_MEMORY_COV"):
        return [sys.executable, "-m", "coverage", "run", "--parallel-mode"]
    return [sys.executable]


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
    score: float | None = None
    # The review status the surface reported: unverified, verified or flagged.
    status: str | None = None
    # The older memory this one supersedes, and the newest one that
    # supersedes it, as the surface reported them.
    supersedes: int | None = None
    superseded_by: int | None = None


class CliDriver:
    """Drives the CLI as a subprocess and parses its output back into data.

    The subprocess is pointed at the live server via `AGENT_MEMORY_API` /
    `AGENT_MEMORY_API_TOKEN`, so it runs the real remote path (ApiClient → HTTP).
    """

    name = "cli"

    def __init__(self, url, token, agent="tester", extra_env=None):
        self.url = url
        self.token = token
        self.agent = agent
        self.extra_env = extra_env or {}

    # ---- plumbing --------------------------------------------------------
    def _env(self):
        env = {**os.environ, "AGENT_NAME": self.agent}
        env["AGENT_MEMORY_API"] = self.url
        if self.token is not None:
            env["AGENT_MEMORY_API_TOKEN"] = self.token
        env.update(self.extra_env)
        return env

    def raw(self, *args, stdin=None):
        """Run the CLI verbatim; returns the CompletedProcess (for surface tests)."""
        return subprocess.run(
            [*_cli_launcher(), str(MEMORY_CLI), *args],
            env=self._env(), input=stdin, capture_output=True, text=True,
        )

    # ---- semantic operations --------------------------------------------
    def add(self, content, *, project=None, tags=None, type=None, agent=None, force=False):
        """Returns the new id, or None when the add was refused (a duplicate)."""
        args = ["add", content]
        if project:
            args += ["--project", project]
        norm = _normalize_tags(tags)
        if norm:
            args += ["--tags", json.dumps(norm)]
        if type:
            args += ["--type", type]
        if agent:
            args += ["--agent", agent]
        if force:
            args += ["--force"]
        m = re.search(r"Memory #(\d+) added", self.raw(*args).stdout)
        return int(m.group(1)) if m else None

    def query(self, **filters):
        args = ["query"]
        if filters.get("since_days") is not None:
            args += ["--since-days", str(filters["since_days"])]
        for flag in ("since", "until", "project", "agent", "tag", "type", "status"):
            if filters.get(flag):
                args += [f"--{flag}", str(filters[flag])]
        if filters.get("current"):
            args += ["--current"]
        if filters.get("limit") is not None:
            args += ["--limit", str(filters["limit"])]
        return self._parse_memories(self.raw(*args).stdout)

    def search(self, text, mode=None, **filters):
        args = ["search", text]
        if mode is not None:
            args += ["--mode", mode]
        for flag in ("project", "agent", "since", "tag"):
            if filters.get(flag):
                args += [f"--{flag}", str(filters[flag])]
        if filters.get("current"):
            args += ["--current"]
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

    # ---- output parsing --------------------------------------------------
    def _parse_memories(self, text, snippet=False):
        if "No memories found" in text:
            return []
        # Each header is `━━━ #<id> status: <status> [supersedes #<n>]
        # [superseded by #<n>] [score <n>] ━━━…`; keep the rest of the line so
        # the status, the links and the score can be read out of it.
        parts = re.split(r"━+ #(\d+)([^\n]*)\n", text)
        out = []
        it = iter(parts[1:])  # parts[0] is the preamble before the first block
        for mid, header, body in zip(it, it, it):
            sm = re.search(r"score (-?\d+\.\d+)", header)
            score = float(sm.group(1)) if sm else None
            st = re.search(r"status: (\w+)", header)
            status = st.group(1) if st else None
            sup = re.search(r"supersedes #(\d+)", header)
            by = re.search(r"superseded by #(\d+)", header)
            out.append(self._parse_block(int(mid), body, snippet=snippet, score=score,
                                         status=status,
                                         supersedes=int(sup.group(1)) if sup else None,
                                         superseded_by=int(by.group(1)) if by else None))
        return out

    def _parse_block(self, mid, body, snippet=False, score=None, status=None,
                     supersedes=None, superseded_by=None):
        agent = project = type_ = None
        tags = []
        collected = []
        in_suggestion = False
        for line in body.split("\n"):
            if in_suggestion and line.startswith("    "):
                continue  # a line of the suggested text
            in_suggestion = False
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
            if line.startswith("review: ") or line.startswith("suggested tags: "):
                # The review model's verdict line and its suggested tags sit
                # with the meta lines, before the content; the drivers
                # compare content only.
                continue
            if line == "suggested:":
                # The suggested text follows, indented; the content is not.
                in_suggestion = True
                continue
            if _SEP.match(line.strip()) or line.startswith("Found "):
                break
            collected.append(line)
        text_body = "\n".join(collected).strip()
        return Memory(
            id=mid, agent=agent, project=project, type=type_, tags=tags,
            content="" if snippet else text_body,
            snippet=text_body if snippet else None,
            score=score,
            status=status,
            supersedes=supersedes,
            superseded_by=superseded_by,
        )
