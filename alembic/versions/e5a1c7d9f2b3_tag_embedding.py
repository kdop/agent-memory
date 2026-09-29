"""Add embedding columns to tags.

Two nullable columns, the same pair `memories` has:

- `embedding REAL[]`: the vector of the tag's `name: description`.
- `embedding_model TEXT`: the name of the model that made it.

The review offers the model the ten tags closest in meaning to the entry.
Before this, every review embedded every tag in use again; now the vector
is stored once, when the tag is created or its description changes, and
reindex fills the tags that have none. Existing rows get NULL and the next
reindex (the server runs one at start) fills them.

Revision ID: e5a1c7d9f2b3
Revises: d3f9a7c2e6b4
Create Date: 2026-09-28 23:30:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'e5a1c7d9f2b3'
down_revision: Union[str, Sequence[str], None] = 'd3f9a7c2e6b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the two nullable columns. Existing tags keep NULL until reindex runs."""
    op.add_column('tags', sa.Column('embedding', postgresql.ARRAY(sa.REAL()), nullable=True))
    op.add_column('tags', sa.Column('embedding_model', sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop both columns. The tag vectors are lost; the tags stay."""
    op.drop_column('tags', 'embedding_model')
    op.drop_column('tags', 'embedding')
