"""Lazy Razorpay SDK client + asyncio wrapper.

The razorpay SDK is synchronous (requests-based). To keep it from
blocking the aiogram event loop, wrap calls with ``asyncio.to_thread``.

Keys come from env:
    RAZORPAY_KEY_ID
    RAZORPAY_KEY_SECRET
    RAZORPAY_WEBHOOK_SECRET   (only needed by webhook handler)

The client is constructed lazily so importing this module without the
env vars set (e.g. during alembic offline migrations) does NOT crash.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable, TypeVar

import razorpay

log = logging.getLogger(__name__)

_T = TypeVar("_T")
_client: razorpay.Client | None = None


def get_client() -> razorpay.Client:
    """Return a process-wide razorpay.Client, constructing on first use."""
    global _client
    if _client is None:
        key_id = os.environ.get("RAZORPAY_KEY_ID")
        key_secret = os.environ.get("RAZORPAY_KEY_SECRET")
        if not key_id or not key_secret:
            raise RuntimeError(
                "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET not set \u2014 see .env.example"
            )
        _client = razorpay.Client(auth=(key_id, key_secret))
        log.info("razorpay client initialised (key_id=%s\u2026)", key_id[:12])
    return _client


def get_webhook_secret() -> str:
    secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET")
    if not secret:
        raise RuntimeError(
            "RAZORPAY_WEBHOOK_SECRET not set \u2014 configure it in the Razorpay "
            "dashboard webhook settings and copy the same value into .env"
        )
    return secret


async def run_blocking(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """Run a blocking razorpay SDK call in a worker thread."""
    return await asyncio.to_thread(fn, *args, **kwargs)
