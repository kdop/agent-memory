"""agent_memory — shared, SQLite-backed persistent memory for AI agents.

Extracted from the former single-file `memory-cli` script (Ticket A) so the same
storage/config layer can back the CLI, a FastAPI service (Ticket B), and an MCP
server (Ticket D). The `memory-cli` command + `memory` alias are preserved; the
file at the repo root is now a thin shim onto `agent_memory.cli:main`.
"""

__version__ = "0.1.0"
