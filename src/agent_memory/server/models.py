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
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


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

    tags: Mapped[list[Tag]] = relationship(
        secondary="memory_tags", back_populates="memories", order_by="Tag.name",
    )

    __table_args__ = (Index("idx_content_tsv", "content_tsv", postgresql_using="gin"),)


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)

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
