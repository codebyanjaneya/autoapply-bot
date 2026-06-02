"""Onboarding wizard for new users.

Flow (driven by an FSM in MemoryStorage):

    /start
      â†’ ROLES           roles list
      â†’ LOCATIONS       cities (Remote allowed)
      â†’ NAME            full name for email signature
      â†’ RESUME          PDF upload
      â†’ SMTP_EMAIL      personal email (used as Reply-To only)
      â†’ done            User.status = active; ready for tomorrow's run.

Apollo note: a previous revision had a Step 7 asking the user for their own
Apollo API key. Apollo's free plan returns 403 API_INACCESSIBLE on the
mixed_people/search endpoint we depend on â€” so the key was useless for
free-tier users. The Apollo client code (core/enrich/apollo.py) is kept
in place and silently no-ops, ready to re-enable as a Pro-tier benefit
once we can subsidise the ~$49/month paid plan.

Why Gmail SMTP and app passwords?
    - Truly zero operator cost (no domain, no transactional sender bill)
    - Per-user 500 emails/day cap scales linearly with users
    - From: header IS the user's own Gmail, so replies land natively in
      their inbox â€” no Reply-To workarounds, no "via" disclosure
    - The 2-minute app-password setup is friction, but acceptable for a
      technical audience hunting dev jobs.

Security:
- SMTP password message is deleted from chat after we capture it.
- Stored encrypted (Fernet) before any database write.
- App password is verified against Gmail SMTP BEFORE we save it, so the
  user finds out immediately if it's wrong.

Idempotency:
- Re-running /start at any point restarts the wizard (FSMContext.clear()).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from core.db import get_session
from core.models import (
    SubscriptionTier, User, UserCredentials, UserPreferences, UserStatus,
)
from core.referrals import decode_referral_code

log = logging.getLogger(__name__)
router = Router(name="onboarding")

MAX_RESUME_BYTES = 10 * 1024 * 1024  # 10 MB â€” generous; Telegram caps at 20 MB anyway


class Onboarding(StatesGroup):
    ROLES = State()
    LOCATIONS = State()
    NAME = State()
    RESUME = State()
    SMTP_EMAIL = State()


# ---------- helpers ----------
async def _upsert_user(message: Message, *, referrer_id: int | None = None) -> User:
    """Idempotent: create or fetch the User row + empty prefs/creds children.

    ``referrer_id``: if this is the user's FIRST onboarding (no User row yet),
    persist them as ``referred_by_user_id = referrer_id``. Ignored when the
    user already exists \u2014 attribution is set-once.
    """
    tg_user = message.from_user
    assert tg_user is not None
    async with get_session() as session:
        user = await session.get(User, tg_user.id)
        if user is None:
            # Self-referrals don't count, and unknown referrer ids are dropped.
            attributed_to: int | None = None
            if referrer_id is not None and referrer_id != tg_user.id:
                if await session.get(User, referrer_id) is not None:
                    attributed_to = referrer_id
                else:
                    log.info("ignoring referral: unknown referrer id %s", referrer_id)
            user = User(
                id=tg_user.id,
                telegram_chat_id=message.chat.id,
                username=tg_user.username,
                first_name=tg_user.first_name,
                language_code=tg_user.language_code,
                subscription_tier=SubscriptionTier.free,
                status=UserStatus.onboarding,
                referred_by_user_id=attributed_to,
            )
            session.add(user)
            session.add(UserPreferences(user_id=tg_user.id, role_keywords=[], locations=[], skills=[]))
            session.add(UserCredentials(user_id=tg_user.id))
        else:
            # Bump status back to onboarding so a re-/start is a clean re-do.
            user.status = UserStatus.onboarding
            user.telegram_chat_id = message.chat.id
        await session.commit()
        # Re-fetch to return a detached snapshot â€” caller only reads tier.
        await session.refresh(user)
        return user


def _split_csv(text: str) -> list[str]:
    return [t.strip() for t in text.split(",") if t.strip()]


async def _silent_delete(message: Message) -> None:
    """Best-effort delete â€” used after capturing sensitive input."""
    try:
        await message.delete()
    except Exception:
        log.warning("could not delete sensitive message %s", message.message_id)


# ---------- step prompts ----------
# Each prompt function sends the question for one onboarding step and
# transitions the FSM into that step's state. Called by _ask_next() which
# decides which step is next based on what's already in the DB / FSM data.

async def _prompt_roles(message: Message, state: FSMContext) -> None:
    await state.set_state(Onboarding.ROLES)
    await message.answer(
        "<b>Step 1/5</b> \u2014 What roles are you looking for?\n"
        "e.g. <code>python developer, backend engineer, ai engineer</code>"
    )


async def _prompt_locations(message: Message, state: FSMContext) -> None:
    await state.set_state(Onboarding.LOCATIONS)
    await message.answer(
        "<b>Step 2/5</b> \u2014 Which locations?\n"
        "e.g. <code>Bengaluru, Hyderabad, Remote</code>"
    )


async def _prompt_name(message: Message, state: FSMContext) -> None:
    await state.set_state(Onboarding.NAME)
    await message.answer(
        "<b>Step 3/5</b> \u2014 What's your full name? "
        "This goes in the email signature recruiters see."
    )


async def _prompt_resume(message: Message, state: FSMContext) -> None:
    await state.set_state(Onboarding.RESUME)
    await message.answer(
        "<b>Step 4/5</b> \u2014 Upload your resume as a PDF (max 10MB).\n"
        "\n"
        "Tip: on phone, tap the \U0001f4ce attachment icon \u2192 <b>File</b> \u2192 pick your PDF."
    )


async def _prompt_smtp_email(message: Message, state: FSMContext) -> None:
    await state.set_state(Onboarding.SMTP_EMAIL)
    await message.answer(
        "<b>Step 5/5</b> \u2014 Your email address (for recruiter replies)\n"
        "\n"
        "AutoApply sends outreach on your behalf from our verified domain. "
        "When a recruiter hits <b>Reply</b>, the message goes <i>straight "
        "to this inbox</i> \u2014 we never see it.\n"
        "\n"
        "Send your email address below \u2014 e.g. <code>you@gmail.com</code>. "
        "Any provider works (Gmail, Outlook, custom domain).",
        disable_web_page_preview=True,
    )


async def _ask_next(message: Message, state: FSMContext) -> None:
    """Send the prompt for the next un-filled onboarding step.

    Skips steps whose values are already in the DB (or already collected in
    this wizard session via ``state.update_data``). If everything is filled,
    finishes onboarding immediately. This is what makes /start and /restart
    idempotent: a user who already entered their Gmail in a previous session
    is NEVER asked for it again.
    """
    assert message.from_user is not None
    user_id = message.from_user.id
    data = await state.get_data()
    async with get_session() as session:
        prefs = await session.get(UserPreferences, user_id)
        creds = await session.get(UserCredentials, user_id)

    # Step 1 \u2014 roles
    if not (data.get("roles") or (prefs and prefs.role_keywords)):
        await _prompt_roles(message, state)
        return
    # Step 2 \u2014 locations
    if not (data.get("locations") or (prefs and prefs.locations)):
        await _prompt_locations(message, state)
        return
    # Step 3 \u2014 candidate name
    if not (data.get("candidate_name") or (creds and creds.candidate_name)):
        await _prompt_name(message, state)
        return
    # Step 4 \u2014 resume
    if not (data.get("resume_pdf") or (creds and creds.resume_pdf)):
        await _prompt_resume(message, state)
        return
    # Step 5 — user email (Reply-To for recruiter responses)
    if not (creds and creds.smtp_email):
        await _prompt_smtp_email(message, state)
        return

    # Nothing missing \u2014 mark active without re-asking anything.
    await _finish_onboarding(message, state)


# ---------- handlers ----------
@router.message(CommandStart(deep_link=True))
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, command: CommandObject) -> None:
    # Telegram deep-link: t.me/<bot>?start=AA<code> arrives as "/start AA<code>".
    # command.args contains everything after "/start ".
    referrer_id = decode_referral_code(command.args) if command and command.args else None

    # Returning user shortcut: if onboarding is already complete, don't wipe
    # state and force them back through the wizard. Just say hello and point
    # them at /settings + /status. Re-onboarding from scratch is opt-in via
    # /restart so a stray /start tap never destroys their config.
    assert message.from_user is not None
    async with get_session() as session:
        existing = await session.get(User, message.from_user.id)
        if existing is not None and existing.status == UserStatus.active:
            # Refresh chat_id in case the user blocked + re-added the bot.
            existing.telegram_chat_id = message.chat.id
            await session.commit()
            name = existing.first_name or "there"
            await state.clear()
            await message.answer(
                f"\U0001f44b Welcome back, <b>{name}</b>!\n\n"
                f"Your pipeline runs automatically every day "
                f"(see /status for your run time).\n\n"
                f"\u2022 /status \u2014 today's run summary\n"
                f"\u2022 /settime \u2014 change your daily run time\n"
                f"\u2022 /settings \u2014 update Gmail, roles, resume, etc.\n"
                f"\u2022 /pause \u2014 pause daily runs (use /resume to turn back on)\n"
                f"\u2022 /referral \u2014 invite friends, earn free months\n"
                f"\u2022 /help \u2014 all commands\n\n"
                f"<i>Want to redo onboarding from scratch? Send /restart \u2014 "
                f"your existing data stays intact until you finish.</i>"
            )
            return

    user = await _upsert_user(message, referrer_id=referrer_id)
    await state.clear()
    tier_blurb = (
        "You're on the <b>free</b> tier (5 outreach emails/day). "
        "Use /upgrade to switch to paid."
    ) if user.subscription_tier == SubscriptionTier.free else (
        "You're on the <b>paid</b> tier (15 outreach emails/day)."
    )
    referral_blurb = (
        "\n\n\U0001f381 <i>You were invited by a friend \u2014 when you upgrade "
        "to Pro, they get a month free as a thank-you.</i>"
        if user.referred_by_user_id else ""
    )
    await message.answer(
        f"Hi {user.first_name or 'there'}! Let's set up your job hunt.\n\n"
        f"{tier_blurb}{referral_blurb}\n\n"
        f"<i>New here? Send /howitworks for a 7-step walkthrough of what "
        f"the bot does \u2014 you can do this any time, even mid-setup.</i>"
    )
    # Ask only for the first field that's actually missing (existing values
    # from a previous incomplete onboarding are preserved \u2014 we never re-ask).
    await _ask_next(message, state)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current is None:
        await message.answer("Nothing to cancel.")
        return
    await state.clear()
    await message.answer("Onboarding cancelled. /start to begin again.")


@router.message(Command("restart"))
async def cmd_restart(message: Message, state: FSMContext) -> None:
    """Explicit opt-in to re-run the onboarding wizard.

    Unlike /start, this works even when the user is already ``active``. Existing
    rows stay intact; ``_ask_next`` will skip any step whose value is already
    saved in the DB, so the user is only asked for fields that are genuinely
    missing (e.g. after a row was nulled by an operator). To change a value
    that's already saved, use /settings instead.
    """
    await _upsert_user(message)  # bumps status back to onboarding
    await state.clear()
    await message.answer(
        "\U0001f504 Resuming onboarding. Any answers you already gave are "
        "kept \u2014 I'll only ask for what's missing. To change something "
        "that's already saved, use /settings."
    )
    await _ask_next(message, state)


# ---------- step 1: roles ----------
@router.message(Onboarding.ROLES)
async def step_roles(message: Message, state: FSMContext) -> None:
    roles = _split_csv(message.text or "")
    if not roles:
        await message.answer("Please send at least one role. e.g. <code>python developer</code>")
        return
    if len(roles) > 5:
        await message.answer("Max 5 roles â€” send a shorter list.")
        return
    await state.update_data(roles=roles)
    await message.answer(f"Got it: <b>{', '.join(roles)}</b>")
    await _ask_next(message, state)


# ---------- step 2: locations ----------
@router.message(Onboarding.LOCATIONS)
async def step_locations(message: Message, state: FSMContext) -> None:
    locs = _split_csv(message.text or "")
    if not locs:
        await message.answer("Please send at least one location.")
        return
    await state.update_data(locations=locs)
    await _ask_next(message, state)


# ---------- step 3: name ----------
@router.message(Onboarding.NAME)
async def step_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if len(name) < 2 or len(name) > 100:
        await message.answer("Name must be 2-100 characters.")
        return
    await state.update_data(candidate_name=name)
    await message.answer(f"Thanks, <b>{name}</b>.")
    await _ask_next(message, state)


# ---------- step 4: resume ----------
@router.message(Onboarding.RESUME, F.document)
async def step_resume(message: Message, state: FSMContext) -> None:
    doc = message.document
    assert doc is not None
    if (doc.file_size or 0) > MAX_RESUME_BYTES:
        await message.answer(f"PDF is too large (max {MAX_RESUME_BYTES // 1024 // 1024} MB).")
        return
    fname = doc.file_name or "resume.pdf"
    if not fname.lower().endswith(".pdf") and doc.mime_type != "application/pdf":
        await message.answer("Please send a PDF file.")
        return
    # Download to memory â€” small file, no need to spool to disk.
    bot = message.bot
    assert bot is not None
    buf = await bot.download(doc)
    if buf is None:
        await message.answer("Couldn't download that file. Try again.")
        return
    resume_bytes = buf.read()
    await state.update_data(resume_pdf=resume_bytes, resume_filename=fname)
    await message.answer(f"Resume saved ({len(resume_bytes) // 1024} KB).")
    await _ask_next(message, state)


@router.message(Onboarding.RESUME)
async def step_resume_not_a_doc(message: Message, state: FSMContext) -> None:
    await message.answer("Please attach a PDF, not text.")


# ---------- step 5: SMTP email (Gmail address) ----------
@router.message(Onboarding.SMTP_EMAIL)
async def step_smtp_email(message: Message, state: FSMContext) -> None:
    email = (message.text or "").strip().lower()
    if (
        "@" not in email
        or len(email) < 5
        or " " in email
        or "." not in email.split("@", 1)[-1]
    ):
        await message.answer(
            "That doesn't look like an email address. Try again, e.g. "
            "<code>you@gmail.com</code>"
        )
        return

    # Persist email immediately so a wizard crash before app-password capture
    # doesn't lose progress. The encrypted password is written in step 6.
    data = await state.get_data()
    assert message.from_user is not None
    async with get_session() as session:
        creds = await session.get(UserCredentials, message.from_user.id)
        if creds is None:
            creds = UserCredentials(user_id=message.from_user.id)
            session.add(creds)
        creds.smtp_email = email
        # Only overwrite these when freshly captured in THIS wizard session
        # \u2014 a re-entry via /restart that only fills missing fields must not
        # nuke values that were already saved on a prior run.
        if data.get("candidate_name"):
            creds.candidate_name = data["candidate_name"]
        if data.get("resume_pdf"):
            creds.resume_pdf = data["resume_pdf"]
            creds.resume_filename = data["resume_filename"]
            creds.resume_uploaded_at = datetime.now(timezone.utc)
        await session.commit()

    await _ask_next(message, state)




async def _finish_onboarding(message: Message, state: FSMContext) -> None:
    # TODO(week-6): once /add_contacts ships, append a "Pro tip" line to the
    # completion message for BOTH tiers:
    #   "\U0001f4a1 <i>Pro tip: already have recruiter emails? Use /add_contacts\n"
    #   "to send directly without using any Apollo lookups.</i>"
    # Keep it as the LAST line so it reads like a friendly aside, not a
    # feature announcement. Don't add it now \u2014 the command doesn't exist yet
    # and we don't want users tapping a dead /command on first run.
    data = await state.get_data()
    assert message.from_user is not None
    user_id = message.from_user.id
    async with get_session() as session:
        user = await session.get(User, user_id)
        prefs = await session.get(UserPreferences, user_id)
        creds = await session.get(UserCredentials, user_id)
        if user is None or prefs is None:
            await message.answer("Internal error: user row missing. /start to restart.")
            return
        # Only overwrite prefs with values freshly captured in THIS wizard
        # session. /restart that skips already-filled steps must keep the
        # existing DB values intact.
        if data.get("roles"):
            prefs.role_keywords = data["roles"]
        if data.get("locations"):
            prefs.locations = data["locations"]
        user.status = UserStatus.active
        tier = user.subscription_tier
        # Snapshot fields for logging before the session closes (ORM
        # instances become unusable after commit / context exit).
        log_name = (creds.candidate_name if creds else None) or "?"
        log_email = (creds.smtp_email if creds else None) or "(none)"
        await session.commit()
    await state.clear()
    # Operator-facing audit line: lets us see real Telegram user IDs in the
    # log as users complete onboarding, so we can trigger /run_now manually.
    log.info(
        "user %s completed onboarding (name=%r, email=%s, tier=%s)",
        user_id, log_name, log_email, tier.value,
    )
    upgrade_footer = (
        "\n\n<i>You're on the free plan \u2014 5 outreach/day, 20 scans/day. "
        "Most users find jobs faster on Pro (15 outreach + 50 scans + no Hunter "
        "quota worries, \u20b9500/mo). Try /upgrade to see why.</i>"
        if tier == SubscriptionTier.free else ""
    )
    await message.answer(
        f"\U0001f389 You're all set!\n\n"
        f"Your pipeline will run automatically every day at <b>09:00 IST</b> "
        f"(change with /settime).\n\n"
        f"Commands:\n"
        f"\u2022 /status \u2014 today's run summary\n"
        f"\u2022 /settime \u2014 change your daily run time (default 9 AM IST)\n"
        f"\u2022 /pause  \u2014 pause daily runs\n"
        f"\u2022 /resume \u2014 resume\n"
        f"\u2022 /referral \u2014 invite friends, earn free months\n"
        f"\u2022 /help   \u2014 all commands\n\n"
        f"\U0001f6df Having issues? /support is always available \u2014 we reply within 24h."
        f"{upgrade_footer}"
    )

