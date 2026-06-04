"""Shared fixtures for the cross-surface test suite.

`driver` is parametrized over every wired-up surface (cli, api, mcp), so every
behavioral test runs against each of them for free — the anti-drift guard.
"""

import importlib.util
import os

import pytest

from drivers import ApiDriver, CliDriver, McpDriver, PgDriver

# Surface name -> factory(db_path).
DRIVER_FACTORIES = {
    "cli": CliDriver,
}

# Each extra surface joins the parametrization only when its deps are installed,
# so a cli-only checkout still runs the full cli suite.
if importlib.util.find_spec("fastapi") is not None:
    DRIVER_FACTORIES["api"] = ApiDriver
if importlib.util.find_spec("mcp") is not None:
    DRIVER_FACTORIES["mcp"] = McpDriver
# Postgres joins only when a test instance is configured (CI service / local podman);
# proves PostgresStore satisfies the same behavior contract as SQLite.
if importlib.util.find_spec("psycopg") is not None and os.environ.get("AGENT_MEMORY_TEST_PG_DSN"):
    DRIVER_FACTORIES["pg"] = PgDriver


@pytest.fixture(params=list(DRIVER_FACTORIES))
def driver(request, tmp_path):
    """A primed, ready-to-use driver against a scratch DB (one per surface)."""
    drv = DRIVER_FACTORIES[request.param](tmp_path / "memory.db")
    drv.initialize()
    return drv


@pytest.fixture
def cli(tmp_path):
    """A raw, un-primed CliDriver for surface-specific (CLI-only) assertions."""
    return CliDriver(tmp_path / "memory.db")
