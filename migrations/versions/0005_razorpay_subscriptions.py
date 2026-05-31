"""razorpay payment links: extend subscriptions + add payment_events

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-01

MVP billing rail = Razorpay Payment Links (one-shot Rs.500 charges that
extend `current_period_end` by 30 days on `payment_link.paid` webhook).

Schema changes:
  - subscriptions.stripe_customer_id  -> nullable (legacy; unused now)
  - subscriptions: add razorpay_{payment_link,payment,order}_id, current_period_start
  - subscriptions.current_period_end gets an index (cron query: WHERE
    current_period_end BETWEEN now+2d AND now+3d for reminders, and
    WHERE current_period_end < now() for daily expiry sweep)
  - payment_events: new table; idempotency guard for Razorpay webhooks
    (UNIQUE on razorpay_event_id is the lock point, same pattern as
    stripe_events).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- subscriptions: relax stripe_customer_id + add Razorpay columns ---
    op.alter_column("subscriptions", "stripe_customer_id", nullable=True)

    op.add_column("subscriptions", sa.Column("razorpay_payment_link_id", sa.String(64)))
    op.add_column("subscriptions", sa.Column("razorpay_payment_id", sa.String(64)))
    op.add_column("subscriptions", sa.Column("razorpay_order_id", sa.String(64)))
    op.add_column("subscriptions", sa.Column("current_period_start", sa.DateTime(timezone=True)))

    op.create_index(
        "ix_subscriptions_razorpay_payment_link_id",
        "subscriptions",
        ["razorpay_payment_link_id"],
    )
    op.create_index(
        "ix_subscriptions_current_period_end",
        "subscriptions",
        ["current_period_end"],
    )

    # --- payment_events: idempotency guard for Razorpay webhooks ---
    op.create_table(
        "payment_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("razorpay_event_id", sa.String(64), nullable=False, unique=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column(
            "user_id", sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("payload_json", postgresql.JSONB, nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("processing_error", sa.Text()),
    )
    op.create_index("ix_payment_events_event_type", "payment_events", ["event_type"])
    op.create_index("ix_payment_events_user_id", "payment_events", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_payment_events_user_id", table_name="payment_events")
    op.drop_index("ix_payment_events_event_type", table_name="payment_events")
    op.drop_table("payment_events")

    op.drop_index("ix_subscriptions_current_period_end", table_name="subscriptions")
    op.drop_index("ix_subscriptions_razorpay_payment_link_id", table_name="subscriptions")
    op.drop_column("subscriptions", "current_period_start")
    op.drop_column("subscriptions", "razorpay_order_id")
    op.drop_column("subscriptions", "razorpay_payment_id")
    op.drop_column("subscriptions", "razorpay_payment_link_id")

    # Restoring NOT NULL would fail if any row has null; leave nullable on downgrade.
    # op.alter_column("subscriptions", "stripe_customer_id", nullable=False)
