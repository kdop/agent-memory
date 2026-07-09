"""Unit tests for the pure config/endpoint/DSN resolution helpers (no server/DB)."""

import pytest

from agent_memory import config
from agent_memory.server.db import resolve_async_dsn


# ── DSN normalization ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("given, expected", [
    ("postgresql://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db"),
    ("postgres://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
    ("postgresql+asyncpg://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
])
def test_resolve_async_dsn_normalizes(given, expected):
    assert resolve_async_dsn(given) == expected


def test_resolve_async_dsn_rejects_non_postgres():
    with pytest.raises(RuntimeError):
        resolve_async_dsn("sqlite:///x.db")
    with pytest.raises(RuntimeError):
        resolve_async_dsn(None)


# ── client endpoint resolution ────────────────────────────────────────────────
def test_api_url_env_wins(monkeypatch):
    monkeypatch.setenv("AGENT_MEMORY_API", "http://env:9000")
    monkeypatch.setattr(config, "load_config", lambda: {"api_url": "http://cfg:1"})
    assert config.resolve_api_url() == "http://env:9000"


def test_api_url_config_then_default(monkeypatch):
    monkeypatch.delenv("AGENT_MEMORY_API", raising=False)
    monkeypatch.setattr(config, "load_config", lambda: {"api_url": "http://cfg:1"})
    assert config.resolve_api_url() == "http://cfg:1"
    monkeypatch.setattr(config, "load_config", lambda: {})
    assert config.resolve_api_url() == config.DEFAULT_API_URL


def test_api_token_resolution(monkeypatch):
    monkeypatch.setenv("AGENT_MEMORY_API_TOKEN", "envtok")
    monkeypatch.setattr(config, "load_config", lambda: {"api_token": "cfgtok"})
    assert config.resolve_api_token() == "envtok"
    monkeypatch.delenv("AGENT_MEMORY_API_TOKEN", raising=False)
    assert config.resolve_api_token() == "cfgtok"
    monkeypatch.setattr(config, "load_config", lambda: {})
    assert config.resolve_api_token() is None


def test_server_bind_defaults_and_override(monkeypatch):
    monkeypatch.delenv("AGENT_MEMORY_HOST", raising=False)
    monkeypatch.delenv("AGENT_MEMORY_PORT", raising=False)
    monkeypatch.setattr(config, "load_config", lambda: {})
    assert config.resolve_server_bind() == (config.DEFAULT_API_HOST, config.DEFAULT_API_PORT)
    monkeypatch.setenv("AGENT_MEMORY_HOST", "0.0.0.0")
    monkeypatch.setenv("AGENT_MEMORY_PORT", "9999")
    assert config.resolve_server_bind() == ("0.0.0.0", 9999)


def test_get_agent_name(monkeypatch):
    monkeypatch.setenv("AGENT_NAME", "agent-a")
    assert config.get_agent_name() == "agent-a"
    monkeypatch.delenv("AGENT_NAME", raising=False)
    assert config.get_agent_name() == "unknown"
