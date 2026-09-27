"""Add embedding columns to memories.

Two nullable columns that hold a meaning vector per memory for semantic search:

- `embedding REAL[]`: the vector itself, as a plain float array.
- `embedding_model TEXT`: the name of the model that made it, so a vector from an
  old model can be told apart from a new one.

No pgvector and no index. Nothing is installed on the database host, and the table
is small enough to scan in full. Both columns are internal: the API never returns
them.

Revision ID: 62fcc84d6c84
Revises: 310017671d9e
Create Date: 2026-09-27 21:55:31.580081

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '62fcc84d6c84'
down_revision: Union[str, Sequence[str], None] = '310017671d9e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the two nullable columns. Old rows keep NULL until a vector is made."""
    op.add_column('memories', sa.Column('embedding', postgresql.ARRAY(sa.REAL()), nullable=True))
    op.add_column('memories', sa.Column('embedding_model', sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop both columns. Any stored vectors are lost."""
    op.drop_column('memories', 'embedding_model')
    op.drop_column('memories', 'embedding')
