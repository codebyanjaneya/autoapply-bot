"""User-facing /feedback flow + operator forwarding.

Mirrors core/bot/support.py: enter an FSM, capture the next text message,
forward a tidy summary to OPERATOR_CHAT_ID, and ACK the user.

Distinct from /support: /support is for things that are broken (24h SLA
language). /feedback is for product input \u2014 features, ideas, polish.
Kept separate so the operator can triage at a glance.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from core.db import get_session
from core.models import User

log = logging.getLogger(__name__)
router = Router(name="feedback")

_IST = ZoneInfo("Asia/Kolkata")
_MAX_FEEDBACK_CHARS = 3500


class Feedback(StatesGroup):
    WAITING_TEXT = State()


def _operator_chat_id() -> int | None:
    raw = os.environ.get("OPERATOR_CHAT_ID", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        log.error("OPERATOR_CHAT_ID is not an integer: %r", raw)
        return None


@router.message(Command("feedback"))
async def cmd_feedback(message: Message, state: FSMContext) -> None:
    await state.set_state(Feedback.WAITING_TEXT)
    await message.answer(
        "\U0001f4ac <b>Help us improve AutoApply!</b>\n\n"
        "What would you like to see improved, added, or fixed? Just type your "
        "message below \u2014 every response is read personally.\n\n"
        "Send /cancel to skip."
    )


@router.message(Command("cancel"), StateFilter(Feedback.WAITING_TEXT))
async def cmd_feedback_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "No problem \u2014 closed the feedback form. Run /feedback again anytime."
    )


@router.message(StateFilter(Feedback.WAITING_TEXT), F.text)
async def feedback_collect(message: Message, state: FSMContext) -> None:
    assert message.from_user is not None
    text = (message.text or "").strip()
    if not text:
        await message.answer(
            "Please type your feedback as a text message. Or send /cancel to exit."
        )
        return

    truncated = text[:_MAX_FEEDBACK_CHARS]
    if len(text) > _MAX_FEEDBACK_CHARS:
        truncated += "\u2026 [truncated]"

    display_name = message.from_user.full_name or message.from_user.username or "?"
    tier_label = "?"
    try:
        async with get_session() as session:
            user = await session.get(User, message.from_user.id)
            if user is not None:
                tier_label = user.subscription_tier.value
                if user.first_name:
                    display_name = user.first_name
    except Exception:  # pragma: no cover
        log.exception("feedback: failed to load user %s for forwarding context",
                      message.from_user.id)

    now_ist = datetime.now(_IST).strftime("%d %b %Y, %I:%M %p IST")
    operator_text = (
        "\U0001f4a1 <b>Feedback</b>\n"
        f"<b>User:</b> {display_name} (ID: <code>{message.from_user.id}</code>)\n"
        f"<b>Tier:</b> {tier_label}\n"
        f"<b>Time:</b> {now_ist}\n"
        f"\n{truncated}"
    )

    op_id = _operator_chat_id()
    if op_id is None:
        log.error(
            "feedback: OPERATOR_CHAT_ID not configured \u2014 feedback from user "
            "%s NOT forwarded. Text: %r",
            message.from_user.id, truncated,
        )
    else:
        try:
            await message.bot.send_message(op_id, operator_text)
            log.info("feedback: forwarded from user %s to operator",
                     message.from_user.id)
        except TelegramAPIError:
            log.exception("feedback: failed forwarding from user %s",
                          message.from_user.id)

    await state.clear()
    await message.answer(
        "\u2705 <b>Thank you!</b> Your feedback has been noted. We build "
        "AutoApply based on what users like you tell us. \U0001f64f"
    )
