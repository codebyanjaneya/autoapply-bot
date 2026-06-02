"""Service entry point: aiogram bot polling + APScheduler daily job.

Run locally:
    python main.py

Required env (.env loaded automatically):
    TELEGRAM_BOT_TOKEN   from @BotFather
    DATABASE_URL         Postgres URL (Neon, etc.)
    ENCRYPTION_KEY       Fernet key (see core/crypto.py for generation)
    GROQ_API_KEY         scoring
    ADZUNA_APP_ID / ADZUNA_APP_KEY   job source
    HUNTER_API_KEY       optional; pooled key for paid-tier users

The bot uses long-polling (no webhook server, no public URL needed for
MVP). Switch to webhooks before scaling past ~100 active users; aiogram's
polling has a cap around 30 updates/s which we won't hit before that.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

# Configure logging BEFORE any other imports so module-level loggers pick
# up the formatter. Keep third-party libs at WARNING to reduce noise.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
for noisy in ("httpx", "httpcore", "asyncio", "sqlalchemy.engine",
              "aiosmtplib", "apscheduler.scheduler", "apscheduler.executors.default"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

log = logging.getLogger("main")

# Fail fast on missing env \u2014 cheaper than failing inside a handler later.
_REQUIRED = ["TELEGRAM_BOT_TOKEN", "DATABASE_URL", "ENCRYPTION_KEY",
             "GROQ_API_KEY", "ADZUNA_APP_ID", "ADZUNA_APP_KEY"]
_missing = [k for k in _REQUIRED if not os.environ.get(k)]
if _missing:
    print(f"FATAL: missing env vars: {', '.join(_missing)}", file=sys.stderr)
    sys.exit(1)

from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.enums import ParseMode  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402
from aiogram.types import BotCommand  # noqa: E402
from aiohttp import web  # noqa: E402

from core.bot import commands_router, contacts_router, onboarding_router, settings_router  # noqa: E402
from core.payments import build_webhook_app  # noqa: E402
from core.scheduler import build_scheduler  # noqa: E402


# Order = order shown in Telegram's Menu button. Keep most-used at the top
# so the Menu list reads like the natural daily flow.
_BOT_COMMANDS: list[BotCommand] = [
    BotCommand(command="start",          description="Set up your job hunt"),
    BotCommand(command="howitworks",     description="How the bot works"),
    BotCommand(command="status",         description="Today's run summary"),
    BotCommand(command="history",        description="All outreach so far (paginated)"),
    BotCommand(command="updaterole",     description="Quick: change roles to search"),
    BotCommand(command="updateresume",   description="Quick: upload a new resume PDF"),
    BotCommand(command="add_contacts",   description="Add recruiter emails you know"),
    BotCommand(command="contacts",       description="View saved recruiter contacts"),
    BotCommand(command="clear_contacts", description="Remove all saved contacts"),
    BotCommand(command="settime",        description="Change your daily run time"),
    BotCommand(command="settings",       description="Update all preferences"),
    BotCommand(command="upgrade",        description="View Pro plan & pricing"),
    BotCommand(command="features",       description="What Pro includes"),
    BotCommand(command="subscription",   description="Your current plan"),
    BotCommand(command="referral",       description="Invite friends, earn free months"),
    BotCommand(command="pause",          description="Pause daily runs"),
    BotCommand(command="resume",         description="Resume daily runs"),
    BotCommand(command="restart",        description="Redo onboarding"),
    BotCommand(command="help",           description="All commands"),
]


async def amain() -> None:
    bot = Bot(
        token=os.environ["TELEGRAM_BOT_TOKEN"],
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    # MemoryStorage is fine for one process \u2014 onboarding state lives in RAM.
    # When we scale horizontally, swap to RedisStorage so multiple bot
    # workers share FSM state.
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(onboarding_router)
    dp.include_router(settings_router)
    dp.include_router(contacts_router)
    dp.include_router(commands_router)

    scheduler = build_scheduler(bot=bot)
    scheduler.start()
    log.info(
        "scheduler started \u2014 hourly fan-out at minute 0, billing crons at "
        "00:05 + 09:30 Asia/Kolkata"
    )

    # --- Razorpay webhook server (aiohttp) ---
    # Runs alongside Dispatcher.start_polling in the same event loop.
    # Bind 0.0.0.0 so the deploy host (Railway/Fly/etc.) can reach it; the
    # public URL is whatever the hosting platform exposes. Locally use a
    # tunnel (cloudflared / ngrok) and point Razorpay dashboard at it.
    #
    # Port resolution order:
    #   1. RAZORPAY_WEBHOOK_PORT  — explicit override
    #   2. PORT                   — Railway / Heroku / Fly inject this
    #   3. 8000                   — local dev default
    # Railway crashes the deploy if the service never binds to $PORT, so
    # the fallback to PORT is the difference between green and red there.
    webhook_port = int(
        os.environ.get("RAZORPAY_WEBHOOK_PORT")
        or os.environ.get("PORT")
        or "8000"
    )
    webhook_app = build_webhook_app(bot=bot)
    runner = web.AppRunner(webhook_app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=webhook_port)
    await site.start()
    log.info(
        "webhook server listening on 0.0.0.0:%d  (POST /webhooks/razorpay)",
        webhook_port,
    )

    me = await bot.get_me()
    log.info("bot online as @%s (id=%s)", me.username, me.id)

    # Register the slash-command list with Telegram so the Menu button at
    # the bottom-left of every chat shows them automatically. set_my_commands
    # is idempotent \u2014 cheap to call on every startup, and updates the list
    # immediately for all users (no BotFather round-trip needed).
    try:
        await bot.set_my_commands(_BOT_COMMANDS)
        log.info("registered %d bot commands with Telegram", len(_BOT_COMMANDS))
    except Exception:
        # Don't crash startup if Telegram is briefly unavailable \u2014 the bot
        # still works, the Menu button just won't show the latest list.
        log.exception("set_my_commands failed (non-fatal)")

    try:
        # drop_pending_updates: ignore the backlog from while the bot was offline.
        # For an MVP this is the right call; later we may want to process the
        # backlog so /status messages aren't dropped.
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        scheduler.shutdown(wait=False)
        await runner.cleanup()
        await bot.session.close()
        log.info("shutdown complete")


def main() -> None:
    try:
        asyncio.run(amain())
    except (KeyboardInterrupt, SystemExit):
        log.info("interrupted; exiting")


if __name__ == "__main__":
    main()
