"""Add the tags column to memory_reviews.

One nullable TEXT[] column: the tags the review model suggests for a
rewritten entry, chosen from the tags that existed when the review ran.
NULL for a verdict that suggests none (every approve and reject, and a
rewrite the model gave no tags for). Advice only, like the rest of the row:
nothing is applied to the memory's own tags.

Revision ID: 7c3e1a9d5b20
Revises: 2906ffedb42f
Create Date: 2026-09-28 18:12:05

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '7c3e1a9d5b20'
down_revision: Union[str, Sequence[str], None] = '2906ffedb42f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the nullable column. Existing reviews keep NULL until re-reviewed."""
    op.add_column('memory_reviews', sa.Column('tags', postgresql.ARRAY(sa.Text()), nullable=True))


def downgrade() -> None:
    """Drop the column. Suggested tags are lost; the verdicts stay."""
    op.drop_column('memory_reviews', 'tags')
