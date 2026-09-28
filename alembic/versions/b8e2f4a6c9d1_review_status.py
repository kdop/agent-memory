"""Add the review_status column to memories.

One column, `review_status TEXT NOT NULL DEFAULT 'unverified'`, with a CHECK
on the three values `unverified`, `verified` and `flagged`, and an index, so
every memory says whether the review model has checked it without a lookup
in memory_reviews. Existing rows are filled from their review row: an
approve gives `verified`, a reject or rewrite gives `flagged`, no row leaves
`unverified`. From here on only verified memories serve as reference when
another memory is checked.

Revision ID: b8e2f4a6c9d1
Revises: 7c3e1a9d5b20
Create Date: 2026-09-28 20:05:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b8e2f4a6c9d1'
down_revision: Union[str, Sequence[str], None] = '7c3e1a9d5b20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the column, the CHECK and the index, then fill existing rows from
    their review row. A memory with no row stays `unverified`."""
    op.add_column('memories', sa.Column('review_status', sa.Text(), server_default='unverified',
                                        nullable=False))
    op.create_check_constraint('ck_memories_review_status', 'memories',
                               "review_status IN ('unverified', 'verified', 'flagged')")
    op.create_index(op.f('ix_memories_review_status'), 'memories', ['review_status'], unique=False)
    op.execute(
        "UPDATE memories SET review_status = 'verified' "
        "FROM memory_reviews r WHERE r.memory_id = memories.id AND r.verdict = 'approve'")
    op.execute(
        "UPDATE memories SET review_status = 'flagged' "
        "FROM memory_reviews r WHERE r.memory_id = memories.id "
        "AND r.verdict IN ('reject', 'rewrite')")


def downgrade() -> None:
    """Drop the index, the CHECK and the column. The verdicts in memory_reviews
    stay, so an upgrade fills the column again."""
    op.drop_index(op.f('ix_memories_review_status'), table_name='memories')
    op.drop_constraint('ck_memories_review_status', 'memories', type_='check')
    op.drop_column('memories', 'review_status')
