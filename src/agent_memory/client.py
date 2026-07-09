"""Client-side API wrapper — the one thing CLI and MCP share.

Turns memory operations into HTTP calls against the FastAPI service. Stdlib
`urllib` only (no third-party import): a CLI runs once per invocation, so a fast
cold start matters more than a fancy HTTP library. Endpoint + token resolve via
config (AGENT_MEMORY_API / api_url, AGENT_MEMORY_API_TOKEN / api_token).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .config import resolve_api_token, resolve_api_url


class ApiUnreachable(RuntimeError):
    """The memory API could not be reached. Carries a ready-to-print, actionable
    message; the CLI renders it as a clean error instead of a traceback."""


class ApiClient:
    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: int = 30):
        self.base_url = (base_url or resolve_api_url()).rstrip("/")
        self.token = token if token is not None else resolve_api_token()
        self.timeout = timeout

    # ---- HTTP plumbing ---------------------------------------------------
    def _call(self, method, path, *, params=None, body=None):
        """Returns (status, parsed_json | None). 404 is handed back to the caller;
        an unreachable server raises ApiUnreachable; other HTTP errors raise."""
        url = self.base_url + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean, doseq=True)
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return 404, None
            detail = e.read().decode(errors="replace")
            raise RuntimeError(f"{method} {path} → HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise ApiUnreachable(
                f"cannot reach the memory API at {self.base_url} ({e.reason}).\n"
                f"Start it (`python -m agent_memory.server`) or point AGENT_MEMORY_API "
                f"/ `memory config set api_url …` at a running server."
            ) from e

    # ---- operations ------------------------------------------------------
    def add(self, content, agent, project, tags, mtype):
        _, data = self._call("POST", "/memories", body={
            "content": content, "agent": agent, "project": project,
            "tags": list(tags), "type": mtype,
        })
        return data["id"]

    def query(self, *, since_days=None, since=None, until=None, project=None,
              agent=None, tag=None, mtype=None, limit=None):
        _, data = self._call("GET", "/memories", params={
            "since_days": since_days, "since": since, "until": until,
            "project": project, "agent": agent, "tag": tag, "type": mtype, "limit": limit,
        })
        return data or []

    def search(self, text, *, project=None, agent=None, since=None, tag=None, limit=None):
        _, data = self._call("GET", "/memories/search", params={
            "q": text, "project": project, "agent": agent,
            "since": since, "tag": tag, "limit": limit,
        })
        return data or []

    def get(self, mid):
        status, data = self._call("GET", f"/memories/{mid}")
        return None if status == 404 else data

    def update(self, mid, *, content=None, project=None, mtype=None,
               set_tags=None, add_tags=None, remove_tags=None):
        body = {}
        if content is not None:
            body["content"] = content
        if project is not None:
            body["project"] = project
        if mtype is not None:
            body["type"] = mtype
        if set_tags is not None:
            body["set_tags"] = set_tags
        if add_tags is not None:
            body["add_tags"] = add_tags
        if remove_tags is not None:
            body["remove_tags"] = remove_tags
        status, data = self._call("PATCH", f"/memories/{mid}", body=body)
        return None if status == 404 else data["changes"]

    def get_many(self, ids):
        _, data = self._call("GET", "/memories/bulk", params={"ids": list(ids)})
        return data or []

    def delete(self, ids):
        _, data = self._call("DELETE", "/memories", params={"ids": list(ids)})
        return data or {"deleted": 0, "missing": list(ids)}

    def list_tags(self):
        _, data = self._call("GET", "/tags")
        return data or []

    def list_projects(self):
        _, data = self._call("GET", "/projects")
        return data or []

    def stats(self):
        _, data = self._call("GET", "/stats")
        return data


def get_client():
    """The resolved API client for CLI/MCP."""
    return ApiClient()
