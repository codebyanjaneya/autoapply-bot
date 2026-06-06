"""Mint a fresh Razorpay Payment Link for /upgrade.

Decisions (MVP):
- Always mint a NEW link on /upgrade (rather than reusing an unpaid prior
  link). Simpler: no expiry-check / re-validate dance, and Razorpay caps
  10k open links per account which is fine for 100s of users.
- Expire links after 7 days. Long enough that someone can sleep on it,
  short enough that abandoned links don't pile up forever.
- Amount: 70000 paise = INR 700. Hard-coded; if pricing ever changes we
  add a `plans` table. Not worth abstracting for one tier.
- `notes.user_id` is the **primary** way the webhook resolves payment ->
  user. `razorpay_payment_link_id` on the Subscription row is the
  fallback if notes are missing (shouldn't happen, but cheap insurance).
- We do NOT prefill customer email/name when missing \u2014 Razorpay will
  collect it on the checkout page and it ends up in the payment notes.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Subscription, SubscriptionStatus, User, UserCredentials
from core.payments.client import get_client, run_blocking

log = logging.getLogger(__name__)

PLAN_AMOUNT_PAISE = 70000   # \u20b9700.00
PLAN_CURRENCY = "INR"
PLAN_DESCRIPTION = "AutoApply Pro \u2014 1 month"
LINK_EXPIRY_DAYS = 7


async def create_payment_link_for_user(
    session: AsyncSession, user: User,
) -> tuple[str, str]:
    """Mint a new Razorpay Payment Link and persist its id on Subscription.

    Returns:
        (short_url, payment_link_id) \u2014 caller renders ``short_url`` to user.

    Raises:
        razorpay.errors.* on API failure (caller catches & shows friendly error).
    """
    creds = await session.get(UserCredentials, user.id)
    customer: dict[str, str] = {
        "name": (user.first_name or user.username or f"User {user.id}")[:50],
    }
    if creds is not None and creds.smtp_email:
        customer["email"] = creds.smtp_email
    # Razorpay requires `contact` OR `email` \u2014 we always have email or
    # leave it for the checkout page to collect. Don't fabricate phone.

    expire_by = int((datetime.now(timezone.utc) + timedelta(days=LINK_EXPIRY_DAYS)).timestamp())

    payload = {
        "amount": PLAN_AMOUNT_PAISE,
        "currency": PLAN_CURRENCY,
        "description": PLAN_DESCRIPTION,
        "expire_by": expire_by,
        "reference_id": f"u{user.id}-{int(datetime.now(timezone.utc).timestamp())}",
        "customer": customer,
        "notify": {"sms": False, "email": False},     # we own the channel (Telegram)
        "reminder_enable": False,
        "notes": {
            "user_id": str(user.id),
            "telegram_username": user.username or "",
            "plan": "autoapply_pro_1mo",
        },
        "callback_url": "",          # leave default; user returns to Telegram
        "callback_method": "get",
    }
    # Razorpay rejects callback_url="" \u2014 drop it if empty.
    payload.pop("callback_url", None)
    payload.pop("callback_method", None)

    client = get_client()
    link = await run_blocking(client.payment_link.create, payload)
    link_id: str = link["id"]
    short_url: str = link["short_url"]
    log.info(
        "razorpay payment link created user_id=%s link_id=%s amount_paise=%d",
        user.id, link_id, PLAN_AMOUNT_PAISE,
    )

    # Upsert Subscription row so the webhook can fall back to lookup-by-link-id.
    sub = await session.get(Subscription, user.id)
    if sub is None:
        sub = Subscription(
            user_id=user.id,
            status=SubscriptionStatus.incomplete,
            razorpay_payment_link_id=link_id,
        )
        session.add(sub)
    else:
        sub.razorpay_payment_link_id = link_id
        # Keep prior status \u2014 don't downgrade an already-active sub just
        # because the user tapped /upgrade again (e.g. to gift renewal).
    await session.flush()

    return short_url, link_id
