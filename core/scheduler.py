"""APScheduler-based daily pipeline runner.

An hourly cron job (CronTrigger(minute=0, tz='Asia/Kolkata')) calls
:func:`run_hourly_fanout`, which selects active users whose
`preferences.preferred_run_hour` matches the current IST hour and runs
the pipeline for each.

Design choices:
- **Per-user scheduled hour.** Users pick their run time via /settime
  (default 9 IST). Switching from one global 09:00 fan-out to an hourly
  check lets night owls and early birds get their results when they want.
- **Sequential, not parallel.** Adzuna and Groq are both rate-limited and a
  single user takes ~30-60s. With N users per hour, total runtime is N*60s;
  for the first ~100 users in any single hour we're inside the 60-min
  window. Switch to a thread pool when one hour bucket > 100 users.
- **Per-user error isolation.** A crash inside one user's pipeline is logged
  and the loop continues to the next user.
- **Bot notification after each run.** The user gets a one-line Telegram
  message: "Today's run: 12 jobs scraped, 3 emails sent." Failed runs send
  a less cheerful but still informative message so the user knows what
  happened. If sending the notification itself fails, log and move on —
  don't double-fail the user's run.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from core.db import get_session
from core.models import DailyRunSummary, User, UserPreferences, UserStatus
from core.pipeline import run_pipeline_for_user
from core.tenant import load_tenant_context

log = logging.getLogger(__name__)

TIMEZONE = "Asia/Kolkata"
_IST = ZoneInfo(TIMEZONE)


async def run_hourly_fanout(bot: Bot) -> None:
    """Fan-out entry point. Fires every hour on the hour (IST).

    Selects active users whose `preferred_run_hour` matches the current
    IST hour and runs the pipeline for each, sequentially. Also safe to
    call manually for testing.
    """
    started = datetime.now(timezone.utc)
    current_hour_ist = datetime.now(_IST).hour
    log.info(
        "hourly fanout: started_utc=%s ist_hour=%d",
        started.isoformat(), current_hour_ist,
    )

    # Snapshot the matching-user list in a short-lived session so we don't
    # hold a tx open during the (long) per-user iteration.
    async with get_session() as session:
        result = await session.execute(
            select(User.id)
            .join(UserPreferences, UserPreferences.user_id == User.id)
            .where(User.status == UserStatus.active)
            .where(UserPreferences.preferred_run_hour == current_hour_ist)
        )
        user_ids = [row[0] for row in result.all()]

    log.info(
        "hourly fanout: ist_hour=%d matched_users=%d",
        current_hour_ist, len(user_ids),
    )
    if not user_ids:
        return

    success = 0
    failed = 0
    for uid in user_ids:
        try:
            await _run_one_user(bot, uid)
            success += 1
        except Exception:
            log.exception("hourly fanout: pipeline crashed for user %s", uid)
            failed += 1

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    log.info(
        "hourly fanout: ist_hour=%d complete success=%d failed=%d elapsed=%.1fs",
        current_hour_ist, success, failed, elapsed,
    )


# Backwards-compat alias so anything that imported the old name still works.
run_daily_for_all_users = run_hourly_fanout


async def _run_one_user(bot: Bot, user_id: int) -> None:
    """Run pipeline for one user inside its own session, then notify on Telegram."""
    log.info("hourly fanout: processing user_id=%s", user_id)
    async with get_session() as session:
        ctx = await load_tenant_context(session, user_id)
        if ctx is None:
            log.warning("hourly fanout: user %s context not loadable; skipping", user_id)
            return
        # If today's pipeline already produced results for this user, skip
        # the re-run. Happens when a user moves /settime later in the day
        # (e.g. 9 -> 22) after the first run already fired. The
        # DailyRunSummary row is the natural single-source-of-truth.
        today_ist = datetime.now(_IST).date()
        existing_summary = await session.get(DailyRunSummary, (user_id, today_ist))
        if existing_summary is not None and existing_summary.jobs_scraped > 0:
            log.info(
                "hourly fanout: user %s already ran today "
                "(jobs_scraped=%d) \u2014 skipping",
                user_id, existing_summary.jobs_scraped,
            )
            return
        # Snapshot key fields up front so the per-user audit line in the
        # log is independent of what run_pipeline_for_user logs.
        log.info(
            "hourly fanout: user=%s name=%r tier=%s status=%s "
            "roles=%r locations=%r preferred_hour=%d",
            user_id, ctx.user.first_name, ctx.user.subscription_tier.value,
            ctx.user.status.value, ctx.preferences.role_keywords,
            ctx.preferences.locations, ctx.preferences.preferred_run_hour,
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
        run_hourly_fanout,
        trigger=CronTrigger(minute=0, timezone=TIMEZONE),
        kwargs={"bot": bot},
        id="hourly_pipeline_fanout",
        # If the bot is down at H:00 and starts at H:30, still run for this
        # hour's matching users (within a 1h grace window).
        misfire_grace_time=3600,
        max_instances=1,         # never run two fan-outs concurrently
        coalesce=True,           # collapse missed runs into a single execution
    )
    return scheduler
