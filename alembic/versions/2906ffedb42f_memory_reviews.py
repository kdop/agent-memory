"""Add the memory_reviews table.

One row per memory with what the review model said about it: the verdict
(approve, reject or rewrite), the rule it breaks, one sentence of reason, a
suggested rewrite, the memory it repeats, the model's name and when. The row
goes with its memory (ON DELETE CASCADE); `duplicate_of` is cleared when the
memory it points at is deleted (ON DELETE SET NULL).

Revision ID: 2906ffedb42f
Revises: 62fcc84d6c84
Create Date: 2026-09-28 14:46:41

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '2906ffedb42f'
down_revision: Union[str, Sequence[str], None] = '62fcc84d6c84'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the table. Existing memories have no row until they are reviewed."""
    op.create_table('memory_reviews',
    sa.Column('memory_id', sa.BigInteger(), nullable=False),
    sa.Column('verdict', sa.Text(), nullable=False),
    sa.Column('rule', sa.Integer(), nullable=True),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('rewrite', sa.Text(), nullable=True),
    sa.Column('duplicate_of', sa.BigInteger(), nullable=True),
    sa.Column('model', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['duplicate_of'], ['memories.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['memory_id'], ['memories.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('memory_id')
    )


def downgrade() -> None:
    """Drop the table. Every stored verdict is lost; the memories stay."""
    op.drop_table('memory_reviews')
