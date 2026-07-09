"""CLI-surface characterization — assertions specific to the CLI: the rendered
chrome (🕒/👤/🏷️ blocks, "Found N memories"), tag listing with descriptions,
malformed-input exit codes, and the offline error path. These have no
cross-surface meaning, so they stay pinned to the CLI (not in test_behaviors.py).

Every test drives the real `memory-cli` subprocess pointed at the live server.
"""

import pytest

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
