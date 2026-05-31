"""APScheduler-based daily pipeline runner.

Single cron job at 09:00 Asia/Kolkata calls :func:`run_daily_for_all_users`,
which iterates active users sequentially and runs the pipeline for each.

Design choices:
- **Sequential, not parallel.** Adzuna and Groq are both rate-limited and a
  single user takes ~30-60s. With N users, total runtime is N*60s; for the
  first 50 users we're well inside a 24h window. Switch to a thread pool
  when N > 200.
- **Per-user error isolation.** A crash inside one user's pipeline is logged
  and the loop continues to the next user.
- **Bot notification after each run.** The user gets a one-line Telegram
  message: "Today's run: 12 jobs scraped, 3 emails sent." Failed runs send
  a less cheerful but still informative message so the user knows what
  happened. If sending the notification itself fails, log and move on \u2014
  don't double-fail the user's run.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from core.db import get_session
from core.models import DailyRunSummary, User, UserStatus
from core.pipeline import run_pipeline_for_user
from core.tenant import load_tenant_context

log = logging.getLogger(__name__)

DAILY_HOUR_IST = 9
DAILY_MINUTE_IST = 0
TIMEZONE = "Asia/Kolkata"


async def run_daily_for_all_users(bot: Bot) -> None:
    """Fan-out entry point. Called by APScheduler at 09:00 IST every day.

    Also safe to call manually for testing.
    """
    started = datetime.now(timezone.utc)
    log.info("daily run: starting fan-out at %s UTC", started.isoformat())

    # Snapshot the active-user list in a short-lived session so we don't
    # hold a tx open during the (long) per-user iteration.
    async with get_session() as session:
        result = await session.execute(
            select(User.id).where(User.status == UserStatus.active)
        )
        user_ids = [row[0] for row in result.all()]

    log.info("daily run: %s active users", len(user_ids))
    success = 0
    failed = 0
    for uid in user_ids:
        try:
            await _run_one_user(bot, uid)
            success += 1
        except Exception:
            log.exception("daily run: pipeline crashed for user %s", uid)
            failed += 1

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    log.info("daily run: complete. success=%s failed=%s elapsed=%.1fs",
             success, failed, elapsed)


async def _run_one_user(bot: Bot, user_id: int) -> None:
    """Run pipeline for one user inside its own session, then notify on Telegram."""
    log.info("daily run: processing user_id=%s", user_id)
    async with get_session() as session:
        ctx = await load_tenant_context(session, user_id)
        if ctx is None:
            log.warning("daily run: user %s context not loadable; skipping", user_id)
            return
        # Snapshot key fields up front so the per-user audit line in the
        # log is independent of what run_pipeline_for_user logs.
        log.info(
            "daily run: user=%s name=%r tier=%s status=%s roles=%r locations=%r",
            user_id, ctx.user.first_name, ctx.user.subscription_tier.value,
            ctx.user.status.value, ctx.preferences.role_keywords,
            ctx.preferences.locations,
        )
        chat_id = ctx.user.telegram_chat_id
        summary = await run_pipeline_for_user(session, ctx)
        # Capture values we'll use after the session closes \u2014 the ORM
        # instance becomes unusable post-commit.
        snapshot = {
            "scraped": summary.jobs_scraped,
            "scored": summary.jobs_scored,
            "sent": summary.outreach_sent,
            "failed": summary.outreach_failed,
            "error": summary.error,
            "hunter_quota_exhausted": getattr(summary, "hunter_quota_exhausted", False),
            "tier": ctx.user.subscription_tier.value,
        }
        # session commits on context exit

    await _notify_user(bot, chat_id, snapshot)


async def _notify_user(bot: Bot, chat_id: int, snapshot: dict) -> None:
    """Send the day's one-line summary. Swallows Telegram errors."""
    if snapshot["error"]:
        text = (
            f"\u26a0\ufe0f Today's run had an error: <code>{snapshot['error'][:200]}</code>\n"
            f"Scraped {snapshot['scraped']}, scored {snapshot['scored']}, "
            f"sent {snapshot['sent']}. /status for details."
        )
    elif snapshot["sent"] == 0 and snapshot["scraped"] == 0:
        text = (
            "Today's run: no new jobs found. "
            "Try broadening your roles/locations via /start."
        )
    else:
        text = (
            f"\u2705 Today: scraped <b>{snapshot['scraped']}</b>, "
            f"scored <b>{snapshot['scored']}</b>, "
            f"sent <b>{snapshot['sent']}</b> outreach email(s)."
        )
        if snapshot["failed"]:
            text += f" ({snapshot['failed']} SMTP failure(s) \u2014 /status)"
    if snapshot.get("hunter_quota_exhausted"):
        if snapshot.get("tier") == "paid":
            text += (
                "\n\n\u26a0\ufe0f Recruiter-lookup pool is tapped out for today. "
                "Lookups will resume tomorrow. (Apollo enrichment for Pro is "
                "coming soon and will eliminate this kind of throttling.)"
            )
        else:
            text += (
                "\n\n\u26a0\ufe0f Our shared recruiter-lookup pool is tapped out "
                "for today \u2014 a normal limit on the free tier. "
                "<b>AutoApply Pro (\u20b9500/month)</b> gets priority on the pool "
                "today, plus 3x outreach and 2.5x scans.\n"
                "\nTap /upgrade to see the full pitch."
            )
    # Free-tier nudge: append a soft upsell to every daily summary so the
    # value gap stays top-of-mind. Skip when we already pitched (quota
    # exhaustion above) to avoid double-asking in one message.
    elif snapshot.get("tier") == "free":
        text += (
            "\n\n\u26a1 <i>Pro users get 3x more outreach emails (15/day) and "
            "50 job scans/day for \u20b9500/month. /upgrade</i>"
        )
    # TODO(week-6): when /add_contacts ships, ALSO append the "know any
    # recruiters?" nudge whenever the run found candidates but couldn't
    # email anyone (e.g. snapshot['sent'] == 0 and snapshot['scored'] > 0,
    # or track no_recruiter count separately on the snapshot):
    #   "\n\n\U0001f4a1 <i>Tip: know any recruiters directly? Add their emails "
    #   "with /add_contacts and we'll reach out for you next run.</i>"
    # Requires plumbing no_recruiter through DailyRunSummary first (today
    # it's only a local int in _run_outreach_phase).
    try:
        await bot.send_message(chat_id, text)
    except TelegramAPIError as e:
        log.warning("could not notify user chat_id=%s: %s", chat_id, e)


def build_scheduler(bot: Bot) -> AsyncIOScheduler:
    """Create and configure the scheduler. Caller is responsible for .start()."""
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(
        run_daily_for_all_users,
        trigger=CronTrigger(hour=DAILY_HOUR_IST, minute=DAILY_MINUTE_IST, timezone=TIMEZONE),
        kwargs={"bot": bot},
        id="daily_pipeline_fanout",
        # If the bot is down at 09:00 and starts at 09:30, still run today
        # (within a 1h grace window).
        misfire_grace_time=3600,
        max_instances=1,         # never run two fan-outs concurrently
        coalesce=True,           # collapse missed runs into a single execution
    )
    return scheduler
