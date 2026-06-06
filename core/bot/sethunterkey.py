"""`/sethunterkey` and `/removehunterkey` — per-user Hunter API key.

Lets users plug their own free Hunter.io account (25 lookups/month) into
the bot, giving them isolated quota that doesn't share the operator's
pooled HUNTER_API_KEY. See ``core.enrich.hunter.build_hunter_client`` for
the lookup-time resolution order.

Key is Fernet-encrypted at rest in ``user_credentials.hunter_api_key_encrypted``
(column already exists from migration 0001 \u2014 was legacy, now re-purposed).
"""
from __future__ import annotations

import logging
import re

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from core.crypto import encrypt
from core.db import get_session
from core.models import SubscriptionTier, User, UserCredentials

log = logging.getLogger(__name__)
router = Router(name="hunterkey")

# Hunter keys are 40-char lowercase hex. Be slightly loose to tolerate any
# future format change \u2014 reject obvious garbage but accept anything in the
# right ballpark.
_KEY_RE = re.compile(r"^[a-f0-9]{30,80}$", re.IGNORECASE)


class SetHunterKey(StatesGroup):
    WAITING_KEY = State()


_PRO_GATE_MESSAGE = (
    "🔒 This is a Pro feature.\n\n"
    "Pro users get their own Hunter.io quota — 25 recruiter searches/month "
    "guaranteed, never shared with other users.\n\n"
    "Upgrade with /upgrade to unlock this."
)


async def _is_paid(user_id: int) -> bool:
    async with get_session() as session:
        user = await session.get(User, user_id)
    return user is not None and user.subscription_tier == SubscriptionTier.paid


@router.message(Command("sethunterkey"))
async def cmd_sethunterkey(message: Message, state: FSMContext) -> None:
    assert message.from_user is not None
    if not await _is_paid(message.from_user.id):
        await message.answer(_PRO_GATE_MESSAGE)
        return
    await state.set_state(SetHunterKey.WAITING_KEY)
    await message.answer(
        "Enter your Hunter.io API key to get your own <b>25 free recruiter "
        "searches/month</b>.\n\n"
        "Get a free key at https://hunter.io (takes 1 minute).\n\n"
        "\U0001f512 I'll delete your message and store the key encrypted.\n\n"
        "Send /cancel to abort."
    )


@router.message(Command("cancel"), StateFilter(SetHunterKey.WAITING_KEY))
async def cmd_sethunterkey_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Cancelled. Hunter key unchanged.")


@router.message(StateFilter(SetHunterKey.WAITING_KEY), F.text)
async def sethunterkey_save(message: Message, state: FSMContext) -> None:
    assert message.from_user is not None
    key = (message.text or "").strip()

    # Delete the plaintext key from chat history immediately. Best-effort \u2014
    # if the bot isn't an admin or the message is too old, we just log.
    try:
        await message.delete()
    except Exception:
        log.warning("sethunterkey: could not delete key message from user %s", message.from_user.id)

    if not _KEY_RE.match(key):
        await message.answer(
            "\u26a0\ufe0f That doesn't look like a valid Hunter API key (expected "
            "a long hex string). Try /sethunterkey again."
        )
        await state.clear()
        return

    async with get_session() as session:
        creds = await session.get(UserCredentials, message.from_user.id)
        if creds is None:
            await message.answer(
                "\u26a0\ufe0f No credentials row yet \u2014 complete onboarding first via /start."
            )
            await state.clear()
            return
        creds.hunter_api_key_encrypted = encrypt(key)
        await session.commit()

    await state.clear()
    log.info("sethunterkey: user=%s saved personal Hunter key", message.from_user.id)
    await message.answer(
        "\u2705 Hunter key saved! You now have your own recruiter search quota."
    )


@router.message(Command("removehunterkey"))
async def cmd_removehunterkey(message: Message) -> None:
    assert message.from_user is not None
    if not await _is_paid(message.from_user.id):
        await message.answer(_PRO_GATE_MESSAGE)
        return
    async with get_session() as session:
        creds = await session.get(UserCredentials, message.from_user.id)
        if creds is None or creds.hunter_api_key_encrypted is None:
            await message.answer(
                "No personal Hunter key on file \u2014 you're already using the shared pool."
            )
            return
        creds.hunter_api_key_encrypted = None
        await session.commit()

    log.info("removehunterkey: user=%s cleared personal Hunter key", message.from_user.id)
    await message.answer(
        "\u2705 Removed. Future runs will use the shared Hunter pool."
    )
