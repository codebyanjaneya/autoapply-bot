"""Onboarding wizard for new users.

Flow (driven by an FSM in MemoryStorage):

    /start
      → ROLES           "What roles? (comma-separated)"
      → LOCATIONS       "Which cities? (comma-separated; 'Remote' allowed)"
      → NAME            "Your full name (used in email signature)"
      → RESUME          (upload PDF)
      → SMTP_EMAIL      "Your Gmail address (we send via SMTP)"
      → SMTP_PASSWORD   "Gmail app password (16 chars)"  (message auto-deleted)
      → done            User.status = active; ready for tomorrow's run.

Apollo note: a previous revision had a Step 7 asking the user for their own
Apollo API key. Apollo's free plan returns 403 API_INACCESSIBLE on the
mixed_people/search endpoint we depend on — so the key was useless for
free-tier users. The Apollo client code (core/enrich/apollo.py) is kept
in place and silently no-ops, ready to re-enable as a Pro-tier benefit
once we can subsidise the ~$49/month paid plan.

Why Gmail SMTP and app passwords?
    - Truly zero operator cost (no domain, no transactional sender bill)
    - Per-user 500 emails/day cap scales linearly with users
    - From: header IS the user's own Gmail, so replies land natively in
      their inbox — no Reply-To workarounds, no "via" disclosure
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

from core.crypto import encrypt
from core.db import get_session
from core.mailer.smtp_sender import SMTPSender
from core.models import (
    SubscriptionTier, User, UserCredentials, UserPreferences, UserStatus,
)
from core.referrals import decode_referral_code

log = logging.getLogger(__name__)
router = Router(name="onboarding")

MAX_RESUME_BYTES = 10 * 1024 * 1024  # 10 MB — generous; Telegram caps at 20 MB anyway


class Onboarding(StatesGroup):
    ROLES = State()
    LOCATIONS = State()
    NAME = State()
    RESUME = State()
    SMTP_EMAIL = State()
    SMTP_PASSWORD = State()


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
        # Re-fetch to return a detached snapshot — caller only reads tier.
        await session.refresh(user)
        return user


def _split_csv(text: str) -> list[str]:
    return [t.strip() for t in text.split(",") if t.strip()]


async def _silent_delete(message: Message) -> None:
    """Best-effort delete — used after capturing sensitive input."""
    try:
        await message.delete()
    except Exception:
        log.warning("could not delete sensitive message %s", message.message_id)


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
                f"Your pipeline runs automatically every day at <b>09:00 IST</b>.\n\n"
                f"\u2022 /status \u2014 today's run summary\n"
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
    await state.set_state(Onboarding.ROLES)
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
        f"<b>Step 1/6</b> — What roles are you looking for? "
        f"Comma-separated, e.g.\n<code>python developer, backend engineer, ai engineer</code>"
    )


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
    """Explicit opt-in to re-run the full onboarding wizard.

    Unlike /start, this works even when the user is already ``active`` and
    has all data filled in. Existing rows stay intact until they finish all
    7 steps and ``_finish_onboarding`` re-writes them, so an abandoned
    /restart never deletes anything.
    """
    await _upsert_user(message)  # bumps status back to onboarding
    await state.clear()
    await state.set_state(Onboarding.ROLES)
    await message.answer(
        "\U0001f504 Restarting onboarding from scratch. Your current "
        "settings stay intact until you finish all 7 steps.\n\n"
        "<b>Step 1/6</b> \u2014 What roles are you looking for? "
        "Comma-separated, e.g.\n<code>python developer, backend engineer, ai engineer</code>"
    )


# ---------- step 1: roles ----------
@router.message(Onboarding.ROLES)
async def step_roles(message: Message, state: FSMContext) -> None:
    roles = _split_csv(message.text or "")
    if not roles:
        await message.answer("Please send at least one role. e.g. <code>python developer</code>")
        return
    if len(roles) > 5:
        await message.answer("Max 5 roles — send a shorter list.")
        return
    await state.update_data(roles=roles)
    await state.set_state(Onboarding.LOCATIONS)
    await message.answer(
        f"Got it: <b>{', '.join(roles)}</b>\n\n"
        f"<b>Step 2/6</b> — Which locations? Comma-separated, e.g.\n"
        f"<code>Bengaluru, Hyderabad, Remote</code>"
    )


# ---------- step 2: locations ----------
@router.message(Onboarding.LOCATIONS)
async def step_locations(message: Message, state: FSMContext) -> None:
    locs = _split_csv(message.text or "")
    if not locs:
        await message.answer("Please send at least one location.")
        return
    await state.update_data(locations=locs)
    await state.set_state(Onboarding.NAME)
    await message.answer(
        f"<b>Step 3/6</b> — What's your full name? "
        f"This goes in the email signature recruiters see."
    )


# ---------- step 3: name ----------
@router.message(Onboarding.NAME)
async def step_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if len(name) < 2 or len(name) > 100:
        await message.answer("Name must be 2-100 characters.")
        return
    await state.update_data(candidate_name=name)
    await state.set_state(Onboarding.RESUME)
    await message.answer(
        f"Thanks, <b>{name}</b>.\n\n"
        f"<b>Step 4/6</b> — Upload your resume as a PDF (max 10MB).\n"
        f"\n"
        f"Tip: on phone, tap the 📎 attachment icon → <b>File</b> → pick your PDF."
    )


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
    # Download to memory — small file, no need to spool to disk.
    bot = message.bot
    assert bot is not None
    buf = await bot.download(doc)
    if buf is None:
        await message.answer("Couldn't download that file. Try again.")
        return
    resume_bytes = buf.read()
    await state.update_data(resume_pdf=resume_bytes, resume_filename=fname)
    await state.set_state(Onboarding.SMTP_EMAIL)
    await message.answer(
        f"Resume saved ({len(resume_bytes) // 1024} KB).\n\n"
        f"<b>Step 5/6</b> — Gmail address for sending outreach emails\n"
        f"\n"
        f"AutoApply sends from your own Gmail (via SMTP), so:\n"
        f"• Recruiters see <i>your</i> name in their inbox\n"
        f"• Replies land directly in <i>your</i> Gmail — we never see them\n"
        f"• You get 500 emails/day quota (Gmail's normal limit)\n"
        f"\n"
        f"⚠️ <b>One small thing:</b> please use a Gmail with "
        f"<b>2-Step Verification enabled</b>.\n"
        f"\n"
        f"<b>Why?</b> It's actually for <i>your</i> protection:\n"
        f"🔒 Keeps your Gmail account safe from break-ins\n"
        f"🛡️ Stops anyone (including us) from ever using your real password\n"
        f"✅ Lets you generate an <b>App Password</b> — a separate, "
        f"revocable key just for AutoApply\n"
        f"\n"
        f"<i>Your real Gmail password is never stored, seen, or even asked for. "
        f"We only use the App Password, and you can revoke it from your Google "
        f"account at any time.</i>\n"
        f"\n"
        f"<b>Haven't turned on 2-Step Verification yet?</b>\n"
        f"👉 Enable it here (takes ~1 minute):\n"
        f"    → https://myaccount.google.com/signinoptions/twosv\n"
        f"\n"
        f"Once that's done, send your Gmail address below — e.g. "
        f"<code>you@gmail.com</code>.\n"
        f"\n"
        f"<i>Google Workspace email (you@yourcompany.com) also works as long "
        f"as it's a Google-hosted mailbox with 2-Step Verification on.</i>",
        disable_web_page_preview=True,
    )


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
        creds.candidate_name = data.get("candidate_name")
        creds.resume_pdf = data["resume_pdf"]
        creds.resume_filename = data["resume_filename"]
        creds.resume_uploaded_at = datetime.now(timezone.utc)
        await session.commit()

    await state.set_state(Onboarding.SMTP_PASSWORD)
    await message.answer(
        f"\u2705 Got it: <b>{email}</b>\n\n"
        f"<b>Step 6/6</b> \u2014 Gmail <b>app password</b> (16 chars)\n"
        f"\n"
        f"This lets AutoApply send mail from your Gmail without your real "
        f"password. <b>It's not your Gmail login password.</b>\n"
        f"\n"
        f"<b>How to get it (2 min, one-time):</b>\n"
        f"\n"
        f"1\ufe0f\u20e3  Make sure 2-Step Verification is ON\n"
        f"    \u2192 https://myaccount.google.com/security\n"
        f"\n"
        f"2\ufe0f\u20e3  Generate an app password\n"
        f"    \u2192 https://myaccount.google.com/apppasswords\n"
        f"    Name it <code>AutoApply</code>, copy the 16-character code.\n"
        f"\n"
        f"3\ufe0f\u20e3  Paste it here (spaces are fine; I'll strip them).\n"
        f"\n"
        f"\U0001f512 I'll delete your message and store the password encrypted. "
        f"You can revoke it anytime from the same Google page.",
        disable_web_page_preview=True,
    )


# ---------- step 6: SMTP app password ----------
@router.message(Onboarding.SMTP_PASSWORD)
async def step_smtp_password(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    # Strip spaces \u2014 Google formats app passwords as "abcd efgh ijkl mnop"
    # to make them readable, but the actual password is the 16 chars.
    password = raw.replace(" ", "")
    await _silent_delete(message)
    if len(password) != 16 or not password.isalnum():
        await message.answer(
            "That doesn't look like a Gmail app password (should be 16 letters "
            "and numbers). Get one at https://myaccount.google.com/apppasswords "
            "and try again."
        )
        return

    assert message.from_user is not None
    async with get_session() as session:
        creds = await session.get(UserCredentials, message.from_user.id)
        if creds is None or not creds.smtp_email:
            await message.answer("Internal error: email row missing. /start to restart.")
            return
        smtp_email = creds.smtp_email

    # Verify against Gmail BEFORE we persist \u2014 saves users from discovering
    # tomorrow that their password was wrong all along.
    verifying = await message.answer("\U0001f50d Verifying with Gmail\u2026")
    sender = SMTPSender(email=smtp_email, password=password)
    ok, reason = await sender.verify()
    try:
        await verifying.delete()
    except Exception:
        pass
    if not ok:
        log.warning("smtp verify failed for user %s: %s", message.from_user.id, reason)
        await message.answer(
            f"\u274c Gmail rejected that password ({reason[:80]}).\n\n"
            f"Common fixes:\n"
            f"\u2022 Make sure 2-Step Verification is ON for {smtp_email}\n"
            f"\u2022 Generate a NEW app password (each is shown only once)\n"
            f"\u2022 Don't paste your normal Gmail password \u2014 must be an app password\n\n"
            f"Try again, or /cancel to abort."
        )
        return

    async with get_session() as session:
        creds = await session.get(UserCredentials, message.from_user.id)
        assert creds is not None
        creds.smtp_password_encrypted = encrypt(password)
        await session.commit()

    await message.answer("\u2705 Gmail connected and verified.")

    # Recruiter lookup runs against the operator-pooled Hunter key only
    # (HUNTER_API_KEY env). A previous revision asked users for their own
    # Apollo key here, but Apollo's free plan returns 403 API_INACCESSIBLE
    # on the People Search endpoint, so the key was useless for free-tier
    # users. Apollo client code is dormant; will re-enable as a Pro perk
    # once we can subsidise the ~$49/mo Apollo paid plan.
    await _finish_onboarding(message, state)


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
        prefs.role_keywords = data.get("roles", [])
        prefs.locations = data.get("locations", [])
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
        f"Your pipeline will run automatically every day at <b>09:00 IST</b>.\n\n"
        f"Commands:\n"
        f"\u2022 /status \u2014 today's run summary\n"
        f"\u2022 /pause  \u2014 pause daily runs\n"
        f"\u2022 /resume \u2014 resume\n"
        f"\u2022 /referral \u2014 invite friends, earn free months\n"
        f"\u2022 /help   \u2014 all commands"
        f"{upgrade_footer}"
    )
