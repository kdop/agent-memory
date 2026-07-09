"""`python -m agent_memory.server` — run the API with uvicorn.

Token: AGENT_MEMORY_API_TOKEN → config `api_token` (required; the server fails
closed without one). Bind host/port resolve AGENT_MEMORY_HOST/PORT → config
`server_host`/`server_port` → local default (127.0.0.1:8099, matching the client
default), so `python -m agent_memory.server` needs no flags. The DB is
AGENT_MEMORY_DB (a postgresql:// DSN) — back it up before pointing at real data.
"""

import sys

from ..config import resolve_api_token, resolve_server_bind


def main():
    token = resolve_api_token()
    if not token:
        print(
            "Refusing to start: set AGENT_MEMORY_API_TOKEN (or `memory-cli config set "
            "api_token …`) to a bearer token first.",
            file=sys.stderr,
        )
        sys.exit(1)

    import uvicorn

    from .app import create_app

    host, port = resolve_server_bind()
    uvicorn.run(create_app(token=token), host=host, port=port)


if __name__ == "__main__":
    main()
