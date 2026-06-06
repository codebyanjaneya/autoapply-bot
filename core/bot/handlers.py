"""Post-onboarding bot commands: /status, /history, /pause, /resume, /upgrade, /help."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from sqlalchemy import select

from core.db import get_session
from core.models import (
    Application, AppStatus, DailyRunSummary, Job, OutreachLog,
    User, UserContact, UserPreferences, UserStatus,
)
from core.pipeline import run_pipeline_for_user
from core.repositories import rate_limits as rl
from core.tenant import load_tenant_context

log = logging.getLogger(__name__)
router = Router(name="commands")

_IST = ZoneInfo("Asia/Kolkata")


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
    await message.answer(
        "<b>AutoApply Bot</b>\n\n"
        "Tap the <b>Menu</b> button (bottom-left of the chat) to see "
        "every command \u2014 you don't need to memorise any of them.\n\n"
        "New here? Try /howitworks for a quick walkthrough.\n\n"
        "\U0001f6df <b>Need help?</b> Run /support \u2014 we reply within 24 hours."
    )


@router.message(Command("howitworks"))
async def cmd_howitworks(message: Message) -> None:
    """Friendly 7-step walkthrough for new (and curious) users.

    Intentionally does NOT call ``_require_user`` \u2014 someone evaluating
    the bot pre-onboarding should be able to read this without /start first.
    """
    await message.answer(
        "\U0001f916 <b>How AutoApply Works</b>\n"
        "\n"
        "<b>Step 1 \u2014 Tell us what you're looking for</b>\n"
        "You enter your target roles, locations, and upload your resume. "
        "Takes 2 minutes.\n"
        "\n"
        "<b>Step 2 \u2014 We find the jobs</b>\n"
        "Every day at your chosen time, we automatically scan hundreds of "
        "fresh job listings from top job boards \u2014 filtered to match your "
        "roles and locations.\n"
        "\n"
        "<b>Step 3 \u2014 AI scores every job</b>\n"
        "Our AI reads each job description and scores it against your resume "
        "(0\u2013100). Only the best matches move forward \u2014 no more applying "
        "to irrelevant jobs.\n"
        "\n"
        "<b>Step 4 \u2014 We find the recruiter</b>\n"
        "For each matched job, we automatically find the hiring manager or "
        "recruiter's email using our recruiter database.\n"
        "\n"
        "<b>Step 5 \u2014 Personalised emails sent automatically</b>\n"
        "We send a personalised outreach email with your resume attached \u2014 "
        "directly to the recruiter's inbox. Not a generic application, "
        "a real human-like email.\n"
        "\n"
        "<b>Step 6 \u2014 You get notified</b>\n"
        "You receive a daily Telegram summary showing how many jobs were "
        "found, scored, and how many emails were sent.\n"
        "\n"
        "<b>Step 7 \u2014 Replies come to your inbox</b>\n"
        "Recruiters reply directly to your Gmail. You handle the conversation "
        "from there.\n"
        "\n"
        "<b>Plans</b>\n"
        "\u2022 <b>Free</b>: 5 emails/day, 20 job scans\n"
        "\u2022 <b>Pro (\u20b9500/month)</b>: 15 emails/day, 50 scans\n"
        "\n"
        "Ready to start? Send /start\n"
        "See pricing: /upgrade\n"
        "See all commands: /help",
        disable_web_page_preview=True,
    )


# ---------------------------------------------------------------------------
# /run \u2014 manually trigger this user's pipeline right now
# ---------------------------------------------------------------------------
# Rate-limited to 1 run per UTC hour via the existing per-day rate-limit
# counter, keyed on a per-hour action string. The counter resets at the
# UTC day boundary which is fine: we only ever check (count <= 1) for the
# current-hour bucket, and stale buckets are ignored.

def _manual_run_action_for(now_utc: datetime) -> str:
    """Per-hour action key so reserve_slot enforces 1 manual run per hour."""
    return f"manual_run_h{now_utc.hour:02d}"


def _next_hour_ist(now_utc: datetime) -> str:
    """Human-readable IST timestamp of the next top-of-hour boundary."""
    next_top = (now_utc + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    return next_top.astimezone(_IST).strftime("%I:%M %p IST").lstrip("0")


async def _run_pipeline_and_notify(bot: Bot, user_id: int, chat_id: int) -> None:
    """Background task: run the full pipeline for one user, then DM result.

    Lives outside the request lifecycle so the /run handler can return
    immediately. Swallows every exception — the caller is a fire-and-
    forget asyncio.create_task() and an uncaught error here would only
    log a noisy 'Task exception was never retrieved' warning.
    """
    try:
        async with get_session() as session:
            ctx = await load_tenant_context(session, user_id)
            if ctx is None:
                await bot.send_message(
                    chat_id,
                    "⚠️ Could not load your profile for the manual run. "
                    "Try /start to repair onboarding.",
                )
                return
            summary = await run_pipeline_for_user(session, ctx)

        if summary.error:
            text = (
                f"⚠️ Manual run finished with an error: "
                f"<code>{summary.error[:200]}</code>\n"
                f"Scraped {summary.jobs_scraped}, scored {summary.jobs_scored}, "
                f"sent {summary.outreach_sent}.\n\n"
                f"🛟 Need help? /support"
            )
        elif summary.outreach_sent == 0 and summary.jobs_scraped == 0:
            text = (
                "✅ Manual run done — no new jobs found this pass. "
                "Try /updaterole or /settings to broaden your search."
            )
        else:
            text = (
                f"✅ <b>Manual run finished.</b>\n"
                f"Scraped <b>{summary.jobs_scraped}</b>, scored "
                f"<b>{summary.jobs_scored}</b>, sent "
                f"<b>{summary.outreach_sent}</b> outreach email(s)."
            )
            if summary.outreach_failed:
                text += f" ({summary.outreach_failed} send failure(s) — /status)"
        try:
            await bot.send_message(chat_id, text)
        except TelegramAPIError:
            log.exception("/run: failed to deliver result message to user %s", user_id)
    except Exception:
        log.exception("/run: pipeline crashed for user %s", user_id)
        try:
            await bot.send_message(
                chat_id,
                "❌ Manual run hit an unexpected error. We've logged it — "
                "run /support if it keeps happening.",
            )
        except TelegramAPIError:
            pass


@router.message(Command("run"))
async def cmd_run(message: Message) -> None:
    user = await _require_user(message)
    if user is None:
        return
    if user.status != UserStatus.active:
        await message.answer("Finish /start first.")
        return

    now_utc = datetime.now(timezone.utc)
    action = _manual_run_action_for(now_utc)
    async with get_session() as session:
        allowed, count = await rl.reserve_slot(
            session, user.id, action, limit=1,
        )
        await session.commit()

    if not allowed:
        await message.answer(
            f"⏳ You already ran this hour ({count} attempt(s)). "
            f"Next manual run available at <b>{_next_hour_ist(now_utc)}</b>."
        )
        return

    await message.answer(
        "🚀 Running your pipeline now — check back in ~2 minutes."
    )
    # Fire-and-forget: handler returns immediately, pipeline runs in bg,
    # result is DM'd via _run_pipeline_and_notify.
    assert message.bot is not None
    asyncio.create_task(
        _run_pipeline_and_notify(message.bot, user.id, message.chat.id)
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

        # Last 10 outreach attempts (any day) \u2014 the "trust" view that
        # answers "which companies has the bot already emailed for me?"
        log_stmt = (
            select(OutreachLog, Application, Job)
            .join(Application, Application.id == OutreachLog.application_id)
            .join(Job, Job.id == Application.job_id)
            .where(OutreachLog.user_id == user.id)
            .order_by(OutreachLog.sent_at.desc())
            .limit(10)
        )
        rows = (await session.execute(log_stmt)).all()

        # Pre-load this user's contact name map so we can show a friendly
        # "(Jane Doe)" next to manually-supplied emails without an N+1.
        contact_names = await _build_contact_name_map(session, user.id)

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
        lines.append("<b>Last 10 outreach attempts</b>")
        for ol, app, job in rows:
            lines.append(_format_outreach_line(ol, app, job, contact_names))
        lines.append("")
        lines.append("<i>Full list: /history</i>")

    await message.answer("\n".join(lines))


def _format_outreach_line(
    ol: OutreachLog,
    app: Application,
    job: Job,
    contact_names: dict[str, str],
) -> str:
    """One line per outreach row, e.g.

        \u2705 28 May \u2014 <b>Google</b> \u2192 john@google.com (Jane Doe) \U0001f4e9 Replied
    """
    if ol.error:
        marker = "\U0001f441" if "[NO-SEND]" in ol.error else "\u274c"
    else:
        marker = "\u2705"
    when = ol.sent_at.strftime("%d %b") if ol.sent_at else "?"
    name = contact_names.get((ol.to_email or "").lower())
    name_part = f" ({name})" if name else " (Recruiter)"
    replied = " \U0001f4e9 Replied" if app.status == AppStatus.replied else ""
    err_tail = f" \u2014 <i>{_short(ol.error, 60)}</i>" if (ol.error and "[NO-SEND]" not in ol.error) else ""
    return (
        f"  {marker} {when} \u2014 <b>{_short(job.company, 25)}</b> "
        f"\u2192 {ol.to_email}{name_part}{replied}{err_tail}"
    )


async def _build_contact_name_map(session, user_id: int) -> dict[str, str]:
    """``{lower(email): name}`` for the user's saved UserContact rows.

    Returns {} when the user has no contacts \u2014 callers fall back to a
    generic "(Recruiter)" label.
    """
    stmt = (
        select(UserContact.email, UserContact.name)
        .where(UserContact.user_id == user_id)
        .where(UserContact.name.is_not(None))
    )
    rows = (await session.execute(stmt)).all()
    return {(e or "").lower(): n for e, n in rows if e and n}


# ---------- /history: paginated full outreach log ----------
_HISTORY_PAGE_SIZE = 10


def _history_kb(page: int, total_pages: int) -> InlineKeyboardMarkup | None:
    """Prev/Next buttons. Returns None when only one page exists."""
    if total_pages <= 1:
        return None
    buttons: list[InlineKeyboardButton] = []
    if page > 0:
        buttons.append(InlineKeyboardButton(text="\u2b05\ufe0f Prev",
                                            callback_data=f"history:page:{page - 1}"))
    buttons.append(InlineKeyboardButton(
        text=f"Page {page + 1}/{total_pages}", callback_data="history:noop",
    ))
    if page < total_pages - 1:
        buttons.append(InlineKeyboardButton(text="Next \u27a1\ufe0f",
                                            callback_data=f"history:page:{page + 1}"))
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


async def _render_history(user_id: int, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
    """Build the message text + keyboard for the given page (0-indexed)."""
    async with get_session() as session:
        from sqlalchemy import func as sql_func
        total = int((await session.execute(
            select(sql_func.count()).select_from(OutreachLog)
            .where(OutreachLog.user_id == user_id)
        )).scalar_one())
        if total == 0:
            return (
                "\U0001f4ed You have no outreach history yet \u2014 nothing has "
                "been emailed on your behalf so far. The next daily run will "
                "populate this list.",
                None,
            )
        total_pages = max(1, (total + _HISTORY_PAGE_SIZE - 1) // _HISTORY_PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        offset = page * _HISTORY_PAGE_SIZE

        stmt = (
            select(OutreachLog, Application, Job)
            .join(Application, Application.id == OutreachLog.application_id)
            .join(Job, Job.id == Application.job_id)
            .where(OutreachLog.user_id == user_id)
            .order_by(OutreachLog.sent_at.desc())
            .limit(_HISTORY_PAGE_SIZE)
            .offset(offset)
        )
        rows = (await session.execute(stmt)).all()
        contact_names = await _build_contact_name_map(session, user_id)

    header = (
        f"\U0001f4dc <b>Outreach history</b> \u2014 {total} total, "
        f"page {page + 1}/{total_pages}\n"
    )
    body_lines = []
    for ol, app, job in rows:
        body_lines.append(
            f"\u2022 <b>{_short(job.company, 25)}</b> \u2014 "
            f"<i>{_short(job.title, 45)}</i>\n"
            f"  {_format_outreach_line(ol, app, job, contact_names).lstrip()}"
        )
    return header + "\n".join(body_lines), _history_kb(page, total_pages)


@router.message(Command("history"))
async def cmd_history(message: Message) -> None:
    user = await _require_user(message)
    if user is None:
        return
    text, kb = await _render_history(user.id, page=0)
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(F.data.startswith("history:page:"))
async def cb_history_page(cb: CallbackQuery) -> None:
    assert cb.data is not None and cb.from_user is not None
    try:
        page = int(cb.data.rsplit(":", 1)[1])
    except ValueError:
        await cb.answer()
        return
    text, kb = await _render_history(cb.from_user.id, page=page)
    if isinstance(cb.message, Message):
        try:
            await cb.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
        except Exception:
            # Edit fails when content is identical (e.g. spam-clicking the
            # noop "Page N/M" button). Telegram raises; we swallow.
            pass
    await cb.answer()


@router.callback_query(F.data == "history:noop")
async def cb_history_noop(cb: CallbackQuery) -> None:
    await cb.answer()


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
    # TODO(week-6): add "Bring your own contacts (/add_contacts) \u2014 zero lookup
    # cost, 100% hit rate" as a value bullet in the Pro pitch.
    from core.payments import create_payment_link_for_user  # local import to avoid cycle

    user = await _require_user(message)
    if user is None:
        return

    # Already paid? Tell them how long they have + offer renewal.
    if user.subscription_tier.value == "paid":
        from core.models import Subscription
        async with get_session() as session:
            sub = await session.get(Subscription, user.id)
        if sub is not None and sub.current_period_end is not None:
            days_left = max(
                0,
                (sub.current_period_end - datetime.now(timezone.utc)).days,
            )
            await message.answer(
                f"\u2728 You're already on <b>AutoApply Pro</b>. "
                f"Your plan is active for <b>{days_left}</b> more day(s) "
                f"(through {sub.current_period_end.strftime('%d %b %Y')}).\n\n"
                f"Want to extend? Reply /upgrade again within 3 days of "
                f"expiry and we'll stack the new month on top."
            )
            return

    # Mint fresh Razorpay payment link.
    try:
        async with get_session() as session:
            # Re-load inside this session so the row is attached.
            u = await session.get(User, user.id)
            assert u is not None
            short_url, link_id = await create_payment_link_for_user(session, u)
    except Exception:
        log.exception("razorpay payment link creation failed user_id=%s", user.id)
        await message.answer(
            "\u26a0\ufe0f Could not create your payment link right now. "
            "Try /upgrade again in a minute, or DM the operator if it keeps failing."
        )
        return

    log.info("/upgrade: minted link_id=%s for user_id=%s", link_id, user.id)
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
        "\u2022 Your own Hunter.io recruiter search quota (never runs out)\n"
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
        f"<b>\u2192 Pay now (Razorpay, \u20b9500):</b>\n{short_url}\n\n"
        "<i>Link valid for 7 days. Pay with UPI / card / netbanking \u2014 your "
        "account upgrades automatically within seconds of payment.</i>\n"
        "\n"
        "Cancel anytime. No lock-in. Try /features for the full list.",
        disable_web_page_preview=True,
    )


@router.message(Command("subscription"))
async def cmd_subscription(message: Message) -> None:
    """Show the user's current plan + expiry."""
    from core.models import Subscription

    user = await _require_user(message)
    if user is None:
        return
    async with get_session() as session:
        sub = await session.get(Subscription, user.id)

    tier_label = "\u2728 Pro" if user.subscription_tier.value == "paid" else "Free"
    lines = [
        f"<b>Plan:</b> {tier_label}",
        f"<b>Daily limits:</b> {user.daily_scan_limit} scans, "
        f"{user.daily_outreach_limit} outreach emails",
    ]
    if user.subscription_tier.value == "paid" and sub is not None and sub.current_period_end is not None:
        days_left = max(
            0,
            (sub.current_period_end - datetime.now(timezone.utc)).days,
        )
        lines.append(
            f"<b>Active until:</b> {sub.current_period_end.strftime('%d %b %Y')} "
            f"({days_left} day(s) remaining)"
        )
        lines.append("")
        lines.append("Tap /upgrade within 3 days of expiry to renew.")
    else:
        lines.append("")
        lines.append("Tap /upgrade to unlock Pro (\u20b9500/month).")

    await message.answer("\n".join(lines))


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
        f"<i>(applied automatically once paid-tier upgrades land via Razorpay)</i>",
        disable_web_page_preview=True,
    )


def _short(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "\u2026"
