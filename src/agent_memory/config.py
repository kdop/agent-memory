"""Configuration, endpoint resolution, and agent-name detection.

Persistent settings live in a JSON file under the XDG config dir.

Client endpoint (CLI + MCP talk to the API, never a DB directly):

    AGENT_MEMORY_API env        →  stored `api_url`    →  local default (127.0.0.1:PORT)
    AGENT_MEMORY_API_TOKEN env  →  stored `api_token`

Server DB target (only the API server reads this):

    AGENT_MEMORY_DB env         →  a postgresql:// DSN (Postgres-only)
"""

import json
import os
from pathlib import Path


def load_dotenv():
    """Load KEY=VALUE pairs from a `.env` file into the environment, stdlib-only
    (no third-party dependency — keeps the client surface dependency-free, rule
    #3). Never overrides an already-set variable, so a real exported env var or a
    CI secret always wins. Searches the current directory and each parent up to
    the filesystem root (like `.git` discovery), so it works whether a command is
    run from the repo root or a subdirectory. Idempotent — safe to call more than
    once (e.g. from both this module and db.py, which doesn't otherwise import
    config.py but needs the same behavior for `alembic`)."""
    here = Path.cwd()
    for d in (here, *here.parents):
        candidate = d / ".env"
        if candidate.is_file():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    os.environ.setdefault(key, value)
            break


load_dotenv()

# The local API the CLI/MCP talk to by default when nothing else is configured.
DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8099
DEFAULT_API_URL = f"http://{DEFAULT_API_HOST}:{DEFAULT_API_PORT}"


def _xdg(env_var, home_subpath):
    base = os.environ.get(env_var)
    return Path(base) if base else Path.home() / home_subpath


def config_path():
    return _xdg("XDG_CONFIG_HOME", ".config") / "agent-memory" / "config.json"


def load_config():
    try:
        return json.loads(config_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_config(cfg):
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2) + "\n")


def resolve_api_url():
    """The API endpoint the client (CLI/MCP) talks to. Always resolves to a URL —
    a local server is the default, so clients never fall back to a direct DB."""
    return os.environ.get("AGENT_MEMORY_API") or load_config().get("api_url") or DEFAULT_API_URL


def resolve_api_token():
    """Bearer token for the API, if configured (None when the server is open)."""
    return os.environ.get("AGENT_MEMORY_API_TOKEN") or load_config().get("api_token")


def resolve_server_bind():
    """(host, port) the API server binds to. Env overrides config overrides defaults."""
    cfg = load_config()
    host = os.environ.get("AGENT_MEMORY_HOST") or cfg.get("server_host") or DEFAULT_API_HOST
    port = os.environ.get("AGENT_MEMORY_PORT") or cfg.get("server_port") or DEFAULT_API_PORT
    return host, int(port)


def get_agent_name():
    """Detect agent name from environment or default."""
    return os.environ.get("AGENT_NAME", "unknown")
