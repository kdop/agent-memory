"""Write-time checks on a new memory.

The memory skill has rules a program can partly see: an entry should be more than a
stub, should name its project, and a decision, lesson or constraint should say why. `warnings_for`
looks at a `MemoryIn` and returns one fixed string per rule it breaks. Nothing here
blocks a write. The memory is stored either way; the warning only tells the writer,
at the moment of writing, while the reason is still in their head.

The rules are data, so adding one means adding a row, not a branch.
"""

from __future__ import annotations

from .schemas import MemoryIn

# A memory shorter than this is probably a stub.
SHORT_LIMIT = 40

# Only these types must carry a why.
REASONING_TYPES = {"constraint", "decision", "lesson"}

# Words that show the entry says why. Matched case-insensitively, anywhere in the text.
REASONING_WORDS = (
    "because",
    "since",
    "reason",
    "why",
    "rejected",
    "instead",
    "so that",
    "cause",
    "trade-off",
    "alternative",
)


def _short(m: MemoryIn) -> bool:
    return len(m.content) < SHORT_LIMIT


def _no_project(m: MemoryIn) -> bool:
    return not (m.project or "").strip()


def _no_reasoning(m: MemoryIn) -> bool:
    if m.type not in REASONING_TYPES:
        return False
    text = m.content.lower()
    return not any(word in text for word in REASONING_WORDS)


# (warning string, check). Order here is the order in the response.
RULES = (
    ("short", _short),
    ("no-project", _no_project),
    ("no-reasoning", _no_reasoning),
)


def warnings_for(memory_in: MemoryIn) -> list[str]:
    """Return the fixed warning string of every rule the memory breaks, in order."""
    return [name for name, broken in RULES if broken(memory_in)]
