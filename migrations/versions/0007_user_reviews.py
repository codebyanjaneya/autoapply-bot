"""user_reviews: 1-row-per-user ratings + comments (/review).

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-02

Why: we want a quick warm feedback loop (1-5 stars + optional text) so users
feel heard and we get a steady drip of testimonial-grade quotes. Storing in
DB (vs Telegram-only forward) means we can compute average rating, paginate
in /reviews for the operator, and dedupe by user.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_reviews",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id", sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False, unique=True,
        ),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.CheckConstraint("rating BETWEEN 1 AND 5", name="ck_user_reviews_rating_range"),
    )


def downgrade() -> None:
    op.drop_table("user_reviews")
