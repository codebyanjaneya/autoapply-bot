"""add user_credentials.email_template

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-03

Lets each user override the default outreach email body with their own
copy. Stored as nullable Text \u2014 NULL means "use the built-in template".
Rendered via str.format() with whitelisted placeholders ({candidate_name},
{role}, {company}) in core/outreach.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "user_credentials",
        sa.Column("email_template", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_credentials", "email_template")
