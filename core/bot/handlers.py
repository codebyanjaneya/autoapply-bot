"""Post-onboarding bot commands: /status, /pause, /resume, /upgrade, /help."""
# =============================================================================
# TODO(week-6): Custom contact list feature \u2014 implement after Stripe lands.
#
# User story: power users already have recruiter emails (from their own
# research, alumni network, LinkedIn). Let them paste those in and bypass
# Hunter/Apollo entirely \u2014 zero lookup cost, 100% hit rate.
#
# New commands to add HERE:
#   /add_contacts \u2014 accept either:
#                     (a) comma-separated emails in the next message, OR
#                     (b) a CSV upload with columns: email[,name,company,position]
#                   Validate each email; store via repository; reply with
#                   "Added N contacts. /contacts to view, /clear_contacts to
#                   start over."
#   /contacts     \u2014 paginated list (first 20 + "showing X of Y").
#   /clear_contacts \u2014 confirmation flow (button or "type CONFIRM"), then wipe.
#
# Data model (new migration 0003):
#   class UserContact(Base):
#       __tablename__ = "user_contacts"
#       id           PK
#       user_id      FK users.id ON DELETE CASCADE, indexed
#       email        String(254), NOT NULL
#       name         String(128) nullable
#       company      String(128) nullable
#       position     String(128) nullable
#       added_at     timestamptz default now()
#       last_used_at timestamptz nullable
#       UNIQUE(user_id, email)
#
# Pipeline integration (core/outreach.py):
#   Before calling RecruiterFinder, check if the application's company has a
#   UserContact match. If yes \u2014 use that, set source="user-contact", skip
#   Hunter/Apollo entirely, no lookup cost. Set last_used_at on send.
#   Fall through to RecruiterFinder when no match.
#
# Messaging hooks already TODO'd in:
#   - cmd_features (this file)        \u2014 add "Bring your own contacts" bullet
#   - cmd_help (this file)            \u2014 list the three new commands
#   - cmd_upgrade (this file)         \u2014 mention as a Pro+free perk
#   - _finish_onboarding (onboarding) \u2014 "Pro tip" line
#   - _notify_user (scheduler)        \u2014 nudge when no_recruiter=N and sent=0
#
# All five sites currently advertise NOTHING about this feature so we don't
# ship broken /commands. Wire copy + handlers together in one Week 6 PR.
# =============================================================================
from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from sqlalchemy import select

from core.db import get_session
from core.models import (
    Application, DailyRunSummary, Job, OutreachLog, User, UserPreferences, UserStatus,
)

log = logging.getLogger(__name__)
router = Router(name="commands")


async def _require_user(message: Message) -> User | None:
    """Load the User row or tell the caller to /start. Returns None if missing."""
    assert message.from_user is not None
    async with get_session() as session:
        user = await session.get(User, message.from_user.id)
    if user is None or user.status == UserStatus.onboarding:
        await message.answer("You haven't finished onboarding yet. Use /start.")
        return None
    return user


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    # TODO(week-6): add "• /add_contacts — paste your own recruiter emails" + sibling commands
    await message.answer(
        "<b>AutoApply Bot</b>\n\n"
        "\u2022 /start \u2014 begin onboarding (or welcome-back if you're already set up)\n"
        "\u2022 /settings \u2014 update Gmail, roles, resume, etc. without redoing /start\n"
        "\u2022 /settime \u2014 change your daily run time (default 9 AM IST)\n"
        "\u2022 /status \u2014 today's pipeline summary + recent outreach\n"
        "\u2022 /pause \u2014 stop daily pipeline runs\n"
        "\u2022 /resume \u2014 resume daily runs\n"
        "\u2022 /upgrade \u2014 unlock Pro (\u20b9500/mo)\n"
        "\u2022 /features \u2014 everything Pro includes\n"
        "\u2022 /referral \u2014 your invite link (earn 1 month free per upgrade)\n"
        "\u2022 /restart \u2014 redo onboarding from scratch (existing data kept until you finish)\n"
        "\u2022 /cancel \u2014 abort the current wizard step\n"
        "\u2022 /help \u2014 this message"
    )


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    user = await _require_user(message)
    if user is None:
        return
    today = datetime.now(timezone.utc).date()
    async with get_session() as session:
        summary = await session.get(DailyRunSummary, (user.id, today))
        prefs = await session.get(UserPreferences, user.id)

        # Top 3 outreach attempts today.
        log_stmt = (
            select(OutreachLog, Application, Job)
            .join(Application, Application.id == OutreachLog.application_id)
            .join(Job, Job.id == Application.job_id)
            .where(OutreachLog.user_id == user.id)
            .order_by(OutreachLog.sent_at.desc())
            .limit(5)
        )
        rows = (await session.execute(log_stmt)).all()

    status_emoji = {
        UserStatus.active: "\u25b6\ufe0f active",
        UserStatus.paused: "\u23f8\ufe0f paused",
    }.get(user.status, str(user.status.value))

    run_hour = prefs.preferred_run_hour if prefs is not None else 9
    run_time_label = _format_hour_label(run_hour)

    lines = [
        f"<b>Status: {status_emoji}</b>",
        f"Tier: <b>{user.subscription_tier.value}</b>  "
        f"(limits: {user.daily_scan_limit} scans, {user.daily_outreach_limit} outreach/day)",
        f"Daily run time: <b>{run_time_label} IST</b>  "
        f"<i>(change with /settime)</i>",
        "",
        f"<b>Today ({today.isoformat()})</b>",
    ]
    if summary is None:
        lines.append("  No pipeline run yet today.")
    else:
        lines.append(f"  Jobs scraped: {summary.jobs_scraped}")
        lines.append(f"  Jobs scored:  {summary.jobs_scored}")
        lines.append(f"  Outreach sent:   {summary.outreach_sent}")
        lines.append(f"  Outreach failed: {summary.outreach_failed}")
        if summary.error:
            lines.append(f"  \u26a0\ufe0f error: <code>{summary.error[:200]}</code>")

    if rows:
        lines.append("")
        lines.append("<b>Recent outreach</b>")
        for ol, _app, job in rows:
            if ol.error:
                marker = "\u274c" if "[NO-SEND]" not in ol.error else "\U0001f441"
                tail = f" \u2014 <i>{_short(ol.error, 80)}</i>"
            else:
                marker = "\u2705"
                tail = ""
            lines.append(
                f"  {marker} {ol.sent_at.strftime('%H:%M')} "
                f"<b>{_short(job.company, 25)}</b> \u2192 {ol.to_email}{tail}"
            )

    await message.answer("\n".join(lines))


@router.message(Command("pause"))
async def cmd_pause(message: Message) -> None:
    user = await _require_user(message)
    if user is None:
        return
    if user.status == UserStatus.paused:
        await message.answer("Already paused. /resume to start again.")
        return
    async with get_session() as session:
        u = await session.get(User, user.id)
        if u is not None:
            u.status = UserStatus.paused
            await session.commit()
    await message.answer("\u23f8\ufe0f Paused. Daily runs will be skipped until /resume.")


@router.message(Command("resume"))
async def cmd_resume(message: Message) -> None:
    user = await _require_user(message)
    if user is None:
        return
    if user.status == UserStatus.active:
        await message.answer("Already active.")
        return
    async with get_session() as session:
        u = await session.get(User, user.id)
        if u is not None:
            u.status = UserStatus.active
            await session.commit()
        prefs = await session.get(UserPreferences, user.id)
    run_label = _format_hour_label(prefs.preferred_run_hour) if prefs else "9 AM"
    await message.answer(
        f"\u25b6\ufe0f Resumed. Next run at <b>{run_label} IST</b>."
    )


# ---------- /settime: change preferred daily run hour (IST) ----------
# Options surfaced as buttons. Keep this short — long lists are noisy in
# Telegram chat. 7am–11am covers morning people; 6pm–10pm covers night
# owls. Anything outside this range can still be set by the operator via
# direct SQL if a user really wants 3am.
_SETTIME_HOURS = [7, 8, 9, 10, 11, 18, 19, 20, 21, 22]


def _format_hour_label(hour: int) -> str:
    """7 -> '7 AM', 18 -> '6 PM'."""
    suffix = "AM" if hour < 12 else "PM"
    h12 = hour if 1 <= hour <= 12 else (hour - 12 if hour > 12 else 12)
    return f"{h12} {suffix}"


def _build_settime_keyboard(current: int | None = None) -> InlineKeyboardMarkup:
    """Two rows: morning (5 buttons) and evening (5 buttons).

    The user's current choice is marked with a check so they can see it
    at a glance.
    """
    morning, evening = _SETTIME_HOURS[:5], _SETTIME_HOURS[5:]

    def _btn(h: int) -> InlineKeyboardButton:
        label = _format_hour_label(h)
        if current == h:
            label = f"\u2705 {label}"
        return InlineKeyboardButton(text=label, callback_data=f"settime:{h}")

    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn(h) for h in morning],
        [_btn(h) for h in evening],
    ])


@router.message(Command("settime"))
async def cmd_settime(message: Message) -> None:
    user = await _require_user(message)
    if user is None:
        return
    async with get_session() as session:
        prefs = await session.get(UserPreferences, user.id)
    current = prefs.preferred_run_hour if prefs is not None else None
    current_label = _format_hour_label(current) if current is not None else "9 AM"
    await message.answer(
        f"\u23f0 <b>Daily run time</b>\n\n"
        f"Currently: <b>{current_label} IST</b>\n\n"
        f"Tap a new time below \u2014 your pipeline will run at that hour "
        f"every day starting tomorrow.",
        reply_markup=_build_settime_keyboard(current),
    )


@router.callback_query(F.data.startswith("settime:"))
async def on_settime_pick(cb: CallbackQuery) -> None:
    assert cb.from_user is not None
    assert cb.data is not None
    try:
        hour = int(cb.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await cb.answer("Invalid choice.", show_alert=True)
        return
    if not (0 <= hour <= 23):
        await cb.answer("Hour out of range.", show_alert=True)
        return

    async with get_session() as session:
        prefs = await session.get(UserPreferences, cb.from_user.id)
        if prefs is None:
            await cb.answer("Finish /start first.", show_alert=True)
            return
        prefs.preferred_run_hour = hour
        await session.commit()

    label = _format_hour_label(hour)
    log.info("user %s set preferred_run_hour=%d", cb.from_user.id, hour)
    # Edit the original message so the button list disappears \u2014 cleaner UX
    # than leaving a stale keyboard behind.
    if cb.message is not None:
        try:
            await cb.message.edit_text(
                f"\u2705 Done! Your pipeline will now run daily at "
                f"<b>{label} IST</b>."
            )
        except Exception:
            # Edit can fail (e.g. message too old). Fall back to a new message.
            await cb.message.answer(
                f"\u2705 Done! Your pipeline will now run daily at "
                f"<b>{label} IST</b>."
            )
    await cb.answer()


@router.message(Command("upgrade"))
async def cmd_upgrade(message: Message) -> None:
    # TODO(week-6): add "Bring your own contacts (/add_contacts) — zero lookup
    # cost, 100% hit rate" as a value bullet in the Pro pitch.
    # Stripe checkout URL will be wired in Week 5. Until then the link
    # below is a placeholder — clicking it tells the user to wait.
    # TODO(week-5): replace with real Stripe payment-link URL.
    stripe_link = "https://buy.stripe.com/test_placeholder_autoapply_pro"
    await message.answer(
        "<b>\U0001f4b0 The math</b>\n"
        "<pre>"
        "Apollo Basic           \u20b9 4,000+/month\n"
        "AutoApply Pro          \u20b9   500/month\n"
        "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        "You save               \u20b9 3,500/month\n"
        "</pre>"
        "<i>That's less than \u20b917/day \u2014 cheaper than a coffee, and you get "
        "recruiter lookups <b>plus</b> the rest of the pipeline (scoring, outreach, "
        "reply tracking) Apollo doesn't do.</i>\n"
        "\n"
        "<b>What \u20b9500/month buys you</b>\n"
        "\u2022 Recruiter lookups (no quota anxiety)\n"
        "\u2022 AI scoring of every job against your resume\n"
        "\u2022 Automated personalised outreach \u2014 15 emails/day\n"
        "\u2022 50 job scans/day across your roles & cities\n"
        "\u2022 Reply tracking + min-score control\n"
        "\u2022 Priority queue when the daily run goes out\n"
        "\n"
        "<b>Free vs Pro</b>\n"
        "<pre>"
        "                  Free      Pro\n"
        "Outreach/day        5       15\n"
        "Job scans/day      20       50\n"
        "Min-score control  no      yes\n"
        "Priority queue     no      yes\n"
        "</pre>"
        "\n"
        f"<b>\u2192 Upgrade:</b> {stripe_link}\n"
        "<i>(Stripe checkout launches in the next release. DM the operator "
        "for early access \u2014 we'll flip your account manually.)</i>\n"
        "\n"
        "Cancel anytime. No lock-in. Try /features for the full list.",
        disable_web_page_preview=True,
    )


@router.message(Command("features"))
async def cmd_features(message: Message) -> None:
    # TODO(week-6): add a "Bring your own contacts" section under <b>Workflow</b>:
    #   "• /add_contacts — paste recruiter emails you already have; we email them
    #      directly with zero Hunter/Apollo lookups (great for alumni / referrals)."
    # Available to BOTH tiers — sells the free tier as more useful and gives paid
    # users another reason to stay.
    await message.answer(
        "<b>\u2728 AutoApply Pro \u2014 everything you get</b>\n"
        "\n"
        "<b>Volume</b>\n"
        "\u2022 50 job scans/day (vs 20 free)\n"
        "\u2022 15 personalised outreach emails/day (vs 5 free)\n"
        "\u2022 Hunter recruiter-lookup pool \u2014 no per-user quota anxiety\n"
        "  <i>(Apollo enrichment is <b>coming soon</b> for Pro users \u2014 their "
        "free plan blocks the People Search API, so we're holding it back "
        "until we can fund the paid plan as a Pro perk.)</i>\n"
        "\n"
        "<b>Smarts</b>\n"
        "\u2022 AI scoring of every job against your resume\n"
        "\u2022 Personalised cold emails written by Llama 3.3\n"
        "\u2022 Min-score control \u2014 only email roles above your threshold\n"
        "\u2022 Hand-curated company-domain map (Indian companies Hunter misses)\n"
        "\n"
        "<b>Workflow</b>\n"
        "\u2022 Daily run at 09:00 IST, summary in Telegram\n"
        "\u2022 Reply tracking via your inbox\n"
        "\u2022 Priority queue on busy mornings\n"
        "\u2022 Pause/resume anytime, cancel anytime\n"
        "\n"
        "<b>The math</b>\n"
        "Apollo Basic alone is \u20b94,000+/month (and you still write the "
        "emails yourself). AutoApply Pro is \u20b9500/month \u2014 cheaper than "
        "coffee, and you get the whole pipeline: scoring, outreach, reply "
        "tracking. Apollo enrichment is coming soon as a Pro perk.\n"
        "\n"
        "Ready? \u2192 /upgrade",
        disable_web_page_preview=True,
    )


@router.message(Command("referral"))
async def cmd_referral(message: Message) -> None:
    from core.referrals import build_referral_code, count_referrals  # local import to avoid cycle

    user = await _require_user(message)
    if user is None:
        return
    code = build_referral_code(user.id)
    async with get_session() as session:
        referred_count, paid_count = await count_referrals(session, user.id)
    # Bot username pulled from the message context (works for any deployment).
    bot_username = (await message.bot.get_me()).username
    invite_link = f"https://t.me/{bot_username}?start={code}"
    await message.answer(
        "<b>\U0001f381 Refer a friend, get a month free</b>\n"
        "\n"
        "Share your invite link below. When someone signs up via your link "
        "<b>and upgrades to Pro</b>, you get <b>1 month of Pro free</b> \u2014 "
        "no cap on how many you can earn.\n"
        "\n"
        f"<b>Your code:</b> <code>{code}</code>\n"
        f"<b>Your link:</b> {invite_link}\n"
        "\n"
        "<b>Your stats</b>\n"
        f"\u2022 People signed up via your link: <b>{referred_count}</b>\n"
        f"\u2022 Of those, upgraded to Pro: <b>{paid_count}</b>\n"
        f"\u2022 Free months earned: <b>{paid_count}</b> "
        f"<i>(applied automatically once Stripe checkout ships)</i>",
        disable_web_page_preview=True,
    )


def _short(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "\u2026"
