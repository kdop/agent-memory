"""Unit tests for the write-time checks (no server, no DB).

One test per rule, one for a clean entry, and the order of the result.
"""

import pytest

from agent_memory.server.checks import REASONING_WORDS, warnings_for
from agent_memory.server.schemas import MemoryIn

# Long enough to clear the `short` rule, says nothing about why.
LONG_NO_WHY = "Switched the build to run on every push to any branch of the repo."
# Long enough and says why.
LONG_WITH_WHY = "Switched the build to run on every push because the nightly was too slow."


def _mem(content, project="proj", type=None):
    return MemoryIn(content=content, project=project, type=type)


# ── clean ───────────────────────────────────────────────────────────────────
def test_clean_entry_has_no_warnings():
    assert warnings_for(_mem(LONG_WITH_WHY, type="decision")) == []


def test_clean_note_has_no_warnings():
    assert warnings_for(_mem(LONG_NO_WHY, type="note")) == []


# ── short ───────────────────────────────────────────────────────────────────
def test_short_under_40_chars():
    assert warnings_for(_mem("x" * 39)) == ["short"]


def test_short_exactly_40_chars_is_fine():
    assert warnings_for(_mem("x" * 40)) == []


# ── no-project ──────────────────────────────────────────────────────────────
def test_no_project_when_missing():
    assert warnings_for(_mem(LONG_NO_WHY, project=None)) == ["no-project"]


def test_no_project_when_blank():
    assert warnings_for(_mem(LONG_NO_WHY, project="  ")) == ["no-project"]


# ── no-reasoning ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("mtype", ["decision", "lesson"])
def test_no_reasoning_for_decision_and_lesson(mtype):
    assert warnings_for(_mem(LONG_NO_WHY, type=mtype)) == ["no-reasoning"]


@pytest.mark.parametrize("mtype", ["note", "preference", None])
def test_no_reasoning_not_applied_to_other_types(mtype):
    assert warnings_for(_mem(LONG_NO_WHY, type=mtype)) == []


@pytest.mark.parametrize("word", REASONING_WORDS)
def test_each_reasoning_word_clears_the_rule(word):
    content = f"We picked the first option, {word} it was the simplest of the three."
    assert warnings_for(_mem(content, type="decision")) == []


def test_reasoning_words_match_any_case():
    content = "We picked the first option BECAUSE it was the simplest of the three."
    assert warnings_for(_mem(content, type="lesson")) == []


# ── several at once ─────────────────────────────────────────────────────────
def test_all_three_in_fixed_order():
    assert warnings_for(MemoryIn(content="tiny", type="decision")) == [
        "short", "no-project", "no-reasoning",
    ]
