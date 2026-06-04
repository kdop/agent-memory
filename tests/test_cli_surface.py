"""CLI-surface characterization — assertions that are specific to the CLI:
exact rendered chrome, exit codes, dry-run text, the create-confirmation guard,
config/DB-path resolution, and the SQLite FTS-trigger migration. These have no
cross-surface meaning, so they stay pinned to the CLI (not in test_behaviors.py).

Faithfully preserves the behavior the old tests/test_memory_cli.py pinned.
"""

import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from drivers import MEMORY_CLI, CliDriver


# ── primed-CLI surface tests ────────────────────────────────────────────────
@pytest.fixture
def primed(tmp_path):
    drv = CliDriver(tmp_path / "memory.db")
    drv.initialize()
    return drv


def test_isolation_uses_tempdir(primed):
    assert str(primed.db_path).startswith(tempfile.gettempdir())


def test_add_chrome(primed):
    assert "✓ Memory #1 added (tester)" in primed.raw("add", "hello world").stdout


def test_add_explicit_agent_chrome(primed):
    assert "✓ Memory #1 added (clu)" in primed.raw("add", "x", "--agent", "clu").stdout


def test_query_empty_chrome(primed):
    assert "No memories found." in primed.raw("query").stdout


def test_query_block_and_footer_chrome(primed):
    primed.raw("add", "remember the milk", "--project", "home",
               "--tags", "shopping,food", "--type", "note")
    out = primed.raw("query").stdout
    assert "#1" in out
    assert "👤 tester @ home [note]" in out
    assert "🏷️" in out
    assert "food, shopping" in out          # alphabetical
    assert "remember the milk" in out
    assert "Found 1 memories" in out        # always 'memories', even for 1


def test_search_chrome(primed):
    primed.raw("add", "the quick brown fox")
    out = primed.raw("search", "brown").stdout
    assert "🔍 Search results for: brown" in out
    assert "Found 1 matches" in out


def test_search_no_match_chrome(primed):
    primed.raw("add", "nothing relevant here")
    assert "No memories found for: zzzznope" in primed.raw("search", "zzzznope").stdout


def test_tags_empty_chrome(primed):
    assert "No tags found." in primed.raw("tags").stdout


def test_tags_listing_chrome(primed):
    primed.raw("add", "a", "--tags", "auth")
    primed.raw("add", "b", "--tags", "auth,db")
    out = primed.raw("tags").stdout
    assert "🏷️" in out
    assert "auth" in out
    assert "(2)" in out
    assert "(1)" in out


def test_projects_empty_chrome(primed):
    assert "No projects found." in primed.raw("projects").stdout


def test_projects_listing_chrome(primed):
    primed.raw("add", "a", "--project", "alpha")
    primed.raw("add", "b", "--project", "alpha")
    out = primed.raw("projects").stdout
    assert "📂 Projects:" in out
    assert "alpha" in out
    assert "(2 memories)" in out


def test_stats_chrome(primed):
    primed.raw("add", "a", "--project", "p", "--tags", "t")
    out = primed.raw("stats").stdout
    assert "📊 Memory Statistics" in out
    assert "Total memories:    1" in out
    assert "Tags:              1" in out


def test_show_found_chrome(primed):
    primed.raw("add", "findable content")
    out = primed.raw("show", "1").stdout
    assert "#1" in out
    assert "findable content" in out


def test_show_not_found_exits_1(primed):
    proc = primed.raw("show", "999")
    assert proc.returncode == 1
    assert "✗ Memory #999 not found." in proc.stdout


def test_update_chrome(primed):
    primed.raw("add", "old text")
    out = primed.raw("update", "1", "--content", "new text").stdout
    assert "✓ Memory #1 updated" in out
    assert "content" in out


def test_update_content_from_stdin(primed):
    primed.raw("add", "old")
    out = primed.raw("update", "1", "--content", "-", stdin="piped in").stdout
    assert "✓ Memory #1 updated" in out
    assert "piped in" in primed.raw("show", "1").stdout


def test_update_no_changes_chrome(primed):
    primed.raw("add", "x")
    assert "No changes specified for memory #1" in primed.raw("update", "1").stdout


def test_update_not_found_exits_1(primed):
    proc = primed.raw("update", "999", "--content", "y")
    assert proc.returncode == 1
    assert "✗ Memory #999 not found." in proc.stdout


def test_delete_dry_run_does_not_delete(primed):
    primed.raw("add", "doomed")
    out = primed.raw("delete", "1").stdout
    assert "Will delete 1 memory:" in out
    assert "Dry run" in out
    assert "#1" in primed.raw("show", "1").stdout      # still there


def test_delete_with_yes_chrome(primed):
    primed.raw("add", "doomed")
    out = primed.raw("delete", "1", "--yes").stdout
    assert "✓ Deleted 1 memory." in out
    assert primed.raw("show", "1").returncode == 1     # gone


def test_delete_reports_missing_ids(primed):
    primed.raw("add", "real")
    assert "⚠ Not found: 999" in primed.raw("delete", "1", "999").stdout


def test_delete_none_found_exits_1(primed):
    proc = primed.raw("delete", "999")
    assert proc.returncode == 1
    assert "No memories found with the given IDs." in proc.stdout


def test_legacy_fts_triggers_upgraded_on_next_run(primed):
    # Reinstall the old buggy triggers, then let the CLI upgrade them on next run.
    conn = sqlite3.connect(primed.db_path)
    conn.executescript("""
        DROP TRIGGER IF EXISTS memories_ad;
        DROP TRIGGER IF EXISTS memories_au;
        CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
            DELETE FROM memories_fts WHERE rowid = old.id;
        END;
        CREATE TRIGGER memories_au AFTER UPDATE ON memories BEGIN
            UPDATE memories_fts SET content = new.content WHERE rowid = new.id;
        END;
    """)
    conn.commit()
    conn.close()

    assert "Upgraded FTS triggers" in primed.raw("stats").stdout
    conn = sqlite3.connect(primed.db_path)
    au = conn.execute("SELECT sql FROM sqlite_master WHERE name='memories_au'").fetchone()[0]
    conn.close()
    assert "INSERT INTO memories_fts(memories_fts" in au
    # idempotent: a second run does not re-report an upgrade
    assert "Upgraded FTS triggers" not in primed.raw("stats").stdout


# ── config / DB-path resolution (AGENT_MEMORY_DB > db_path > XDG default) ────
class ConfigEnv:
    """Runs the CLI with isolated XDG dirs and no AGENT_MEMORY_DB."""

    def __init__(self, root):
        self.root = root
        self.data_home = root / "data"
        self.env = {k: v for k, v in os.environ.items() if k != "AGENT_MEMORY_DB"}
        self.env.update({
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(self.data_home),
            "AGENT_NAME": "tester",
        })

    def run(self, *args, env=None):
        return subprocess.run(
            [sys.executable, str(MEMORY_CLI), *args],
            env=env or self.env, capture_output=True, text=True,
        )

    @property
    def default_db(self):
        return self.data_home / "agent-memory" / "memory.db"


@pytest.fixture
def cfg(tmp_path):
    return ConfigEnv(tmp_path)


def test_config_set_get_path(cfg):
    out = cfg.run("config", "set", "db_path", "/tmp/foo.db").stdout
    assert "✓ Set db_path = /tmp/foo.db" in out
    assert cfg.run("config", "get", "db_path").stdout.strip() == "/tmp/foo.db"
    assert "config.json" in cfg.run("config", "path").stdout


def test_config_get_unset_key(cfg):
    assert "(unset) nope" in cfg.run("config", "get", "nope").stdout


def test_config_command_creates_no_db(cfg):
    cfg.run("config", "get")
    assert not cfg.default_db.exists()


def test_default_is_xdg_data_dir(cfg):
    out = cfg.run("--yes", "stats").stdout
    assert str(cfg.data_home) in out
    assert cfg.default_db.exists()


def test_stored_db_path_is_used(cfg):
    target = cfg.root / "custom" / "m.db"
    cfg.run("config", "set", "db_path", str(target))
    cfg.run("--yes", "add", "hello via config")
    assert target.exists()
    assert "hello via config" in cfg.run("query").stdout


def test_env_overrides_stored(cfg):
    stored = cfg.root / "stored.db"
    envdb = cfg.root / "env.db"
    cfg.run("config", "set", "db_path", str(stored))
    cfg.run("--yes", "add", "in env db", env={**cfg.env, "AGENT_MEMORY_DB": str(envdb)})
    assert envdb.exists()
    assert not stored.exists()          # env won; stored path never created


def test_missing_db_without_yes_errors_and_creates_nothing(cfg):
    proc = cfg.run("stats")             # no --yes, no TTY in subprocess
    assert proc.returncode == 1
    assert "Pass --yes" in proc.stderr
    assert not cfg.default_db.exists()


def test_missing_db_with_yes_creates(cfg):
    proc = cfg.run("--yes", "stats")
    assert proc.returncode == 0
    assert cfg.default_db.exists()


def test_existing_db_needs_no_yes(cfg):
    cfg.run("--yes", "stats")           # create it once
    assert cfg.run("stats").returncode == 0   # now exists -> no prompt, no error
