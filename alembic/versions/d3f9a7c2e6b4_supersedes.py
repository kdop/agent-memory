"""Add the supersedes column to memories.

One nullable column, `supersedes BIGINT REFERENCES memories(id) ON DELETE
SET NULL`, with an index: the id of the older memory this one reverses or
replaces, set from the review model's verdict when it says so and never
from a request. Both memories stay in the timeline; the old one is read as
superseded by the new one, and `current=true` on a query or search hides
it. Deleting the old memory clears the link on the new one. Existing rows
get NULL: nothing is linked until the model reviews a new memory.

Revision ID: d3f9a7c2e6b4
Revises: b8e2f4a6c9d1
Create Date: 2026-09-28 22:40:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd3f9a7c2e6b4'
down_revision: Union[str, Sequence[str], None] = 'b8e2f4a6c9d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the column, the foreign key onto memories itself and the index.
    Existing rows keep NULL."""
    op.add_column('memories', sa.Column('supersedes', sa.BigInteger(), nullable=True))
    op.create_foreign_key('fk_memories_supersedes', 'memories', 'memories',
                          ['supersedes'], ['id'], ondelete='SET NULL')
    op.create_index(op.f('ix_memories_supersedes'), 'memories', ['supersedes'], unique=False)


def downgrade() -> None:
    """Drop the index, the foreign key and the column. The links are lost;
    the memories and their reviews stay."""
    op.drop_index(op.f('ix_memories_supersedes'), table_name='memories')
    op.drop_constraint('fk_memories_supersedes', 'memories', type_='foreignkey')
    op.drop_column('memories', 'supersedes')
