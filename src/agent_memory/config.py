"""Configuration, DB-path resolution, and agent-name detection.

Persistent settings live in a JSON file under the XDG config dir. The DB path
resolves highest-priority-first:

    AGENT_MEMORY_DB env  →  stored db_path setting  →  XDG data-dir default

The same config file will later hold the cloud backend selection (api_url/token, #6).
"""

import json
import os
from pathlib import Path


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


def default_db_path():
    return _xdg("XDG_DATA_HOME", ".local/share") / "agent-memory" / "memory.db"


def resolve_db_path():
    env = os.environ.get("AGENT_MEMORY_DB")
    if env:
        return Path(env)
    stored = load_config().get("db_path")
    if stored:
        return Path(stored)
    return default_db_path()


def get_agent_name():
    """Detect agent name from environment or default"""
    return os.environ.get("AGENT_NAME", "unknown")
