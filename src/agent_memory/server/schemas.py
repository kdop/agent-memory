"""Pydantic v2 request/response models — the HTTP contract.

Tags are structured objects (`{"name", "description"}`), never delimited strings, so
descriptions may contain any character. Validation lives here, once: a tag name is
required and non-blank; a blank description collapses to None (the repo then defaults
a new tag's descriptor to its own name).
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

# The only allowed memory `type` values. Anything else — a typo, a lazy default like
# the old "code", a one-off like "feedback"/"reference" — is rejected at the API
# boundary rather than silently accepted, so the classification stays meaningful.
ALLOWED_MEMORY_TYPES = {"constraint", "decision", "lesson", "note", "preference"}


def _validate_memory_type(v: str | None) -> str | None:
    """Blank/None means "no type" (allowed everywhere; also how UpdateIn clears an
    existing type). Anything else must be one of ALLOWED_MEMORY_TYPES."""
    if v is None:
        return None
    v = v.strip()
    if not v:
        return v  # "" — UpdateIn's clear-the-column sentinel
    if v not in ALLOWED_MEMORY_TYPES:
        allowed = ", ".join(sorted(ALLOWED_MEMORY_TYPES))
        raise ValueError(f"type must be one of: {allowed} (got {v!r})")
    return v


class TagIn(BaseModel):
    name: str = Field(min_length=1)
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _name_nonblank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("tag name must not be blank")
        return v

    @field_validator("description")
    @classmethod
    def _desc_blank_to_none(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None


class MemoryIn(BaseModel):
    content: str
    project: str | None = None
    agent: str | None = None
    tags: list[TagIn] = []
    type: str | None = None

    _validate_type = field_validator("type")(_validate_memory_type)


class ReviewOut(BaseModel):
    """What the review model said about a memory (see server/review.py)."""

    verdict: str
    rule: int | None = None
    reason: str = ""
    rewrite: str | None = None
    duplicate_of: int | None = None
    # Tags suggested for the rewrite, from the tags that existed at the time.
    # Empty unless the verdict is rewrite.
    tags: list[str] = []
    # The memory this one reverses or replaces, when the model said so. The
    # same value as the memory's own `supersedes`.
    supersedes: int | None = None


class ReviewEntry(BaseModel):
    """One verdict in a memory's review history (`GET /memories/{id}/reviews`):
    the fields of `ReviewOut` without `supersedes`, plus when it was given. The
    link lives on the memory and follows the newest verdict, so an older
    entry has none to show."""

    created_at: str
    verdict: str
    rule: int | None = None
    reason: str = ""
    rewrite: str | None = None
    duplicate_of: int | None = None
    tags: list[str] = []


class MemoryOut(BaseModel):
    id: int
    timestamp: str | None = None
    agent: str | None = None
    project: str | None = None
    content: str = ""
    type: str | None = None
    tags: list[str] = []
    snippet: str | None = None
    # Search only: the ts_rank of a keyword hit, the cosine of a semantic hit.
    # None for rows that come from anything other than a search.
    score: float | None = None
    # The model's verdict, once the review has run. None until then, and
    # always None when review is off.
    review: ReviewOut | None = None
    # Whether the model has checked this memory: `unverified` until a verdict
    # is stored, then `verified` (approve) or `flagged` (reject or rewrite).
    review_status: str = "unverified"
    # The older memory this one reverses or replaces, set from the model's
    # verdict, never from a request; and the newest memory that supersedes
    # this one, if any. Both None for a memory that stands on its own.
    supersedes: int | None = None
    superseded_by: int | None = None
    # When the review archived this memory, or None while it is live. Only a
    # read by id, or a listing with `archived=true`, returns an archived one.
    archived_at: str | None = None


class RestoreResult(BaseModel):
    """What `POST /memories/{id}/restore` answers: `restored` is False when
    the memory was not archived, so nothing changed."""

    id: int
    restored: bool


class UpdateIn(BaseModel):
    # Only fields actually sent are applied. project/type == "" clears the column;
    # set_tags == [] removes all tags.
    content: str | None = None
    project: str | None = None
    type: str | None = None
    set_tags: list[TagIn] | None = None
    add_tags: list[TagIn] | None = None
    remove_tags: list[str] | None = None

    _validate_type = field_validator("type")(_validate_memory_type)


class AddResult(BaseModel):
    id: int
    # Rule names the entry breaks (see server/checks.py). Empty when it is clean.
    warnings: list[str] = []


class UpdateResult(BaseModel):
    changes: list[str]


class DeleteResult(BaseModel):
    deleted: int
    missing: list[int] = []


class TagCount(BaseModel):
    name: str
    count: int
    description: str = ""


class ProjectCount(BaseModel):
    project: str
    count: int


class AgentCount(BaseModel):
    agent: str
    count: int


# ---- tag management (dashboard, D1) ---------------------------------------
class TagPatch(BaseModel):
    name: str | None = None          # rename (collision → merge into the existing tag)
    description: str | None = None   # re-describe

    @field_validator("name")
    @classmethod
    def _name_nonblank(cls, v):
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("tag name must not be blank")
        return v


class TagMergeIn(BaseModel):
    sources: list[str] = Field(min_length=1)
    target: str = Field(min_length=1)
    description: str | None = None


class TagDetachIn(BaseModel):
    memory_ids: list[int] | None = None   # omit / empty = detach from all memories
