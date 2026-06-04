"""CLI-over-API integration (Ticket C).

Proves the full remote path: the real `memory-cli` subprocess → `ApiStore`
(selected because `AGENT_MEMORY_API` is set) → HTTP → the FastAPI service →
`SqliteStore`. One test per CLI command, so "every CLI command hits the service
identically" (PLAN Ticket C accept). Full behavior parity is already covered by
`ApiDriver` (direct API) and `CliDriver` (direct SQLite); this pins the wiring +
`get_store()` selection end to end.

Each test gets a fresh in-process server over a scratch DB (rule #1).
"""

import socket
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

import uvicorn  # noqa: E402

from agent_memory.server import create_app  # noqa: E402
from agent_memory.store import SqliteStore  # noqa: E402
from drivers import CliDriver  # noqa: E402

TOKEN = "dev"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _ThreadedServer:
    """Run uvicorn in a background thread bound to a scratch-DB app."""

    def __init__(self, db_path):
        self._app = create_app(store=SqliteStore(db_path), token=TOKEN)

    def __enter__(self):
        port = _free_port()
        config = uvicorn.Config(self._app, host="127.0.0.1", port=port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 15
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("server did not start in time")
            time.sleep(0.02)
        self.url = f"http://127.0.0.1:{port}"
        return self

    def __exit__(self, *exc):
        self._server.should_exit = True
        self._thread.join(timeout=5)


@pytest.fixture
def api_cli(tmp_path):
    """A CliDriver whose subprocess is steered at the live service via env, so the
    CLI runs through ApiStore instead of touching SQLite directly."""
    with _ThreadedServer(tmp_path / "memory.db") as srv:
        drv = CliDriver(
            tmp_path / "memory.db",
            extra_env={"AGENT_MEMORY_API": srv.url, "AGENT_MEMORY_API_TOKEN": TOKEN},
        )
        drv.initialize()
        yield drv


def test_add_and_show_over_api(api_cli):
    mid = api_cli.add("hello via api", project="p", tags="x,y", type="note")
    assert mid == 1
    m = api_cli.get(mid)
    assert m.content == "hello via api"
    assert m.project == "p"
    assert m.type == "note"
    assert m.tags == ["x", "y"]


def test_query_filter_over_api(api_cli):
    api_cli.add("a", project="alpha")
    api_cli.add("b", project="beta")
    rows = api_cli.query(project="alpha")
    assert len(rows) == 1
    assert "a" in rows[0].content


def test_search_over_api(api_cli):
    api_cli.add("the quick brown fox")
    rows = api_cli.search("brown")
    assert len(rows) == 1
    assert "brown" in rows[0].snippet


def test_update_over_api(api_cli):
    mid = api_cli.add("old text")
    assert api_cli.update(mid, content="new text") == "updated"
    assert "new text" in api_cli.get(mid).content


def test_delete_over_api(api_cli):
    mid = api_cli.add("doomed")
    assert api_cli.delete(mid) == 1
    assert api_cli.get(mid) is None


def test_show_not_found_over_api(api_cli):
    assert api_cli.get(999) is None


def test_tags_over_api(api_cli):
    api_cli.add("a", tags="auth")
    api_cli.add("b", tags="auth,db")
    assert dict(api_cli.tags())["auth"] == 2


def test_projects_over_api(api_cli):
    api_cli.add("a", project="alpha")
    api_cli.add("b", project="alpha")
    assert ("alpha", 2) in api_cli.projects()


def test_stats_over_api(api_cli):
    api_cli.add("a", project="p", tags="t")
    s = api_cli.stats()
    assert s["total"] == 1
    assert s["tags"] == 1
