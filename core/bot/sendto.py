"""`/sendto` \u2014 one-off manual outreach to a recruiter the user already knows.

Bypasses the daily pipeline (no Hunter lookup, no Groq scoring, no
RateLimitCounter check) so the user can fire a quick intro at a contact
they sourced themselves. Uses the same SMTPSender + resume attachment as
the pipeline so deliverability behaviour is identical.

Flow:
    /sendto -> WAITING_DETAILS (Name | Email | Company | Role)
            -> WAITING_CONFIRM (inline \u2705 / \u274c)
            -> send via SMTPSender.from_credentials(creds)
"""
from __future__ import annotations

import logging
import re

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from core.db import get_session
from core.mailer.smtp_sender import SMTPSender
from core.models import User, UserCredentials

log = logging.getLogger(__name__)
router = Router(name="sendto")

# RFC-ish address check; deliberately loose \u2014 SMTP will reject hard
# garbage. We just want to catch obvious typos before sending.
_EMAIL_RE = re.compile(r"^[^@\s|]+@[^@\s|]+\.[^@\s|]+$")


class SendTo(StatesGroup):
    WAITING_DETAILS = State()
    WAITING_CONFIRM = State()


@router.message(Command("sendto"))
async def cmd_sendto(message: Message, state: FSMContext) -> None:
    assert message.from_user is not None
    # Fail fast if the user hasn't connected SMTP \u2014 no point asking for
    # details if we can't send.
    async with get_session() as session:
        creds = await session.get(UserCredentials, message.from_user.id)
    if creds is None or not creds.smtp_email or creds.smtp_password_encrypted is None:
        await message.answer(
            "\u26a0\ufe0f Please complete onboarding first \u2014 I need your Gmail "
            "to send emails. Run /start."
        )
        return

    await state.set_state(SendTo.WAITING_DETAILS)
    await message.answer(
        "Enter details, one field per line. <b>Only the email is required:</b>\n\n"
        "<code>john@google.com\n"
        "John Smith\n"
        "Google\n"
        "Senior Developer</code>\n\n"
        "Line 1 — email (required)\n"
        "Line 2 — recruiter name (optional)\n"
        "Line 3 — company (optional)\n"
        "Line 4 — role / subject (optional)\n\n"
        "Send /cancel to exit."
    )


@router.message(Command("cancel"), StateFilter(SendTo.WAITING_DETAILS, SendTo.WAITING_CONFIRM))
async def cmd_sendto_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Cancelled. Run /sendto again anytime.")


@router.message(StateFilter(SendTo.WAITING_DETAILS), F.text)
async def sendto_collect(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        await message.answer(
            "⚠️ No input found. Please send at least the recipient's email "
            "on the first line, or /cancel."
        )
        return

    email = lines[0]
    if not _EMAIL_RE.match(email):
        await message.answer(
            f"⚠️ That doesn't look like a valid email: <code>{email}</code>\n\n"
            "The email goes on the <b>first line</b>."
        )
        return

    name = lines[1] if len(lines) > 1 else ""
    company = lines[2] if len(lines) > 2 else ""
    role = lines[3] if len(lines) > 3 else ""

    await state.update_data(name=name, email=email, company=company, role=role)
    await state.set_state(SendTo.WAITING_CONFIRM)

    # Confirmation line degrades gracefully when optional fields are missing.
    who = f"<b>{name}</b> (<code>{email}</code>)" if name else f"<code>{email}</code>"
    if role and company:
        what = f" for <b>{role}</b> at <b>{company}</b>"
    elif role:
        what = f" for <b>{role}</b>"
    elif company:
        what = f" at <b>{company}</b>"
    else:
        what = ""

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Send", callback_data="sendto:confirm"),
        InlineKeyboardButton(text="❌ Cancel", callback_data="sendto:cancel"),
    ]])
    await message.answer(
        f"Send outreach email to {who}{what}?",
        reply_markup=kb,
    )


@router.callback_query(F.data == "sendto:cancel", StateFilter(SendTo.WAITING_CONFIRM))
async def cb_sendto_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if cb.message:
        await cb.message.edit_text("Cancelled.")
    await cb.answer()


@router.callback_query(F.data == "sendto:confirm", StateFilter(SendTo.WAITING_CONFIRM))
async def cb_sendto_confirm(cb: CallbackQuery, state: FSMContext) -> None:
    assert cb.from_user is not None
    data = await state.get_data()
    await state.clear()

    name = data.get("name", "")
    email = data.get("email", "")
    company = data.get("company", "")
    role = data.get("role", "")

    if cb.message:
        await cb.message.edit_text(f"\u23f3 Sending to <code>{email}</code>\u2026")
    await cb.answer()

    # Reload creds + user under a fresh session \u2014 keeps the SMTP password in
    # memory only for the duration of the send.
    async with get_session() as session:
        creds = await session.get(UserCredentials, cb.from_user.id)
        user = await session.get(User, cb.from_user.id)

    if creds is None or not creds.smtp_email or creds.smtp_password_encrypted is None:
        if cb.message:
            await cb.message.edit_text(
                "\u26a0\ufe0f No SMTP credentials on file. Run /start to onboard."
            )
        return

    sender = SMTPSender.from_credentials(creds)
    if sender is None:
        if cb.message:
            await cb.message.edit_text(
                "\u274c Couldn't load your SMTP credentials (decrypt failed). "
                "Re-run onboarding with /start."
            )
        return

    candidate = creds.candidate_name or (user.first_name if user else None) or "Applicant"
    salutation = f"Hi {name.split()[0]}," if name else "Hi,"

    # Subject + intro gracefully fall back when company/role are missing.
    if role and company:
        subject = f"{role} role at {company} — {candidate}"
        intro = f"I came across the {role} opening at {company} and wanted to introduce myself."
    elif role:
        subject = f"{role} — {candidate}"
        intro = f"I'm reaching out about the {role} opening and wanted to introduce myself."
    elif company:
        subject = f"Introduction — {candidate} (interested in {company})"
        intro = f"I wanted to introduce myself — I'm interested in opportunities at {company}."
    else:
        subject = f"Quick introduction — {candidate}"
        intro = "I wanted to introduce myself and share my resume."

    body = (
        f"{salutation}\n"
        f"\n"
        f"{intro} I'm {candidate}.\n"
        f"\n"
        f"My resume is attached. Happy to chat at a time that works for you.\n"
        f"\n"
        f"Best regards,\n"
        f"{candidate}\n"
    )

    try:
        async with sender:
            await sender.send(
                to=email,
                subject=subject,
                body=body,
                reply_to=creds.smtp_email,
                attachment=creds.resume_pdf,
                attachment_filename=f"{candidate.replace(' ', '_')}_resume.pdf",
            )
    except Exception as e:
        log.exception("sendto: user=%s -> %s failed", cb.from_user.id, email)
        if cb.message:
            await cb.message.edit_text(f"\u274c Failed: {type(e).__name__}: {e}")
        return

    log.info("sendto: user=%s sent to %s (%s @ %s, role=%s)",
             cb.from_user.id, email, name, company, role)
    if cb.message:
        await cb.message.edit_text(f"\u2705 Email sent to <code>{email}</code>!")
