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
ALLOWED_MEMORY_TYPES = {"decision", "lesson", "note", "preference"}


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


class MemoryOut(BaseModel):
    id: int
    timestamp: str | None = None
    agent: str | None = None
    project: str | None = None
    content: str = ""
    type: str | None = None
    tags: list[str] = []
    snippet: str | None = None


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
