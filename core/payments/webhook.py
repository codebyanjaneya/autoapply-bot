"""Razorpay webhook handler (aiohttp).

Mounted at POST /webhooks/razorpay by core/payments/web_server.py.

Idempotency contract (Razorpay retries aggressively on non-2xx):
  1. Verify HMAC signature using RAZORPAY_WEBHOOK_SECRET. 401 on mismatch.
  2. Parse the JSON event; extract a stable id (`x-razorpay-event-id`
     header, falling back to a hash of payload + delivery time).
  3. INSERT into payment_events. UNIQUE constraint = lock point. If a
     duplicate id arrives, the second insert raises IntegrityError; we
     swallow it and return 200 so Razorpay stops retrying.
  4. Dispatch on `event` field:
        payment_link.paid  -> upgrade_to_paid(...)
        payment.failed     -> log only (link still valid for retry)
        else               -> log + 200 OK (ignored).
  5. Mark processed_at, return 200.

ALWAYS return 200 within Razorpay's 5s budget, except for genuine auth
failures (401). Any business logic error is logged + stored in
processing_error but still returns 200 \u2014 we don't want Razorpay
spamming retries for a bug on our side.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from aiohttp import web
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.db import get_session
from core.models import PaymentEvent, Subscription
from core.payments.client import get_client, get_webhook_secret
from core.payments.subscription_service import upgrade_to_paid

log = logging.getLogger(__name__)


async def razorpay_webhook(request: web.Request) -> web.Response:
    body_bytes = await request.read()
    signature = request.headers.get("X-Razorpay-Signature", "")
    event_id = request.headers.get("X-Razorpay-Event-Id", "")

    if not signature:
        log.warning("razorpay webhook: missing signature header")
        return web.Response(status=401, text="missing signature")

    # --- 1. Verify HMAC signature ---
    try:
        secret = get_webhook_secret()
    except RuntimeError as e:
        log.error("razorpay webhook: %s", e)
        return web.Response(status=500, text="webhook secret not configured")

    client = get_client()
    try:
        # SDK utility raises SignatureVerificationError on mismatch.
        client.utility.verify_webhook_signature(body_bytes.decode("utf-8"), signature, secret)
    except Exception as e:
        log.warning("razorpay webhook: signature verification failed: %s", e)
        return web.Response(status=401, text="invalid signature")

    # --- 2. Parse payload ---
    try:
        payload: dict[str, Any] = json.loads(body_bytes)
    except json.JSONDecodeError:
        log.warning("razorpay webhook: invalid JSON")
        return web.Response(status=400, text="invalid json")

    event_type = str(payload.get("event") or "unknown")
    if not event_id:
        # Razorpay always includes the header in production. Fallback: build a
        # deterministic id from payment_id / payment_link_id + event so retries
        # still dedupe correctly.
        fallback_seed = (
            _extract_payment_id(payload)
            or _extract_payment_link_id(payload)
            or str(payload.get("created_at", ""))
        )
        event_id = f"local-{event_type}-{fallback_seed}"[:64]

    # --- 3. Idempotency guard (INSERT, dedup on UNIQUE) ---
    notes_user_id = _extract_notes_user_id(payload)

    async with get_session() as session:
        ev = PaymentEvent(
            razorpay_event_id=event_id,
            event_type=event_type,
            user_id=notes_user_id,
            payload_json=payload,
        )
        session.add(ev)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            log.info(
                "razorpay webhook: duplicate event %s (%s) \u2014 200 OK",
                event_id, event_type,
            )
            return web.Response(status=200, text="duplicate, ignored")

        # Need the row id later to mark processed_at.
        ev_id = ev.id
        await session.commit()

    # --- 4. Dispatch ---
    bot = request.app["bot"]
    processing_error: str | None = None
    try:
        if event_type == "payment_link.paid":
            await _handle_payment_link_paid(bot, payload)
        elif event_type == "payment.failed":
            await _handle_payment_failed(payload)
        else:
            log.info("razorpay webhook: ignored event_type=%s", event_type)
    except Exception as e:
        log.exception("razorpay webhook: processing error event=%s", event_id)
        processing_error = str(e)[:500]

    # --- 5. Mark processed ---
    async with get_session() as session:
        ev2 = await session.get(PaymentEvent, ev_id)
        if ev2 is not None:
            ev2.processed_at = datetime.now(timezone.utc)
            ev2.processing_error = processing_error
            await session.commit()

    return web.Response(status=200, text="ok")


# ---------- event handlers ----------

async def _handle_payment_link_paid(bot: Any, payload: dict[str, Any]) -> None:
    """Resolve user_id from notes (primary) or payment_link_id (fallback)."""
    user_id = _extract_notes_user_id(payload)
    payment_id = _extract_payment_id(payload)
    order_id = _extract_order_id(payload)
    link_id = _extract_payment_link_id(payload)

    if user_id is None and link_id is not None:
        async with get_session() as session:
            row = (await session.execute(
                select(Subscription.user_id)
                .where(Subscription.razorpay_payment_link_id == link_id)
            )).first()
            if row is not None:
                user_id = row[0]

    if user_id is None:
        log.error(
            "razorpay webhook: payment_link.paid could not resolve user "
            "(link_id=%s payment_id=%s)", link_id, payment_id,
        )
        return

    await upgrade_to_paid(bot, user_id, payment_id=payment_id, order_id=order_id)


async def _handle_payment_failed(payload: dict[str, Any]) -> None:
    payment_id = _extract_payment_id(payload)
    log.warning(
        "razorpay webhook: payment.failed payment_id=%s notes_user_id=%s",
        payment_id, _extract_notes_user_id(payload),
    )


# ---------- payload extractors ----------

def _extract_notes_user_id(payload: dict[str, Any]) -> int | None:
    """Razorpay nests entities under payload.<entity>.entity.notes."""
    entities = payload.get("payload") or {}
    for ent_name in ("payment_link", "payment", "order"):
        ent = (entities.get(ent_name) or {}).get("entity") or {}
        notes = ent.get("notes") or {}
        raw = notes.get("user_id")
        if raw:
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None
    return None


def _extract_payment_id(payload: dict[str, Any]) -> str | None:
    ent = ((payload.get("payload") or {}).get("payment") or {}).get("entity") or {}
    pid = ent.get("id")
    return str(pid) if pid else None


def _extract_order_id(payload: dict[str, Any]) -> str | None:
    ent = ((payload.get("payload") or {}).get("payment") or {}).get("entity") or {}
    oid = ent.get("order_id")
    return str(oid) if oid else None


def _extract_payment_link_id(payload: dict[str, Any]) -> str | None:
    ent = ((payload.get("payload") or {}).get("payment_link") or {}).get("entity") or {}
    lid = ent.get("id")
    return str(lid) if lid else None
