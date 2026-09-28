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

# The values a memory's `review_status` can take, as the server defines them
# (server/review.py, STATUSES): the client half cannot import the server, so
# the list is copied here and a test checks the two match.
REVIEW_STATUSES = ("unverified", "verified", "flagged")


class ApiUnreachable(RuntimeError):
    """The memory API could not be reached. Carries a ready-to-print, actionable
    message; the CLI renders it as a clean error instead of a traceback."""


class DuplicateMemory(RuntimeError):
    """The server refused an add (HTTP 409) because a memory this close already
    exists in the same project. `existing_id` says which one; `score` is the
    cosine between the two. Pass `force=True` to store it anyway."""

    def __init__(self, existing_id: int, score: float):
        self.existing_id = existing_id
        self.score = score
        super().__init__(f"duplicate of memory #{existing_id} (score {score:.2f})")


class ReviewRefused(RuntimeError):
    """The server refused an add (HTTP 422) because its review model said
    the entry breaks the rules: `verdict` is "reject" or "rewrite", `rule`
    the number of the rule it breaks (None for a repeat, then `duplicate_of`
    names the memory it repeats), `explanation` the model's one sentence.
    For a rewrite, `rewrite` is the suggested text and `tags` the suggested
    tag names (None and [] for a reject). Nothing was stored. Pass
    `force=True` to store the entry as written."""

    def __init__(self, verdict: str, rule: int | None, explanation: str,
                 rewrite: str | None = None, tags: list[str] | None = None,
                 duplicate_of: int | None = None):
        self.verdict = verdict
        self.rule = rule
        self.explanation = explanation
        self.rewrite = rewrite
        self.tags = list(tags or [])
        self.duplicate_of = duplicate_of
        super().__init__(f"review: {verdict}: {explanation}")


class ApiRefused(RuntimeError):
    """The server turned the request down and said why (an HTTP 4xx, or a 503
    for a model the server does not have, with a plain `detail` string, such
    as a search mode it cannot serve). `status` is the HTTP code; `str(e)` is
    the server's message, ready to print."""

    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(detail)


def _detail_from(body: str) -> str | None:
    """The plain-string `detail` of an error body, or None when the body is
    not the shape `{"detail": "<message>"}`."""
    try:
        d = json.loads(body).get("detail")
    except (ValueError, AttributeError):
        return None
    return d if isinstance(d, str) else None


def _duplicate_from(detail: str) -> DuplicateMemory | None:
    """Build a DuplicateMemory from a 409 body, or None when the body is not
    the duplicate shape `{"detail": {"reason": "duplicate", ...}}`."""
    try:
        d = json.loads(detail).get("detail")
    except (ValueError, AttributeError):
        return None
    if not isinstance(d, dict) or d.get("reason") != "duplicate":
        return None
    return DuplicateMemory(int(d["existing_id"]), float(d["score"]))


def _review_refusal_from(detail: str) -> ReviewRefused | None:
    """Build a ReviewRefused from a 422 body, or None when the body is not
    the review shape `{"detail": {"reason": "review", ...}}`."""
    try:
        d = json.loads(detail).get("detail")
    except (ValueError, AttributeError):
        return None
    if not isinstance(d, dict) or d.get("reason") != "review":
        return None
    return ReviewRefused(str(d["verdict"]), d.get("rule"), str(d.get("explanation") or ""),
                         d.get("rewrite"), d.get("tags"), d.get("duplicate_of"))


class ApiClient:
    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: int = 30):
        self.base_url = (base_url or resolve_api_url()).rstrip("/")
        self.token = token if token is not None else resolve_api_token()
        self.timeout = timeout

    # ---- HTTP plumbing ---------------------------------------------------
    def _call(self, method, path, *, params=None, body=None):
        """Returns (status, parsed_json | None). 404 is handed back to the caller;
        an unreachable server raises ApiUnreachable; other HTTP errors raise."""
        status, _, data = self._request(method, path, params=params, body=body)
        return status, data

    def _request(self, method, path, *, params=None, body=None):
        """Like _call but also returns the response headers: (status, headers, data)."""
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
                return resp.status, resp.headers, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return 404, e.headers, None
            detail = e.read().decode(errors="replace")
            if e.code == 409:
                dup = _duplicate_from(detail)
                if dup is not None:
                    raise dup from e
            if e.code == 422:
                refusal = _review_refusal_from(detail)
                if refusal is not None:
                    raise refusal from e
            if 400 <= e.code < 500 or e.code == 503:
                message = _detail_from(detail)
                if message is not None:
                    raise ApiRefused(e.code, message) from e
            raise RuntimeError(f"{method} {path} → HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise ApiUnreachable(
                f"cannot reach the memory API at {self.base_url} ({e.reason}).\n"
                f"Start it (`python -m agent_memory.server`) or point AGENT_MEMORY_API "
                f"/ `memory config set api_url …` at a running server."
            ) from e

    # ---- operations ------------------------------------------------------
    def add(self, content, agent, project, tags, mtype, force=False):
        """The new id only."""
        mid, _ = self.add_with_warnings(content, agent, project, tags, mtype, force=force)
        return mid

    def add_with_warnings(self, content, agent, project, tags, mtype, force=False):
        """(id, warnings): warnings is the list of rule names the entry breaks
        (see server/checks.py). The memory is stored either way, unless the
        server finds a near-duplicate in the same project (then it raises
        `DuplicateMemory`) or its review model, in enforce mode, rejects the
        entry or wants it rewritten (then `ReviewRefused`); in both cases
        nothing is stored. `force=True` skips both checks."""
        body = {
            "content": content, "agent": agent, "project": project,
            "tags": list(tags), "type": mtype,
        }
        params = {"force": "true"} if force else None
        _, data = self._call("POST", "/memories", params=params, body=body)
        return data["id"], data.get("warnings") or []

    def query(self, *, since_days=None, since=None, until=None, project=None,
              agent=None, tag=None, mtype=None, status=None, limit=None):
        """Rows only. `status` keeps to one review status ("unverified",
        "verified" or "flagged"; None for all). `limit=0` returns everything;
        None uses the server default."""
        rows, _ = self.query_with_total(
            since_days=since_days, since=since, until=until, project=project,
            agent=agent, tag=tag, mtype=mtype, status=status, limit=limit)
        return rows

    def query_with_total(self, *, since_days=None, since=None, until=None, project=None,
                         agent=None, tag=None, mtype=None, status=None, limit=None):
        """(rows, total): total is the match count ignoring the limit."""
        _, headers, data = self._request("GET", "/memories", params={
            "since_days": since_days, "since": since, "until": until,
            "project": project, "agent": agent, "tag": tag, "type": mtype,
            "status": status, "limit": limit,
        })
        rows = data or []
        total = int((headers or {}).get("X-Total-Count") or len(rows))
        return rows, total

    def search(self, text, *, project=None, agent=None, since=None, tag=None, limit=None,
               mode=None):
        """Rows with a `snippet` and a `score`. `mode` is "keyword", "semantic"
        or "hybrid"; None leaves it out, so the server picks its default
        (keyword). A mode the server cannot serve raises `ApiRefused`."""
        _, data = self._call("GET", "/memories/search", params={
            "q": text, "project": project, "agent": agent,
            "since": since, "tag": tag, "limit": limit, "mode": mode,
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

    def reindex(self):
        """Ask the server to give every memory a vector from its current model.
        Returns the number of rows updated."""
        _, data = self._call("POST", "/admin/reindex")
        return data["updated"]

    def flagged(self, project=None, verdict=None, status=None, limit=None):
        """The memories the review flagged, newest review first, each with its
        `review`. `verdict` is "reject" or "rewrite"; None lists both. With
        `status` ("unverified", "verified" or "flagged") the memories with
        that review status instead. `limit=0` returns everything; None uses
        the server default."""
        rows, _ = self.flagged_with_total(project=project, verdict=verdict, status=status,
                                          limit=limit)
        return rows

    def flagged_with_total(self, project=None, verdict=None, status=None, limit=None):
        """(rows, total): total is the match count ignoring the limit."""
        _, headers, data = self._request("GET", "/memories/flagged", params={
            "project": project, "verdict": verdict, "status": status, "limit": limit,
        })
        rows = data or []
        total = int((headers or {}).get("X-Total-Count") or len(rows))
        return rows, total

    def review_catch_up(self, limit=None):
        """The catch-up: ask the server to review the unverified memories,
        oldest first, one after another, up to `limit` (None uses the server
        default, 0 means all). The reviews run in the background; returns
        how many were scheduled. A server without a review model raises
        `ApiRefused`."""
        _, data = self._call("POST", "/admin/review", params={"limit": limit})
        return data["scheduled"]

    # The old name, kept for callers that still use it.
    review_missing = review_catch_up


def get_client():
    """The resolved API client for CLI/MCP."""
    return ApiClient()
