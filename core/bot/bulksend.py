"""`/bulksend` \u2014 send one-off outreach to many contacts in a single command.

Same template + SMTPSender path as /sendto, just fanned out over a list.
Sequential sends (not gather) so a single Gmail throttle doesn't tank the
whole batch \u2014 and so the user sees per-row failures clearly.
"""
from __future__ import annotations

import asyncio
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
router = Router(name="bulksend")

_EMAIL_RE = re.compile(r"^[^@\s|]+@[^@\s|]+\.[^@\s|]+$")
# Cap to keep Telegram message lengths sane and avoid getting flagged by
# Gmail's per-session sending limits.
_MAX_RECIPIENTS = 20
# Small gap between sends to stay under Gmail's per-second cap.
_INTER_SEND_DELAY_S = 1.0


class BulkSend(StatesGroup):
    WAITING_LIST = State()
    WAITING_CONFIRM = State()


@router.message(Command("bulksend"))
async def cmd_bulksend(message: Message, state: FSMContext) -> None:
    assert message.from_user is not None
    async with get_session() as session:
        creds = await session.get(UserCredentials, message.from_user.id)
    if creds is None or not creds.smtp_email or creds.smtp_password_encrypted is None:
        await message.answer(
            "\u26a0\ufe0f Please complete onboarding first \u2014 I need your Gmail "
            "to send emails. Run /start."
        )
        return

    await state.set_state(BulkSend.WAITING_LIST)
    await message.answer(
        "Send to multiple people at once. Enter one person per line in this format:\n"
        "<code>Name | Email | Company | Role</code>\n\n"
        "Example:\n"
        "<code>Priya Sharma | priya@gmail.com | Google | Software Engineer</code>\n"
        "<code>Rahul Mehta | rahul@ms.com | Microsoft | PM</code>\n\n"
        f"Max <b>{_MAX_RECIPIENTS}</b> per batch. Send /cancel to abort."
    )


@router.message(Command("cancel"), StateFilter(BulkSend.WAITING_LIST, BulkSend.WAITING_CONFIRM))
async def cmd_bulksend_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Cancelled. Run /bulksend again anytime.")


@router.message(StateFilter(BulkSend.WAITING_LIST), F.text)
async def bulksend_collect(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        await message.answer("\u26a0\ufe0f No lines found. Please send at least one entry.")
        return
    if len(lines) > _MAX_RECIPIENTS:
        await message.answer(
            f"\u26a0\ufe0f Too many ({len(lines)}). Max {_MAX_RECIPIENTS} per batch. "
            "Trim the list and resend."
        )
        return

    parsed: list[dict[str, str]] = []
    errors: list[str] = []
    for idx, line in enumerate(lines, start=1):
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 4 or not all(parts):
            errors.append(f"Line {idx}: expected 4 fields, got {len(parts)}")
            continue
        name, email, company, role = parts
        if not _EMAIL_RE.match(email):
            errors.append(f"Line {idx}: invalid email <code>{email}</code>")
            continue
        parsed.append({"name": name, "email": email, "company": company, "role": role})

    if errors:
        await message.answer(
            "\u26a0\ufe0f Couldn't parse some lines:\n" + "\n".join(errors) +
            "\n\nFix the list and resend, or /cancel."
        )
        return

    await state.update_data(recipients=parsed)
    await state.set_state(BulkSend.WAITING_CONFIRM)

    preview = "\n".join(f"\u2022 {r['name']} ({r['company']})" for r in parsed)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="\u2705 Send All", callback_data="bulksend:confirm"),
        InlineKeyboardButton(text="\u274c Cancel", callback_data="bulksend:cancel"),
    ]])
    await message.answer(
        f"Ready to send to <b>{len(parsed)}</b> people:\n{preview}\n\nConfirm?",
        reply_markup=kb,
    )


@router.callback_query(F.data == "bulksend:cancel", StateFilter(BulkSend.WAITING_CONFIRM))
async def cb_bulksend_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if cb.message:
        await cb.message.edit_text("Cancelled.")
    await cb.answer()


@router.callback_query(F.data == "bulksend:confirm", StateFilter(BulkSend.WAITING_CONFIRM))
async def cb_bulksend_confirm(cb: CallbackQuery, state: FSMContext) -> None:
    assert cb.from_user is not None
    data = await state.get_data()
    recipients: list[dict[str, str]] = data.get("recipients") or []
    await state.clear()

    if not recipients:
        if cb.message:
            await cb.message.edit_text("\u26a0\ufe0f No recipients on file. Run /bulksend again.")
        await cb.answer()
        return

    if cb.message:
        await cb.message.edit_text(f"\u23f3 Sending to {len(recipients)} recipient(s)\u2026")
    await cb.answer()

    async with get_session() as session:
        creds = await session.get(UserCredentials, cb.from_user.id)
        user = await session.get(User, cb.from_user.id)

    if creds is None or not creds.smtp_email or creds.smtp_password_encrypted is None:
        if cb.message:
            await cb.message.edit_text("\u26a0\ufe0f No SMTP credentials. Run /start to onboard.")
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
    template = creds.email_template  # may be None

    sent = 0
    failures: list[str] = []

    async with sender:
        for i, r in enumerate(recipients):
            name, email, company, role = r["name"], r["email"], r["company"], r["role"]
            subject = f"{role} role at {company} \u2014 {candidate}"
            if template:
                try:
                    body = template.format(
                        candidate_name=candidate, role=role, company=company,
                    )
                except (KeyError, IndexError, ValueError) as e:
                    log.warning("bulksend: template render failed for user %s (%s); "
                                "using default body", cb.from_user.id, e)
                    body = _default_body(name, candidate, role, company)
            else:
                body = _default_body(name, candidate, role, company)

            try:
                await sender.send(
                    to=email,
                    subject=subject,
                    body=body,
                    reply_to=creds.smtp_email,
                    attachment=creds.resume_pdf,
                    attachment_filename=f"{candidate.replace(' ', '_')}_resume.pdf",
                )
                sent += 1
                log.info("bulksend: user=%s sent to %s (%s @ %s)",
                         cb.from_user.id, email, name, company)
            except Exception as e:
                log.exception("bulksend: user=%s -> %s failed", cb.from_user.id, email)
                failures.append(f"\u2022 {email}: {type(e).__name__}: {e}")

            # Light pacing between sends \u2014 cheap insurance against Gmail's
            # per-second throttle. Skip after the last one.
            if i < len(recipients) - 1:
                await asyncio.sleep(_INTER_SEND_DELAY_S)

    summary = f"\u2705 Sent: <b>{sent}</b> | \u274c Failed: <b>{len(failures)}</b>"
    if failures:
        summary += "\n\nFailures:\n" + "\n".join(failures[:10])
        if len(failures) > 10:
            summary += f"\n\u2026 and {len(failures) - 10} more."
    if cb.message:
        await cb.message.edit_text(summary)


def _default_body(recipient_name: str, candidate: str, role: str, company: str) -> str:
    salutation = f"Hi {recipient_name.split()[0]}," if recipient_name else "Hi,"
    return (
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
