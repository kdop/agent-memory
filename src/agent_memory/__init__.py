"""agent_memory — a shared, Postgres-backed persistent memory service for AI agents.

The CLI and MCP surfaces are thin HTTP clients (``ApiClient``) over the FastAPI
service (``agent_memory.server``); only the server touches the database, via
SQLAlchemy 2.0 async. The ``memory-cli`` command + ``memory`` alias are preserved
by a thin shim at the repo root onto ``agent_memory.cli:main``.
"""

__version__ = "0.2.0"
