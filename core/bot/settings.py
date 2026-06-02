"""Settings wizard — let returning users update individual fields without
redoing the full /start onboarding.

Surface area:
    /settings              \u2192 inline-keyboard overview of current config
    callback "settings:X"  \u2192 enter an FSM state to capture a new value for X
    Settings.* states      \u2192 one-shot handlers that validate, persist, exit

Why a separate router?
    Onboarding.* states are the linear wizard; Settings.* states are
    independent edits that DON'T chain. Splitting them keeps the two flows
    from accidentally short-circuiting each other (e.g. a /skip during a
    Settings edit shouldn't trigger Onboarding's _finish_onboarding).

Security:
    SMTP app password messages are best-effort deleted from the chat
    after capture; the password is Fernet-encrypted before any DB write.

Apollo note: a previous revision exposed an "Apollo key" field here.
Apollo's free plan returns 403 API_INACCESSIBLE on People Search, so we
stopped collecting keys from free users. The Apollo backend client is
kept dormant in core/enrich/apollo.py and will be re-enabled as a Pro-
tier perk once we can subsidise Apollo's ~$49/mo paid plan.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from core.bot.handlers import _build_settime_keyboard, _format_hour_label
from core.crypto import encrypt
from core.db import get_session
from core.mailer.smtp_sender import SMTPSender
from core.models import User, UserCredentials, UserPreferences, UserStatus

log = logging.getLogger(__name__)
router = Router(name="settings")

MAX_RESUME_BYTES = 10 * 1024 * 1024  # match onboarding.py


class Settings(StatesGroup):
    SMTP_EMAIL = State()       # awaits new Gmail; transitions to SMTP_PASSWORD
    SMTP_PASSWORD = State()    # awaits app password; verifies; persists
    ROLES = State()
    LOCATIONS = State()
    NAME = State()
    RESUME = State()


# ---------- helpers ----------
def _split_csv(text: str) -> list[str]:
    return [t.strip() for t in text.split(",") if t.strip()]


async def _silent_delete(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        log.warning("settings: could not delete sensitive message %s", message.message_id)


def _format_relative(when: datetime | None) -> str:
    """"3 days ago" / "today" / "2 hours ago" — best-effort, never raises."""
    if when is None:
        return "—"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - when
    if delta < timedelta(minutes=1):
        return "just now"
    if delta < timedelta(hours=1):
        m = int(delta.total_seconds() // 60)
        return f"{m} minute{'s' if m != 1 else ''} ago"
    if delta < timedelta(days=1):
        h = int(delta.total_seconds() // 3600)
        return f"{h} hour{'s' if h != 1 else ''} ago"
    d = delta.days
    if d == 1:
        return "yesterday"
    if d < 30:
        return f"{d} days ago"
    if d < 365:
        return f"{d // 30} month{'s' if d // 30 != 1 else ''} ago"
    return when.strftime("%Y-%m-%d")


async def _load_config(user_id: int) -> tuple[User, UserPreferences, UserCredentials] | None:
    async with get_session() as session:
        user = await session.get(User, user_id)
        prefs = await session.get(UserPreferences, user_id)
        creds = await session.get(UserCredentials, user_id)
    if user is None or prefs is None or creds is None:
        return None
    return user, prefs, creds


def _build_keyboard(smtp_missing: bool) -> InlineKeyboardMarkup:
    """Two-column inline keyboard. The Gmail button gets a \u26a0\ufe0f badge
    when unconfigured so the user's eye lands on the gap first."""
    gmail_label = "\U0001f4e7 Gmail" + (" \u26a0\ufe0f" if smtp_missing else "")
    rows = [
        [
            InlineKeyboardButton(text=gmail_label, callback_data="settings:gmail"),
            InlineKeyboardButton(text="\U0001f4c4 Resume", callback_data="settings:resume"),
        ],
        [
            InlineKeyboardButton(text="\U0001f464 Name", callback_data="settings:name"),
            InlineKeyboardButton(text="\U0001f4bc Roles", callback_data="settings:roles"),
        ],
        [
            InlineKeyboardButton(text="\U0001f4cd Locations", callback_data="settings:locations"),
            InlineKeyboardButton(text="\u23f0 Run time", callback_data="settings:settime"),
        ],
        [
            InlineKeyboardButton(text="\u2716\ufe0f Done", callback_data="settings:done"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _render_overview(user: User, prefs: UserPreferences, creds: UserCredentials) -> tuple[str, InlineKeyboardMarkup]:
    smtp_set = bool(creds.smtp_email) and creds.smtp_password_encrypted is not None
    resume_set = creds.resume_pdf is not None

    # Pre-compute fallback strings: f-string expressions can't contain
    # backslashes, so we can't inline things like '\u2014 not set' inside {}.
    not_set = "\u2014 not set"
    name_txt = creds.candidate_name or not_set
    roles_txt = ", ".join(prefs.role_keywords) if prefs.role_keywords else not_set
    locs_txt = ", ".join(prefs.locations) if prefs.locations else not_set

    lines = ["\U0001f6e0\ufe0f <b>Your AutoApply settings</b>", "", "<b>Current config</b>"]
    lines.append(f"\u2022 Name: {name_txt}")
    lines.append(f"\u2022 Roles: {roles_txt}")
    lines.append(f"\u2022 Locations: {locs_txt}")
    if resume_set:
        size_kb = (len(creds.resume_pdf) // 1024) if creds.resume_pdf else 0  # type: ignore[arg-type]
        when = _format_relative(creds.resume_uploaded_at)
        rname = creds.resume_filename or "resume.pdf"
        lines.append(f"\u2022 Resume: \u2705 {rname} ({size_kb} KB, updated {when})")
    else:
        lines.append("\u2022 Resume: \u274c not uploaded")
    if smtp_set:
        lines.append(f"\u2022 Gmail: \u2705 {creds.smtp_email} (verified)")
    elif creds.smtp_email:
        lines.append(f"\u2022 Gmail: \u26a0\ufe0f {creds.smtp_email} (app password not set)")
    else:
        lines.append("\u2022 Gmail: \u274c not set \u2014 outreach disabled")
    lines.append(
        f"\u2022 Run time: \u23f0 "
        f"{_format_hour_label(prefs.preferred_run_hour)} IST"
    )

    if not smtp_set:
        lines += [
            "",
            "\u26a0\ufe0f <b>Gmail not connected</b> \u2014 outreach won't run until "
            "you finish setting up the Gmail app password.",
        ]

    lines += ["", "Tap a button below to update any field."]
    return "\n".join(lines), _build_keyboard(smtp_missing=not smtp_set)


# ---------- /settings entry point ----------
async def _jump_to(message: Message, state: FSMContext, action: str) -> None:
    """Shared entry point used by /settings callbacks AND the /updaterole,
    /updateresume shortcut commands. Loads the user, validates they've
    onboarded, sets the FSM state, sends the prompt."""
    assert message.from_user is not None
    bundle = await _load_config(message.from_user.id)
    if bundle is None:
        await message.answer("You haven't onboarded yet. Send /start to begin.")
        return
    user, _prefs, _creds = bundle
    if user.status == UserStatus.onboarding:
        await message.answer(
            "You're still in the middle of onboarding. Finish /start first."
        )
        return
    prompt, fsm_state = _PROMPTS[action]
    await state.set_state(fsm_state)
    await message.answer(prompt, disable_web_page_preview=True)


@router.message(Command("updaterole", "updateroles"))
async def cmd_updaterole(message: Message, state: FSMContext) -> None:
    """Shortcut: jump straight into the roles editor without /settings."""
    await _jump_to(message, state, "roles")


@router.message(Command("updateresume"))
async def cmd_updateresume(message: Message, state: FSMContext) -> None:
    """Shortcut: jump straight into the resume-upload step."""
    await _jump_to(message, state, "resume")


@router.message(Command("settings"))
async def cmd_settings(message: Message, state: FSMContext) -> None:
    assert message.from_user is not None
    bundle = await _load_config(message.from_user.id)
    if bundle is None:
        await message.answer("You haven't onboarded yet. Send /start to begin.")
        return
    user, prefs, creds = bundle
    if user.status == UserStatus.onboarding:
        await message.answer(
            "You're still in the middle of onboarding. Finish /start first, "
            "then come back to /settings to tweak individual fields."
        )
        return
    await state.clear()
    text, kb = _render_overview(user, prefs, creds)
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)


# ---------- callback router ----------
_PROMPTS: dict[str, tuple[str, State]] = {
    "gmail": (
        "Send your new <b>Gmail address</b> (you'll re-enter the app password "
        "next), or /cancel.",
        Settings.SMTP_EMAIL,
    ),
    "resume": (
        "Upload your <b>new resume PDF</b> (max 10 MB), or /cancel.",
        Settings.RESUME,
    ),
    "name": (
        "Send your <b>new full name</b> (2\u2013100 chars), or /cancel.",
        Settings.NAME,
    ),
    "roles": (
        "Send your new <b>roles</b>, comma-separated (max 5), or /cancel.\n"
        "e.g. <code>python developer, backend engineer, ai engineer</code>",
        Settings.ROLES,
    ),
    "locations": (
        "Send your new <b>locations</b>, comma-separated, or /cancel.\n"
        "e.g. <code>Bengaluru, Hyderabad, Remote</code>",
        Settings.LOCATIONS,
    ),
}


@router.callback_query(F.data.startswith("settings:"))
async def cb_settings(cb: CallbackQuery, state: FSMContext) -> None:
    assert cb.data is not None
    action = cb.data.split(":", 1)[1]
    if action == "done":
        await state.clear()
        if isinstance(cb.message, Message):
            try:
                await cb.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
        await cb.answer("Settings closed.")
        return
    if action == "settime":
        # Re-use the same picker the /settime command builds. The callback
        # for settime:<hour> lives in core.bot.handlers and persists the
        # choice — no need to duplicate that logic here.
        assert cb.from_user is not None
        async with get_session() as session:
            prefs = await session.get(UserPreferences, cb.from_user.id)
        current = prefs.preferred_run_hour if prefs is not None else None
        current_label = _format_hour_label(current) if current is not None else "9 AM"
        if isinstance(cb.message, Message):
            await cb.message.answer(
                f"\u23f0 <b>Daily run time</b>\n\n"
                f"Currently: <b>{current_label} IST</b>\n\n"
                f"Tap a new time below.",
                reply_markup=_build_settime_keyboard(current),
            )
        await cb.answer()
        return
    if action not in _PROMPTS:
        await cb.answer()
        return
    prompt, fsm_state = _PROMPTS[action]
    await state.set_state(fsm_state)
    if isinstance(cb.message, Message):
        await cb.message.answer(prompt, disable_web_page_preview=True)
    await cb.answer()


# ---------- shared /cancel during a Settings edit ----------
@router.message(
    Command("cancel"),
    StateFilter(
        Settings.SMTP_EMAIL, Settings.SMTP_PASSWORD,
        Settings.ROLES, Settings.LOCATIONS, Settings.NAME, Settings.RESUME,
    ),
)
async def settings_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Edit cancelled. /settings to reopen.")


# ---------- field handlers ----------
@router.message(Settings.SMTP_EMAIL)
async def set_smtp_email(message: Message, state: FSMContext) -> None:
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
    assert message.from_user is not None
    async with get_session() as session:
        creds = await session.get(UserCredentials, message.from_user.id)
        if creds is None:
            await message.answer("Internal error: account row missing.")
            await state.clear()
            return
        creds.smtp_email = email
        # Clear the old encrypted password \u2014 it won't auth against the new
        # address, and leaving it set would falsely show "verified" in
        # /settings until the user supplied the new app password.
        creds.smtp_password_encrypted = None
        await session.commit()
    await state.set_state(Settings.SMTP_PASSWORD)
    await message.answer(
        f"\u2705 Got it: <b>{email}</b>\n\n"
        f"Now send the <b>Gmail app password</b> for this address "
        f"(16 chars, spaces OK \u2014 I'll strip them).\n\n"
        f"Generate one at https://myaccount.google.com/apppasswords if you "
        f"don't have it handy. Send /cancel to abort \u2014 your old app "
        f"password is already cleared, so outreach is paused until you "
        f"finish this step.",
        disable_web_page_preview=True,
    )


@router.message(Settings.SMTP_PASSWORD)
async def set_smtp_password(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    password = raw.replace(" ", "")
    await _silent_delete(message)
    if len(password) != 16 or not password.isalnum():
        await message.answer(
            "That doesn't look like a Gmail app password (16 letters/numbers). "
            "Get one at https://myaccount.google.com/apppasswords and try again, "
            "or /cancel."
        )
        return
    assert message.from_user is not None
    async with get_session() as session:
        creds = await session.get(UserCredentials, message.from_user.id)
        if creds is None or not creds.smtp_email:
            await message.answer("Internal error: Gmail address missing. /settings to retry.")
            await state.clear()
            return
        smtp_email = creds.smtp_email

    verifying = await message.answer("\U0001f50d Verifying with Gmail\u2026")
    sender = SMTPSender(email=smtp_email, password=password)
    ok, reason = await sender.verify()
    try:
        await verifying.delete()
    except Exception:
        pass
    if not ok:
        log.warning("settings: smtp verify failed for user %s: %s", message.from_user.id, reason)
        await message.answer(
            f"\u274c Gmail rejected that password ({reason[:80]}).\n\n"
            f"Make sure 2-Step Verification is ON for {smtp_email} and that "
            f"you're pasting an <b>app password</b>, not your real Gmail "
            f"password. Try again or /cancel."
        )
        return

    async with get_session() as session:
        creds = await session.get(UserCredentials, message.from_user.id)
        assert creds is not None
        creds.smtp_password_encrypted = encrypt(password)
        await session.commit()
    await state.clear()
    await message.answer("\u2705 Gmail reconnected and verified. /settings to view all config.")


@router.message(Settings.NAME)
async def set_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not (2 <= len(name) <= 100):
        await message.answer("Name must be 2\u2013100 characters. Try again or /cancel.")
        return
    assert message.from_user is not None
    async with get_session() as session:
        creds = await session.get(UserCredentials, message.from_user.id)
        if creds is None:
            await message.answer("Internal error: account row missing.")
            await state.clear()
            return
        creds.candidate_name = name
        await session.commit()
    await state.clear()
    await message.answer(f"\u2705 Name updated to <b>{name}</b>. /settings to view all config.")


@router.message(Settings.ROLES)
async def set_roles(message: Message, state: FSMContext) -> None:
    roles = _split_csv(message.text or "")
    if not roles:
        await message.answer("Send at least one role. e.g. <code>python developer</code>")
        return
    if len(roles) > 5:
        await message.answer("Max 5 roles \u2014 send a shorter list.")
        return
    assert message.from_user is not None
    async with get_session() as session:
        prefs = await session.get(UserPreferences, message.from_user.id)
        if prefs is None:
            await message.answer("Internal error: preferences row missing.")
            await state.clear()
            return
        prefs.role_keywords = roles
        await session.commit()
    await state.clear()
    await message.answer(
        f"\u2705 Roles updated: <b>{', '.join(roles)}</b>. /settings to view all config."
    )


@router.message(Settings.LOCATIONS)
async def set_locations(message: Message, state: FSMContext) -> None:
    locs = _split_csv(message.text or "")
    if not locs:
        await message.answer("Send at least one location.")
        return
    assert message.from_user is not None
    async with get_session() as session:
        prefs = await session.get(UserPreferences, message.from_user.id)
        if prefs is None:
            await message.answer("Internal error: preferences row missing.")
            await state.clear()
            return
        prefs.locations = locs
        await session.commit()
    await state.clear()
    await message.answer(
        f"\u2705 Locations updated: <b>{', '.join(locs)}</b>. /settings to view all config."
    )


@router.message(Settings.RESUME, F.document)
async def set_resume(message: Message, state: FSMContext) -> None:
    doc = message.document
    assert doc is not None
    if (doc.file_size or 0) > MAX_RESUME_BYTES:
        await message.answer(f"PDF is too large (max {MAX_RESUME_BYTES // 1024 // 1024} MB).")
        return
    fname = doc.file_name or "resume.pdf"
    if not fname.lower().endswith(".pdf") and doc.mime_type != "application/pdf":
        await message.answer("Please send a PDF file.")
        return
    bot = message.bot
    assert bot is not None
    buf = await bot.download(doc)
    if buf is None:
        await message.answer("Couldn't download that file. Try again.")
        return
    resume_bytes = buf.read()
    assert message.from_user is not None
    async with get_session() as session:
        creds = await session.get(UserCredentials, message.from_user.id)
        if creds is None:
            await message.answer("Internal error: account row missing.")
            await state.clear()
            return
        creds.resume_pdf = resume_bytes
        creds.resume_filename = fname
        creds.resume_uploaded_at = datetime.now(timezone.utc)
        await session.commit()
    await state.clear()
    await message.answer(
        f"\u2705 <b>Resume updated</b> ({len(resume_bytes) // 1024} KB).\n\n"
        f"Your next outreach emails will use the new resume \u2014 the daily "
        f"run tomorrow morning will pick it up automatically. /settings to "
        f"view all config."
    )


@router.message(Settings.RESUME)
async def set_resume_not_a_doc(message: Message, state: FSMContext) -> None:
    await message.answer("Please attach a PDF file, not text. Or /cancel.")
