"""Have a language model read each new memory and say what it thinks.

The model sees the five rules from the memory skill, the new memory, the
five memories closest to it in the same project, and the ten existing tags
closest to it in meaning. It answers approve, reject (with the rule the entry
breaks) or rewrite (with better text and the tags from that list that fit it).
The verdict is stored next to the memory and shown with it.

In `warn` mode it is advice only: nothing is refused or changed because of
it, and the write never waits for it. In `enforce` mode the model reads the
entry before it is stored, and a reject or rewrite verdict refuses the write
(HTTP 422 with the verdict and the suggestion); `force=true` stores it anyway.
In both modes a model that does not answer never blocks a write: the memory
is stored and one log line says the review did not run.

Settings, read once when `make_reviewer()` runs at server start:

    AGENT_MEMORY_REVIEW          `off` (default), `warn` or `enforce`
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
from dataclasses import asdict, dataclass, field

log = logging.getLogger(__name__)

DEFAULT_MODEL = "qwen3:14b"
DEFAULT_TIMEOUT = 30.0
NEIGHBOUR_COUNT = 5
TAG_COUNT = 10

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

# The values AGENT_MEMORY_REVIEW may take. `off` means no model is asked;
# `warn` stores the verdict after the write; `enforce` refuses a write the
# model rejects or wants rewritten.
MODES = ("off", "warn", "enforce")


@dataclass(frozen=True)
class Verdict:
    """What the model said about one memory.

    `verdict` is one of `VERDICTS`. `rule` is the number of the rule the entry
    breaks or falls short of (None for an approve). `reason` is one sentence.
    `rewrite` is the suggested text when the verdict is `rewrite`, else None.
    `duplicate_of` is the id of the listed neighbour this entry repeats, when
    the verdict is a reject for that reason, else None. `tags` are the tags
    the model suggests for the rewritten text, chosen from the ones it was
    offered; empty unless the verdict is `rewrite`."""

    verdict: str
    rule: int | None
    reason: str
    rewrite: str | None
    duplicate_of: int | None
    tags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


class Reviewer:
    """What the server needs from a review backend.

    `model_name` names the model (None when there is none). `review` takes the
    new memory and its nearest neighbours as dicts with at least `id`,
    `project`, `type`, `tags` and `content`, and the names of the existing
    tags the model may suggest for a rewrite, and returns a `Verdict`, or None
    when the model gave no usable answer. It may block: call it in a thread."""

    model_name: str | None

    def review(self, memory: dict, neighbours: list[dict],
               tags: list[str] = ()) -> Verdict | None:
        raise NotImplementedError


class NullReviewer(Reviewer):
    """The reviewer the server runs with when review is off. It is never
    called; `reason` says why there is no model."""

    model_name = None

    def __init__(self, reason: str = "review is off"):
        self.reason = reason

    def review(self, memory: dict, neighbours: list[dict],
               tags: list[str] = ()) -> Verdict | None:
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

    def review(self, memory: dict, neighbours: list[dict],
               tags: list[str] = ()) -> Verdict | None:
        body = self.request_body(memory, neighbours, tags)
        try:
            text = self._chat(body)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log.warning("review of memory %s failed: %s at %s: %s",
                        _label(memory), self.model_name, self.url, _cause(e))
            return None
        verdict = parse_verdict(text, [n["id"] for n in neighbours], list(tags))
        if verdict is None:
            log.warning("review of memory %s failed: %s gave no usable JSON: %.200s",
                        _label(memory), self.model_name, text)
        return verdict

    def request_body(self, memory: dict, neighbours: list[dict],
                     tags: list[str] = ()) -> dict:
        """The JSON sent to `/api/chat`. Separate from the call so a test can
        check it without a server."""
        return {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt(memory, neighbours, tags)},
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


def _label(memory: dict) -> str:
    """How a log line names a memory: `#12`, or `(new)` for one that is
    reviewed before it is stored and so has no id yet."""
    mid = memory.get("id")
    return f"#{mid}" if mid is not None else "(new)"


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
    "You get one new entry, up to five existing entries from the same project, and a "
    "list of existing tag names. Answer with one JSON object and nothing else, with "
    "exactly these six keys:\n"
    '{"verdict": "approve" | "reject" | "rewrite", "rule": <number or null>, '
    '"reason": "<one sentence>", "rewrite": "<text>" or null, "duplicate_of": <id or null>, '
    '"tags": [<names from the list only>]}\n'
    "\n"
    "- approve: the entry follows the rules. rule, rewrite and duplicate_of are null; "
    "tags is [].\n"
    "- reject: the entry breaks a rule, or says the same as an existing entry in other "
    "words. Set rule to the number of the rule it breaks. For a repeat of an existing "
    "entry set duplicate_of to that entry's id and rule to null. rewrite is null; "
    "tags is [].\n"
    "- rewrite: the entry is worth keeping but would follow the rules better in other "
    "words, for example a decision that has a why buried in it. Put the new text in "
    "rewrite and the rule it falls short of in rule. duplicate_of is null. In tags put "
    "the names from the offered list that fit the new text, most fitting first, at most "
    "five; never a name that is not on the list, and [] when none fits.\n"
    "- An entry that reverses or replaces what an existing entry says is not a repeat: "
    "approve it.\n"
    "- A short entry is fine when it follows the rules. Do not reject for length.\n"
    "- reason is one plain sentence a person can act on.\n"
    "\n"
    "Check the new entry in this order, and stop at the first that applies:\n"
    "1. It tells what was done in a session (rule 2): reject, rule 2.\n"
    "2. It says the same as a listed existing entry in other words: reject, duplicate_of "
    "that id.\n"
    "3. It states what was decided, chosen, learned or will be done, but not why: no "
    "'because', no cause, no alternative that was rejected. Then it falls short of rule "
    "3: rewrite it, keeping the writer's words and adding the why when the entry or the "
    "existing entries state it; otherwise reject, rule 3.\n"
    "4. It states the what and the why, in any order: approve."
)


def _entry(m: dict) -> str:
    tags = ", ".join(m.get("tags") or []) or "none"
    mid = m.get("id")
    return (f"id: {mid if mid is not None else 'new'}\nproject: {m.get('project') or 'none'}\n"
            f"type: {m.get('type') or 'none'}\ntags: {tags}\ncontent: {m.get('content', '')}")


def user_prompt(memory: dict, neighbours: list[dict], tags: list[str] = ()) -> str:
    """The new memory, its neighbours and the tags on offer, laid out for the
    model. `tags` are the only names the model may suggest for a rewrite."""
    parts = ["New entry:\n" + _entry(memory)]
    if neighbours:
        listed = "\n\n".join(_entry(n) for n in neighbours)
        parts.append(f"Existing entries in the same project, closest in meaning first:\n{listed}")
    else:
        parts.append("There are no existing entries to compare with.")
    if tags:
        parts.append("Existing tags you may suggest for a rewrite, closest in meaning "
                     "first: " + ", ".join(tags))
    else:
        parts.append("There are no existing tags to suggest: tags must be [].")
    return "\n\n".join(parts)


# ---- the answer ------------------------------------------------------------
def parse_verdict(text: str, neighbour_ids: list[int] | None = None,
                  offered_tags: list[str] | None = None) -> Verdict | None:
    """Turn the model's answer into a `Verdict`, or None when it is not the
    expected shape.

    Bad JSON, a missing key, a verdict outside `VERDICTS`, a rule number that
    is not one of the rules, or a `duplicate_of` that names an entry the model
    was not shown all count as no answer. The caller stores nothing then.

    `tags` may be left out (then it is empty) but must be a list of strings
    when present. Only a rewrite keeps its tags, and only the names in
    `offered_tags`, matched without regard to case and returned in the offered
    spelling; the rest are dropped. With `offered_tags` None the names are
    kept as given."""
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
    raw_tags = data.get("tags") or []
    if not isinstance(raw_tags, list) or not all(isinstance(t, str) for t in raw_tags):
        return None
    tags = _keep_offered(raw_tags, offered_tags) if verdict == "rewrite" else []
    return Verdict(verdict=verdict, rule=rule, reason=reason.strip(),
                   rewrite=rewrite, duplicate_of=duplicate_of, tags=tags)


def _keep_offered(names: list[str], offered: list[str] | None) -> list[str]:
    """The names that are on the offered list, in the model's order, each once,
    spelled as offered. All of them, once each, when there is no list."""
    spelling: dict[str, str] = {}
    for name in (offered if offered is not None else [n.strip() for n in names]):
        if name:
            spelling.setdefault(name.lower(), name)
    kept: list[str] = []
    for name in names:
        match = spelling.get(name.strip().lower())
        if match is not None and match not in kept:
            kept.append(match)
    return kept


def _is_int(v) -> bool:
    # bool is a subclass of int; `true` is not a rule number.
    return isinstance(v, int) and not isinstance(v, bool)


# ---- setup -----------------------------------------------------------------
def review_mode() -> str:
    """The mode AGENT_MEMORY_REVIEW asks for, as one of `MODES`. Unset, blank
    or a value that is not one of them counts as `off`. Whether the server
    can act on it is `make_reviewer`'s call: `warn` and `enforce` need a
    server address too."""
    mode = os.environ.get("AGENT_MEMORY_REVIEW", "off").strip().lower()
    return mode if mode in MODES else "off"


def make_reviewer() -> Reviewer:
    """Build the reviewer from the environment. Returns a `NullReviewer`, and
    logs one line saying why, when review is off, the setting is not a known
    value, or no server address is given."""
    raw = os.environ.get("AGENT_MEMORY_REVIEW", "off").strip().lower()
    mode = review_mode()
    if mode == "off":
        if raw and raw != "off":
            reason = (f"review is off: AGENT_MEMORY_REVIEW={raw!r} is not one of "
                      + ", ".join(repr(m) for m in MODES))
            log.warning(reason)
            return NullReviewer(reason)
        log.info("review is off (AGENT_MEMORY_REVIEW=off)")
        return NullReviewer("review is off (AGENT_MEMORY_REVIEW=off)")
    url = os.environ.get("AGENT_MEMORY_REVIEW_URL", "").strip()
    if not url:
        reason = (f"review is off: AGENT_MEMORY_REVIEW={mode} but "
                  "AGENT_MEMORY_REVIEW_URL is not set")
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
    log.info("review is on (%s): %s at %s (timeout %ss)", mode, model, url, timeout)
    return OllamaReviewer(url, model, timeout)
