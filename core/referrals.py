"""Referral codes and lookups.

Design choices:
- The code is *derived* from the user_id (base36) with an ``AA`` prefix, so we
  never need a unique column or collision-handling. Decoding is reversible.
- We DO store ``users.referred_by_user_id`` on the referee (nullable, set
  once at onboarding). Reverse-lookup gives "who did I refer?".
- "1 month free" fulfilment is deferred to the Stripe webhook (Week 5).
  Until then ``count_referrals`` just exposes the counter so /referral can
  display "you've earned N months (applied when checkout launches)".
"""
from __future__ import annotations

import logging
import string

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import SubscriptionTier, User

log = logging.getLogger(__name__)

_PREFIX = "AA"
_ALPHABET = string.digits + string.ascii_uppercase  # base36


def _to_base36(n: int) -> str:
    if n == 0:
        return "0"
    out: list[str] = []
    while n:
        n, rem = divmod(n, 36)
        out.append(_ALPHABET[rem])
    return "".join(reversed(out))


def _from_base36(s: str) -> int:
    return int(s, 36)


def build_referral_code(user_id: int) -> str:
    """Deterministic code for a given user id. Example: 42 -> 'AA16'."""
    return _PREFIX + _to_base36(user_id)


def decode_referral_code(code: str) -> int | None:
    """Reverse of :func:`build_referral_code`. Returns None on malformed input.

    Accepts the code with or without the ``AA`` prefix and is case-insensitive
    so users pasting from chat clients (which sometimes auto-lower) still work.
    """
    if not code:
        return None
    s = code.strip().upper()
    if s.startswith(_PREFIX):
        s = s[len(_PREFIX):]
    if not s or any(c not in _ALPHABET for c in s):
        return None
    try:
        return _from_base36(s)
    except ValueError:
        return None


async def count_referrals(session: AsyncSession, user_id: int) -> tuple[int, int]:
    """Return ``(total_referred, upgraded_to_paid)`` for the given referrer."""
    total = await session.scalar(
        select(func.count(User.id)).where(User.referred_by_user_id == user_id)
    )
    paid = await session.scalar(
        select(func.count(User.id)).where(
            User.referred_by_user_id == user_id,
            User.subscription_tier == SubscriptionTier.paid,
        )
    )
    return (int(total or 0), int(paid or 0))
