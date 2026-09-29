"""Have a language model read each new memory and say what it thinks.

The model sees the five rules from the memory skill, the new memory, the
five memories closest to it in the same project, and the ten existing tags
closest to it in meaning. It answers approve, reject (with the rule the entry
breaks), improve (with what the entry needs: see `NEEDS`) or rewrite. The
verdict is stored next to the memory and shown with it. Two cases look
at the neighbours: an entry that reverses or replaces one is approved with
`supersedes` set to that id, which the server stores on the new memory as a
link (both memories stay); an entry that repeats one and adds to it gets a
rewrite with the merged text and `duplicate_of` that id, for the writer to
apply to the old memory. That merge is the only verdict with suggested
text. The model never changes a stored memory. A plain approve is followed
by a second, short question (`GAP_PROMPT`), which turns it into an improve
when the entry leans on something it never gives.

In `flag` mode it is advice only: nothing is refused or changed because of
it, and the write never waits for it. In `refuse` mode the model reads the
entry before it is stored, and any verdict but approve refuses the write
(HTTP 422 with the verdict, and the merged text for a rewrite); `force=true`
stores it anyway.
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

VERDICTS = ("approve", "reject", "improve", "rewrite")

# What an `improve` verdict says the entry lacks: its why (rule 3, for a
# decision, lesson or constraint), words a later reader can follow, the fact
# needed to act on it (a number, a name), or where it holds.
NEEDS = ("reason", "clarity", "detail", "scope")

# The types rule 3 (the why) holds for. A preference, a note or an entry with
# no type never needs a why.
WHY_TYPES = ("decision", "lesson", "constraint")

# The message a refused `improve` starts with, before what is missing.
LOW_VALUE = "Low value memory, retry with more context or skip"

# What a memory's `review_status` says about the model's check of it:
# `unverified` (not checked yet, or the model gave no answer), `verified`
# (approved) or `flagged` (rejected, told to improve, or a merge was
# suggested). Only verified memories serve as reference when another memory
# is checked.
STATUSES = ("unverified", "verified", "flagged")
UNVERIFIED, VERIFIED, FLAGGED = STATUSES


def status_for(verdict: str) -> str:
    """The status a memory gets when `verdict` is stored for it: approve
    gives `verified`, reject, improve or rewrite gives `flagged`."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {', '.join(VERDICTS)} (got {verdict!r})")
    return VERIFIED if verdict == "approve" else FLAGGED


# The values AGENT_MEMORY_REVIEW may take. `off` means no model is asked;
# `flag` stores the verdict after the write; `refuse` refuses a write the
# model does not approve.
MODES = ("off", "flag", "refuse")
# The names the modes had before: accepted for one release, with one log
# line saying which name to use now.
OLD_MODE_NAMES = {"warn": "flag", "enforce": "refuse"}


@dataclass(frozen=True)
class Verdict:
    """What the model said about one memory.

    `verdict` is one of `VERDICTS`. `rule` is the number of the rule the entry
    breaks or falls short of (None for an approve). `reason` is one sentence;
    for an improve it starts with `needs <what>: ` (see `improve_reason`), so
    the stored text says what is missing without a column of its own.
    `needs` is that word, one of `NEEDS`, for an improve, else None.
    `rewrite` is the merged text when the verdict is `rewrite`, else None.
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
    needs: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def improve_reason(needs: str, line: str) -> str:
    """The `reason` of an improve verdict: `needs <what>: <line>`. The review
    table has no column for `needs`, so it lives in the reason text, and
    `needs_of` reads it back."""
    return f"needs {needs}: {line}"


def needs_of(verdict: str, reason: str | None) -> str | None:
    """What an improve verdict says the entry needs, read from its stored
    `reason` (see `improve_reason`); None for any other verdict, or a reason
    not in that form."""
    if verdict != "improve" or not reason or not reason.startswith("needs "):
        return None
    word = reason[len("needs "):].split(":", 1)[0].strip()
    return word if word in NEEDS else None


def refusal_message(verdict: Verdict) -> str | None:
    """The message a refused improve gives the writer: `LOW_VALUE`, then what
    is missing. None for any other verdict."""
    if verdict.verdict != "improve":
        return None
    missing = verdict.reason[:1].upper() + verdict.reason[1:]
    return f"{LOW_VALUE}. {missing}"


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
        verdict = parse_verdict(text, [n["id"] for n in neighbours], list(tags),
                                entry_type=memory.get("type"))
        if verdict is None:
            log.warning("review of memory %s failed: %s gave no usable JSON: %.200s",
                        _label(memory), self.model_name, text)
            return None
        if verdict.verdict == "approve" and verdict.supersedes is None:
            return self._check_gap(memory, verdict)
        return verdict

    def _check_gap(self, memory: dict, approve: Verdict) -> Verdict:
        """The second question, asked only of a plain approve: does the entry
        lean on something it never gives (see `GAP_PROMPT`)? An improve when
        it does; the approve as it was when it does not, or when the model
        gives no usable answer (one log line), since the entry passed the
        rules and a missing second answer must not flag it."""
        try:
            text = self._chat(self.gap_request_body(memory))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log.warning("gap check of memory %s failed: %s at %s: %s",
                        _label(memory), self.model_name, self.url, _cause(e))
            return approve
        gap = parse_gap(text, memory.get("content", ""))
        return gap if gap is not None else approve

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

    def gap_request_body(self, memory: dict) -> dict:
        """The JSON sent to `/api/chat` for the gap check: the entry's type
        and text only, with the same settings as `request_body`."""
        user = f"type: {memory.get('type') or 'none'}\ncontent: {memory.get('content', '')}"
        return {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": GAP_PROMPT},
                {"role": "user", "content": user},
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
# borrows the example's reason for an entry on the same subject). The project-fact
# step has an example of its kind on another subject: without it the model approved
# a plain "the API is a FastAPI app on Postgres" note. The `step` key after the lead
# keys makes it name the step before the verdict: without it, once `improve` was on
# the list, it answered improve or rewrite for plain repeats. Measure a change here
# with tests/test_verdict_quality.py before keeping it.
SYSTEM_PROMPT = (
    "You review entries that AI agents write to a shared memory. Memory is for what "
    "will still matter in a later session. The rules:\n"
    + "\n".join(f"{n}. {text}" for n, text in RULES)
    + "\n\n"
    "You get one new entry, up to five existing entries from the same project, and a "
    "list of existing tag names. Answer with one JSON object and nothing else, with "
    "exactly these eleven keys, in this order:\n"
    '{"why": "<the words of the new entry that give its reason, or \\"\\">", '
    '"new_facts": "<what the new entry adds to the closest listed entry, or \\"\\">", '
    '"step": <the number of the first step below that applies>, '
    '"supersedes": <id or null>, "duplicate_of": <id or null>, '
    '"verdict": "approve" | "reject" | "rewrite" | "improve", '
    '"needs": <"reason" for improve, else null>, '
    '"rule": <number or null>, '
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
    "Go through these steps in order and stop at the first that applies. Write its "
    "number as step, then give the verdict that step names.\n"
    "1. Out of date. The new entry reverses or replaces what a listed existing entry "
    "says, with a why. Answer approve, supersedes that entry's id, "
    "everything else null and tags []. Example: the listed entry says 'Kept the weekly "
    "report as a PDF because the board reads it on paper' and the new entry says 'Moved "
    "the weekly report to a web page because the board now reads it on phones': "
    "approve, supersedes that entry's id.\n"
    '2. Repeat. new_facts is "" and the new entry says the same as a listed existing '
    "entry, word for word or in other words, and nothing more. Reject, "
    "duplicate_of that entry's id, rule null, rewrite null. A repeat is never approved "
    "and never rewritten.\n"
    '3. Repeat with more. new_facts is not "" and the new entry says what a listed '
    "existing entry says and adds something to it. Rewrite, with one merged "
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
    "6. Project fact. The new entry describes how the project is made, as anyone can "
    "read it in the code: its framework, its database, its services, where a file, a "
    "module or a setting lives. It belongs in the project's own files, which git "
    "tracks. Reject, rule 4. An entry that weighs a choice and says why it was made is "
    "a decision, not a project fact. Example: 'The billing service is a Django app on "
    "MySQL.' is a project fact; 'Billing runs on MySQL because the team already runs "
    "it for the shop.' is a decision.\n"
    "7. No why. Only for the types decision, lesson and constraint; skip this step for "
    'any other type. why is "": the entry states what was decided, learned or must be '
    "done, but not why. Improve, needs reason, rule 3, however sensible the choice "
    "looks. Never add a why yourself.\n"
    "8. Otherwise approve, everything else null and tags [].\n"
    "\n"
    "- needs is set for improve only, else null.\n"
    "- rule is the number of the rule a rejected or improved entry breaks; null for a "
    "repeat, an approve or a rewrite.\n"
    "- rewrite is the merged text from step 3, else null. tags go with a rewrite only: "
    "the names from the offered list that fit the new text, most fitting first, at most "
    "five; never a name that is not on the list, and [] when none fits.\n"
    "- Ids are numbers, not strings.\n"
    "- A short entry is fine when it follows the rules. Do not reject for length.\n"
    "- reason is one plain sentence a person can act on."
)


# The gap check: a second, short question asked only when the checklist above
# approves. The model does not find a missing value or an unclear word inside
# the long checklist (it approves nearly every such entry there), but finds most
# of them when asked on their own. It copies the phrase first, names its kind
# from the phrase's form, and only then says whether a reader would have to ask
# someone: without that last key it flagged every entry that had a phrase at all.
GAP_PROMPT = (
    "You check one entry from a shared memory. Someone will act on it months later, "
    "with only the entry in front of them. Answer with one JSON object and nothing "
    "else, with these keys in this order: "
    '{"vague": "<a short phrase copied from the entry, or \\"\\">", '
    '"kind": "clarity" | "detail" | "scope" | null, '
    '"kept_to": "<words copied from the entry, or \\"\\">", "gap": "yes" | "no"}.\n'
    "vague: when the entry states a rule with never, always, any, every or all, copy "
    "that rule. Otherwise copy, word for word, the one short phrase of the entry that "
    'a reader would most likely have to ask about. "" when there is none.\n'
    "kind: from the form of that phrase. scope: a rule with never, always, any, every "
    "or all. detail: a quantity or a point in time (an amount, a size, a limit, a "
    "count, a time, a date, a deadline). clarity: any other phrase. null when vague "
    'is "".\n'
    "kept_to: for kind scope only: copy the words of the rule itself that limit it to "
    "one place, service, system or kind of thing (from the staging server, in the "
    "mobile app, for card payments). The case the rule came from is not a limit, nor "
    'is the thing the rule is about. "" when the rule has no such words, and "" for '
    "any other kind.\n"
    'gap: "yes" only when the reader cannot act without asking someone:\n'
    "- clarity or detail: the phrase leans on something known only outside the entry "
    "(an earlier talk, a past session, a test run, what someone asked for or agreed, "
    "what another party set) and the entry never gives it. A name, a figure the entry "
    "gives, or a common word is no gap.\n"
    '- scope: kept_to is "" and the rule covers more than the case the entry tells of.\n'
    'Otherwise "no".'
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
    if memory.get("type") not in WHY_TYPES:
        parts.append(f"Rule 3 does not hold for this entry: its type is "
                     f"{memory.get('type') or 'none'}, so it needs no why. Skip the No why step.")
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
                  offered_tags: list[str] | None = None,
                  entry_type: str | None = None) -> Verdict | None:
    """Turn the model's answer into a `Verdict`, or None when it is not the
    expected shape.

    Bad JSON, a missing key, a verdict outside `VERDICTS`, a rule number that
    is not one of the rules, or a `duplicate_of` or `supersedes` that names
    an entry the model was not shown all count as no answer. So do an
    improve whose `needs` is not one of `NEEDS`, and a rewrite that is not a
    merge (no `duplicate_of`, or no text): suggested text comes only from
    the two memories of a merge. The caller stores nothing then. Keys the
    prompt asks for on top of these (`why`, `new_facts`) are ignored.

    An improve's reason is stored as `needs <what>: <line>` (see
    `improve_reason`) and `needs` is kept on the verdict; any other verdict
    has `needs` None, whatever the model put there. Only a rewrite keeps
    its `rewrite` text.

    Two of the checklist's steps follow from what the model already wrote,
    so they are applied here rather than trusted to its verdict: a rewrite
    of a listed entry with `new_facts` "" is a plain repeat, so a reject;
    and an improve for a missing why on an entry whose type is given as
    `entry_type` and is not one of `WHY_TYPES` is an approve, since rule 3
    does not hold for it.

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
    needs = data.get("needs")
    if verdict == "improve":
        if needs not in NEEDS:
            return None
        line = reason.strip()
        # The model sometimes writes the prefix itself; keep it once.
        if needs_of("improve", line) == needs:
            line = line.split(":", 1)[1].strip() or line
        reason = improve_reason(needs, line)
    else:
        needs = None
    if verdict == "rewrite" and duplicate_of is not None and data.get("new_facts") == "":
        verdict, reason = "reject", reason.strip()
    if (verdict == "improve" and needs == "reason" and entry_type is not None
            and entry_type not in WHY_TYPES):
        verdict, needs, rule = "approve", None, None
        reason = f"A {entry_type} needs no why."
    if verdict == "rewrite":
        if duplicate_of is None or not (rewrite or "").strip():
            return None
    else:
        rewrite = None
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
                   supersedes=supersedes, needs=needs)


def parse_gap(text: str, content: str) -> Verdict | None:
    """Turn the gap check's answer into an improve verdict, or None when it
    finds no gap or is not the expected shape. A gap counts only when the
    phrase is copied from `content` (case and spaces aside): a phrase the
    model made up names nothing a writer can fix."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("gap") != "yes":
        return None
    vague, kind = data.get("vague"), data.get("kind")
    if kind not in ("clarity", "detail", "scope") or not isinstance(vague, str):
        return None
    phrase = " ".join(vague.split()).strip(" .")
    if not phrase or phrase.lower() not in " ".join(content.split()).lower():
        return None
    line = {"clarity": f'"{phrase}" is not said in the entry; name what it means.',
            "detail": f'"{phrase}" is not given in the entry; write the value.',
            "scope": f'"{phrase}" does not say where it holds; name the part it is for.'}
    return Verdict(verdict="improve", rule=None, reason=improve_reason(kind, line[kind]),
                   rewrite=None, duplicate_of=None, needs=kind)


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
