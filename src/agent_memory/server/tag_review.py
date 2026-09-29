"""Have the review model read each tag and say whether it should stay.

Tags get what memories have: a status (`unverified`, `verified`, `flagged`)
and a verdict from the same model, with the same settings and the same
fallback rule. The model sees the tag's name and description, how many live
memories use it and three of them, and the five verified tags closest to it
in meaning. It answers keep, merge (into one of those tags), rename (to a
better name) or drop (the tag repeats a memory type, or names no subject).

What the server does with the verdict (`repository.set_tag_review`):

- keep: the tag is verified.
- merge: applied at once, with the existing merge code, only when both
  checks agree: the model says merge into X, and the two tags' vectors are
  at cosine `MERGE_THRESHOLD` or above, or their names match after the name
  cleanup (the same name, or its plural or singular). Otherwise it waits.
- rename, drop, and a merge that did not pass: the tag is flagged and the
  proposal is stored in `tag_reviews` for a person to apply or reject.

There is no undo. The tag review runs only inside the catch-up, after the
unverified memories, oldest tag first, so a tag verified early is on the
list the later ones are compared with.

The HTTP call is `OllamaReviewer`'s (review.py): `TagReviewer` builds its
body with `chat_body` and sends it with `_chat`, so the settings (think off,
JSON, temperature 0, num_ctx) stay in one place.
"""

from __future__ import annotations

import json
import logging
import urllib.error
from dataclasses import asdict, dataclass

from .review import OllamaReviewer, Reviewer, _cause

log = logging.getLogger(__name__)

TAG_VERDICTS = ("keep", "merge", "rename", "drop")
KEEP, MERGE, RENAME, DROP = TAG_VERDICTS

# How a stored proposal ended: applied (by the server, or by a person) or
# rejected by a person. None while it waits.
RESOLUTIONS = ("applied", "rejected")
APPLIED, REJECTED = RESOLUTIONS

# A merge the model asks for is applied on its own only when the two tags'
# vectors (of `name: description`) score this close, or their names match.
MERGE_THRESHOLD = 0.90

# How many of the tag's memories, and how many verified tags, the model sees.
MEMORY_COUNT = 3
NEIGHBOUR_COUNT = 5

# How much of each memory the model sees: enough to tell the subject.
MEMORY_CHARS = 300

# The memory types (schemas.ALLOWED_MEMORY_TYPES). A tag that only repeats
# one of them says nothing the memory's type does not say already.
MEMORY_TYPES = ("constraint", "decision", "lesson", "note", "preference")

# Other words for each type, for the name facts in the prompt: a tag named
# with one of them says no more than the type does. Plurals are found by
# the name cleanup's plural rule, so each word is listed once.
TYPE_WORDS = {
    "constraint": ("rule", "must", "hard-rule"),
    "decision": ("decided", "choice"),
    "lesson": ("learned", "learning", "takeaway"),
    "note": ("reminder", "info"),
    "preference": ("preferred", "like"),
}


@dataclass(frozen=True)
class TagVerdict:
    """What the model said about one tag. `into` is the name of the listed
    tag to merge into (merge only), `new_name` the better name (rename
    only), `reason` one sentence."""

    verdict: str
    into: str | None
    new_name: str | None
    reason: str

    def as_dict(self) -> dict:
        return asdict(self)


class TagReviewer:
    """Asks the model behind an `OllamaReviewer` about one tag. A model that
    cannot be reached, a slow answer, or an answer that is not the expected
    JSON all give None and one log line; the tag then stays unverified."""

    def __init__(self, chat: OllamaReviewer):
        self.chat = chat

    @property
    def model_name(self) -> str | None:
        return self.chat.model_name

    def review(self, tag: dict, memories: list[dict],
               neighbours: list[dict]) -> TagVerdict | None:
        body = self.request_body(tag, memories, neighbours)
        try:
            text = self.chat._chat(body)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log.warning("review of tag %r failed: %s at %s: %s",
                        tag.get("name"), self.model_name, self.chat.url, _cause(e))
            return None
        verdict = parse_tag_verdict(text, tag.get("name", ""), [n["name"] for n in neighbours])
        if verdict is None:
            log.warning("review of tag %r failed: %s gave no usable JSON: %.200s",
                        tag.get("name"), self.model_name, text)
        return verdict

    def request_body(self, tag: dict, memories: list[dict], neighbours: list[dict]) -> dict:
        """The JSON sent to `/api/chat`, with the memory review's settings."""
        return self.chat.chat_body(SYSTEM_PROMPT, user_prompt(tag, memories, neighbours))


def make_tag_reviewer(reviewer: Reviewer | None) -> TagReviewer | None:
    """The tag reviewer that goes with the memory reviewer: the same model at
    the same server, or None when the memory review has no real model (off,
    or a stand-in), so the tag review is on exactly when the memory review
    is."""
    if isinstance(reviewer, OllamaReviewer):
        return TagReviewer(reviewer)
    return None


# ---- the prompt ------------------------------------------------------------
# The answer starts with four keys the server drops: `why` (the subject),
# `is_type`, `same_as` and `name_problem`. The model fills them before the
# verdict, so it looks before it judges, as in the memory prompt; with only
# `why` it called nearly every tag a clear subject and kept it. No example shares a subject with the tag verdict set
# (tests/data/tag_verdict_set.json). Measure a change here with
# tests/test_tag_review.py::test_tag_verdict_quality before keeping it.
SYSTEM_PROMPT = (
    "You review the tags of a shared memory that AI agents write to. A tag names one "
    "subject (a part of the system, a tool, a field of work), so an agent can find every "
    "memory on that subject. Every memory also has a type, one of "
    + ", ".join(MEMORY_TYPES) + ", which says what kind of entry it is; a tag that "
    "says the same as the type is useless.\n\n"
    "You get one tag: its name, its description, how many memories use it and up to "
    "three of them; and up to five existing tags, closest in meaning first. Answer with "
    "one JSON object and nothing else, with exactly these eight keys, in this order:\n"
    '{"why": "<the subject the tag names, or \\"\\">", "is_type": true | false, '
    '"same_as": "<a listed tag name>" or null, "name_problem": "<what is wrong with the '
    'name, or \\"\\">", "verdict": "keep" | "merge" | "rename" | "drop", '
    '"into": "<a listed tag name>" or null, "new_name": "<name>" or null, '
    '"reason": "<one sentence>"}\n'
    "why: the subject the tag names, in a few words, judged from its name, its "
    "description and its memories. A subject is something a person would look for: "
    "a part of the system, a tool, a kind of work. When the name names none, why is "
    '"": a filler word (misc, stuff, general, other, important, todo), a date or a '
    "number alone, a word that would fit any memory at all.\n"
    "is_type: true when the name says what kind of entry the memories are, not what "
    "they are about: a memory type, its plural, a verb or another word for one "
    "(decided, learned, preferred, takeaways, reminders, must-do). Ask: would the name "
    "fit any memory of that type, whatever its subject? Then is_type is true. When the "
    "facts say the name, or a word of it, is a memory type, is_type is true unless the "
    "rest of the name names a subject.\n"
    "same_as: the listed tag that names the same subject as this tag: a synonym, the "
    "plural or the singular of the name, another spelling, a short form. Two tags that "
    "are close but not the same have different subjects: a tool and the field it "
    "belongs to, a part and the whole. Example: 'docs' and 'documentation' are the same; "
    "'fonts' and 'typography' are not. null when no listed tag is the same.\n"
    "name_problem: what makes the name hard to read or to find: a typo, a sentence or a "
    "task instead of a name (more than three words), capital letters, spaces or "
    'underscores. "" when the name is fine.\n'
    "\n"
    "Then decide from those keys, in this order, and stop at the first that applies. "
    "Do not judge again here: the verdict follows from the keys.\n"
    "1. is_type is true: drop.\n"
    '2. why is "": drop.\n'
    "3. same_as is set: merge, into the same_as tag's name exactly as listed.\n"
    '4. name_problem is not "": rename, new_name a short lowercase name, one to three '
    "words joined by hyphens.\n"
    "5. Otherwise: keep.\n\n"
    "- into is set for a merge only and new_name for a rename only; else null.\n"
    "- A tag used by one memory is fine. Do not drop for low use.\n"
    "- reason is one plain sentence a person can act on."
)


def _memory_line(m: dict) -> str:
    content = " ".join(str(m.get("content", "")).split())
    if len(content) > MEMORY_CHARS:
        content = content[:MEMORY_CHARS].rstrip() + "..."
    return (f"- type: {m.get('type') or 'none'}, project: {m.get('project') or 'none'}: "
            f"{content}")


def user_prompt(tag: dict, memories: list[dict], neighbours: list[dict]) -> str:
    """The tag, some of its memories and the verified tags closest to it,
    laid out for the model. `neighbours` are the only names `into` may
    take."""
    count = int(tag.get("count") or 0)
    parts = [f"Tag: {tag.get('name', '')}\nDescription: {tag.get('description') or ''}\n"
             f"Used by {count} {'memory' if count == 1 else 'memories'}."]
    if memories:
        listed = "\n".join(_memory_line(m) for m in memories)
        parts.append(f"Memories with this tag:\n{listed}")
    else:
        parts.append("No memory uses this tag now.")
    if neighbours:
        listed = "\n".join(f"- {n['name']}: {n.get('description') or n['name']}"
                           for n in neighbours)
        parts.append(f"Existing tags, closest in meaning first:\n{listed}")
    else:
        parts.append("There are no existing tags to compare with: into must be null.")
    facts = name_facts(tag.get("name", ""), [n["name"] for n in neighbours])
    if facts:
        parts.append("Facts about the name, checked by the server:\n"
                     + "\n".join(f"- {f}" for f in facts))
    return "\n\n".join(parts)


def name_facts(name: str, listed: list[str]) -> list[str]:
    """What the server can tell about a tag's name without a model, for the
    prompt: that it is a memory type, that it is not in the clean form
    (lowercase, hyphens), or that a listed tag has the same name in the
    plural or singular. The model is weak at spotting these itself; the
    verdict stays the model's."""
    from .repository import _plural_forms, clean_tag_name

    def type_of(word):
        forms = {word} | _plural_forms(word)
        for t in MEMORY_TYPES:
            if forms & ({t} | set(TYPE_WORDS[t])):
                return t
        return None

    facts = []
    clean = clean_tag_name(name)
    forms = {clean} | _plural_forms(clean)
    whole = type_of(clean)
    if whole in forms:
        facts.append(f"The name is the memory type '{whole}'.")
    elif whole:
        facts.append(f"The name is another word for the memory type '{whole}'.")
    else:
        for word in clean.split("-"):
            t = type_of(word)
            if t:
                facts.append(f"The word '{word}' in the name is the memory type '{t}', "
                             "or a word for it.")
                break
    if clean and clean != name.strip():
        facts.append(f"The name is not in the clean form (lowercase, words joined by "
                     f"hyphens); cleaned it would be '{clean}'.")
    for other in listed:
        c = clean_tag_name(other)
        if c != clean and c in forms:
            facts.append(f"The listed tag '{other}' is the same name in the plural or "
                         "the singular.")
    return facts


# ---- the answer ------------------------------------------------------------
def parse_tag_verdict(text: str, name: str = "",
                      offered: list[str] | None = None) -> TagVerdict | None:
    """Turn the model's answer into a `TagVerdict`, or None when it is not
    the expected shape.

    Bad JSON, a verdict outside `TAG_VERDICTS`, a blank reason, a merge
    whose `into` is not one of `offered` (matched without regard to case,
    returned in the offered spelling; with `offered` None any name goes) or
    is the tag itself, and a rename with no usable `new_name` all count as
    no answer. A rename whose new name, cleaned, is the name the tag
    has is a keep. When the model's own checks and its verdict
    disagree, the checks win, since the prompt says the verdict follows from
    them and the model at times skips that step: `is_type` true, or a
    `why` of "", is a drop; a keep with `same_as` naming an offered tag is
    a merge into it. `into` is dropped from anything but a merge and
    `new_name` from anything but a rename. The check keys are read for
    that and nothing else."""
    from .repository import clean_tag_name

    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    verdict, reason = data.get("verdict"), data.get("reason")
    if verdict not in TAG_VERDICTS or not isinstance(reason, str) or not reason.strip():
        return None
    reason = reason.strip()
    if data.get("is_type") is True or data.get("why") == "":
        verdict = DROP
    elif (verdict == KEEP and isinstance(data.get("same_as"), str) and data["same_as"].strip()
          and data["same_as"].strip().lower() != name.strip().lower()):
        if offered is None or data["same_as"].strip().lower() in {o.lower() for o in offered}:
            verdict, data = MERGE, dict(data, into=data["same_as"])
    into = new_name = None
    if verdict == MERGE:
        raw = data.get("into")
        if not isinstance(raw, str) or not raw.strip():
            return None
        raw = raw.strip()
        if offered is not None:
            match = {o.lower(): o for o in offered}.get(raw.lower())
            if match is None:
                return None
            raw = match
        if raw.lower() == name.strip().lower():
            return None
        into = raw
    elif verdict == RENAME:
        raw = data.get("new_name")
        if not isinstance(raw, str):
            return None
        new_name = clean_tag_name(raw)
        if not new_name:
            return None
        if new_name == name.strip():
            return TagVerdict(KEEP, None, None, reason)
    return TagVerdict(verdict, into, new_name, reason)
