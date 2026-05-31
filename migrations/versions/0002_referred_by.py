"""add users.referred_by_user_id

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-31

Adds a nullable self-referencing FK on ``users`` used by the referral
program. Set once during onboarding when a new user runs
``/start AA<code>``; never mutated afterwards.

ON DELETE SET NULL so removing a referrer doesn't cascade-delete the
people they invited (their accounts are still valid; we just lose the
"who invited them" attribution).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("referred_by_user_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_users_referred_by_user_id",
        "users", "users",
        ["referred_by_user_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_users_referred_by_user_id", "users", ["referred_by_user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_users_referred_by_user_id", table_name="users")
    op.drop_constraint("fk_users_referred_by_user_id", "users", type_="foreignkey")
    op.drop_column("users", "referred_by_user_id")
