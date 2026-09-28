"""CLI-surface characterization — assertions specific to the CLI: the rendered
chrome (🕒/👤/🏷️ blocks, "Found N memories"), tag listing with descriptions,
malformed-input exit codes, and the offline error path. These have no
cross-surface meaning, so they stay pinned to the CLI (not in test_behaviors.py).

Every test drives the real `memory-cli` subprocess pointed at the live server.
"""

import re

import pytest

from conftest import verify
from drivers import CliDriver


@pytest.fixture
def cli(live_server):
    url, token = live_server
    return CliDriver(url, token)


# ── add / query chrome ───────────────────────────────────────────────────────
def test_add_chrome(cli):
    assert "✓ Memory #1 added (tester)" in cli.raw("add", "hello world").stdout


def test_add_explicit_agent_chrome(cli):
    assert "✓ Memory #1 added (clu)" in cli.raw("add", "x", "--agent", "clu").stdout


def test_add_prints_warnings_after_id(cli):
    out = cli.raw("add", "tiny", "--type", "decision").stdout
    lines = out.splitlines()
    assert lines[0] == "✓ Memory #1 added (tester)"
    assert lines[1:] == ["warning: short", "warning: no-project", "warning: no-reasoning"]
    # The memory is stored even with warnings.
    assert cli.get(1) is not None


def test_add_clean_entry_prints_no_warnings(cli):
    out = cli.raw("add", "Run the build on every push because the nightly was too slow.",
                  "--project", "ci", "--type", "decision").stdout
    assert "warning:" not in out


def test_query_empty_chrome(cli):
    assert "No memories found." in cli.raw("query").stdout


def test_query_block_and_footer_chrome(cli):
    cli.raw("add", "remember the milk", "--project", "home",
            "--tags", '[{"name":"shopping"},{"name":"food"}]', "--type", "note")
    out = cli.raw("query").stdout
    assert "#1" in out
    assert "🕒" in out
    assert "👤 tester @ home [note]" in out
    assert "🏷️" in out
    assert "food, shopping" in out          # alphabetical
    assert "remember the milk" in out
    assert "Found 1 memories" in out        # always 'memories', even for 1


def test_search_chrome(cli):
    cli.raw("add", "the quick brown fox")
    out = cli.raw("search", "brown").stdout
    assert "🔍 Search results for: brown" in out
    assert "Found 1 matches" in out


def test_search_no_match_chrome(cli):
    cli.raw("add", "nothing relevant here")
    assert "No memories found for: zzzznope" in cli.raw("search", "zzzznope").stdout


# ── search --mode ────────────────────────────────────────────────────────────
def _seed_search(cli):
    for content in ("the cat sat on the mat", "a dog in the yard",
                    "rain on the window", "coffee before code"):
        cli.raw("add", content)


def test_search_help_explains_each_mode(cli):
    out = cli.raw("search", "--help").stdout
    assert "--mode" in out
    assert "keyword" in out and "matches words" in out
    assert "semantic" in out and "matches meaning" in out
    assert "hybrid" in out and "combines both" in out


def test_search_defaults_to_keyword(cli):
    _seed_search(cli)
    plain = cli.raw("search", "cat").stdout
    keyword = cli.raw("search", "cat", "--mode", "keyword").stdout
    assert plain == keyword
    assert "Found 1 matches" in plain
    assert "→cat←" in plain                  # the keyword snippet marks the match


def test_search_keyword_prints_score_in_header(cli):
    _seed_search(cli)
    out = cli.raw("search", "cat").stdout
    assert re.search(r"^━━━ #1 status: unverified score -?\d\.\d\d ━+$", out, re.M)


def test_search_semantic_prints_score_and_ranks_exact_text_first(cli):
    _seed_search(cli)
    proc = cli.raw("search", "the cat sat on the mat", "--mode", "semantic")
    assert proc.returncode == 0
    out = proc.stdout
    # Every stored memory comes back, best first, with a two-decimal score.
    headers = re.findall(r"^━━━ #(\d+) status: \w+ score (-?\d\.\d\d) ━+$", out, re.M)
    assert len(headers) == 4
    assert headers[0] == ("1", "1.00")
    scores = [float(sc) for _id, sc in headers]
    assert scores == sorted(scores, reverse=True)
    # Semantic search has no snippet, so the content is shown instead of "None".
    assert "the cat sat on the mat" in out
    assert "None" not in out
    assert "Found 4 matches" in out


def test_search_mode_rejects_unknown_value(cli):
    proc = cli.raw("search", "x", "--mode", "fuzzy")
    assert proc.returncode == 2
    assert "invalid choice: 'fuzzy'" in proc.stderr


def test_search_hybrid_returns_fused_results(cli):
    # hybrid reaches the server and comes back fused: the exact text is found by
    # words and by meaning, so it ranks first with a score of 2/61.
    cli.raw("add", "the cat sat on the mat")
    cli.raw("add", "a dog in the yard")
    proc = cli.raw("search", "the cat sat on the mat", "--mode", "hybrid")
    assert proc.returncode == 0, proc.stderr
    assert "#1 status: unverified score 0.03" in proc.stdout
    assert proc.stdout.index("#1 ") < proc.stdout.index("#2 ")


# ── tags listing ─────────────────────────────────────────────────────────────
def test_tags_empty_chrome(cli):
    assert "No tags found." in cli.raw("tags").stdout


def test_tags_listing_chrome(cli):
    cli.raw("add", "a", "--tags", '[{"name":"auth"}]')
    cli.raw("add", "b", "--tags", '[{"name":"auth"},{"name":"db"}]')
    out = cli.raw("tags").stdout
    assert "🏷️" in out
    assert "auth" in out
    assert "(2)" in out
    assert "(1)" in out


def test_tag_descriptor_shown_in_listing(cli):
    cli.raw("add", "x", "--tags", '[{"name":"auth","description":"authentication flow"}]')
    assert "authentication flow" in cli.raw("tags").stdout


def test_tag_auto_default_description_not_shown_redundantly(cli):
    # A new tag with no description defaults to its own name; the listing skips the
    # '↳ description' line in that case rather than echoing the name back.
    cli.raw("add", "x", "--tags", '[{"name":"plain"}]')
    out = cli.raw("tags").stdout
    assert "plain" in out
    assert "↳" not in out


# ── malformed --tags ─────────────────────────────────────────────────────────
def test_tags_malformed_json_errors(cli):
    proc = cli.raw("add", "x", "--tags", "not-json-and-not-comma-either")
    assert proc.returncode != 0
    assert "JSON array" in proc.stdout
    assert "No memories found." in cli.raw("query").stdout  # nothing was stored


def test_tags_missing_name_field_errors(cli):
    proc = cli.raw("add", "x", "--tags", '[{"description":"no name given"}]')
    assert proc.returncode != 0
    assert "name" in proc.stdout.lower()


# ── projects / stats / show chrome ───────────────────────────────────────────
def test_projects_empty_chrome(cli):
    assert "No projects found." in cli.raw("projects").stdout


def test_projects_listing_chrome(cli):
    cli.raw("add", "a", "--project", "alpha")
    cli.raw("add", "b", "--project", "alpha")
    out = cli.raw("projects").stdout
    assert "📂 Projects:" in out
    assert "alpha" in out
    assert "(2 memories)" in out


def test_stats_chrome(cli):
    cli.raw("add", "a", "--project", "p", "--tags", '[{"name":"t"}]')
    out = cli.raw("stats").stdout
    assert "📊 Memory Statistics" in out
    assert "Total memories:    1" in out
    assert "Tags:              1" in out


def test_show_found_chrome(cli):
    cli.raw("add", "findable content")
    out = cli.raw("show", "1").stdout
    assert "#1" in out
    assert "findable content" in out


def test_show_not_found_exits_1(cli):
    proc = cli.raw("show", "999")
    assert proc.returncode == 1
    assert "✗ Memory #999 not found." in proc.stdout


# ── update chrome ────────────────────────────────────────────────────────────
def test_update_chrome(cli):
    cli.raw("add", "old text")
    out = cli.raw("update", "1", "--content", "new text").stdout
    assert "✓ Memory #1 updated" in out
    assert "content" in out


def test_update_content_from_stdin(cli):
    cli.raw("add", "old")
    out = cli.raw("update", "1", "--content", "-", stdin="piped in").stdout
    assert "✓ Memory #1 updated" in out
    assert "piped in" in cli.raw("show", "1").stdout


def test_update_no_changes_chrome(cli):
    cli.raw("add", "x")
    assert "No changes specified for memory #1" in cli.raw("update", "1").stdout


def test_update_not_found_exits_1(cli):
    proc = cli.raw("update", "999", "--content", "y")
    assert proc.returncode == 1
    assert "✗ Memory #999 not found." in proc.stdout


# ── delete chrome ────────────────────────────────────────────────────────────
def test_delete_dry_run_does_not_delete(cli):
    cli.raw("add", "doomed")
    out = cli.raw("delete", "1").stdout
    assert "Will delete 1 memory:" in out
    assert "Dry run" in out
    assert "#1" in cli.raw("show", "1").stdout      # still there


def test_delete_with_yes_chrome(cli):
    cli.raw("add", "doomed")
    out = cli.raw("delete", "1", "--yes").stdout
    assert "✓ Deleted 1 memory." in out
    assert cli.raw("show", "1").returncode == 1     # gone


def test_delete_reports_missing_ids(cli):
    cli.raw("add", "real")
    assert "⚠ Not found: 999" in cli.raw("delete", "1", "999").stdout


def test_delete_none_found_exits_1(cli):
    proc = cli.raw("delete", "999")
    assert proc.returncode == 1
    assert "No memories found with the given IDs." in proc.stdout


# ── duplicate refusal ────────────────────────────────────────────────────────
def test_add_duplicate_exits_3_with_hint(cli):
    assert "✓ Memory #1 added" in cli.raw("add", "dup me", "--project", "proj").stdout
    verify(1)   # only a verified memory counts as reference
    proc = cli.raw("add", "dup me", "--project", "proj")
    assert proc.returncode == 3
    assert proc.stdout.strip() == (
        "✗ Duplicate of memory #1 (score 1.00). Use 'memory update 1' or --force.")
    assert "Traceback" not in proc.stderr
    assert "Found 1 memories" in cli.raw("query").stdout   # nothing new stored


def test_add_force_stores_the_duplicate(cli):
    cli.raw("add", "dup me", "--project", "proj")
    verify(1)
    proc = cli.raw("add", "dup me", "--project", "proj", "--force")
    assert proc.returncode == 0
    assert "✓ Memory #2 added (tester)" in proc.stdout
    assert "Found 2 memories" in cli.raw("query").stdout


def test_add_help_lists_force(cli):
    assert "--force" in cli.raw("add", "--help").stdout


# ── offline error path (no traceback, exit 1) ────────────────────────────────
def test_offline_api_prints_clean_error(cli):
    # Point the CLI at a dead endpoint: it must render a clean "cannot reach"
    # message and exit 1 — never dump a traceback.
    dead = CliDriver("http://127.0.0.1:1", cli.token)
    proc = dead.raw("stats")
    assert proc.returncode == 1
    assert "cannot reach the memory API" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert "Traceback" not in proc.stdout


# ── --all / truncation footer ────────────────────────────────────────────────
def test_query_footer_says_when_truncated(cli):
    for i in range(3):
        cli.raw("add", f"m{i}")
    out = cli.raw("query", "--limit", "2").stdout
    assert "Found 2 of 3 memories (use --all or --limit to see more)" in out


def test_query_all_flag_returns_everything(cli):
    for i in range(3):
        cli.raw("add", f"m{i}")
    out = cli.raw("query", "--all", "--limit", "1").stdout   # --all wins
    assert "Found 3 memories" in out


def test_search_footer_says_when_limit_reached(cli):
    for i in range(3):
        cli.raw("add", f"needle {i}")
    out = cli.raw("search", "needle", "--limit", "2").stdout
    assert "Found 2 matches (limit reached; use --all or --limit to see more)" in out
    assert "Found 3 matches" in cli.raw("search", "needle", "--all").stdout


# ── reindex ──────────────────────────────────────────────────────────────────
def test_reindex_chrome(cli):
    import asyncio

    from sqlalchemy import text

    from conftest import make_test_engine

    cli.raw("add", "one")
    cli.raw("add", "two")

    # Take the vectors away, as if the rows were written before the model existed.
    async def _clear():
        engine = make_test_engine()
        try:
            async with engine.begin() as conn:
                await conn.execute(text("UPDATE memories SET embedding = NULL, embedding_model = NULL"))
        finally:
            await engine.dispose()

    asyncio.run(_clear())

    proc = cli.raw("reindex")
    assert proc.returncode == 0
    assert "✓ Reindexed 2 memories, 0 tags" in proc.stdout
    assert "✓ Reindexed 0 memories, 0 tags" in cli.raw("reindex").stdout
