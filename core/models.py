"""Database models for the multi-tenant AutoApply Bot.

Design rules:
1. Every business table has `user_id BIGINT NOT NULL` as the first column
   after the primary key, and `user_id` is indexed.
2. `User.id` IS the Telegram user ID (BigInteger). No surrogate keys for users
   \u2014 Telegram's IDs are stable and globally unique.
3. Sensitive credentials (SMTP password, optional Hunter key) are stored as
   `LargeBinary` columns containing Fernet ciphertext. Plaintext NEVER lands
   in this schema. See `core/crypto.py`.
4. Resume PDFs live in `UserCredentials.resume_pdf` (bytea) for MVP simplicity.
   Move to object storage (R2 / S3) when median > 5MB or total > 1GB.
5. Compound uniqueness `(user_id, source, external_id)` on jobs: the same
   listing scraped for two different users counts as two rows. Each user's
   pipeline is independent \u2014 no cross-user dedup.
"""
from __future__ import annotations

import enum
from datetime import datetime, date

from sqlalchemy import (
    BigInteger, Boolean, Date, DateTime, Enum, Float, ForeignKey, Index, Integer,
    LargeBinary, String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ---------- enums ----------
class SubscriptionTier(str, enum.Enum):
    """Source of truth for what a user is *allowed* to do.

    `users.subscription_tier` is updated by the Stripe webhook handler when a
    subscription transitions to `active` or `cancelled`. The pipeline reads
    this to apply daily limits stored on `user_preferences`.
    """
    free = "free"
    paid = "paid"


class UserStatus(str, enum.Enum):
    onboarding = "onboarding"   # in /start wizard, not yet complete
    active = "active"           # full pipeline runs daily
    paused = "paused"           # /pause \u2014 retained but skipped
    unsubscribed = "unsubscribed"  # /unsubscribe \u2014 deleted on next GC


class JobSource(str, enum.Enum):
    adzuna = "adzuna"
    jsearch = "jsearch"


class AppStatus(str, enum.Enum):
    new = "new"
    scored = "scored"
    skipped = "skipped"
    outreach = "outreach"   # queued for cold email
    sent = "sent"           # email dispatched (replaces "applied" \u2014 we're not auto-applying anymore)
    viewed = "viewed"
    replied = "replied"
    rejected = "rejected"
    error = "error"


class SubscriptionStatus(str, enum.Enum):
    incomplete = "incomplete"
    active = "active"
    past_due = "past_due"
    cancelled = "cancelled"


# ---------- users ----------
class User(Base):
    __tablename__ = "users"

    # Telegram user ID (natural primary key; 64-bit because IDs > 2^31 exist)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    language_code: Mapped[str | None] = mapped_column(String(8))

    subscription_tier: Mapped[SubscriptionTier] = mapped_column(
        Enum(SubscriptionTier, name="subscription_tier"),
        default=SubscriptionTier.free, nullable=False, index=True,
    )
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus, name="user_status"), default=UserStatus.onboarding, nullable=False, index=True,
    )

    # Daily quotas \u2014 derived from `subscription_tier` by the Stripe webhook
    # handler. Stored here (not on preferences) so a tier change is a single
    # UPDATE on `users`.
    #   free:  5 outreach / 20 scans per day
    #   paid: 15 outreach / 50 scans per day
    daily_outreach_limit: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    daily_scan_limit: Mapped[int] = mapped_column(Integer, default=20, nullable=False)

    # Referrals: nullable FK to the User who invited this account. Set once
    # during onboarding when the user starts via `/start AA<code>`; never
    # mutated afterwards. Used by core/referrals.count_referrals to compute
    # "people you've invited" and (post-Stripe) grant free months.
    referred_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True,
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )

    # 1:1 relationships
    preferences: Mapped["UserPreferences"] = relationship(back_populates="user", uselist=False, cascade="all, delete-orphan")
    credentials: Mapped["UserCredentials"] = relationship(back_populates="user", uselist=False, cascade="all, delete-orphan")
    subscription: Mapped["Subscription | None"] = relationship(back_populates="user", uselist=False, cascade="all, delete-orphan")


class UserPreferences(Base):
    __tablename__ = "user_preferences"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True,
    )
    role_keywords: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    locations: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    skills: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)

    min_score: Mapped[int] = mapped_column(Integer, default=75, nullable=False)

    user: Mapped[User] = relationship(back_populates="preferences")


class UserCredentials(Base):
    """Encrypted user-provided secrets + resume blob.

    `*_encrypted` columns hold Fernet ciphertext. Decrypt only at use-site.
    """
    __tablename__ = "user_credentials"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True,
    )
    smtp_email: Mapped[str | None] = mapped_column(String(255))
    smtp_password_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    # Legacy \u2014 Hunter is now operator-pool-only (HUNTER_API_KEY env). Left
    # nullable for backwards compatibility with rows from early testers; no
    # code reads or writes this column anymore. See migration 0003 notes.
    hunter_api_key_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    # User-supplied Apollo key (Apollo accepts Gmail signups; Hunter doesn't).
    # ~50 lookups/month on Apollo free tier. Used as fallback when the
    # operator's Hunter pool returns nothing or its monthly cap is hit.
    apollo_api_key_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)

    resume_pdf: Mapped[bytes | None] = mapped_column(LargeBinary)
    resume_filename: Mapped[str | None] = mapped_column(String(255))
    resume_uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    candidate_name: Mapped[str | None] = mapped_column(String(128))

    user: Mapped[User] = relationship(back_populates="credentials")


class Subscription(Base):
    """Stripe billing state. Source of truth for `User.plan` upgrades."""
    __tablename__ = "subscriptions"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True,
    )
    stripe_customer_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus, name="subscription_status"),
        default=SubscriptionStatus.incomplete, nullable=False, index=True,
    )
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    plan_price_id: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )

    user: Mapped[User] = relationship(back_populates="subscription")


# ---------- pipeline state ----------
class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    source: Mapped[JobSource] = mapped_column(Enum(JobSource, name="job_source"), nullable=False, index=True)
    # Job board's native ID \u2014 used with `source` for dedup within a user
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)

    title: Mapped[str] = mapped_column(String(512), nullable=False)
    company: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    location: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)

    salary_min: Mapped[float | None] = mapped_column(Float)
    salary_max: Mapped[float | None] = mapped_column(Float)
    salary_currency: Mapped[str | None] = mapped_column(String(3))
    salary_is_predicted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scraped_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True,
    )

    __table_args__ = (
        UniqueConstraint("user_id", "source", "external_id", name="uq_jobs_user_source_external"),
    )


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    job_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True,
    )

    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    score_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[AppStatus] = mapped_column(
        Enum(AppStatus, name="app_status"), default=AppStatus.new, nullable=False, index=True,
    )

    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recruiter_email: Mapped[str | None] = mapped_column(String(255))
    recruiter_source: Mapped[str | None] = mapped_column(String(32))  # 'hunter' | 'apollo' | 'cache'
    reply_snippet: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "job_id", name="uq_applications_user_job"),
    )


class OutreachLog(Base):
    """Per-send audit log. One row per SMTP attempt (success or failure).
    Useful for debugging delivery issues and per-user analytics.
    """
    __tablename__ = "outreach_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    application_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    to_email: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)


class DailyRunSummary(Base):
    """Denormalised per-user-per-day counters. Written by the worker after
    each pipeline run. Read by the /status command for fast responses without
    aggregating across `applications` every time.
    """
    __tablename__ = "daily_run_summary"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True,
    )
    run_date: Mapped[date] = mapped_column(Date, primary_key=True)

    jobs_scraped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    jobs_scored: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    outreach_sent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    outreach_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    replies_received: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)  # set if the pipeline crashed for this user


# ---------- rate limiting ----------
class RateLimitCounter(Base):
    """Per-user-per-day-per-action counter for enforcing tier limits.

    Why a separate table from `DailyRunSummary`?
    - `DailyRunSummary` is written ONCE at the end of a pipeline run for
      reporting. It's not safe to read mid-run.
    - `RateLimitCounter` is incremented LIVE as each action happens (every
      outreach email, every scan call). The pipeline checks this before
      every action: `if counter.count >= limit: stop`.
    - Use `INSERT ... ON CONFLICT (user_id, action, period_date) DO UPDATE
      SET count = rate_limit_counters.count + 1` to make increments atomic
      and concurrency-safe across multiple workers.

    `action` values: 'outreach', 'scan'. Add more as needed (e.g. 'followup').
    `period_date` is the UTC date. Keep all rate-limit logic in UTC and
    convert for display only.
    """
    __tablename__ = "rate_limit_counters"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True,
    )
    action: Mapped[str] = mapped_column(String(32), primary_key=True)
    period_date: Mapped[date] = mapped_column(Date, primary_key=True)

    count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )

    # Supports the GC job that prunes counters older than 90 days
    __table_args__ = (
        Index("ix_rate_limit_counters_period_date", "period_date"),
    )


# ---------- stripe webhook replay protection ----------
class StripeEvent(Base):
    """Idempotency guard for Stripe webhooks.

    Stripe retries webhooks aggressively. Without this table, a retried
    `invoice.payment_succeeded` could double-extend a subscription, and a
    retried `customer.subscription.deleted` is wasteful work at best.

    Webhook handler MUST:
      1. BEGIN transaction
      2. INSERT into stripe_events (event_id) -- raises on duplicate PK
      3. Process the event (update Subscription, User.subscription_tier, ...)
      4. UPDATE stripe_events SET processed_at = now()
      5. COMMIT

    The primary key on `event_id` makes step 2 the lock point. If two workers
    receive the same retry, only one commits; the other rolls back and the
    handler returns 200 OK to Stripe (idempotent success).
    """
    __tablename__ = "stripe_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Raw payload for debugging; drop after 30 days via GC.
    payload: Mapped[str | None] = mapped_column(Text)
