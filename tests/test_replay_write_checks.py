"""Tests for scripts/replay_write_checks.py: it runs against the test database
as a subprocess with the fake embedder, and it refuses a DSN that is not local.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from agent_memory.server import repository as repo
from agent_memory.server.db import make_sessionmaker
from conftest import PG_DSN, make_test_engine

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "replay_write_checks.py"

SAME = "Moved the nightly build to run on every push to any branch of the repo."
DECISION = "Kept the nightly build because a build on every push made the runners too slow."


def _run(*args):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, env=env)


def _insert(rows):
    """Add rows to the (truncated) test DB through the repository; return the ids."""
    async def _do():
        eng = make_test_engine()
        try:
            async with make_sessionmaker(eng)() as session, session.begin():
                return [await repo.add(session, content, "tester", project, [], mtype)
                        for content, project, mtype in rows]
        finally:
            await eng.dispose()

    return asyncio.run(_do())


def test_reports_the_second_of_an_identical_pair_as_a_refusal():
    first, second, third = _insert([
        (SAME, "proj", "note"),
        (SAME, "proj", "note"),
        (DECISION, "proj", "decision"),
    ])

    out = _run("--dsn", PG_DSN, "--embedder", "fake")
    assert out.returncode == 0, out.stderr
    lines = out.stdout.splitlines()

    by_id = {int(line.split("  ")[0]): line for line in lines[:3]}
    assert f"  proj  ok  -" in by_id[first]
    assert f"  proj  refuse #{first} 1.00  -" in by_id[second]
    assert f"  proj  ok  -" in by_id[third]

    assert lines[3] == ""
    assert lines[4] == "totals: 3 memories, 1 refused, " \
                       "warned (short 0, no-project 0, no-reasoning 0), 2 clean"


def test_since_and_until_limit_the_report_but_not_what_the_check_sees():
    first, second = _insert([(SAME, "proj", "note"), (SAME, "proj", "note")])
    # Both rows were written today, so a window ending yesterday shows nothing
    # and one starting today shows both, the second still refused against the first.
    out = _run("--dsn", PG_DSN, "--embedder", "fake", "--until", "2000-01-01")
    assert out.stdout.splitlines()[0] == ""
    assert "totals: 0 memories" in out.stdout

    out = _run("--dsn", PG_DSN, "--embedder", "fake", "--since", "2000-01-01")
    assert f"{second}  " in out.stdout and f"refuse #{first} 1.00" in out.stdout


def test_refuses_a_dsn_that_is_not_local():
    out = _run("--dsn", "postgresql://memory:memory@db.example.org:5432/memory",
               "--embedder", "fake")
    assert out.returncode == 2
    assert "only against a copy" in out.stderr
    assert out.stdout == ""
