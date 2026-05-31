"""add user_preferences.preferred_run_hour

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-31

Per-user daily run time. Stored as hour-of-day (0-23) in Asia/Kolkata.
The scheduler runs CronTrigger(minute=0, tz='Asia/Kolkata') every hour
and selects only users whose `preferred_run_hour` matches the current
IST hour. Default 9 = 09:00 IST, matching the previous global fan-out
so existing users see zero behavior change.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "user_preferences",
        sa.Column(
            "preferred_run_hour",
            sa.SmallInteger(),
            nullable=False,
            server_default="9",
        ),
    )
    op.create_check_constraint(
        "ck_user_preferences_preferred_run_hour_range",
        "user_preferences",
        "preferred_run_hour >= 0 AND preferred_run_hour <= 23",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_user_preferences_preferred_run_hour_range",
        "user_preferences",
        type_="check",
    )
    op.drop_column("user_preferences", "preferred_run_hour")
