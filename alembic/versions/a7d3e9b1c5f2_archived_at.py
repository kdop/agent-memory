"""Add the archived_at column to memories.

One nullable column, `archived_at TIMESTAMPTZ`, with an index: when the
review archived the memory (a reject under rule 2 or 4, or the older memory
of a merge), NULL while it is live. An archived memory is hidden from every
listing and from the reference set until it is restored, and the review
poll deletes it once it has been archived for AGENT_MEMORY_ARCHIVE_DAYS
days. Existing rows get NULL: nothing is archived until the model says so.

Revision ID: a7d3e9b1c5f2
Revises: f4c2a8e6d1b9
Create Date: 2026-09-29 10:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a7d3e9b1c5f2'
down_revision: Union[str, Sequence[str], None] = 'f4c2a8e6d1b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the column and its index. Existing rows keep NULL."""
    op.add_column('memories', sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f('ix_memories_archived_at'), 'memories', ['archived_at'], unique=False)


def downgrade() -> None:
    """Drop the index and the column. Every archived memory is live again;
    none is deleted."""
    op.drop_index(op.f('ix_memories_archived_at'), table_name='memories')
    op.drop_column('memories', 'archived_at')
