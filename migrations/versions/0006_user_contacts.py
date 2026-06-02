"""user_contacts: directly-supplied recruiter emails (/add_contacts).

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-02

Why: free-tier users blow through Hunter's 25-lookups/month quickly. Many
already know recruiters at their target companies (LinkedIn DMs, friends-of-
friends). Letting them paste those emails directly:
  - Skips Hunter entirely for those companies (saves quota)
  - Increases reply rate (warm intro vs cold cold)
  - Costs us nothing extra

Schema:
  - user_id, email mandatory; (user_id, email) is unique so re-pasting is
    idempotent.
  - company / name optional. company is used by the pipeline to match
    against scraped jobs (case-insensitive); name is just shown in /contacts.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_contacts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id", sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("company", sa.String(256), nullable=True),
        sa.Column("name", sa.String(128), nullable=True),
        sa.Column(
            "added_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.UniqueConstraint("user_id", "email", name="uq_user_contacts_user_email"),
    )
    op.create_index("ix_user_contacts_user_id", "user_contacts", ["user_id"])
    # The pipeline looks up matches via lower(company) so the company column
    # gets a functional index for the hot path.
    op.create_index(
        "ix_user_contacts_user_company_lower",
        "user_contacts",
        ["user_id", sa.text("lower(company)")],
    )


def downgrade() -> None:
    op.drop_index("ix_user_contacts_user_company_lower", table_name="user_contacts")
    op.drop_index("ix_user_contacts_user_id", table_name="user_contacts")
    op.drop_table("user_contacts")
