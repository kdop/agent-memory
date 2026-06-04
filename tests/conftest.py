"""Shared fixtures for the cross-surface test suite.

`driver` is parametrized over every wired-up surface; today only `cli` exists,
`api`/`mcp` join the dict as Tickets B/D land — and every behavioral test then
runs against them for free.
"""

import importlib.util

import pytest

from drivers import ApiDriver, CliDriver

# Surface name -> factory(db_path). Add "mcp" here in Ticket D.
DRIVER_FACTORIES = {
    "cli": CliDriver,
}

# The API surface only joins the parametrization when FastAPI is installed (the
# [server] extra). A cli-only checkout still runs the full cli suite.
if importlib.util.find_spec("fastapi") is not None:
    DRIVER_FACTORIES["api"] = ApiDriver


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
