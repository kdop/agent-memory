"""`python -m agent_memory.server` — run the API with uvicorn.

Token comes from AGENT_MEMORY_API_TOKEN (required; the server fails closed
without one). Host/port via AGENT_MEMORY_HOST / AGENT_MEMORY_PORT. The DB is the
usual resolved store (AGENT_MEMORY_DB / config / XDG default) — back it up before
pointing the server at the live DB for real (rule #1).
"""

import os
import sys


def main():
    token = os.environ.get("AGENT_MEMORY_API_TOKEN")
    if not token:
        print(
            "Refusing to start: set AGENT_MEMORY_API_TOKEN to a bearer token first.",
            file=sys.stderr,
        )
        sys.exit(1)

    import uvicorn

    from .app import create_app

    host = os.environ.get("AGENT_MEMORY_HOST", "127.0.0.1")
    port = int(os.environ.get("AGENT_MEMORY_PORT", "8000"))
    uvicorn.run(create_app(token=token), host=host, port=port)


if __name__ == "__main__":
    main()
