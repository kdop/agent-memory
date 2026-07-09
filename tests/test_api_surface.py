"""API-surface characterization — assertions specific to the HTTP service:
bearer auth (401), the unauthenticated /health probe, and that the server boots
via `python -m agent_memory.server`. Cross-surface behavior lives in
test_behaviors.py (run against the API through `ApiDriver`).
"""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agent_memory.server import create_app  # noqa: E402
from agent_memory.store import SqliteStore  # noqa: E402

TOKEN = "dev"
SRC = Path(__file__).resolve().parent.parent / "src"


@pytest.fixture
def client(tmp_path):
    app = create_app(store=SqliteStore(tmp_path / "memory.db"), token=TOKEN)
    return TestClient(app)


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


# ── auth ────────────────────────────────────────────────────────────────────
def test_health_needs_no_auth(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_protected_route_without_token_401(client):
    assert client.get("/stats").status_code == 401


def test_protected_route_bad_token_401(client):
    resp = client.get("/stats", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


def test_protected_route_good_token_200(client):
    assert client.get("/stats", headers=_auth()).status_code == 200


def test_server_with_no_token_configured_fails_closed(tmp_path):
    app = create_app(store=SqliteStore(tmp_path / "memory.db"), token="")
    c = TestClient(app)
    assert c.get("/stats", headers=_auth()).status_code == 503


def test_add_then_read_round_trips_over_http(client):
    mid = client.post(
        "/memories",
        json={"content": "over the wire", "tags": [{"name": "net"}], "agent": "tester"},
        headers=_auth(),
    ).json()["id"]
    got = client.get(f"/memories/{mid}", headers=_auth()).json()
    assert got["content"] == "over the wire"
    assert got["tags"] == ["net"]


def test_search_route_not_shadowed_by_id_route(client):
    # /memories/search must win over /memories/{mid:int}; a search must not 422.
    client.post("/memories", json={"content": "alpha beta", "agent": "t"}, headers=_auth())
    resp = client.get("/memories/search", params={"q": "beta"}, headers=_auth())
    assert resp.status_code == 200
    assert len(resp.json()) == 1


# ── boots via `python -m agent_memory.server` ───────────────────────────────
def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_runs_via_python_m_server(tmp_path):
    pytest.importorskip("uvicorn")
    httpx = pytest.importorskip("httpx")

    port = _free_port()
    env = {
        **os.environ,
        "AGENT_MEMORY_API_TOKEN": TOKEN,
        "AGENT_MEMORY_DB": str(tmp_path / "memory.db"),
        "AGENT_MEMORY_HOST": "127.0.0.1",
        "AGENT_MEMORY_PORT": str(port),
        "PYTHONPATH": str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent_memory.server"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                pytest.fail(f"server exited early:\n{proc.stdout.read()}")
            try:
                if httpx.get(f"{base}/health", timeout=0.5).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.15)
        else:
            pytest.fail("server did not become ready in time")

        assert httpx.get(f"{base}/stats", headers=_auth()).status_code == 200
        assert httpx.get(f"{base}/stats", headers={"Authorization": "Bearer nope"}).status_code == 401
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_python_m_server_without_token_exits_nonzero(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "AGENT_MEMORY_API_TOKEN"}
    env["PYTHONPATH"] = str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "agent_memory.server"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 1
    assert "AGENT_MEMORY_API_TOKEN" in proc.stderr
