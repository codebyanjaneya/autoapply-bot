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
        "Enter details in this format:\n"
        "<code>Name | Email | Company | Role</code>\n\n"
        "Example:\n"
        "<code>Priya Sharma | priya@gmail.com | Google | Software Engineer</code>\n\n"
        "Send /cancel to exit."
    )


@router.message(Command("cancel"), StateFilter(SendTo.WAITING_DETAILS, SendTo.WAITING_CONFIRM))
async def cmd_sendto_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Cancelled. Run /sendto again anytime.")


@router.message(StateFilter(SendTo.WAITING_DETAILS), F.text)
async def sendto_collect(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) != 4 or not all(parts):
        await message.answer(
            "\u26a0\ufe0f Couldn't parse that. Please use exactly this format "
            "(four fields separated by <code>|</code>):\n"
            "<code>Name | Email | Company | Role</code>"
        )
        return

    name, email, company, role = parts
    if not _EMAIL_RE.match(email):
        await message.answer(f"\u26a0\ufe0f That doesn't look like a valid email: <code>{email}</code>")
        return

    await state.update_data(name=name, email=email, company=company, role=role)
    await state.set_state(SendTo.WAITING_CONFIRM)

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="\u2705 Send", callback_data="sendto:confirm"),
        InlineKeyboardButton(text="\u274c Cancel", callback_data="sendto:cancel"),
    ]])
    await message.answer(
        f"Send outreach email to <b>{name}</b> (<code>{email}</code>) for "
        f"<b>{role}</b> at <b>{company}</b>?",
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
    subject = f"{role} role at {company} \u2014 {candidate}"
    body = (
        f"{salutation}\n"
        f"\n"
        f"I came across the {role} opening at {company} and wanted to introduce myself. "
        f"I'm {candidate}.\n"
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
