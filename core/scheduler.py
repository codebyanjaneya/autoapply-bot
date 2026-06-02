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
from core.models import (
    Application, DailyRunSummary, Job, OutreachLog, User, UserPreferences, UserStatus,
)
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
        # Companies we successfully emailed today, in send order. Pulled
        # right after the pipeline so the user sees concrete names rather
        # than just counters in the daily summary.
        today_date = datetime.now(_IST).date()
        companies_stmt = (
            select(Job.company)
            .join(Application, Application.job_id == Job.id)
            .join(OutreachLog, OutreachLog.application_id == Application.id)
            .where(OutreachLog.user_id == user_id)
            .where(OutreachLog.error.is_(None))  # successful sends only
            .where(OutreachLog.sent_at >= datetime.combine(today_date, datetime.min.time(), tzinfo=_IST))
            .order_by(OutreachLog.sent_at.asc())
        )
        companies_today = [c for (c,) in (await session.execute(companies_stmt)).all() if c]
        # De-dupe while preserving order (one company can have multiple jobs).
        _seen: set[str] = set()
        companies_today = [c for c in companies_today if not (c in _seen or _seen.add(c))]
        # Capture values we'll use after the session closes \u2014 the ORM
        # instance becomes unusable post-commit.
        snapshot = {
            "scraped": summary.jobs_scraped,
            "scored": summary.jobs_scored,
            "sent": summary.outreach_sent,
            "failed": summary.outreach_failed,
            "error": summary.error,
            "hunter_quota_exhausted": getattr(summary, "hunter_quota_exhausted", False),
            "no_recruiter": getattr(summary, "no_recruiter_count", 0),
            "companies_today": companies_today,
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
        # Concrete "who got emailed" line so the user trusts what the bot
        # did on their behalf today. Cap the rendered list so we don't blow
        # Telegram's 4096-char message ceiling on a quota-50 paid user.
        companies = snapshot.get("companies_today") or []
        if companies:
            shown = companies[:10]
            extra = f" \u2026and {len(companies) - len(shown)} more" if len(companies) > len(shown) else ""
            text += (
                f"\n\n\U0001f4e7 <i>Today I emailed recruiters at:</i> "
                f"<b>{', '.join(shown)}</b>{extra}"
            )
    # Universal relevance nudge — cheaper to update roles than to
    # complain about results. Shown on every run regardless of tier/outcome
    # (except hard errors above, where the error message is louder).
    if not snapshot["error"]:
        text += (
            "\n\n\U0001f4a1 <i>Not getting relevant jobs? Update your roles "
            "with /updaterole or full settings via /settings.</i>"
        )
    # No-recruiter nudge — fired when we scored jobs but couldn't find
    # ANYONE to email at any of those companies. Direct contacts the user
    # supplies via /add_contacts skip Hunter entirely and ship every time.
    if (
        not snapshot["error"]
        and snapshot.get("no_recruiter", 0) >= 3
        and snapshot.get("sent", 0) == 0
    ):
        text += (
            "\n\n\U0001f4ec <i>Know any recruiters personally? Add their "
            "emails with /add_contacts — we'll reach out for you directly "
            "next run, no Hunter lookup needed.</i>"
        )
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
    # Local import to avoid a cycle: core.payments -> core.db -> (...) and we
    # only need the cron job entry points here.
    from core.payments import expire_due_subscriptions, send_expiry_reminders

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

    # --- billing: expire paid users whose period_end has passed ---
    # 00:05 IST so it runs once after midnight, before the morning fan-out.
    scheduler.add_job(
        expire_due_subscriptions,
        trigger=CronTrigger(hour=0, minute=5, timezone=TIMEZONE),
        kwargs={"bot": bot},
        id="expire_subscriptions_daily",
        misfire_grace_time=3600,
        max_instances=1,
        coalesce=True,
    )

    # --- billing: friendly nudge ~3 days before expiry ---
    # 09:30 IST: morning timezone, doesn't collide with the on-the-hour
    # pipeline fan-out (which runs at minute=0).
    scheduler.add_job(
        send_expiry_reminders,
        trigger=CronTrigger(hour=9, minute=30, timezone=TIMEZONE),
        kwargs={"bot": bot},
        id="expiry_reminder_daily",
        misfire_grace_time=3600,
        max_instances=1,
        coalesce=True,
    )
    return scheduler
