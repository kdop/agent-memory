"""Shared fixtures for the cross-surface test suite.

`driver` is parametrized over every wired-up surface; today only `cli` exists,
`api`/`mcp` join the dict as Tickets B/D land — and every behavioral test then
runs against them for free.
"""

import pytest

from drivers import CliDriver

# Surface name -> factory(db_path). Add "api"/"mcp" here in later tickets.
DRIVER_FACTORIES = {
    "cli": CliDriver,
}


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
