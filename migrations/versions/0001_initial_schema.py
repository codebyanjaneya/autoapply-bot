"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-05-30

Creates 10 tables + 5 PostgreSQL ENUM types. Hand-written, reviewed.

Order matters:
  1. CREATE TYPE for all enums (so column defs can reference them).
  2. CREATE TABLE in FK dependency order (users first, then anything that
     references users, then anything that references those, ...).
  3. Indexes created explicitly after each table (faster than letting
     CREATE TABLE inline them, and easier to drop in downgrade).

The downgrade() reverses exactly: drop tables in reverse, then drop enum
types last (DROP TYPE fails if a column still references the type).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# --- Alembic identifiers ---
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# --- ENUM type handles. create_type=False on columns so the ENUM is created
#     exactly once below, not implicitly per-column. ---
SUBSCRIPTION_TIER = postgresql.ENUM(
    "free", "paid",
    name="subscription_tier", create_type=False,
)
USER_STATUS = postgresql.ENUM(
    "onboarding", "active", "paused", "unsubscribed",
    name="user_status", create_type=False,
)
JOB_SOURCE = postgresql.ENUM(
    "adzuna", "jsearch",
    name="job_source", create_type=False,
)
APP_STATUS = postgresql.ENUM(
    "new", "scored", "skipped", "outreach", "sent",
    "viewed", "replied", "rejected", "error",
    name="app_status", create_type=False,
)
SUBSCRIPTION_STATUS = postgresql.ENUM(
    "incomplete", "active", "past_due", "cancelled",
    name="subscription_status", create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Enum types first
    SUBSCRIPTION_TIER.create(bind, checkfirst=False)
    USER_STATUS.create(bind, checkfirst=False)
    JOB_SOURCE.create(bind, checkfirst=False)
    APP_STATUS.create(bind, checkfirst=False)
    SUBSCRIPTION_STATUS.create(bind, checkfirst=False)

    # 2. users (root of FK graph)
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(64)),
        sa.Column("first_name", sa.String(128)),
        sa.Column("language_code", sa.String(8)),
        sa.Column("subscription_tier", SUBSCRIPTION_TIER, nullable=False, server_default="free"),
        sa.Column("status", USER_STATUS, nullable=False, server_default="onboarding"),
        sa.Column("daily_outreach_limit", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("daily_scan_limit", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_users_telegram_chat_id", "users", ["telegram_chat_id"])
    op.create_index("ix_users_subscription_tier", "users", ["subscription_tier"])
    op.create_index("ix_users_status", "users", ["status"])

    # 3. user_preferences (1:1 with users)
    op.create_table(
        "user_preferences",
        sa.Column(
            "user_id", sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("role_keywords", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("locations", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("skills", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("min_score", sa.Integer(), nullable=False, server_default="75"),
    )

    # 4. user_credentials (1:1 with users; encrypted columns are bytea)
    op.create_table(
        "user_credentials",
        sa.Column(
            "user_id", sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("smtp_email", sa.String(255)),
        sa.Column("smtp_password_encrypted", sa.LargeBinary()),
        sa.Column("hunter_api_key_encrypted", sa.LargeBinary()),
        sa.Column("resume_pdf", sa.LargeBinary()),
        sa.Column("resume_filename", sa.String(255)),
        sa.Column("resume_uploaded_at", sa.DateTime(timezone=True)),
        sa.Column("candidate_name", sa.String(128)),
    )

    # 5. subscriptions (1:1 with users)
    op.create_table(
        "subscriptions",
        sa.Column(
            "user_id", sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("stripe_customer_id", sa.String(64), nullable=False, unique=True),
        sa.Column("stripe_subscription_id", sa.String(64), unique=True),
        sa.Column("status", SUBSCRIPTION_STATUS, nullable=False, server_default="incomplete"),
        sa.Column("current_period_end", sa.DateTime(timezone=True)),
        sa.Column("plan_price_id", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_subscriptions_status", "subscriptions", ["status"])

    # 6. jobs (per-user; dedup via (user_id, source, external_id))
    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id", sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", JOB_SOURCE, nullable=False),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("company", sa.String(256), nullable=False),
        sa.Column("location", sa.String(256), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("salary_min", sa.Float()),
        sa.Column("salary_max", sa.Float()),
        sa.Column("salary_currency", sa.String(3)),
        sa.Column("salary_is_predicted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("posted_at", sa.DateTime(timezone=True)),
        sa.Column("scraped_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "source", "external_id", name="uq_jobs_user_source_external"),
    )
    op.create_index("ix_jobs_user_id", "jobs", ["user_id"])
    op.create_index("ix_jobs_source", "jobs", ["source"])
    op.create_index("ix_jobs_company", "jobs", ["company"])
    op.create_index("ix_jobs_scraped_at", "jobs", ["scraped_at"])

    # 7. applications (per-user x per-job)
    op.create_table(
        "applications",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id", sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "job_id", sa.BigInteger(),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("score_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", APP_STATUS, nullable=False, server_default="new"),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("recruiter_email", sa.String(255)),
        sa.Column("recruiter_source", sa.String(32)),
        sa.Column("reply_snippet", sa.Text()),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.UniqueConstraint("user_id", "job_id", name="uq_applications_user_job"),
    )
    op.create_index("ix_applications_user_id", "applications", ["user_id"])
    op.create_index("ix_applications_job_id", "applications", ["job_id"])
    op.create_index("ix_applications_status", "applications", ["status"])

    # 8. outreach_logs (per send attempt)
    op.create_table(
        "outreach_logs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id", sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "application_id", sa.BigInteger(),
            sa.ForeignKey("applications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("to_email", sa.String(255), nullable=False),
        sa.Column("subject", sa.String(512), nullable=False),
        sa.Column("error", sa.Text()),
    )
    op.create_index("ix_outreach_logs_user_id", "outreach_logs", ["user_id"])
    op.create_index("ix_outreach_logs_application_id", "outreach_logs", ["application_id"])

    # 9. daily_run_summary (per-user-per-day reporting)
    op.create_table(
        "daily_run_summary",
        sa.Column(
            "user_id", sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("run_date", sa.Date(), primary_key=True),
        sa.Column("jobs_scraped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("jobs_scored", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("outreach_sent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("outreach_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("replies_received", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text()),
    )

    # 10. rate_limit_counters (atomic per-user-per-day-per-action counter)
    op.create_table(
        "rate_limit_counters",
        sa.Column(
            "user_id", sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("action", sa.String(32), primary_key=True),
        sa.Column("period_date", sa.Date(), primary_key=True),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_rate_limit_counters_period_date", "rate_limit_counters", ["period_date"])

    # 11. stripe_events (webhook idempotency \u2014 no FK to users; event arrives
    #     before customer/subscription is linked in some flows)
    op.create_table(
        "stripe_events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("payload", sa.Text()),
    )
    op.create_index("ix_stripe_events_event_type", "stripe_events", ["event_type"])


def downgrade() -> None:
    # Reverse order: drop tables that have FKs first, then their targets.
    op.drop_table("stripe_events")
    op.drop_table("rate_limit_counters")
    op.drop_table("daily_run_summary")
    op.drop_table("outreach_logs")
    op.drop_table("applications")
    op.drop_table("jobs")
    op.drop_table("subscriptions")
    op.drop_table("user_credentials")
    op.drop_table("user_preferences")
    op.drop_table("users")

    # Enum types last \u2014 DROP TYPE fails while any column references it.
    bind = op.get_bind()
    SUBSCRIPTION_STATUS.drop(bind, checkfirst=False)
    APP_STATUS.drop(bind, checkfirst=False)
    JOB_SOURCE.drop(bind, checkfirst=False)
    USER_STATUS.drop(bind, checkfirst=False)
    SUBSCRIPTION_TIER.drop(bind, checkfirst=False)
