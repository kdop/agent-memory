"""Tests for deploy/deploy.sh: how it reads its arguments and the checks that
stop it before it touches anything, run as a subprocess against a temporary
git repo. Nothing here dumps a database or restarts a unit.
"""

import os
import socket
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "deploy.sh"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True, text=True)


@pytest.fixture
def home(tmp_path):
    """A HOME with no release checkout under it, so the default path is missing."""
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture
def release(tmp_path):
    """A clean git repo with one commit, a tag, and a .env whose port nothing
    listens on. It has a remote (itself) so `git fetch --tags` works."""
    repo = tmp_path / "release"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README.md").write_text("release\n")
    (repo / ".gitignore").write_text(".env\nbackups/\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "first")
    _git(repo, "tag", "v1")
    _git(repo, "remote", "add", "origin", str(repo))
    (repo / ".env").write_text(f"AGENT_MEMORY_PORT={_free_port()}\n")
    return repo


def run(*args, home=None):
    env = dict(os.environ)
    if home is not None:
        env["HOME"] = str(home)
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env)


# ---- arguments ---------------------------------------------------------------

def test_no_command_prints_usage_and_exits_2():
    out = run()
    assert out.returncode == 2
    assert "deploy <ref>" in out.stderr
    assert "rollback <tag>" in out.stderr
    assert "status" in out.stderr


@pytest.mark.parametrize("args", [
    ("deploy",),                 # a tag is required
    ("rollback",),
    ("status", "v1"),            # status takes no tag
    ("frob", "v1"),              # not a command
    ("deploy", "v1", "extra"),   # one tag, not two
    ("--bogus", "deploy", "v1"), # not an option
    ("deploy", "v1", "--release"),  # an option with no value
])
def test_bad_arguments_exit_2(args):
    out = run(*args)
    assert out.returncode == 2, out.stderr
    assert "deploy <ref>" in out.stderr


def test_help_prints_usage():
    out = run("--help")
    assert out.returncode == 2
    assert "--release <path>" in out.stderr
    assert "--no-restart" in out.stderr


# ---- the release checkout ----------------------------------------------------

def test_missing_release_path_is_named(home):
    out = run("deploy", "v1", "--release", str(home / "nowhere"))
    assert out.returncode == 1
    assert f"no release checkout at {home / 'nowhere'}" in out.stderr
    assert "release checkout:" in out.stderr   # the step that stopped


def test_default_release_path_is_under_home(home):
    out = run("status", home=home)
    assert out.returncode == 1
    assert f"no release checkout at {home}/workspace/agent-memory-live" in out.stderr


def test_a_folder_that_is_not_a_checkout_is_refused(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    out = run("status", "--release", str(plain))
    assert out.returncode == 1
    assert "is not a git checkout" in out.stderr


def test_never_runs_against_its_own_checkout():
    out = run("deploy", "v1", "--release", str(ROOT))
    assert out.returncode == 1
    assert "the folder this script lives in" in out.stderr


def test_dirty_tree_stops_before_anything_else(release):
    (release / "README.md").write_text("changed\n")
    (release / "stray.txt").write_text("untracked\n")
    out = run("deploy", "v1", "--release", str(release), "--no-restart")
    assert out.returncode == 1
    assert "changed or untracked files" in out.stderr
    assert " M README.md" in out.stderr
    assert "?? stray.txt" in out.stderr
    assert "database" not in out.stdout        # it never got to the next step
    assert not (release / "backups").exists()


def test_rollback_checks_the_tree_too(release):
    (release / "stray.txt").write_text("untracked\n")
    out = run("rollback", "v1", "--release", str(release), "--no-restart")
    assert out.returncode == 1
    assert "changed or untracked files" in out.stderr


# ---- stopping with the step named --------------------------------------------

def test_no_dsn_names_the_step(release):
    out = run("deploy", "v1", "--release", str(release), "--no-restart")
    assert out.returncode == 1
    assert "database: no AGENT_MEMORY_DB in" in out.stderr
    assert "and no --dsn" in out.stderr
    assert not (release / "backups").exists()


def test_unknown_tag_stops_before_the_backup(release):
    out = run("deploy", "v9", "--release", str(release), "--no-restart",
              "--dsn", "postgresql://nobody:nothing@127.0.0.1:1/none")
    assert out.returncode == 1
    assert "fetching: v9 is not a tag, a branch on origin, or a commit" in out.stderr
    assert not (release / "backups").exists()


def test_option_order_does_not_matter(release):
    a = run("--release", str(release), "--no-restart", "deploy", "v9",
            "--dsn=postgresql://nobody:nothing@127.0.0.1:1/none")
    b = run("deploy", "--dsn", "postgresql://nobody:nothing@127.0.0.1:1/none",
            "v9", f"--release={release}", "--no-restart")
    assert a.returncode == b.returncode == 1
    assert "v9 is not a tag, a branch on origin, or a commit" in a.stderr
    assert "v9 is not a tag, a branch on origin, or a commit" in b.stderr


# ---- status ------------------------------------------------------------------

def test_status_reports_tag_unit_and_health(release):
    out = run("status", "--release", str(release), "--unit", "agent-memory-no-such-unit")
    assert out.returncode == 0, out.stderr
    assert "v1" in out.stdout
    assert "working tree clean" in out.stdout
    assert "unit agent-memory-no-such-unit" in out.stdout
    assert "not answering" in out.stdout


def test_status_off_a_tag_shows_the_commit(release):
    (release / "more.txt").write_text("more\n")
    _git(release, "add", "more.txt")
    _git(release, "commit", "-q", "-m", "second")
    out = run("status", "--release", str(release), "--unit", "agent-memory-no-such-unit")
    assert out.returncode == 0, out.stderr
    assert "(not on a tag)" in out.stdout
