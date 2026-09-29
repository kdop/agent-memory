"""What each client surface shows: the CLI's output and exit codes, the MCP
tools' result shapes, and the client's return values and errors.

The behaviour behind them is tested once, in process, in the other files.
These tests keep to what differs between the surfaces. The CLI runs as a
real subprocess and the MCP tools and the client talk HTTP, so they run
against live servers: `live_server` (no review model) and `reviewing` (a
`FakeReviewer`, flag or refuse mode as the test sets). Test data goes in
through the repository or the API, which is quicker than the CLI.
"""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import text, update

from agent_memory.client import (
    ApiClient,
    ApiRefused,
    ApiUnreachable,
    DuplicateMemory,
    ReviewRefused,
)
from agent_memory.mcp_server import create_mcp
from agent_memory.server.app import create_app
from agent_memory.server.db import make_sessionmaker
from agent_memory.server.models import MemoryReview
from agent_memory.server.review import Verdict
from conftest import (
    APPROVE,
    ARCHIVING,
    AUTH,
    REJECT,
    REWRITE,
    FakeEmbedder,
    FakeReviewer,
    add_rows,
    db_session,
    flag,
    live_app,
    make_test_engine,
    plant_review,
    statuses,
    verify,
    wait_until,
)
from drivers import CliDriver

MERGED = "Chose Postgres, because several agents write at once; the pool holds 10 connections."
MERGE = Verdict("rewrite", None, "Repeats #1 and adds the pool size.", MERGED, 1, ["database"])
REPEAT = Verdict("reject", None, "Says the same as #1.", None, 1)
HEADER = re.compile(r"^━━━ #(\d+) (.*?) ━+$", re.M)


def headers(out):
    """`[(id, rest of the header)]` of every memory block in CLI output."""
    return [(int(mid), rest) for mid, rest in HEADER.findall(out)]


def supersede(old):
    return Verdict("approve", None, f"Reverses #{old} and says why.", None, None, [],
                   supersedes=old)


def post(url, content, project=None, tags=(), type=None, agent="tester"):
    """Add a memory over HTTP; returns its id."""
    resp = httpx.post(f"{url}/memories", headers=AUTH, json={
        "content": content, "project": project, "type": type, "agent": agent,
        "tags": [t if isinstance(t, dict) else {"name": t} for t in tags]})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.fixture
def cli(live_server):
    return CliDriver(*live_server)


@pytest.fixture
def url(live_server):
    return live_server[0]


# ── the CLI: add ─────────────────────────────────────────────────────────────
def test_cli_add_prints_the_id_and_one_line_per_warning(cli):
    proc = cli.raw("add", "tiny", "--type", "decision")
    assert proc.stdout.splitlines() == ["✓ Memory #1 added (tester)", "warning: short",
                                        "warning: no-project", "warning: no-reasoning"]
    out = cli.raw("add", "Run the build on every push because the nightly was too slow.",
                  "--project", "ci", "--type", "decision", "--agent", "clu",
                  "--tags", '[{"name":"ci","description":"the build"}]').stdout
    assert out == "✓ Memory #2 added (clu)\n"
    assert "--force" in cli.raw("add", "--help").stdout


@pytest.mark.parametrize("tags, message", [
    ("not-json-and-not-comma-either", "JSON array"),
    ('[{"description":"no name given"}]', '"name" field'),
])
def test_cli_add_refuses_malformed_tags_and_stores_nothing(cli, url, tags, message):
    proc = cli.raw("add", "x", "--tags", tags)
    assert proc.returncode == 1 and message in proc.stdout
    assert httpx.get(f"{url}/memories", headers=AUTH).json() == []


def test_cli_passes_the_constraint_type_through(cli):
    out = cli.raw("add", "Apply for remote jobs only, because the user will not move.",
                  "--project", "jobs", "--type", "constraint").stdout
    assert out == "✓ Memory #1 added (tester)\n"
    cli.raw("add", "Prefer short cover letters over long ones.", "--project", "jobs",
            "--type", "preference")
    (m,) = cli.query(type="constraint")
    assert (m.id, m.type) == (1, "constraint")
    assert "✓ Memory #2 updated: type → constraint" in cli.raw(
        "update", "2", "--type", "constraint").stdout
    assert [m.id for m in cli.query(type="constraint")] == [2, 1]
    assert "constraint" in cli.raw("add", "--help").stdout
    proc = cli.raw("add", "Some rule for the jobs project.", "--type", "rule")
    assert proc.returncode == 1 and "422" in proc.stdout + proc.stderr


def test_cli_add_duplicate_exits_3_and_force_stores_it(cli):
    cli.raw("add", "dup me", "--project", "proj")
    verify(1)
    proc = cli.raw("add", "dup me", "--project", "proj")
    assert proc.returncode == 3
    assert proc.stdout.strip() == (
        "✗ Duplicate of memory #1 (score 1.00). Use 'memory update 1' or --force.")
    assert "Traceback" not in proc.stderr
    proc = cli.raw("add", "dup me", "--project", "proj", "--force")
    assert proc.returncode == 0 and "✓ Memory #2 added (tester)" in proc.stdout


def test_cli_without_a_server_prints_a_clean_error(cli):
    proc = CliDriver("http://127.0.0.1:1", cli.token).raw("stats")
    assert proc.returncode == 1
    assert "cannot reach the memory API" in proc.stderr
    assert "Traceback" not in proc.stderr + proc.stdout


# ── the CLI: listings ────────────────────────────────────────────────────────
def test_cli_empty_listings(cli):
    assert cli.raw("query").stdout.strip() == "No memories found."
    assert cli.raw("search", "zzzznope").stdout.strip() == "No memories found for: zzzznope"
    assert cli.raw("tags").stdout.strip() == "No tags found."
    assert cli.raw("projects").stdout.strip() == "No projects found."
    assert cli.raw("review").stdout.strip() == "No flagged memories."
    stats = cli.raw("stats").stdout
    assert "Total memories:    0" in stats and "Oldest:            N/A" in stats


def test_cli_query_prints_blocks_and_a_footer(cli, url):
    post(url, "remember the milk", project="home", tags=["shopping", "food"], type="note")
    post(url, "second")
    post(url, "third")
    out = cli.raw("query").stdout
    assert [mid for mid, _ in headers(out)] == [3, 2, 1]
    assert "━━━ #1 status: unverified ━━━" in out
    for line in ("👤 tester @ home [note]", "🏷️  food, shopping", "remember the milk",
                 "Found 3 memories"):
        assert line in out
    assert "review:" not in out                 # no verdict, no review line
    assert re.search(r"^🕒 \d{4}-\d{2}-\d{2}", out, re.M)
    assert "Found 2 of 3 memories (use --all or --limit to see more)" in \
        cli.raw("query", "--limit", "2").stdout
    assert "Found 3 memories" in cli.raw("query", "--all", "--limit", "1").stdout   # --all wins
    # Every filter reaches the server.
    out = cli.raw("query", "--since-days", "0", "--since", "2000-01-01", "--until", "2999-01-01",
                  "--project", "home", "--agent", "tester", "--tag", "food", "--type", "note",
                  "--status", "unverified", "--current").stdout
    assert [mid for mid, _ in headers(out)] == [1] and "Found 1 memories" in out
    # The driver reads the blocks back into records.
    (m,) = cli.query(project="home")
    assert (m.id, m.agent, m.project, m.type, m.tags, m.content, m.status) == (
        1, "tester", "home", "note", ["food", "shopping"], "remember the milk", "unverified")


def test_cli_search_prints_each_mode(cli, url):
    for content in ("the cat sat on the mat", "a dog in the yard", "rain on the window",
                    "coffee before code"):
        post(url, content, project="p")
    out = cli.raw("search", "cat").stdout
    assert out.startswith("🔍 Search results for: cat\n")
    ((mid, rest),) = headers(out)
    assert mid == 1 and re.fullmatch(r"status: unverified score \d\.\d\d", rest)
    assert "→cat←" in out and "Found 1 matches" in out
    assert cli.raw("search", "cat", "--mode", "keyword").stdout == out

    proc = cli.raw("search", "the cat sat on the mat", "--mode", "semantic")
    found = [(mid, float(rest.split("score ")[1])) for mid, rest in headers(proc.stdout)]
    assert len(found) == 4 and found[0] == (1, 1.0)
    assert [s for _, s in found] == sorted((s for _, s in found), reverse=True)
    # No snippet by meaning: the content is shown, never "None".
    assert "the cat sat on the mat" in proc.stdout and "None" not in proc.stdout
    out = cli.raw("search", "the cat sat on the mat", "--mode", "hybrid").stdout
    assert headers(out)[0] == (1, "status: unverified score 0.03")       # 2/61

    out = cli.raw("search", "the cat sat on the mat", "--mode", "semantic", "--limit", "2").stdout
    assert "Found 2 matches (limit reached; use --all or --limit to see more)" in out
    assert "Found 4 matches" in cli.raw("search", "the", "--mode", "semantic", "--all").stdout
    out = cli.raw("search", "cat", "--project", "p", "--agent", "tester", "--tag", "none",
                  "--since", "2000-01-01", "--current").stdout
    assert out.strip() == "No memories found for: cat"

    proc = cli.raw("search", "x", "--mode", "fuzzy")
    assert proc.returncode == 2 and "invalid choice: 'fuzzy'" in proc.stderr
    help_ = cli.raw("search", "--help").stdout
    for words in ("keyword", "matches words", "semantic", "matches meaning", "hybrid",
                  "combines both", "--current"):
        assert words in help_


def test_cli_tags_projects_and_stats(cli, url):
    post(url, "a", project="alpha", tags=[{"name": "auth", "description": "authentication flow"}])
    post(url, "b", project="alpha", tags=["auth", "plain"])
    out = cli.raw("tags").stdout
    assert out.startswith("🏷️  Tags:")
    assert re.search(r"^  auth\s+\(2\)\n       ↳ authentication flow$", out, re.M)
    # A tag whose description is its own name has no ↳ line.
    assert re.search(r"^  plain\s+\(1\)$", out, re.M) and out.count("↳") == 1
    out = cli.raw("projects").stdout
    assert out.startswith("📂 Projects:") and re.search(r"alpha\s+\(2 memories\)", out)
    out = cli.raw("stats").stdout
    assert out.startswith("📊 Memory Statistics")
    for line in ("Total memories:    2", "Agents:            1", "Projects:          1",
                 "Tags:              2", "Today:             2", "Last 7 days:       2"):
        assert line in out


def test_cli_show_update_and_delete(cli, url, tmp_path):
    post(url, "old text", tags=["keep", "drop"])
    post(url, "second " + "x" * 120, project="p", type="note")
    shown = cli.raw("show", "1").stdout
    assert headers(shown) == [(1, "status: unverified")] and "old text" in shown
    proc = cli.raw("show", "999")
    assert proc.returncode == 1 and proc.stdout.strip() == "✗ Memory #999 not found."

    assert cli.raw("update", "1", "--content", "new text").stdout.strip() == \
        "✓ Memory #1 updated: content"
    cli.raw("update", "1", "--content", "-", stdin="piped in")
    assert cli.get(1).content == "piped in"
    path = tmp_path / "content.txt"
    path.write_text("from a file")
    cli.raw("update", "1", "--content-file", str(path))
    assert cli.get(1).content == "from a file"
    out = cli.raw("update", "1", "--project", "p", "--type", "note", "--add-tags",
                  '[{"name":"new"}]', "--remove-tags", "drop").stdout
    assert out.strip() == "✓ Memory #1 updated: project → p; type → note; +tags: new; -tags: drop"
    assert cli.get(1).tags == ["keep", "new"]
    cli.raw("update", "1", "--set-tags", '[{"name":"only"}]')
    assert cli.get(1).tags == ["only"]
    assert cli.raw("update", "1").stdout.strip() == \
        "No changes specified for memory #1. Use --help for options."
    proc = cli.raw("update", "999", "--content", "y")
    assert proc.returncode == 1 and proc.stdout.strip() == "✗ Memory #999 not found."

    out = cli.raw("delete", "1", "2", "999").stdout
    assert "⚠ Not found: 999" in out and "Will delete 2 memories:" in out
    assert "#1 [tester@p|note] from a file" in out and "Dry run" in out
    # A long entry is cut to 100 characters in the preview.
    assert f"#2 [tester@p|note] second {'x' * 93}...\n" in out
    assert cli.get(1) is not None
    assert "✓ Deleted 2 memories." in cli.raw("delete", "1", "2", "--yes").stdout
    proc = cli.raw("delete", "1")
    assert proc.returncode == 1 and "No memories found with the given IDs." in proc.stdout


def test_cli_reindex(cli, url):
    post(url, "one")
    post(url, "two")

    async def clear():
        async with db_session() as s:
            await s.execute(text("UPDATE memories SET embedding = NULL, embedding_model = NULL"))

    asyncio.run(clear())
    assert cli.raw("reindex").stdout.strip() == "✓ Reindexed 2 memories, 0 tags"


def test_cli_config_is_local(tmp_path):
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    cli = CliDriver("http://127.0.0.1:1", None, extra_env=env)
    assert cli.raw("config", "get").stdout.strip() == "No settings configured."
    assert cli.raw("config", "set", "api_url", "http://x:1").stdout.strip() == \
        "✓ Set api_url = http://x:1"
    assert cli.raw("config", "get", "api_url").stdout.strip() == "http://x:1"
    assert cli.raw("config", "get", "nope").stdout.strip() == "(unset) nope"
    assert cli.raw("config", "get").stdout.strip() == "api_url = http://x:1"
    assert cli.raw("config", "path").stdout.strip() == \
        str(tmp_path / "agent-memory" / "config.json")
    assert json.loads((tmp_path / "agent-memory" / "config.json").read_text()) == {
        "api_url": "http://x:1"}
    assert cli.raw("config").returncode == 1
    assert cli.raw().returncode == 1


# ── the CLI: the review ──────────────────────────────────────────────────────
def test_cli_headers_carry_the_status_and_the_links(cli, url):
    """#1 verified, superseded by #2; #2 supersedes #1 and is superseded by
    #4; #3 flagged."""
    for content in ("Chose SQLite for the store.", "Moved the store to Postgres.",
                    "A diary line about the store.", "Back to SQLite for the store."):
        post(url, content, project="p")
    verify(1)
    asyncio.run(plant_review(2, supersede(1)))
    flag(3)
    asyncio.run(plant_review(4, supersede(2)))

    assert headers(cli.raw("show", "2").stdout) == [
        (2, "status: verified supersedes #1 superseded by #4")]
    assert headers(cli.raw("query", "--project", "p").stdout) == [
        (4, "status: verified supersedes #2"), (3, "status: flagged"),
        (2, "status: verified supersedes #1 superseded by #4"),
        (1, "status: verified superseded by #2")]
    found = headers(cli.raw("search", "store", "--project", "p").stdout)
    assert re.fullmatch(r"status: verified superseded by #2 score \d\.\d\d", dict(found)[1])
    assert [mid for mid, _ in headers(cli.raw("query", "--current").stdout)] == [4, 3]
    # The driver reads the header back.
    two = cli.get(2)
    assert (two.status, two.supersedes, two.superseded_by, two.content) == (
        "verified", 1, 4, "Moved the store to Postgres.")
    hit = next(m for m in cli.search("store", project="p") if m.id == 1)
    assert (hit.status, hit.superseded_by, hit.score is not None) == ("verified", 2, True)


def test_cli_prints_the_review_line_in_each_form(cli, url):
    for n in range(5):
        post(url, f"entry {n}", project="p")
    asyncio.run(plant_review(1, REJECT))
    asyncio.run(plant_review(2, REPEAT))
    asyncio.run(plant_review(3, APPROVE))
    asyncio.run(plant_review(4, REWRITE))
    asyncio.run(plant_review(5, Verdict("rewrite", 3, "Say why.", "entry 4, because of X.",
                                        None, [])))
    shown = cli.raw("show", "1").stdout
    assert "review: reject, rule 1: It will not matter in a later session." in shown
    # The line sits with the meta lines, before the content.
    assert shown.index("review:") < shown.index("entry 0")
    assert "review: reject, duplicate of #1: Says the same as #1." in cli.raw("show", "2").stdout
    assert "review: approve: A decision with its reason." in cli.raw("show", "3").stdout
    lines = cli.raw("show", "4").stdout.splitlines()
    at = lines.index("review: rewrite, rule 3: Say why.")
    assert lines[at + 1:at + 5] == ["suggested:", "    Chose Postgres,", "    because of X.",
                                    "suggested tags: database, search"]
    five = cli.raw("show", "5").stdout
    assert "suggested:\n    entry 4, because of X.\n" in five and "suggested tags" not in five
    # The parsed content is unchanged by the extra lines.
    assert cli.get(4).content == "entry 3"
    assert cli.raw("query").stdout.count("review:") == 5


async def _date_review(mid, verdict, minutes_ago):
    """Date the review row of `mid` with `verdict` `minutes_ago`."""
    async with db_session() as s:
        await s.execute(update(MemoryReview)
                        .where(MemoryReview.memory_id == mid, MemoryReview.verdict == verdict)
                        .values(created_at=datetime.now(timezone.utc)
                                - timedelta(minutes=minutes_ago)))


def _seed_flagged():
    """Five memories: #1 rejected 30 minutes ago, #2 (project beta) a
    rewrite 20 minutes ago, #3 approved, #4 rejected now, #5 unreviewed."""
    async def seed():
        await add_rows("Spent the afternoon tidying.")
        await add_rows("Chose Postgres.", project="beta")
        await add_rows("Chose Postgres because several agents write at once.",
                       "Had lunch, then read the config module.", "Not reviewed yet.")
        for mid, verdict, age in ((1, REJECT, 30), (2, REWRITE, 20), (3, APPROVE, 10),
                                  (4, REJECT, 0)):
            await plant_review(mid, verdict)
            await _date_review(mid, verdict.verdict, age)
    asyncio.run(seed())


def test_cli_review_lists_flagged_memories_like_query(cli):
    _seed_flagged()
    out = cli.raw("review").stdout
    assert headers(out) == [(4, "status: flagged"), (2, "status: flagged"),
                            (1, "status: flagged")]
    assert "👤 tester @ alpha" in out and "review: rewrite, rule 3: Say why." in out
    assert "Not reviewed yet." not in out and "Found 3 flagged memories" in out
    assert [m.content for m in cli._parse_memories(out)][0] == \
        "Had lunch, then read the config module."
    assert [mid for mid, _ in headers(cli.raw("review", "--verdict", "rewrite").stdout)] == [2]
    assert [mid for mid, _ in headers(cli.raw("review", "--project", "alpha").stdout)] == [4, 1]
    out = cli.raw("review", "--limit", "1").stdout
    assert "Found 1 of 3 flagged memories (use --all or --limit to see more)" in out
    assert "Found 3 flagged memories" in cli.raw("review", "--all", "--limit", "1").stdout
    out = cli.raw("review", "--status", "unverified").stdout
    assert headers(out) == [(5, "status: unverified")] and "Found 1 unverified memories" in out
    assert cli.raw("review", "--status", "verified", "--project", "q").stdout.strip() == \
        "No verified memories."
    for args in (("review", "--verdict", "approve"), ("review", "--status", "maybe"),
                 ("query", "--status", "maybe")):
        proc = cli.raw(*args)
        assert proc.returncode == 2 and "invalid choice" in proc.stderr
    help_ = cli.raw("review", "--help").stdout
    for word in ("--project", "--verdict", "--status", "--limit", "--all", "--catch-up",
                 "reject", "rewrite", "unverified"):
        assert word in help_
    assert "review" in cli.raw("--help").stdout


def test_cli_show_reviews_prints_the_history_oldest_first(cli, url):
    post(url, "Chose Postgres, because several agents write at once.", project="p")
    post(url, "Nothing checked this one.", project="p")
    for verdict, age in ((REJECT, 30), (REWRITE, 20), (APPROVE, 10)):
        asyncio.run(plant_review(1, verdict))
        asyncio.run(_date_review(1, verdict.verdict, age))
    dates = {r["verdict"]: r["created_at"] for r in ApiClient(cli.url, cli.token).reviews(1)}
    out = cli.raw("show", "1", "--reviews").stdout
    # The memory as `show` prints it, the newest verdict on its review line;
    # then the history under the content, oldest first.
    assert "review: approve: A decision with its reason." in out
    assert re.findall(r"^review (\S+ \S+): (.+)$", out, re.M) == [
        (dates["reject"], "reject, rule 1: It will not matter in a later session."),
        (dates["rewrite"], "rewrite, rule 3: Say why."),
        (dates["approve"], "approve: A decision with its reason.")]
    assert out.index("Chose Postgres") < out.index(f"review {dates['reject']}")
    at = out.index(f"review {dates['rewrite']}")
    assert out[at:].split("\n")[1:3] == ["suggested:", "    Chose Postgres,"]
    assert cli.raw("show", "1").stdout.count("review") == 1
    out = cli.raw("show", "2", "--reviews").stdout
    assert "No reviews yet." in out and "review " not in out
    help_ = cli.raw("show", "--help").stdout
    assert "--reviews" in help_ and "oldest first" in help_


def test_cli_catch_up(cli, reviewing):
    # The shared server has no review model.
    proc = cli.raw("review", "--catch-up")
    assert proc.returncode == 1 and "✗ Cannot review:" in proc.stderr
    assert "Traceback" not in proc.stderr
    proc = cli.raw("review", "--catch-up", "--verdict", "reject")
    assert proc.returncode == 2 and "--catch-up goes with --limit only" in proc.stdout

    for n in range(3):
        post(cli.url, f"memory {n}")
    rcli = CliDriver(reviewing.url, cli.token)
    reviewing.fake.reset(APPROVE)
    assert rcli.raw("review", "--catch-up", "--limit", "1").stdout.strip() == \
        "✓ Scheduled 1 reviews"
    wait_until(lambda: len(reviewing.fake.calls) == 1 and asyncio.run(statuses())[0][1] ==
               "verified")
    # `--missing` is the old name.
    assert rcli.raw("review", "--missing").stdout.strip() == "✓ Scheduled 2 reviews"
    wait_until(lambda: [s for _, s in asyncio.run(statuses())] == ["verified"] * 3)
    assert rcli.raw("review", "--catch-up").stdout.strip() == "✓ Scheduled 0 reviews"

    # One catch-up at a time: while one waits on the model, another says so.
    post(cli.url, "memory 3")
    reviewing.fake.reset(APPROVE, block=True)
    try:
        assert httpx.post(f"{reviewing.url}/admin/review", headers=AUTH).json() == {
            "scheduled": 1, "tags": 0}
        assert reviewing.fake.started.wait(10)
        assert rcli.raw("review", "--catch-up").stdout.strip() == \
            "✓ A catch-up is already running; nothing new scheduled"
    finally:
        reviewing.fake.release.set()
    wait_until(lambda: asyncio.run(statuses())[-1] == (4, "verified"))


def test_cli_in_refuse_mode(reviewing):
    reviewing.app.state.review_mode = "refuse"
    fake, rcli = reviewing.fake, CliDriver(reviewing.url, "test-token")
    fake.verdict = APPROVE
    assert rcli.raw("add", "Chose Postgres, because several agents write at once.",
                    "--project", "p").stdout == "✓ Memory #1 added (tester)\n"
    assert "review: approve: A decision with its reason." in rcli.raw("show", "1").stdout

    cases = [
        (REJECT, ["✗ Review: reject, rule 1: It will not matter in a later session.",
                  "Fix the entry, or pass --force to store it as written."]),
        (REPEAT, ["✗ Review: reject, duplicate of #1: Says the same as #1.",
                  "Fix the entry, or pass --force to store it as written."]),
        (REWRITE, ["✗ Review: rewrite, rule 3: Say why.", "suggested:", "    Chose Postgres,",
                   "    because of X.", "suggested tags: database, search",
                   "Fix the entry, or pass --force to store it as written."]),
        (MERGE, ["✗ Review: rewrite, duplicate of #1: Repeats #1 and adds the pool size.",
                 "suggested:", f"    {MERGED}", "suggested tags: database",
                 "Apply it with 'memory update 1' instead of adding."]),
    ]
    for verdict, lines in cases:
        fake.verdict = verdict
        proc = rcli.raw("add", "Spent the afternoon tidying.", "--project", "p")
        assert proc.returncode == 4 and proc.stdout.splitlines() == lines
        assert "Traceback" not in proc.stderr
    assert [m.id for m in rcli.query()] == [1]

    # --force stores it, and the review follows in the background.
    fake.verdict = REJECT
    proc = rcli.raw("add", "Spent the afternoon tidying.", "--project", "p", "--force")
    assert proc.stdout.startswith("✓ Memory #2 added (tester)")
    wait_until(lambda: asyncio.run(statuses()) == [(1, "verified"), (2, "flagged")])


async def until_status(mid, status):
    """Wait until memory `mid` has `status`: a review that runs after the
    response has landed."""
    deadline = asyncio.get_running_loop().time() + 10
    while dict(await statuses()).get(mid) != status:
        assert asyncio.get_running_loop().time() < deadline, f"#{mid} never got {status}"
        await asyncio.sleep(0.02)


# ── the MCP tools ────────────────────────────────────────────────────────────
@asynccontextmanager
async def mcp_session(url, token="test-token"):
    """One in-memory MCP session over `create_mcp(ApiClient(url))`. Yields
    `call(tool, **args)`, which returns the tool's one JSON result."""
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    async with connect(create_mcp(client=ApiClient(url, token))) as session:
        async def call(tool, **args):
            result = await session.call_tool(tool, {k: v for k, v in args.items()
                                                    if v is not None})
            return json.loads(result.content[0].text)

        call.session = session
        yield call


async def test_mcp_tools_and_their_result_shapes(url):
    async with mcp_session(url) as call:
        first = await call("memory_add", content="the cat sat on the mat", project="p",
                           tags=[{"name": "pets", "description": "animals"}])
        assert first == {"id": 1, "warnings": ["short"], "notes": []}
        for content in ("a dog in the yard", "rain on the window"):
            await call("memory_add", content=content, project="p", agent="clu", type="note")

        plain = await call("memory_search", q="cat")
        assert plain == await call("memory_search", q="cat", mode="keyword")
        assert [m["id"] for m in plain["memories"]] == [1]
        assert "→cat←" in plain["memories"][0]["snippet"]
        hits = (await call("memory_search", q="the cat sat on the mat", mode="semantic",
                           limit=0))["memories"]
        assert len(hits) == 3 and hits[0]["score"] == pytest.approx(1.0, abs=1e-6)
        hits = (await call("memory_search", q="the cat sat on the mat", mode="hybrid",
                           current=True))["memories"]
        assert hits[0]["id"] == 1 and hits[0]["score"] == pytest.approx(2 / 61, abs=1e-6)

        # A duplicate is an error result, and nothing is stored; force stores it.
        await plant_review(1, APPROVE)
        dup = await call("memory_add", content="the cat sat on the mat", project="p")
        assert dup == {"error": "duplicate", "existing_id": 1,
                       "score": pytest.approx(1.0, abs=1e-5)}
        forced = await call("memory_add", content="the cat sat on the mat", project="p",
                            force=True)
        assert forced["id"] == 4

        rows = (await call("memory_query", project="p", agent="clu", limit=0))["memories"]
        assert [m["id"] for m in rows] == [3, 2] and rows[0]["review_status"] == "unverified"
        assert [m["id"] for m in (await call("memory_query", status="verified",
                                             current=True))["memories"]] == [1]
        assert await call("memory_query", status="maybe") == {
            "error": "status must be one of unverified, verified, flagged (got 'maybe')"}

        shown = await call("memory_show", id=1)
        assert set(shown) == {"memory"} and shown["memory"]["tags"] == ["pets"]
        assert await call("memory_show", id=999) == {"memory": None}
        assert await call("memory_update", id=1, content="the cat sat on the rug") == {
            "found": True, "changes": ["content"]}
        assert await call("memory_update", id=999, content="x") == {"found": False,
                                                                   "changes": []}
        assert await call("memory_delete", ids=[4, 999]) == {"deleted": 1, "missing": [999]}
        assert await call("memory_tags") == {"tags": [{"name": "pets", "count": 1,
                                                       "description": "animals",
                                                       "review_status": "unverified"}]}
        assert await call("memory_projects") == {"projects": [{"project": "p", "count": 3}]}
        assert (await call("memory_stats"))["total"] == 3


async def test_mcp_passes_the_constraint_type_through(url):
    async with mcp_session(url) as call:
        added = await call("memory_add", content="Apply for remote jobs only, because the "
                           "user will not move.", project="jobs", type="constraint")
        assert added == {"id": 1, "warnings": [], "notes": []}
        await call("memory_add", content="Prefer short cover letters over long ones.",
                   project="jobs", type="note")
        rows = (await call("memory_query", type="constraint"))["memories"]
        assert [(m["id"], m["type"]) for m in rows] == [(1, "constraint")]
        assert await call("memory_update", id=2, type="constraint") == {
            "found": True, "changes": ["type → constraint"]}
        # An unknown type is refused and nothing is stored.
        bad = await call.session.call_tool("memory_add", {"content": "x", "type": "rule"})
        assert bad.isError and "422" in bad.content[0].text
        assert (await call("memory_stats"))["total"] == 2
        tools = {t.name: t for t in (await call.session.list_tools()).tools}
    for name in ("memory_add", "memory_query", "memory_update"):
        assert "constraint" in tools[name].description


async def test_mcp_flagged_and_the_history(url):
    await asyncio.to_thread(_seed_flagged)
    async with mcp_session(url) as call:
        data = await call("memory_flagged")
        assert set(data) == {"memories"}
        assert [r["id"] for r in data["memories"]] == [4, 2, 1]
        assert data["memories"][1]["review"] == REWRITE.as_dict()
        query_row = (await call("memory_query"))["memories"][0]
        assert set(data["memories"][0]) == set(query_row)
        for args, ids in (({"verdict": "rewrite"}, [2]), ({"project": "beta"}, [2]),
                          ({"limit": 1}, [4]), ({"limit": 0}, [4, 2, 1]),
                          ({"status": "unverified"}, [5]), ({"project": "gamma"}, [])):
            assert [r["id"] for r in (await call("memory_flagged", **args))["memories"]] == ids
        bad = await call("memory_flagged", verdict="approve")
        assert set(bad) == {"error"} and "approve" in bad["error"]
        assert "maybe" in (await call("memory_flagged", status="maybe"))["error"]

        for verdict in (REJECT, REWRITE):
            await plant_review(3, verdict)
        data = await call("memory_show", id=3, reviews=True)
        assert set(data) == {"memory", "reviews"}
        assert data["memory"]["review"] == REWRITE.as_dict()
        assert [r["verdict"] for r in data["reviews"]] == ["rewrite", "reject", "approve"]
        assert set(data["reviews"][0]) == {"created_at", "verdict", "rule", "reason",
                                           "rewrite", "duplicate_of", "tags"}
        assert await call("memory_show", id=999, reviews=True) == {"memory": None,
                                                                  "reviews": None}


async def test_mcp_tool_descriptions_and_parameters(url):
    async with mcp_session(url) as call:
        tools = {t.name: t for t in (await call.session.list_tools()).tools}
    params = {name: t.inputSchema.get("properties", {}) for name, t in tools.items()}
    assert params["memory_search"]["mode"]["default"] == "keyword"
    for word in ("keyword", "semantic", "hybrid", "embedding model"):
        assert word in tools["memory_search"].description
    assert params["memory_flagged"]["limit"]["default"] == 20
    for word in ("reject", "rewrite", "newest review first", "memory_query"):
        assert word in tools["memory_flagged"].description
    assert params["memory_show"]["reviews"]["default"] is False
    assert "newest first" in tools["memory_show"].description
    # The link is set by the review model only, never by a caller.
    assert "supersedes" not in params["memory_add"]
    assert "supersedes" not in params["memory_update"]
    assert "current" in params["memory_query"] and "current" in params["memory_search"]


async def test_a_search_the_server_cannot_serve_is_an_error_result(reviewing):
    from agent_memory.server.embedding import NullEmbedder

    reviewing.app.state.embedder = NullEmbedder("the model is off")
    async with mcp_session(reviewing.url) as call:
        error = (await call("memory_search", q="x", mode="semantic"))["error"]
    assert error.startswith("semantic search is not available: the model is off")
    with pytest.raises(ApiRefused) as refused:
        ApiClient(reviewing.url, "test-token").search("x", mode="semantic")
    assert refused.value.status == 400 and str(refused.value) == error


async def test_mcp_add_in_refuse_mode_returns_the_review_error(reviewing):
    reviewing.app.state.review_mode = "refuse"
    reviewing.fake.verdict = APPROVE
    async with mcp_session(reviewing.url) as call:
        assert (await call("memory_add", content="Chose Postgres, because of X.",
                           project="p"))["id"] == 1
        reviewing.fake.verdict = REJECT
        assert await call("memory_add", content="Spent the afternoon tidying.", project="p") == {
            "error": "review", "verdict": "reject", "rule": 1,
            "explanation": "It will not matter in a later session.",
            "rewrite": None, "tags": [], "duplicate_of": None}
        reviewing.fake.verdict = MERGE
        out = await call("memory_add", content=MERGED, project="p")
        assert out == {"error": "review", "verdict": "rewrite", "rule": None,
                       "explanation": "Repeats #1 and adds the pool size.", "rewrite": MERGED,
                       "tags": ["database"], "duplicate_of": 1}
        # The writer applies a merge with the update tool, on the old memory.
        assert await call("memory_update", id=1, content=MERGED) == {"found": True,
                                                                    "changes": ["content"]}
        forced = await call("memory_add", content="Chose Postgres.", project="p", force=True)
        assert set(forced) == {"id", "warnings", "notes"}
    # The forced write is reviewed after the response; let it land.
    await until_status(forced["id"], "flagged")


# ── the client ───────────────────────────────────────────────────────────────
def test_the_client_methods(url, live_server):
    api = ApiClient(*live_server)
    assert api.add("first memory in p", "tester", "p", [{"name": "t"}], "note") == 1
    mid, warnings = api.add_with_warnings("second", "tester", "p", [], None)
    assert (mid, warnings) == (2, ["short"])
    api.add("third", "tester", "q", [], None)
    verify(1)
    flag(2)
    rows, total = api.query_with_total(project="p", limit=1)
    assert [m["id"] for m in rows] == [2] and total == 2
    assert [m["id"] for m in api.query(status="verified", current=True, since_days=1,
                                       tag="t", mtype="note", agent="tester")] == [1]
    hits = api.search("second", project="p", agent="tester", since="2000-01-01", limit=1,
                      mode="semantic", current=True)
    assert [m["id"] for m in hits] == [2] and hits[0]["score"] == pytest.approx(1.0, abs=1e-6)
    assert [m["id"] for m in api.flagged()] == [2]
    rows, total = api.flagged_with_total(status="unverified", limit=0)
    assert [m["id"] for m in rows] == [3] and total == 1
    assert api.get(1)["review_status"] == "verified" and api.get(999) is None
    assert [r["verdict"] for r in api.reviews(1)] == ["approve"]
    assert api.reviews(3) == [] and api.reviews(999) is None
    assert api.update(3, content="3rd", project="q", mtype="note", set_tags=[{"name": "x"}],
                      add_tags=[{"name": "y"}], remove_tags=["x"]) == [
        "content", "project → q", "type → note", "tags set to: x", "+tags: y", "-tags: x"]
    assert api.update(999, content="x") is None
    assert {r["id"] for r in api.get_many([1, 3, 999])} == {1, 3}
    assert api.get_many([]) == []
    assert {t["name"] for t in api.list_tags()} == {"t", "y"}
    assert api.list_projects() == [{"project": "p", "count": 2}, {"project": "q", "count": 1}]
    assert api.stats()["total"] == 3
    assert api.reindex() == {"updated": 0, "tags": 0}
    assert api.delete([3, 999]) == {"deleted": 1, "missing": [999]}


def _seed_archived(url):
    """#1 live, #2 archived by a reject under rule 2; both match `store`."""
    post(url, "Chose the store because it is fast.", project="p")
    post(url, "Tidied the store module today.", project="p")
    asyncio.run(plant_review(2, ARCHIVING))


def test_cli_archived_listings_and_restore(cli, url):
    _seed_archived(url)
    assert [mid for mid, _ in headers(cli.raw("query").stdout)] == [1]
    out = cli.raw("query", "--archived").stdout
    assert headers(out) == [(2, "status: flagged archived")]
    assert [mid for mid, _ in headers(cli.raw("search", "store").stdout)] == [1]
    assert [mid for mid, _ in headers(cli.raw("search", "store", "--archived").stdout)] == [2]
    assert cli.raw("review").stdout.strip() == "No flagged memories."
    out = cli.raw("review", "--archived").stdout
    assert [mid for mid, _ in headers(out)] == [2] and "Found 1 archived flagged memories" in out
    assert "Archived:          1 (not counted above)" in cli.raw("stats").stdout
    # `show` still finds it, and says it is archived.
    assert headers(cli.raw("show", "2").stdout) == [(2, "status: flagged archived")]

    assert cli.raw("restore", "2").stdout.strip() == "✓ Memory #2 restored"
    assert cli.raw("restore", "2").stdout.strip() == "Memory #2 is not archived; nothing to do."
    proc = cli.raw("restore", "999")
    assert proc.returncode == 1 and proc.stdout.strip() == "✗ Memory #999 not found."
    assert [mid for mid, _ in headers(cli.raw("query").stdout)] == [2, 1]
    assert "Archived:" not in cli.raw("stats").stdout
    proc = cli.raw("review", "--catch-up", "--archived")
    assert proc.returncode == 2 and "--archived" in proc.stdout


async def test_mcp_archived_and_restore(url):
    await asyncio.to_thread(_seed_archived, url)
    async with mcp_session(url) as call:
        assert [m["id"] for m in (await call("memory_query"))["memories"]] == [1]
        rows = (await call("memory_query", archived=True))["memories"]
        assert [m["id"] for m in rows] == [2] and rows[0]["archived_at"] is not None
        assert [m["id"] for m in (await call("memory_search", q="store"))["memories"]] == [1]
        hits = (await call("memory_search", q="store", archived=True))["memories"]
        assert [m["id"] for m in hits] == [2]
        assert (await call("memory_flagged"))["memories"] == []
        assert [m["id"] for m in (await call("memory_flagged", archived=True))["memories"]] == [2]
        assert await call("memory_restore", id=2) == {"found": True, "restored": True}
        assert await call("memory_restore", id=2) == {"found": True, "restored": False}
        assert await call("memory_restore", id=999) == {"found": False, "restored": False}
        assert [m["id"] for m in (await call("memory_query"))["memories"]] == [2, 1]
        tools = {t.name: t for t in (await call.session.list_tools()).tools}
    assert "archived" in tools["memory_restore"].description
    for name in ("memory_query", "memory_search", "memory_flagged"):
        assert "archived" in tools[name].inputSchema["properties"], name


def test_the_client_archived_and_restore(url, live_server):
    _seed_archived(url)
    api = ApiClient(*live_server)
    assert [m["id"] for m in api.query()] == [1]
    rows, total = api.query_with_total(archived=True)
    assert [m["id"] for m in rows] == [2] and total == 1
    assert [m["id"] for m in api.search("store", archived=True)] == [2]
    rows, total = api.flagged_with_total(archived=True)
    assert [m["id"] for m in rows] == [2] and total == 1
    assert api.flagged() == []
    assert api.get(2)["archived_at"] is not None
    assert api.stats()["archived"] == 1
    assert api.restore(2) is True and api.restore(2) is False and api.restore(999) is None
    assert api.get(2)["archived_at"] is None


def test_the_client_errors(live_server, reviewing):
    api = ApiClient(*live_server)
    api.add("dup me", "tester", "p", [], None)
    verify(1)
    with pytest.raises(DuplicateMemory) as dup:
        api.add("dup me", "tester", "p", [], None)
    assert (dup.value.existing_id, dup.value.score) == (1, pytest.approx(1.0, abs=1e-5))
    assert api.add("dup me", "tester", "p", [], None, force=True) == 2
    # A bad type is refused by the request model: a plain 422, not a review.
    with pytest.raises(RuntimeError) as caught:
        api.add("anything", "tester", "p", [], "feedback")
    assert not isinstance(caught.value, ReviewRefused) and "HTTP 422" in str(caught.value)
    # No review model here: the catch-up is refused, with the server's reason.
    with pytest.raises(ApiRefused) as refused:
        api.review_catch_up()
    assert refused.value.status == 503 and "Cannot review" in str(refused.value)
    with pytest.raises(ApiUnreachable, match="cannot reach the memory API"):
        ApiClient("http://127.0.0.1:1", "t").stats()

    reviewing.app.state.review_mode = "refuse"
    reviewing.fake.verdict = MERGE
    rapi = ApiClient(reviewing.url, "test-token")
    with pytest.raises(ReviewRefused) as refused:
        rapi.add(MERGED, "tester", "p", [], "decision")
    e = refused.value
    assert (e.verdict, e.rule, e.explanation, e.rewrite, e.tags, e.duplicate_of) == (
        "rewrite", None, "Repeats #1 and adds the pool size.", MERGED, ["database"], 1)
    assert str(e) == "review: rewrite: Repeats #1 and adds the pool size."
    # force skips the review on the client side too; the review runs after.
    assert rapi.add(MERGED, "tester", "p", [], "decision", force=True) == 3
    wait_until(lambda: asyncio.run(statuses())[2] == (3, "flagged"))

    # The catch-up, and its old name.
    reviewing.fake.reset(APPROVE)
    assert ApiClient.review_missing is ApiClient.review_catch_up
    assert rapi.review_catch_up(limit=1) == {"scheduled": 1, "tags": 0, "running": False}
    wait_until(lambda: asyncio.run(statuses())[1] == (2, "verified"))


@pytest.mark.parametrize("code, body, error", [
    (409, b"not json", RuntimeError),                            # no duplicate shape
    (409, b'{"detail": {"reason": "other"}}', RuntimeError),
    (422, b"not json", RuntimeError),                            # no review shape
    (400, b"not json", RuntimeError),                            # no plain detail
    (400, b'{"detail": "bad request"}', ApiRefused),
    (500, b'{"detail": "boom"}', RuntimeError),
])
def test_the_client_reads_an_error_body_it_does_not_know_as_an_error(
        monkeypatch, code, body, error):
    """Error bodies from something other than this server (a proxy, say):
    the client still raises, with the status and the body in the message."""
    import io
    import urllib.error
    import urllib.request

    def urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, code, "error", {}, io.BytesIO(body))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(error) as caught:
        ApiClient("http://api", "t").stats()
    assert type(caught.value) is error
    if error is RuntimeError:
        assert f"HTTP {code}" in str(caught.value)


def test_the_client_reads_its_endpoint_from_the_environment(monkeypatch, live_server):
    url, token = live_server
    monkeypatch.setenv("AGENT_MEMORY_API", url)
    monkeypatch.setenv("AGENT_MEMORY_API_TOKEN", token)
    from agent_memory.client import get_client

    api = get_client()
    assert (api.base_url, api.token) == (url, token)
    assert api.stats()["total"] == 0


# ── the poll, on a real server ───────────────────────────────────────────────
def test_a_live_server_catches_up_on_its_own(cli):
    """The poll needs a running loop: memories written while the model was
    away are reviewed once the server finds it back, with no one asking."""
    for text_ in ("one", "two"):
        post(cli.url, text_)
    fake = FakeReviewer(REJECT)
    engine = make_test_engine()
    app = create_app(sessionmaker=make_sessionmaker(engine), token="test-token",
                     embedder=FakeEmbedder(), reviewer=fake, review_poll=0.05)
    with live_app(app) as polling_url:
        wait_until(lambda: httpx.get(f"{polling_url}/health").json()["review_model"] ==
                   "reachable")
        wait_until(lambda: asyncio.run(statuses()) == [(1, "flagged"), (2, "flagged")])
    asyncio.run(engine.dispose())
    assert fake.ids == [1, 2]
    assert [mid for mid, _ in headers(cli.raw("review").stdout)] == [2, 1]
