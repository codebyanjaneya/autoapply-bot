"""`/stats` — operator-only ops dashboard, gated on OPERATOR_CHAT_ID.

Same gate pattern as /reviews: silently no-op for anyone who isn't the
operator (no help text, not in the menu) so the command's existence stays
private. Aggregations run in a single async session; counts are cheap on
indexed columns and the dataset is small enough that we don't bother with
a materialised view.

"Today" is anchored to IST to match the pipeline's daily boundary.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import distinct, func, select

from core.db import get_session
from core.models import (
    Application,
    Job,
    OutreachLog,
    SubscriptionTier,
    User,
    UserReview,
    UserStatus,
)

log = logging.getLogger(__name__)
router = Router(name="stats")

_IST = ZoneInfo("Asia/Kolkata")
_UTC = ZoneInfo("UTC")


def _operator_chat_id() -> int | None:
    raw = os.environ.get("OPERATOR_CHAT_ID", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        log.error("OPERATOR_CHAT_ID is not an integer: %r", raw)
        return None


def _ist_today_utc_window() -> tuple[datetime, datetime]:
    """Return (start_utc, end_utc) for "today in IST" — IST midnight to
    next IST midnight, expressed in UTC for filtering timestamp columns.
    """
    now_ist = datetime.now(_IST)
    start_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    end_ist = start_ist + timedelta(days=1)
    return start_ist.astimezone(_UTC), end_ist.astimezone(_UTC)


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    op_id = _operator_chat_id()
    if op_id is None or message.from_user is None or message.from_user.id != op_id:
        # Pretend the command doesn't exist for non-operators.
        return

    today_start_utc, today_end_utc = _ist_today_utc_window()

    async with get_session() as session:
        # ---- Users ----
        total_users = (await session.execute(
            select(func.count(User.id))
        )).scalar_one() or 0
        active_users = (await session.execute(
            select(func.count(User.id)).where(User.status == UserStatus.active)
        )).scalar_one() or 0
        paid_users = (await session.execute(
            select(func.count(User.id)).where(User.subscription_tier == SubscriptionTier.paid)
        )).scalar_one() or 0
        free_users = (await session.execute(
            select(func.count(User.id)).where(User.subscription_tier == SubscriptionTier.free)
        )).scalar_one() or 0
        new_today = (await session.execute(
            select(func.count(User.id)).where(
                User.created_at >= today_start_utc,
                User.created_at < today_end_utc,
            )
        )).scalar_one() or 0

        # ---- Outreach (all-time, successful only) ----
        total_emails = (await session.execute(
            select(func.count(OutreachLog.id)).where(OutreachLog.error.is_(None))
        )).scalar_one() or 0

        # Unique companies = distinct jobs.company across successful sends.
        unique_companies = (await session.execute(
            select(func.count(distinct(Job.company)))
            .select_from(OutreachLog)
            .join(Application, Application.id == OutreachLog.application_id)
            .join(Job, Job.id == Application.job_id)
            .where(OutreachLog.error.is_(None))
        )).scalar_one() or 0

        # ---- Today (IST) ----
        users_ran_today = (await session.execute(
            select(func.count(distinct(OutreachLog.user_id))).where(
                OutreachLog.sent_at >= today_start_utc,
                OutreachLog.sent_at < today_end_utc,
            )
        )).scalar_one() or 0
        emails_today = (await session.execute(
            select(func.count(OutreachLog.id)).where(
                OutreachLog.sent_at >= today_start_utc,
                OutreachLog.sent_at < today_end_utc,
                OutreachLog.error.is_(None),
            )
        )).scalar_one() or 0

        # ---- Reviews ----
        review_count = (await session.execute(
            select(func.count(UserReview.user_id))
        )).scalar_one() or 0
        avg_rating = (await session.execute(select(func.avg(UserReview.rating)))).scalar() or 0

    avg_str = f"{float(avg_rating):.1f}" if review_count else "—"

    text = (
        "📊 <b>AutoApply Stats</b>\n\n"
        "👥 <b>Users</b>\n"
        f"• Total: <b>{total_users}</b>\n"
        f"• Active: <b>{active_users}</b>\n"
        f"• Paid: <b>{paid_users}</b>\n"
        f"• Free: <b>{free_users}</b>\n"
        f"• New today: <b>{new_today}</b>\n\n"
        "📧 <b>Outreach (all time)</b>\n"
        f"• Total emails sent: <b>{total_emails}</b>\n"
        f"• Unique companies contacted: <b>{unique_companies}</b>\n\n"
        "📅 <b>Today</b>\n"
        f"• Users ran today: <b>{users_ran_today}</b>\n"
        f"• Emails sent today: <b>{emails_today}</b>\n\n"
        "⭐ <b>Reviews</b>\n"
        f"• Total: <b>{review_count}</b> — Average: <b>{avg_str}/5</b>"
    )
    await message.answer(text)
