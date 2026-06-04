#!/usr/bin/env python3
"""
Characterization tests for memory-cli.

These pin the CLI's *current* observable behavior so the storage-abstraction
refactor (#1) can be proven to preserve it. Each test runs the real script as a
subprocess against a fresh temp DB (AGENT_MEMORY_DB), so nothing here ever touches
the live database. Stdlib only — no third-party deps.

Run:  python -m unittest discover -s tests
"""

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MEMORY_CLI = Path(__file__).resolve().parent.parent / "memory-cli"

# Lines carrying a timestamp are volatile; collapse them for stable assertions.
_TS_LINE = re.compile(r"(🕒|Oldest:|Newest:).*")


def normalize(text):
    return "\n".join(_TS_LINE.sub(r"\1 <TS>", line) for line in text.splitlines())


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "memory.db"
        self.env = {
            **os.environ,
            "AGENT_MEMORY_DB": str(self.db),
            "AGENT_NAME": "tester",
        }
        # Prime the DB so the one-time "Initialized..." banner doesn't bleed into
        # the assertions of the command actually under test.
        self.run_cli("stats")
        self.assertTrue(self.db.exists(), "priming run should create the DB")

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args, stdin=None, expect_code=None):
        proc = subprocess.run(
            [sys.executable, str(MEMORY_CLI), *args],
            env=self.env,
            input=stdin,
            capture_output=True,
            text=True,
        )
        if expect_code is not None:
            self.assertEqual(
                proc.returncode, expect_code,
                f"args={args}\nstdout={proc.stdout}\nstderr={proc.stderr}",
            )
        return proc

    # ---- isolation guard --------------------------------------------------
    def test_never_touches_live_db(self):
        self.assertTrue(str(self.db).startswith(tempfile.gettempdir()))

    # ---- add --------------------------------------------------------------
    def test_add_reports_id_and_agent(self):
        out = self.run_cli("add", "hello world").stdout
        self.assertIn("✓ Memory #1 added (tester)", out)

    def test_add_explicit_agent_overrides_env(self):
        out = self.run_cli("add", "x", "--agent", "clu").stdout
        self.assertIn("✓ Memory #1 added (clu)", out)

    def test_add_increments_ids(self):
        self.run_cli("add", "first")
        out = self.run_cli("add", "second").stdout
        self.assertIn("✓ Memory #2 added (tester)", out)

    # ---- query ------------------------------------------------------------
    def test_query_empty(self):
        self.assertIn("No memories found.", self.run_cli("query").stdout)

    def test_query_shows_block_and_footer(self):
        self.run_cli("add", "remember the milk",
                     "--project", "home", "--tags", "shopping,food", "--type", "note")
        out = self.run_cli("query").stdout
        self.assertIn("#1", out)
        self.assertIn("👤 tester @ home [note]", out)
        self.assertIn("🏷️", out)
        self.assertIn("food, shopping", out)         # tags rendered alphabetically (ORDER BY name)
        self.assertIn("remember the milk", out)
        self.assertIn("Found 1 memories", out)       # always 'memories', even for 1

    def test_query_limit(self):
        for i in range(3):
            self.run_cli("add", f"m{i}")
        out = self.run_cli("query", "--limit", "2").stdout
        self.assertIn("Found 2 memories", out)

    def test_query_project_filter(self):
        self.run_cli("add", "a", "--project", "alpha")
        self.run_cli("add", "b", "--project", "beta")
        out = self.run_cli("query", "--project", "alpha").stdout
        self.assertIn("Found 1 memories", out)
        self.assertIn("a", out)

    def test_query_tag_filter(self):
        self.run_cli("add", "tagged", "--tags", "auth")
        self.run_cli("add", "untagged")
        out = self.run_cli("query", "--tag", "auth").stdout
        self.assertIn("Found 1 memories", out)

    def test_query_type_filter(self):
        self.run_cli("add", "d", "--type", "decision")
        self.run_cli("add", "c", "--type", "code")
        out = self.run_cli("query", "--type", "decision").stdout
        self.assertIn("Found 1 memories", out)

    # ---- search -----------------------------------------------------------
    def test_search_match(self):
        self.run_cli("add", "the quick brown fox")
        out = self.run_cli("search", "brown").stdout
        self.assertIn("🔍 Search results for: brown", out)
        self.assertIn("Found 1 matches", out)

    def test_search_no_match(self):
        self.run_cli("add", "nothing relevant here")
        out = self.run_cli("search", "zzzznope").stdout
        self.assertIn("No memories found for: zzzznope", out)

    # ---- tags / projects --------------------------------------------------
    def test_tags_empty(self):
        self.assertIn("No tags found.", self.run_cli("tags").stdout)

    def test_tags_listing_with_counts(self):
        self.run_cli("add", "a", "--tags", "auth")
        self.run_cli("add", "b", "--tags", "auth,db")
        out = self.run_cli("tags").stdout
        self.assertIn("🏷️", out)
        self.assertIn("auth", out)
        self.assertIn("(2)", out)   # auth used twice
        self.assertIn("(1)", out)   # db used once

    def test_projects_empty(self):
        self.assertIn("No projects found.", self.run_cli("projects").stdout)

    def test_projects_listing_with_counts(self):
        self.run_cli("add", "a", "--project", "alpha")
        self.run_cli("add", "b", "--project", "alpha")
        out = self.run_cli("projects").stdout
        self.assertIn("📂 Projects:", out)
        self.assertIn("alpha", out)
        self.assertIn("(2 memories)", out)

    # ---- stats ------------------------------------------------------------
    def test_stats_counts(self):
        self.run_cli("add", "a", "--project", "p", "--tags", "t")
        out = self.run_cli("stats").stdout
        self.assertIn("📊 Memory Statistics", out)
        self.assertIn("Total memories:", out)
        self.assertRegex(out, r"Total memories:\s+1")
        self.assertRegex(out, r"Tags:\s+1")

    # ---- show -------------------------------------------------------------
    def test_show_found(self):
        self.run_cli("add", "findable content")
        out = self.run_cli("show", "1").stdout
        self.assertIn("#1", out)
        self.assertIn("findable content", out)

    def test_show_not_found_exits_1(self):
        proc = self.run_cli("show", "999", expect_code=1)
        self.assertIn("✗ Memory #999 not found.", proc.stdout)

    # ---- update -----------------------------------------------------------
    def test_update_content(self):
        self.run_cli("add", "old text")
        out = self.run_cli("update", "1", "--content", "new text").stdout
        self.assertIn("✓ Memory #1 updated", out)
        self.assertIn("content", out)
        self.assertIn("new text", self.run_cli("show", "1").stdout)

    def test_update_content_from_stdin(self):
        self.run_cli("add", "old")
        out = self.run_cli("update", "1", "--content", "-", stdin="piped in").stdout
        self.assertIn("✓ Memory #1 updated", out)
        self.assertIn("piped in", self.run_cli("show", "1").stdout)

    def test_update_add_and_remove_tags(self):
        self.run_cli("add", "x", "--tags", "keep,drop")
        out = self.run_cli("update", "1", "--add-tags", "new", "--remove-tags", "drop").stdout
        self.assertIn("✓ Memory #1 updated", out)
        shown = self.run_cli("show", "1").stdout
        self.assertIn("keep", shown)
        self.assertIn("new", shown)
        self.assertNotIn("drop", shown)

    def test_update_no_changes(self):
        self.run_cli("add", "x")
        out = self.run_cli("update", "1").stdout
        self.assertIn("No changes specified for memory #1", out)

    def test_update_not_found_exits_1(self):
        proc = self.run_cli("update", "999", "--content", "y", expect_code=1)
        self.assertIn("✗ Memory #999 not found.", proc.stdout)

    # ---- delete -----------------------------------------------------------
    def test_delete_dry_run_does_not_delete(self):
        self.run_cli("add", "doomed")
        out = self.run_cli("delete", "1").stdout
        self.assertIn("Will delete 1 memory:", out)
        self.assertIn("Dry run", out)
        self.assertIn("#1", self.run_cli("show", "1").stdout)   # still there

    def test_delete_with_yes(self):
        self.run_cli("add", "doomed")
        out = self.run_cli("delete", "1", "--yes").stdout
        self.assertIn("✓ Deleted 1 memory.", out)
        self.run_cli("show", "1", expect_code=1)                 # gone

    def test_delete_reports_missing_ids(self):
        self.run_cli("add", "real")
        out = self.run_cli("delete", "1", "999").stdout
        self.assertIn("⚠ Not found: 999", out)

    def test_delete_none_found_exits_1(self):
        proc = self.run_cli("delete", "999", expect_code=1)
        self.assertIn("No memories found with the given IDs.", proc.stdout)

    # ---- FTS index consistency (regression for #10) -----------------------
    def test_repeated_content_updates_keep_fts_searchable(self):
        # External-content FTS5 must be kept in sync via the 'delete' command;
        # the naive UPDATE-trigger pattern corrupts the index under repeated edits.
        self.run_cli("add", "seed alpha beta gamma")
        for i in range(30):
            self.run_cli("update", "1", "--content",
                         f"iteration {i} delta epsilon zeta {i}", expect_code=0)
        self.assertIn("Found 1 matches", self.run_cli("search", "epsilon").stdout)

    def test_update_reindexes_old_term_gone_new_term_found(self):
        self.run_cli("add", "findme original orangutan")
        self.run_cli("update", "1", "--content", "replaced penguin", expect_code=0)
        self.assertIn("No memories found for: orangutan",
                      self.run_cli("search", "orangutan").stdout)
        self.assertIn("Found 1 matches", self.run_cli("search", "penguin").stdout)

    def test_delete_removes_from_fts(self):
        self.run_cli("add", "keepme aardvark")
        self.run_cli("add", "deleteme aardvark")
        self.run_cli("delete", "2", "--yes")
        self.assertIn("Found 1 matches", self.run_cli("search", "aardvark").stdout)

    def test_legacy_fts_triggers_are_upgraded_on_next_run(self):
        # Simulate a pre-fix DB by reinstalling the old buggy triggers, then let the
        # CLI upgrade them on its next invocation.
        import sqlite3
        conn = sqlite3.connect(self.db)
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

        out = self.run_cli("stats").stdout
        self.assertIn("Upgraded FTS triggers", out)

        conn = sqlite3.connect(self.db)
        au = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='memories_au'"
        ).fetchone()[0]
        conn.close()
        self.assertIn("INSERT INTO memories_fts(memories_fts", au)
        # idempotent: a second run does not re-report an upgrade
        self.assertNotIn("Upgraded FTS triggers", self.run_cli("stats").stdout)


class ConfigTestCase(unittest.TestCase):
    """DB-path resolution: AGENT_MEMORY_DB env > stored db_path > XDG data default."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.cfg_home = root / "config"
        self.data_home = root / "data"
        self.root = root
        self.env = {k: v for k, v in os.environ.items() if k != "AGENT_MEMORY_DB"}
        self.env.update({
            "XDG_CONFIG_HOME": str(self.cfg_home),
            "XDG_DATA_HOME": str(self.data_home),
            "AGENT_NAME": "tester",
        })

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args, env=None):
        return subprocess.run(
            [sys.executable, str(MEMORY_CLI), *args],
            env=env or self.env, capture_output=True, text=True,
        )

    def test_config_set_get_path(self):
        out = self.run_cli("config", "set", "db_path", "/tmp/foo.db").stdout
        self.assertIn("✓ Set db_path = /tmp/foo.db", out)
        self.assertEqual(self.run_cli("config", "get", "db_path").stdout.strip(), "/tmp/foo.db")
        self.assertIn("config.json", self.run_cli("config", "path").stdout)

    def test_config_get_unset_key(self):
        self.assertIn("(unset) nope", self.run_cli("config", "get", "nope").stdout)

    def test_config_command_creates_no_db(self):
        self.run_cli("config", "get")
        self.assertFalse((self.data_home / "agent-memory" / "memory.db").exists())

    def test_default_is_xdg_data_dir(self):
        out = self.run_cli("stats").stdout
        self.assertIn(str(self.data_home), out)
        self.assertTrue((self.data_home / "agent-memory" / "memory.db").exists())

    def test_stored_db_path_is_used(self):
        target = self.root / "custom" / "m.db"
        self.run_cli("config", "set", "db_path", str(target))
        self.run_cli("add", "hello via config")
        self.assertTrue(target.exists())
        self.assertIn("hello via config", self.run_cli("query").stdout)

    def test_env_overrides_stored(self):
        stored = self.root / "stored.db"
        envdb = self.root / "env.db"
        self.run_cli("config", "set", "db_path", str(stored))
        self.run_cli("add", "in env db", env={**self.env, "AGENT_MEMORY_DB": str(envdb)})
        self.assertTrue(envdb.exists())
        self.assertFalse(stored.exists())  # env won; stored path never created


if __name__ == "__main__":
    unittest.main()
