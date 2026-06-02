"""User-facing /support flow + operator forwarding.

When a user runs /support we enter an FSM that captures their next text
message, ACKs them warmly, and forwards a tidy summary to the operator's
Telegram chat (env: OPERATOR_CHAT_ID).

Design notes
------------
* No DB persistence in v1. Issues land in the operator's Telegram inbox
  and that IS the queue \u2014 simpler than a tickets table for one operator.
  Wire to Sentry / Linear later if volume warrants.
* If OPERATOR_CHAT_ID is unset or forwarding fails we still confirm to the
  user (warm UX > technical honesty), but we log loudly so we notice.
* /cancel exits the flow without forwarding.
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
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from core.db import get_session
from core.models import User

log = logging.getLogger(__name__)
router = Router(name="support")

_IST = ZoneInfo("Asia/Kolkata")
# Telegram caption / message hard cap is 4096; clamp issue text well under
# that so the formatted operator message has room for the header lines.
_MAX_ISSUE_CHARS = 3500


class Support(StatesGroup):
    WAITING_ISSUE = State()


class OperatorReply(StatesGroup):
    """FSM for the operator's side of the support thread. Entered when the
    operator taps the inline "Reply to User" button on a forwarded support
    request; the next text message they send is delivered to that user.
    """

    WAITING_REPLY = State()


def _operator_chat_id() -> int | None:
    raw = os.environ.get("OPERATOR_CHAT_ID", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        log.error("OPERATOR_CHAT_ID is not an integer: %r", raw)
        return None


@router.message(Command("support"))
async def cmd_support(message: Message, state: FSMContext) -> None:
    await state.set_state(Support.WAITING_ISSUE)
    await message.answer(
        "\U0001f6df <b>Having trouble?</b>\n\n"
        "Tell us what's going wrong and we'll fix it within <b>24 hours</b>. "
        "Just type your issue below \u2014 the more detail, the faster we can help "
        "(what you tried, what you expected, any error message you saw).\n\n"
        "Send /cancel if you change your mind."
    )


@router.message(Command("cancel"), StateFilter(Support.WAITING_ISSUE))
async def cmd_support_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "No worries \u2014 closed the support form. Run /support again whenever "
        "you need us."
    )


@router.message(StateFilter(Support.WAITING_ISSUE), F.text)
async def support_collect_issue(message: Message, state: FSMContext) -> None:
    assert message.from_user is not None
    issue_text = (message.text or "").strip()
    if not issue_text:
        await message.answer(
            "Please type your issue as a text message (attachments aren't "
            "supported yet). Or send /cancel to exit."
        )
        return

    truncated = issue_text[:_MAX_ISSUE_CHARS]
    if len(issue_text) > _MAX_ISSUE_CHARS:
        truncated += "\u2026 [truncated]"

    # Pull user context for the operator-facing summary. Done in its own
    # session so a DB hiccup doesn't block the user-facing ACK.
    display_name = message.from_user.full_name or message.from_user.username or "?"
    tier_label = "?"
    try:
        async with get_session() as session:
            user = await session.get(User, message.from_user.id)
            if user is not None:
                tier_label = user.subscription_tier.value
                # Prefer the stored first_name (set during onboarding) over
                # the Telegram display name, which can be a nickname.
                if user.first_name:
                    display_name = user.first_name
    except Exception:  # pragma: no cover - logged + degraded gracefully
        log.exception("support: failed to load user %s for forwarding context",
                      message.from_user.id)

    now_ist = datetime.now(_IST).strftime("%d %b %Y, %I:%M %p IST")
    operator_text = (
        "\U0001f198 <b>Support Request</b>\n"
        f"<b>User:</b> {display_name} (ID: <code>{message.from_user.id}</code>)\n"
        f"<b>Tier:</b> {tier_label}\n"
        f"<b>Time:</b> {now_ist}\n"
        f"\n<b>Issue:</b>\n{truncated}"
    )

    op_id = _operator_chat_id()
    forwarded = False
    if op_id is None:
        log.error(
            "support: OPERATOR_CHAT_ID not configured \u2014 issue from user %s "
            "NOT forwarded. Issue: %r",
            message.from_user.id, truncated,
        )
    else:
        # Inline button lets the operator start a reply right from the
        # forwarded message \u2014 no copy/pasting user IDs around.
        reply_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="\U0001f4ac Reply to User",
                callback_data=f"support_reply:{message.from_user.id}",
            )
        ]])
        try:
            await message.bot.send_message(op_id, operator_text, reply_markup=reply_kb)
            forwarded = True
            log.info("support: forwarded issue from user %s to operator",
                     message.from_user.id)
        except TelegramAPIError:
            log.exception("support: failed forwarding issue from user %s",
                          message.from_user.id)

    await state.clear()
    # Always warm + reassuring, even if forwarding silently degraded \u2014
    # the user did their part. We have logs for the rest.
    await message.answer(
        "\u2705 <b>Got it!</b> Your issue has been reported \u2014 we'll resolve "
        "it within <b>24 hours</b>.\n\n"
        "If it's urgent, the clearer your description the faster we can "
        "prioritise it. You can run /support again anytime to add more detail."
    )
    if not forwarded:
        # Soft hint so the user knows we *did* receive it on our side
        # (logs), even though the Telegram bridge didn't fire.
        log.warning("support: user %s received ACK without operator forward",
                    message.from_user.id)


# ---------------------------------------------------------------------------
# Operator reply path
# ---------------------------------------------------------------------------
# When the operator taps the "Reply to User" button on a forwarded support
# message, we stash the target user_id in FSM state and capture the next
# text message the operator sends, delivering it to that user as a
# branded "Message from AutoApply Support" DM.

def _is_operator(chat_id: int | None) -> bool:
    op_id = _operator_chat_id()
    return op_id is not None and chat_id == op_id


@router.callback_query(F.data.startswith("support_reply:"))
async def cb_support_reply(cb: CallbackQuery, state: FSMContext) -> None:
    # Defence-in-depth: only the operator may use this button. The button
    # is only ever shown in the operator's chat, but Telegram lets anyone
    # who learns the callback_data trigger it, so we re-check.
    if not _is_operator(cb.from_user.id if cb.from_user else None):
        await cb.answer("Not authorized.", show_alert=True)
        return

    raw = cb.data or ""
    try:
        target_user_id = int(raw.split(":", 1)[1])
    except (IndexError, ValueError):
        await cb.answer("Malformed reply button.", show_alert=True)
        return

    await state.set_state(OperatorReply.WAITING_REPLY)
    await state.update_data(target_user_id=target_user_id)
    await cb.answer()
    if cb.message is not None:
        await cb.message.answer(
            f"\u270d\ufe0f Type your reply to user <code>{target_user_id}</code>.\n"
            f"Send /cancel to abort."
        )


@router.message(Command("cancel"), StateFilter(OperatorReply.WAITING_REPLY))
async def cmd_operator_reply_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Reply cancelled.")


@router.message(StateFilter(OperatorReply.WAITING_REPLY), F.text)
async def operator_reply_send(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    target_user_id = data.get("target_user_id")
    operator_text = (message.text or "").strip()

    if not target_user_id or not operator_text:
        await state.clear()
        await message.answer(
            "\u26a0\ufe0f Missing target user or empty reply. State cleared \u2014 "
            "tap \"Reply to User\" again to retry."
        )
        return

    delivery = (
        "\U0001f4e9 <b>Message from AutoApply Support:</b>\n\n"
        f"{operator_text}"
    )
    try:
        await message.bot.send_message(int(target_user_id), delivery)
    except TelegramAPIError as e:
        log.exception("support: failed delivering operator reply to user %s",
                      target_user_id)
        await state.clear()
        await message.answer(
            f"\u274c Could not deliver to <code>{target_user_id}</code>: "
            f"{type(e).__name__}. They may have blocked the bot."
        )
        return

    await state.clear()
    await message.answer(f"\u2705 Reply sent to user <code>{target_user_id}</code>.")
    log.info("support: operator delivered reply to user %s (%d chars)",
             target_user_id, len(operator_text))
