"""Have a language model read each new memory and say what it thinks.

The model sees the five rules from the memory skill, the new memory, and the
five memories closest to it in the same project. It answers approve, reject
(with the rule the entry breaks) or rewrite (with better text). The verdict is
stored next to the memory and shown with it. It is advice only: nothing is
refused or changed because of it, and the write never waits for it.

Settings, read once when `make_reviewer()` runs at server start:

    AGENT_MEMORY_REVIEW          `off` (default) or `warn`
    AGENT_MEMORY_REVIEW_URL      an Ollama server, for example http://host:11434
    AGENT_MEMORY_REVIEW_MODEL    the model to ask (default qwen3:14b)
    AGENT_MEMORY_REVIEW_TIMEOUT  seconds to wait for an answer (default 30)

The Ollama call uses urllib from the standard library, so the server needs no
new package. The caller runs it in a thread (`asyncio.to_thread`) so the event
loop stays free while the model thinks.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass

log = logging.getLogger(__name__)

DEFAULT_MODEL = "qwen3:14b"
DEFAULT_TIMEOUT = 30.0
NEIGHBOUR_COUNT = 5

# The rules the model checks against. Copied word for word from the numbered
# rules in skills/memory/SKILL.md; the server never reads that file. The
# numbers are permanent ids, so a deleted rule's number is never reused.
RULES: tuple[tuple[int, str], ...] = (
    (1, "Log only what will still matter in later sessions: durable decisions, lessons, "
        "preferences and facts."),
    (2, "Do not log diary-style entries of what was done."),
    (3, "Every entry carries the why, not just the what: the reasoning behind a decision, "
        "the alternatives rejected, the cause of a lesson."),
    (4, "Never write down anything that can be retrieved from git history."),
    (5, "Project facts and data go in the project's own folder, tracked by git, not in the "
        "memory DB. Write them the way that project's user has instructed. With no "
        "instruction, write them at the project's top level in a folder named `notes/`, "
        "one markdown file per ISO week named `<year>_<week>.md` (for example `2026_39.md`)."),
)

VERDICTS = ("approve", "reject", "rewrite")


@dataclass(frozen=True)
class Verdict:
    """What the model said about one memory.

    `verdict` is one of `VERDICTS`. `rule` is the number of the rule the entry
    breaks or falls short of (None for an approve). `reason` is one sentence.
    `rewrite` is the suggested text when the verdict is `rewrite`, else None.
    `duplicate_of` is the id of the listed neighbour this entry repeats, when
    the verdict is a reject for that reason, else None."""

    verdict: str
    rule: int | None
    reason: str
    rewrite: str | None
    duplicate_of: int | None

    def as_dict(self) -> dict:
        return asdict(self)


class Reviewer:
    """What the server needs from a review backend.

    `model_name` names the model (None when there is none). `review` takes the
    new memory and its nearest neighbours as dicts with at least `id`,
    `project`, `type`, `tags` and `content`, and returns a `Verdict`, or None
    when the model gave no usable answer. It may block: call it in a thread."""

    model_name: str | None

    def review(self, memory: dict, neighbours: list[dict]) -> Verdict | None:
        raise NotImplementedError


class NullReviewer(Reviewer):
    """The reviewer the server runs with when review is off. It is never
    called; `reason` says why there is no model."""

    model_name = None

    def __init__(self, reason: str = "review is off"):
        self.reason = reason

    def review(self, memory: dict, neighbours: list[dict]) -> Verdict | None:
        return None


class OllamaReviewer(Reviewer):
    """Asks a model on an Ollama server through `/api/chat`.

    The request turns thinking off and asks for JSON, at temperature 0 so the
    same entry gets the same verdict. A server that cannot be reached, a slow
    answer, or an answer that is not the expected JSON all give None and one
    log line; the caller stores nothing in that case."""

    def __init__(self, url: str, model: str = DEFAULT_MODEL, timeout: float = DEFAULT_TIMEOUT):
        self.url = url.rstrip("/")
        self.model_name = model
        self.timeout = timeout

    def review(self, memory: dict, neighbours: list[dict]) -> Verdict | None:
        body = self.request_body(memory, neighbours)
        try:
            text = self._chat(body)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log.warning("review of memory #%s failed: %s at %s: %s",
                        memory.get("id"), self.model_name, self.url, _cause(e))
            return None
        verdict = parse_verdict(text, [n["id"] for n in neighbours])
        if verdict is None:
            log.warning("review of memory #%s failed: %s gave no usable JSON: %.200s",
                        memory.get("id"), self.model_name, text)
        return verdict

    def request_body(self, memory: dict, neighbours: list[dict]) -> dict:
        """The JSON sent to `/api/chat`. Separate from the call so a test can
        check it without a server."""
        return {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt(memory, neighbours)},
            ],
            "stream": False,
            "format": "json",
            "think": False,
            "options": {"num_ctx": 8192, "temperature": 0},
        }

    def _chat(self, body: dict) -> str:
        """POST the body and return the text of the model's message."""
        req = urllib.request.Request(
            self.url + "/api/chat",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("message", {}).get("content", "")


def _cause(e: Exception) -> str:
    """One readable line for a urllib error, which often wraps the real one."""
    reason = getattr(e, "reason", None)
    return f"{type(e).__name__}: {reason if reason is not None else e}"


# ---- the prompt ------------------------------------------------------------
SYSTEM_PROMPT = (
    "You review entries that AI agents write to a shared memory. Memory is for what "
    "will still matter in a later session. Judge each new entry against these rules:\n"
    + "\n".join(f"{n}. {text}" for n, text in RULES)
    + "\n\n"
    "You get one new entry and up to five existing entries from the same project. "
    "Answer with one JSON object and nothing else, with exactly these five keys:\n"
    '{"verdict": "approve" | "reject" | "rewrite", "rule": <number or null>, '
    '"reason": "<one sentence>", "rewrite": "<text>" or null, "duplicate_of": <id or null>}\n'
    "\n"
    "- approve: the entry follows the rules. rule, rewrite and duplicate_of are null.\n"
    "- reject: the entry breaks a rule, or says the same as an existing entry in other "
    "words. Set rule to the number of the rule it breaks. For a repeat of an existing "
    "entry set duplicate_of to that entry's id and rule to null. rewrite is null.\n"
    "- rewrite: the entry is worth keeping but would follow the rules better in other "
    "words, for example a decision that has a why buried in it. Put the new text in "
    "rewrite and the rule it falls short of in rule. duplicate_of is null.\n"
    "- An entry that reverses or replaces what an existing entry says is not a repeat: "
    "approve it.\n"
    "- A short entry is fine when it follows the rules. Do not reject for length.\n"
    "- reason is one plain sentence a person can act on."
)


def _entry(m: dict) -> str:
    tags = ", ".join(m.get("tags") or []) or "none"
    return (f"id: {m.get('id')}\nproject: {m.get('project') or 'none'}\n"
            f"type: {m.get('type') or 'none'}\ntags: {tags}\ncontent: {m.get('content', '')}")


def user_prompt(memory: dict, neighbours: list[dict]) -> str:
    """The new memory and its neighbours, laid out for the model."""
    parts = ["New entry:\n" + _entry(memory)]
    if neighbours:
        listed = "\n\n".join(_entry(n) for n in neighbours)
        parts.append(f"Existing entries in the same project, closest in meaning first:\n{listed}")
    else:
        parts.append("There are no existing entries to compare with.")
    return "\n\n".join(parts)


# ---- the answer ------------------------------------------------------------
def parse_verdict(text: str, neighbour_ids: list[int] | None = None) -> Verdict | None:
    """Turn the model's answer into a `Verdict`, or None when it is not the
    expected shape.

    Bad JSON, a missing key, a verdict outside `VERDICTS`, a rule number that
    is not one of the rules, or a `duplicate_of` that names an entry the model
    was not shown all count as no answer. The caller stores nothing then."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    keys = ("verdict", "rule", "reason", "rewrite", "duplicate_of")
    if any(k not in data for k in keys):
        return None
    verdict, rule, reason = data["verdict"], data["rule"], data["reason"]
    rewrite, duplicate_of = data["rewrite"], data["duplicate_of"]
    if verdict not in VERDICTS or not isinstance(reason, str) or not reason.strip():
        return None
    if rule is not None and (not _is_int(rule) or rule not in {n for n, _ in RULES}):
        return None
    if rewrite is not None and not isinstance(rewrite, str):
        return None
    if duplicate_of is not None:
        if not _is_int(duplicate_of):
            return None
        if neighbour_ids is not None and duplicate_of not in neighbour_ids:
            return None
    return Verdict(verdict=verdict, rule=rule, reason=reason.strip(),
                   rewrite=rewrite, duplicate_of=duplicate_of)


def _is_int(v) -> bool:
    # bool is a subclass of int; `true` is not a rule number.
    return isinstance(v, int) and not isinstance(v, bool)


# ---- setup -----------------------------------------------------------------
def make_reviewer() -> Reviewer:
    """Build the reviewer from the environment. Returns a `NullReviewer`, and
    logs one line saying why, when review is off, the setting is not a known
    value, or no server address is given."""
    mode = os.environ.get("AGENT_MEMORY_REVIEW", "off").strip().lower()
    if mode == "off":
        log.info("review is off (AGENT_MEMORY_REVIEW=off)")
        return NullReviewer("review is off (AGENT_MEMORY_REVIEW=off)")
    if mode != "warn":
        reason = f"review is off: AGENT_MEMORY_REVIEW={mode!r} is not 'off' or 'warn'"
        log.warning(reason)
        return NullReviewer(reason)
    url = os.environ.get("AGENT_MEMORY_REVIEW_URL", "").strip()
    if not url:
        reason = "review is off: AGENT_MEMORY_REVIEW=warn but AGENT_MEMORY_REVIEW_URL is not set"
        log.warning(reason)
        return NullReviewer(reason)
    model = os.environ.get("AGENT_MEMORY_REVIEW_MODEL", "").strip() or DEFAULT_MODEL
    raw_timeout = os.environ.get("AGENT_MEMORY_REVIEW_TIMEOUT", "").strip()
    try:
        timeout = float(raw_timeout) if raw_timeout else DEFAULT_TIMEOUT
    except ValueError:
        log.warning("AGENT_MEMORY_REVIEW_TIMEOUT=%r is not a number; using %s",
                    raw_timeout, DEFAULT_TIMEOUT)
        timeout = DEFAULT_TIMEOUT
    log.info("review is on: %s at %s (timeout %ss)", model, url, timeout)
    return OllamaReviewer(url, model, timeout)
