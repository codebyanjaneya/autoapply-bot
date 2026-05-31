"""add user_credentials.apollo_api_key_encrypted

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-31

Hunter.io requires a work email at signup, which most of our target
users (job-seekers using personal Gmail) don't have. We pivot to Apollo
as the user-supplied recruiter-lookup provider \u2014 Apollo accepts Gmail
signups and gives ~50 free credits/month.

Hunter stays as an operator-only pooled key (HUNTER_API_KEY env), tried
FIRST in :class:`RecruiterFinder` to preserve each user's personal Apollo
quota. Apollo is fallback when Hunter pool returns nothing or 429s.

The legacy ``hunter_api_key_encrypted`` column is intentionally left in
place \u2014 some early testers may have keys stored. It's no longer read or
written by application code; cleanup can happen in a later migration
once those rows are confirmed obsolete.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "user_credentials",
        sa.Column("apollo_api_key_encrypted", sa.LargeBinary(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_credentials", "apollo_api_key_encrypted")
