"""Have a language model read each new memory and say what it thinks.

The model sees the five rules from the memory skill, the new memory, the
five memories closest to it in the same project, and the ten existing tags
closest to it in meaning. It answers approve, reject (with the rule the entry
breaks) or rewrite (with better text and the tags from that list that fit it).
The verdict is stored next to the memory and shown with it. Two cases look
at the neighbours: an entry that reverses or replaces one is approved with
`supersedes` set to that id, which the server stores on the new memory as a
link (both memories stay); an entry that repeats one and adds to it gets a
rewrite with the merged text and `duplicate_of` that id, for the writer to
apply to the old memory. The model never changes a stored memory.

In `flag` mode it is advice only: nothing is refused or changed because of
it, and the write never waits for it. In `refuse` mode the model reads the
entry before it is stored, and a reject or rewrite verdict refuses the write
(HTTP 422 with the verdict and the suggestion); `force=true` stores it anyway.
In both modes a model that does not answer never blocks a write: the memory
is stored and one log line says the review did not run.

Settings, read once when `make_reviewer()` runs at server start:

    AGENT_MEMORY_REVIEW          `off` (default), `flag` or `refuse`; the old
                                 names `warn` and `enforce` still work for one
                                 release, with one log line
    AGENT_MEMORY_REVIEW_URL      an Ollama server, for example http://host:11434
    AGENT_MEMORY_REVIEW_MODEL    the model to ask (default qwen3:14b)
    AGENT_MEMORY_REVIEW_TIMEOUT  seconds to wait for an answer (default 30)
    AGENT_MEMORY_REVIEW_POLL     seconds between checks whether the model is back
                                 (default 300; 0 turns the check off)

The Ollama calls use urllib from the standard library, so the server needs no
new package. The caller runs them in a thread (`asyncio.to_thread`) so the
event loop stays free while the model thinks.

The model may be off for hours (it runs on a machine that is switched off at
times). `reachable()` is one cheap request that says whether it answers now;
the server asks every AGENT_MEMORY_REVIEW_POLL seconds and, when the model is
back, reviews the memories written while it was away.
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
DEFAULT_POLL = 300.0
# How long `reachable()` waits for the model server to answer.
REACHABLE_TIMEOUT = 2.0
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

# What a memory's `review_status` says about the model's check of it:
# `unverified` (not checked yet, or the model gave no answer), `verified`
# (approved) or `flagged` (rejected, or a rewrite was suggested). Only
# verified memories serve as reference when another memory is checked.
STATUSES = ("unverified", "verified", "flagged")
UNVERIFIED, VERIFIED, FLAGGED = STATUSES


def status_for(verdict: str) -> str:
    """The status a memory gets when `verdict` is stored for it: approve
    gives `verified`, reject or rewrite gives `flagged`."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {', '.join(VERDICTS)} (got {verdict!r})")
    return VERIFIED if verdict == "approve" else FLAGGED


# The values AGENT_MEMORY_REVIEW may take. `off` means no model is asked;
# `flag` stores the verdict after the write; `refuse` refuses a write the
# model rejects or wants rewritten.
MODES = ("off", "flag", "refuse")
# The names the modes had before: accepted for one release, with one log
# line saying which name to use now.
OLD_MODE_NAMES = {"warn": "flag", "enforce": "refuse"}


@dataclass(frozen=True)
class Verdict:
    """What the model said about one memory.

    `verdict` is one of `VERDICTS`. `rule` is the number of the rule the entry
    breaks or falls short of (None for an approve). `reason` is one sentence.
    `rewrite` is the suggested text when the verdict is `rewrite`, else None.
    `duplicate_of` is the id of the listed neighbour this entry repeats, when
    the verdict is a reject for that reason, or the listed neighbour this
    entry repeats and adds to, when the verdict is a rewrite with the merged
    text, else None. `tags` are the tags the model suggests for the
    rewritten text, chosen from the ones it was offered; empty unless the
    verdict is `rewrite`. `supersedes` is the id of the listed neighbour this
    entry reverses or replaces, else None; it is stored on the memory as its
    `supersedes` link."""

    verdict: str
    rule: int | None
    reason: str
    rewrite: str | None
    duplicate_of: int | None
    tags: list[str] = field(default_factory=list)
    supersedes: int | None = None

    def as_dict(self) -> dict:
        return asdict(self)


class Reviewer:
    """What the server needs from a review backend.

    `model_name` names the model (None when there is none). `review` takes the
    new memory and its nearest neighbours as dicts with at least `id`,
    `project`, `type`, `tags` and `content`, and the names of the existing
    tags the model may suggest for a rewrite, and returns a `Verdict`, or None
    when the model gave no usable answer. `reachable` says whether the model
    answers right now, with one cheap request. Both may block: call them in
    a thread."""

    model_name: str | None

    def review(self, memory: dict, neighbours: list[dict],
               tags: list[str] = ()) -> Verdict | None:
        raise NotImplementedError

    def reachable(self) -> bool:
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

    def reachable(self) -> bool:
        return False


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

    def reachable(self) -> bool:
        """Whether the Ollama server answers now: one `GET /api/tags`, which
        costs it nothing, with a short timeout (`REACHABLE_TIMEOUT`). False
        when it does not answer in time, for whatever reason."""
        try:
            with urllib.request.urlopen(self.url + "/api/tags", timeout=REACHABLE_TIMEOUT):
                return True
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

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
# The answer starts with two keys the server drops, `why` and `new_facts`: the model
# fills them before the verdict, so it looks at the entry before it judges. Without
# them it judged first and made up a reason to fit. Two more rules, both learned
# from the verdict set (tests/data/verdict_set.json): an example never carries an
# id (the model copies it), and never shares a subject with a test entry (the model
# borrows the example's reason for an entry on the same subject). Measure a change
# here with tests/test_verdict_quality.py before keeping it.
SYSTEM_PROMPT = (
    "You review entries that AI agents write to a shared memory. Memory is for what "
    "will still matter in a later session. The rules:\n"
    + "\n".join(f"{n}. {text}" for n, text in RULES)
    + "\n\n"
    "You get one new entry, up to five existing entries from the same project, and a "
    "list of existing tag names. Answer with one JSON object and nothing else, with "
    "exactly these nine keys, in this order:\n"
    '{"why": "<the words of the new entry that give its reason, or \\"\\">", '
    '"new_facts": "<what the new entry adds to the closest listed entry, or \\"\\">", '
    '"supersedes": <id or null>, "duplicate_of": <id or null>, '
    '"verdict": "approve" | "reject" | "rewrite", "rule": <number or null>, '
    '"reason": "<one sentence>", "rewrite": "<text>" or null, '
    '"tags": [<names from the list only>]}\n'
    "why: copy the words of the new entry that give the reason or the cause, in any "
    "form: a because, since or so that clause, a cause, what went wrong, what the other "
    "choice would cost, who asked for it and what happened before. It is the part of "
    "the entry that answers the question why, never the whole entry. Naming the "
    "other choice (X, not Y; X instead of Y; X over Y) is not a reason. When there "
    'are no such words, why is "". Example: \'Picked Redis over Memcached for the '
    'cache.\' gives no reason, so why is "".\n'
    "new_facts: what a reader who knows the closest listed entry would learn from the "
    "new entry, in a few words: a number, a date, a later change, another cause. Other "
    "words for the same thing (a synonym, another unit, another order of the sentences) "
    'teach nothing. With no listed entry, or nothing to learn, new_facts is "".\n'
    "supersedes: the id of the listed entry the new entry makes out of date, because it "
    "changes a choice, a value or a behaviour that entry states (now X instead of Y, "
    "moved from A to B, no longer Z), so that what the listed entry says no longer "
    "holds; null when no listed entry is changed. An entry that keeps what the listed "
    "entry says and adds to it changes nothing: that is duplicate_of, not supersedes.\n"
    "duplicate_of: the id of the listed entry the new entry repeats, with or without "
    "more; null when it repeats none. Never set both supersedes and duplicate_of.\n"
    "\n"
    "Go through these steps in order and stop at the first that applies:\n"
    "1. Out of date. supersedes is set: the new entry reverses or replaces what a listed "
    "existing entry says, with a why. Answer approve, supersedes that entry's id, "
    "everything else null and tags []. Example: the listed entry says 'Kept the weekly "
    "report as a PDF because the board reads it on paper' and the new entry says 'Moved "
    "the weekly report to a web page because the board now reads it on phones': "
    "approve, supersedes that entry's id.\n"
    '2. Repeat. duplicate_of is set and new_facts is "": the new entry says the same as '
    "a listed existing entry, word for word or in other words, and nothing more. Reject, "
    "duplicate_of that entry's id, rule null, rewrite null. A repeat is never approved "
    "and never rewritten.\n"
    '3. Repeat with more. duplicate_of is set and new_facts is not "": the new entry says '
    "what a listed existing entry says and adds something to it. Rewrite, with one merged "
    "text that keeps the listed entry's words and adds the new part, duplicate_of that "
    "entry's id, and rule null. Example: the listed entry says 'Cache entries expire after "
    "an hour because prices change hourly' and the new entry says 'Cache entries expire "
    "after an hour because prices change hourly; the cache holds 10,000 entries': rewrite "
    "'Cache entries expire after an hour because prices change hourly; the cache holds "
    "10,000 entries', duplicate_of that entry's id.\n"
    "4. Diary. The new entry tells what was done in a session (spent the morning on, "
    "ran, fixed, pushed, will look at it tomorrow): reject, rule 2.\n"
    "5. Git fact. The new entry states what git already records: which version or "
    "release was tagged and what it contains, a commit hash and what it changed, a "
    "file, function or setting that was moved, renamed or changed, a commit-shaped "
    "sentence (added X to Y, raised the timeout from 5 to 10). Reject, rule 4. Such a "
    "fact is true and durable and still does not belong here.\n"
    '6. No why. why is "": the entry states what was decided, chosen, learned or must '
    "be done, but not why. Reject, rule 3, however sensible the choice looks. Never add "
    "a why yourself.\n"
    "7. Otherwise the entry states the what and the why: approve, everything else null "
    "and tags [].\n"
    "\n"
    "- rule is the number of the rule a rejected entry breaks; null for a repeat, an "
    "approve or a rewrite.\n"
    "- rewrite is the merged text from step 3, else null. tags go with a rewrite only: "
    "the names from the offered list that fit the new text, most fitting first, at most "
    "five; never a name that is not on the list, and [] when none fits.\n"
    "- Ids are numbers, not strings.\n"
    "- A short entry is fine when it follows the rules. Do not reject for length.\n"
    "- reason is one plain sentence a person can act on."
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
    is not one of the rules, or a `duplicate_of` or `supersedes` that names
    an entry the model was not shown all count as no answer. The caller
    stores nothing then. Keys the prompt asks for on top of these (`why`,
    `new_facts`) are ignored.

    `tags` may be left out (then it is empty) but must be a list of strings
    when present. Only a rewrite keeps its tags, and only the names in
    `offered_tags`, matched without regard to case and returned in the offered
    spelling; the rest are dropped. With `offered_tags` None the names are
    kept as given. `supersedes` may be left out too (then it is None); when
    present it must be null or the id of a listed neighbour, like
    `duplicate_of`."""
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
    supersedes = data.get("supersedes")
    for ref in (duplicate_of, supersedes):
        if ref is not None:
            if not _is_int(ref):
                return None
            if neighbour_ids is not None and ref not in neighbour_ids:
                return None
    raw_tags = data.get("tags") or []
    if not isinstance(raw_tags, list) or not all(isinstance(t, str) for t in raw_tags):
        return None
    tags = _keep_offered(raw_tags, offered_tags) if verdict == "rewrite" else []
    return Verdict(verdict=verdict, rule=rule, reason=reason.strip(),
                   rewrite=rewrite, duplicate_of=duplicate_of, tags=tags,
                   supersedes=supersedes)


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
def _mode_from(raw: str) -> str:
    """`raw` (the setting, already stripped and lowered) as one of `MODES`:
    an old name is mapped to its new one, and anything else that is not a
    mode counts as `off`. Logs nothing; `review_mode` does that."""
    if raw in OLD_MODE_NAMES:
        return OLD_MODE_NAMES[raw]
    return raw if raw in MODES else "off"


def review_mode() -> str:
    """The mode AGENT_MEMORY_REVIEW asks for, as one of `MODES`. Unset, blank
    or a value that is not one of them counts as `off`. An old name (`warn`,
    `enforce`) still counts as its new one, with one log line that says so.
    Whether the server can act on the mode is `make_reviewer`'s call: `flag`
    and `refuse` need a server address too."""
    raw = os.environ.get("AGENT_MEMORY_REVIEW", "off").strip().lower()
    mode = _mode_from(raw)
    if raw in OLD_MODE_NAMES:
        log.warning("AGENT_MEMORY_REVIEW=%s is an old name and will stop working; "
                    "use %s", raw, mode)
    return mode


def review_poll() -> float:
    """How many seconds AGENT_MEMORY_REVIEW_POLL puts between two checks
    whether the model is back: `DEFAULT_POLL` when unset or blank, and also
    (with one log line) when the value is not a number. 0 or less means no
    check at all."""
    raw = os.environ.get("AGENT_MEMORY_REVIEW_POLL", "").strip()
    if not raw:
        return DEFAULT_POLL
    try:
        return float(raw)
    except ValueError:
        log.warning("AGENT_MEMORY_REVIEW_POLL=%r is not a number; using %s", raw, DEFAULT_POLL)
        return DEFAULT_POLL


def make_reviewer() -> Reviewer:
    """Build the reviewer from the environment. Returns a `NullReviewer`, and
    logs one line saying why, when review is off, the setting is not a known
    value, or no server address is given."""
    raw = os.environ.get("AGENT_MEMORY_REVIEW", "off").strip().lower()
    # Not `review_mode()`: the lifespan calls that too, and the old-name
    # line should be logged once per start, not twice.
    mode = _mode_from(raw)
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
        reason = (f"review is off: AGENT_MEMORY_REVIEW={raw} but "
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
