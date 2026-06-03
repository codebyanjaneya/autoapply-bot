"""`/settemplate` \u2014 user-customisable outreach email body.

Stored on UserCredentials.email_template (added in migration 0008).
NULL = use the default template. Rendered via str.format with whitelisted
placeholders {candidate_name}, {role}, {company} \u2014 see
core/outreach._render_email.
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from core.db import get_session
from core.models import UserCredentials

log = logging.getLogger(__name__)
router = Router(name="settemplate")

_MAX_TEMPLATE_CHARS = 4000

_DEFAULT_PREVIEW = (
    "Hi {recruiter_first_name},\n\n"
    "I came across the {role} opening at {company} and wanted to introduce "
    "myself. I'm {candidate_name}.\n\n"
    "My resume is attached. Happy to chat at a time that works for you.\n\n"
    "Best regards,\n{candidate_name}"
)


class SetTemplate(StatesGroup):
    WAITING_TEMPLATE = State()


@router.message(Command("settemplate"))
async def cmd_settemplate(message: Message, state: FSMContext) -> None:
    assert message.from_user is not None
    async with get_session() as session:
        creds = await session.get(UserCredentials, message.from_user.id)

    current = (creds.email_template if creds else None) or "(using default template)"

    await state.set_state(SetTemplate.WAITING_TEMPLATE)
    await message.answer(
        "<b>Custom email template</b>\n\n"
        "You can set a custom email template. Use these placeholders:\n"
        "<code>{candidate_name}</code> \u2014 your name\n"
        "<code>{role}</code> \u2014 job role\n"
        "<code>{company}</code> \u2014 company name\n\n"
        f"<b>Current template:</b>\n<pre>{_escape(current)}</pre>\n\n"
        "Send your template below, or /reset to go back to default, "
        "or /cancel to abort."
    )


@router.message(Command("cancel"), StateFilter(SetTemplate.WAITING_TEMPLATE))
async def cmd_settemplate_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Cancelled. Template unchanged.")


@router.message(Command("reset"), StateFilter(SetTemplate.WAITING_TEMPLATE))
async def cmd_settemplate_reset(message: Message, state: FSMContext) -> None:
    assert message.from_user is not None
    await state.clear()
    async with get_session() as session:
        creds = await session.get(UserCredentials, message.from_user.id)
        if creds is None:
            await message.answer(
                "\u26a0\ufe0f No credentials row yet \u2014 complete onboarding first via /start."
            )
            return
        creds.email_template = None
        await session.commit()
    await message.answer("\u2705 Reset to default template.")


@router.message(StateFilter(SetTemplate.WAITING_TEMPLATE), F.text)
async def settemplate_save(message: Message, state: FSMContext) -> None:
    assert message.from_user is not None
    text = (message.text or "").strip()
    if not text:
        await message.answer("Please send the template as text, or /cancel.")
        return
    if len(text) > _MAX_TEMPLATE_CHARS:
        await message.answer(
            f"\u26a0\ufe0f Template too long ({len(text)} chars). "
            f"Max {_MAX_TEMPLATE_CHARS}."
        )
        return

    # Validate placeholders by doing a dry-format. Unknown placeholders
    # (e.g. {salary}) would silently break every future send \u2014 catch here.
    try:
        text.format(candidate_name="Alex", role="SWE", company="Acme")
    except (KeyError, IndexError, ValueError) as e:
        await message.answer(
            f"\u26a0\ufe0f Template has an unsupported placeholder: <code>{e}</code>\n\n"
            "Only <code>{candidate_name}</code>, <code>{role}</code>, and "
            "<code>{company}</code> are allowed."
        )
        return

    async with get_session() as session:
        creds = await session.get(UserCredentials, message.from_user.id)
        if creds is None:
            await message.answer(
                "\u26a0\ufe0f No credentials row yet \u2014 complete onboarding first via /start."
            )
            await state.clear()
            return
        creds.email_template = text
        await session.commit()

    await state.clear()
    await message.answer(
        "\u2705 Template saved! All future outreach emails will use your template."
    )


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
