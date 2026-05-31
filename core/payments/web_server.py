"""aiohttp web app hosting the Razorpay webhook.

main.py runs this alongside Dispatcher.start_polling using
``aiohttp.web.AppRunner`` + ``TCPSite``. Single process, two
event-loop tasks; no separate worker required.

Exposed routes:
    POST /webhooks/razorpay   \u2014 Razorpay webhook receiver
    GET  /health              \u2014 trivial liveness probe (returns 200 "ok")
"""
from __future__ import annotations

from aiogram import Bot
from aiohttp import web

from core.payments.webhook import razorpay_webhook


async def _health(_: web.Request) -> web.Response:
    return web.Response(text="ok")


def build_webhook_app(bot: Bot) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app.router.add_post("/webhooks/razorpay", razorpay_webhook)
    app.router.add_get("/health", _health)
    return app
