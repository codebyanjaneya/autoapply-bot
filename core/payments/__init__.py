"""Razorpay payments: Payment Links (MVP rail).

Public surface used elsewhere:
    create_payment_link_for_user(session, user)  -> (short_url, payment_link_id)
    upgrade_to_paid(session, bot, user_id, period_end)
    expire_due_subscriptions(bot)
    send_expiry_reminders(bot)
    build_webhook_app(bot) -> aiohttp.web.Application

See core/payments/README in commit message for the end-to-end flow.
"""
from core.payments.payment_links import create_payment_link_for_user
from core.payments.subscription_service import (
    expire_due_subscriptions,
    send_expiry_reminders,
    upgrade_to_paid,
)
from core.payments.web_server import build_webhook_app

__all__ = [
    "create_payment_link_for_user",
    "upgrade_to_paid",
    "expire_due_subscriptions",
    "send_expiry_reminders",
    "build_webhook_app",
]
