"""Plan transitions + Telegram nudges.

Three entry points:

  upgrade_to_paid(session, bot, user_id, period_end, *, payment_id=None, order_id=None)
      Called by the Razorpay webhook on `payment_link.paid`. Idempotent
      (re-upgrading a paid user just bumps period_end if later).

  expire_due_subscriptions(bot)
      Daily cron @ 00:05 IST. Finds paid users whose
      current_period_end < now() and downgrades them.

  send_expiry_reminders(bot)
      Daily cron @ 09:30 IST. Finds paid users expiring in 2-3 days and
      sends the friendly nudge.

Quota numbers live HERE (and on .env for the pipeline). Keep in sync.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select

from core.db import get_session
from core.models import Subscription, SubscriptionStatus, SubscriptionTier, User

log = logging.getLogger(__name__)

PAID_OUTREACH_PER_DAY = 15
PAID_SCANS_PER_DAY = 50
FREE_OUTREACH_PER_DAY = 5
FREE_SCANS_PER_DAY = 20

_PRO_PERIOD_DAYS = 30


def _format_date(dt: datetime) -> str:
    """Human-friendly IST-ish date for Telegram messages (e.g. '12 Jul 2026')."""
    return dt.strftime("%d %b %Y")


async def upgrade_to_paid(
    bot: Bot,
    user_id: int,
    *,
    payment_id: str | None = None,
    order_id: str | None = None,
    period_end: datetime | None = None,
) -> None:
    """Flip a user to the paid tier and notify them on Telegram.

    If ``period_end`` is None, defaults to now + 30 days (renewal extends
    from the LATER of (now, existing period_end) so users who renew early
    don't lose days).
    """
    now = datetime.now(timezone.utc)

    async with get_session() as session:
        user = await session.get(User, user_id)
        if user is None:
            log.warning("upgrade_to_paid: user_id=%s not found \u2014 ignoring", user_id)
            return

        sub = await session.get(Subscription, user_id)
        if sub is None:
            sub = Subscription(user_id=user_id, status=SubscriptionStatus.incomplete)
            session.add(sub)

        anchor = now
        if sub.current_period_end is not None and sub.current_period_end > now:
            # Renewing early: stack the new month on top of remaining time.
            anchor = sub.current_period_end
        new_period_end = period_end or (anchor + timedelta(days=_PRO_PERIOD_DAYS))

        user.subscription_tier = SubscriptionTier.paid
        user.daily_outreach_limit = PAID_OUTREACH_PER_DAY
        user.daily_scan_limit = PAID_SCANS_PER_DAY

        sub.status = SubscriptionStatus.active
        sub.current_period_start = now
        sub.current_period_end = new_period_end
        if payment_id:
            sub.razorpay_payment_id = payment_id
        if order_id:
            sub.razorpay_order_id = order_id

        chat_id = user.telegram_chat_id
        period_end_label = _format_date(new_period_end)
        await session.commit()

    log.info(
        "user %s upgraded to paid (period_end=%s payment_id=%s)",
        user_id, new_period_end.isoformat(), payment_id,
    )

    # Verbatim message text per product spec \u2014 do not edit casually.
    text = (
        "\U0001f389 <b>Welcome to AutoApply Pro!</b>\n"
        "\n"
        f"Your payment of \u20b9500 is confirmed. You now have:\n"
        f"\u2705 {PAID_OUTREACH_PER_DAY} outreach emails/day "
        f"(was {FREE_OUTREACH_PER_DAY})\n"
        f"\u2705 {PAID_SCANS_PER_DAY} job scans/day "
        f"(was {FREE_SCANS_PER_DAY})\n"
        "\u2705 Priority Groq scoring\n"
        "\u2705 Apollo enrichment coming soon\n"
        "\n"
        f"Your Pro plan is active until <b>{period_end_label}</b>. "
        "Use /status to see today's run or /settime to change your daily schedule."
    )
    try:
        await bot.send_message(chat_id, text)
    except TelegramAPIError as e:
        log.warning("upgrade notify failed user_id=%s: %s", user_id, e)


async def expire_due_subscriptions(bot: Bot) -> None:
    """Daily sweep: downgrade paid users whose period_end is in the past."""
    now = datetime.now(timezone.utc)
    downgraded: list[int] = []

    async with get_session() as session:
        stmt = (
            select(User.id, User.telegram_chat_id)
            .join(Subscription, Subscription.user_id == User.id)
            .where(User.subscription_tier == SubscriptionTier.paid)
            .where(Subscription.current_period_end != None)         # noqa: E711
            .where(Subscription.current_period_end < now)
        )
        rows = (await session.execute(stmt)).all()
        chat_map: dict[int, int] = {uid: chat for uid, chat in rows}

        for uid in chat_map:
            user = await session.get(User, uid)
            sub = await session.get(Subscription, uid)
            if user is None or sub is None:
                continue
            user.subscription_tier = SubscriptionTier.free
            user.daily_outreach_limit = FREE_OUTREACH_PER_DAY
            user.daily_scan_limit = FREE_SCANS_PER_DAY
            sub.status = SubscriptionStatus.cancelled
            downgraded.append(uid)
        await session.commit()

    log.info("expire_due_subscriptions: downgraded=%d", len(downgraded))
    if not downgraded:
        return

    text = (
        "\u23f0 Your AutoApply Pro plan has expired \u2014 you're back on the Free tier "
        f"({FREE_OUTREACH_PER_DAY} outreach/day, {FREE_SCANS_PER_DAY} scans/day).\n"
        "\n"
        "Tap /upgrade to renew for another month at \u20b9500."
    )
    for uid in downgraded:
        try:
            await bot.send_message(chat_map[uid], text)
        except TelegramAPIError as e:
            log.warning("expiry notify failed user_id=%s: %s", uid, e)


async def send_expiry_reminders(bot: Bot) -> None:
    """Daily reminder for paid users whose plan ends in ~3 days.

    Window: ``[now + 2 days, now + 3 days)``. Combined with a daily 09:30 IST
    cron this fires exactly once per user per renewal cycle (the window
    advances every day, the user's period_end is fixed).
    """
    now = datetime.now(timezone.utc)
    window_start = now + timedelta(days=2)
    window_end = now + timedelta(days=3)

    async with get_session() as session:
        stmt = (
            select(User.id, User.telegram_chat_id)
            .join(Subscription, Subscription.user_id == User.id)
            .where(User.subscription_tier == SubscriptionTier.paid)
            .where(Subscription.current_period_end >= window_start)
            .where(Subscription.current_period_end < window_end)
        )
        rows = (await session.execute(stmt)).all()

    log.info("send_expiry_reminders: matched=%d", len(rows))
    text = (
        "\u23f0 Your AutoApply Pro plan expires in 3 days. "
        "Tap /upgrade to renew for another month at \u20b9500."
    )
    for uid, chat_id in rows:
        try:
            await bot.send_message(chat_id, text)
        except TelegramAPIError as e:
            log.warning("reminder notify failed user_id=%s: %s", uid, e)
