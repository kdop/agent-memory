"""SQLAlchemy 2.0 models — the schema source of truth (Alembic autogenerates from here).

Postgres-only. Full-text search rides a generated `content_tsv` tsvector column with a
GIN index, so it stays in sync on every write with no triggers. Tag names are unique
case-insensitively via a functional `lower(name)` index; every tag carries a required
descriptor.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    REAL,
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    desc,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .review import STATUSES, UNVERIFIED

# The CHECK on `memories.review_status`: one of the three statuses, nothing else.
REVIEW_STATUS_CHECK = "review_status IN (" + ", ".join(f"'{s}'" for s in STATUSES) + ")"


class Base(DeclarativeBase):
    pass


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    agent: Mapped[str] = mapped_column(Text, index=True)
    project: Mapped[str | None] = mapped_column(Text, index=True)
    content: Mapped[str] = mapped_column(Text)
    type: Mapped[str | None] = mapped_column(Text, index=True)

    # Generated, stored tsvector kept in sync by Postgres itself (no triggers).
    content_tsv: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', content)", persisted=True)
    )

    # Meaning vector for semantic search, plus the name of the model that made it.
    # A plain float array: no pgvector, so nothing has to be installed on the DB
    # host, and the table is small enough to scan without an index. Both are
    # internal — the API never returns them.
    embedding: Mapped[list[float] | None] = mapped_column(ARRAY(REAL))
    embedding_model: Mapped[str | None] = mapped_column(Text)

    # Whether the review model has checked this memory: `unverified` until a
    # verdict is stored, then `verified` (approve) or `flagged` (reject or
    # rewrite). Only verified memories are used as reference when another
    # memory is checked (see server/review.py).
    review_status: Mapped[str] = mapped_column(
        Text, default=UNVERIFIED, server_default=UNVERIFIED, index=True
    )

    # The older memory this one reverses or replaces, set from the review
    # model's verdict and never from a request. Both memories stay; the old
    # one shows as superseded by this one. Cleared, not cascaded, when the
    # old memory is deleted.
    supersedes: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("memories.id", ondelete="SET NULL"), index=True
    )

    # When the memory was archived, or None while it is live. The review
    # archives a memory it rejects under rule 2 or 4, and the older memory
    # of a merge (see `repository.set_review`). An archived memory is left
    # out of every listing, search and count, and of the reference set,
    # unless a listing asks for `archived=true`; a read by id still returns
    # it. The review poll deletes it once it has been archived for
    # AGENT_MEMORY_ARCHIVE_DAYS days; `POST /memories/{id}/restore` clears it.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    tags: Mapped[list[Tag]] = relationship(
        secondary="memory_tags", back_populates="memories", order_by="Tag.name",
    )

    # Every verdict the review model gave on this memory, newest first, so
    # the first is the one a read reports as `review`. A re-review adds a
    # row and never rewrites one: the history stays whole.
    reviews: Mapped[list[MemoryReview]] = relationship(
        back_populates="memory", foreign_keys="MemoryReview.memory_id",
        order_by=lambda: (MemoryReview.created_at.desc(), MemoryReview.id.desc()),
        cascade="all, delete-orphan", passive_deletes=True,
    )

    @property
    def review(self) -> MemoryReview | None:
        """The newest verdict, or None when the memory has not been reviewed."""
        return self.reviews[0] if self.reviews else None

    # The memories whose `supersedes` points at this one, newest first, so
    # the first is the one a read reports as `superseded_by`. The database
    # clears the link when this memory is deleted (`passive_deletes`).
    superseded_by_rows: Mapped[list[Memory]] = relationship(
        back_populates="supersedes_memory", foreign_keys=[supersedes],
        order_by=lambda: (Memory.timestamp.desc(), Memory.id.desc()),
        passive_deletes=True,
    )
    supersedes_memory: Mapped[Memory | None] = relationship(
        back_populates="superseded_by_rows", foreign_keys=[supersedes], remote_side=[id],
    )

    __table_args__ = (
        Index("idx_content_tsv", "content_tsv", postgresql_using="gin"),
        CheckConstraint(REVIEW_STATUS_CHECK, name="ck_memories_review_status"),
    )


class MemoryReview(Base):
    """One verdict of the review model on one memory. A memory keeps every
    verdict it ever got, one row each; a re-review adds a row, and the newest
    (by `created_at`, then `id`) is the one a read shows. Advice only: the
    memory itself is never changed because of it."""

    __tablename__ = "memory_reviews"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    memory_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("memories.id", ondelete="CASCADE")
    )
    # approve, reject or rewrite (see server/review.py).
    verdict: Mapped[str] = mapped_column(Text)
    # The rule the entry breaks or falls short of; None for an approve.
    rule: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text)
    # Suggested text when the verdict is rewrite.
    rewrite: Mapped[str | None] = mapped_column(Text)
    # The memory this one repeats, when the verdict is a reject for that reason.
    # Cleared, not cascaded, when that memory is deleted.
    duplicate_of: Mapped[int | None] = mapped_column(
        ForeignKey("memories.id", ondelete="SET NULL")
    )
    # Tags suggested for the rewritten text, chosen from the tags that existed
    # when the review ran. NULL when the verdict suggested none.
    tags: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    # The model that gave the verdict.
    model: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    memory: Mapped[Memory] = relationship(back_populates="reviews", foreign_keys=[memory_id])

    # The newest row per memory is what every read and listing wants.
    __table_args__ = (
        Index("ix_memory_reviews_memory_id_created_at", "memory_id", desc(created_at)),
    )


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)

    # The vector of `name: description`, and the model that made it, so the
    # review can rank the tags on offer without embedding them again on
    # every call. Set when the tag is created or its description changes,
    # and by reindex for the rest. Internal, like the memory's vector.
    embedding: Mapped[list[float] | None] = mapped_column(ARRAY(REAL))
    embedding_model: Mapped[str | None] = mapped_column(Text)

    memories: Mapped[list[Memory]] = relationship(
        secondary="memory_tags", back_populates="tags",
    )

    # Case-insensitive uniqueness on the tag name.
    __table_args__ = (Index("idx_tags_lower_name", func.lower(name), unique=True),)


class MemoryTag(Base):
    __tablename__ = "memory_tags"

    memory_id: Mapped[int] = mapped_column(
        ForeignKey("memories.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(
        ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True, index=True
    )
