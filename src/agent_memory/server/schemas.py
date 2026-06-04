"""Request/response models for the HTTP API.

These mirror the plain dicts/tuples the store already returns; they exist to give
FastAPI validation + OpenAPI docs, not to reshape the data.
"""

from typing import List, Optional

from pydantic import BaseModel


class MemoryIn(BaseModel):
    content: str
    project: Optional[str] = None
    agent: Optional[str] = None
    tags: List[str] = []
    type: Optional[str] = None


class MemoryOut(BaseModel):
    id: int
    timestamp: Optional[str] = None
    agent: Optional[str] = None
    project: Optional[str] = None
    content: str = ""
    type: Optional[str] = None
    tags: List[str] = []
    snippet: Optional[str] = None


class UpdateIn(BaseModel):
    # Same shapes the store.update() seam expects: project/type "" clears, tag
    # fields are comma-separated strings. Only fields actually sent are applied.
    content: Optional[str] = None
    project: Optional[str] = None
    type: Optional[str] = None
    set_tags: Optional[str] = None
    add_tags: Optional[str] = None
    remove_tags: Optional[str] = None


class AddResult(BaseModel):
    id: int


class UpdateResult(BaseModel):
    changes: List[str]


class DeleteResult(BaseModel):
    deleted: int
    missing: List[int] = []


class TagCount(BaseModel):
    name: str
    count: int


class ProjectCount(BaseModel):
    project: str
    count: int
