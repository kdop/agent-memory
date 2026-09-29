"""Add the tag review: tags.review_status and the tag_reviews table.

`tags.review_status TEXT NOT NULL DEFAULT 'unverified'`, with a CHECK on the
three values and an index, says whether the review model has checked the
tag. Existing tags start unverified, so the next catch-up reads them all.

`tag_reviews` holds the model's merge, rename and drop verdicts: a merge the
server applied on its own (`resolved = 'applied'`), and proposals that wait
for a person (`resolved` NULL until applied or rejected). `tag_id` is
cleared when the tag goes, and `tag_name` keeps its name.

Revision ID: c6b1d8f3a2e7
Revises: a7d3e9b1c5f2
Create Date: 2026-09-29 18:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c6b1d8f3a2e7'
down_revision: Union[str, Sequence[str], None] = 'a7d3e9b1c5f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the column, its CHECK and index, and the table. Every existing tag
    is unverified."""
    op.add_column('tags', sa.Column('review_status', sa.Text(), server_default='unverified',
                                    nullable=False))
    op.create_check_constraint('ck_tags_review_status', 'tags',
                               "review_status IN ('unverified', 'verified', 'flagged')")
    op.create_index(op.f('ix_tags_review_status'), 'tags', ['review_status'], unique=False)
    op.create_table('tag_reviews',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('tag_id', sa.BigInteger(), nullable=True),
    sa.Column('tag_name', sa.Text(), nullable=False),
    sa.Column('verdict', sa.Text(), nullable=False),
    sa.Column('into', sa.Text(), nullable=True),
    sa.Column('new_name', sa.Text(), nullable=True),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('model', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
              nullable=False),
    sa.Column('resolved', sa.Text(), nullable=True),
    sa.CheckConstraint("resolved IS NULL OR resolved IN ('applied', 'rejected')",
                       name='ck_tag_reviews_resolved'),
    sa.ForeignKeyConstraint(['tag_id'], ['tags.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_tag_reviews_tag_id'), 'tag_reviews', ['tag_id'], unique=False)


def downgrade() -> None:
    """Drop the table, the index, the CHECK and the column. Every stored
    proposal and every tag's status are lost; the tags stay."""
    op.drop_index(op.f('ix_tag_reviews_tag_id'), table_name='tag_reviews')
    op.drop_table('tag_reviews')
    op.drop_index(op.f('ix_tags_review_status'), table_name='tags')
    op.drop_constraint('ck_tags_review_status', 'tags', type_='check')
    op.drop_column('tags', 'review_status')
