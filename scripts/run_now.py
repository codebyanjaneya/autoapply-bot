"""Manually trigger the daily pipeline fan-out without waiting for 09:00 IST.

Use cases:
  - End-to-end testing right after onboarding finishes
  - Re-running today's pipeline for a single user after fixing a bug
  - One-off "catch-up" run after the bot was offline at 09:00

Examples:
    # Run for all active users (same as the scheduled job)
    python -m scripts.run_now

    # Run for one specific user (useful while testing onboarding)
    python -m scripts.run_now --user-id 839198672

    # Skip the Telegram notification (e.g. when debugging pipeline output
    # and you don't want the chat to flood)
    python -m scripts.run_now --quiet

    # Re-run today after a prior run already consumed the scan quota.
    # Wipes today's RateLimitCounter + DailyRunSummary for the user so the
    # pipeline actually re-scrapes. Does NOT touch jobs/applications/outreach
    # history — those represent real user state.
    python -m scripts.run_now --user-id 839198672 --reset-today
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
for noisy in ("httpx", "httpcore", "asyncio", "sqlalchemy.engine", "aiosmtplib"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

_REQUIRED = ["TELEGRAM_BOT_TOKEN", "DATABASE_URL", "ENCRYPTION_KEY",
             "GROQ_API_KEY", "ADZUNA_APP_ID", "ADZUNA_APP_KEY"]
_missing = [k for k in _REQUIRED if not os.environ.get(k)]
if _missing:
    print(f"FATAL: missing env vars: {', '.join(_missing)}", file=sys.stderr)
    sys.exit(1)

from datetime import datetime, timezone  # noqa: E402

from aiogram import Bot  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.enums import ParseMode  # noqa: E402
from sqlalchemy import delete  # noqa: E402

from core.db import get_session  # noqa: E402
from core.models import DailyRunSummary, RateLimitCounter  # noqa: E402
from core.scheduler import _run_one_user, run_daily_for_all_users  # noqa: E402

log = logging.getLogger("run_now")


async def _reset_today_for_user(user_id: int) -> None:
    """Wipe today's rate-limit + daily-summary rows so a manual re-run
    actually re-scrapes. Never touches Job/Application/OutreachLog rows —
    those are durable per-user history we want to keep across debug runs.
    """
    today = datetime.now(timezone.utc).date()
    async with get_session() as session:
        rl_result = await session.execute(
            delete(RateLimitCounter)
            .where(RateLimitCounter.user_id == user_id)
            .where(RateLimitCounter.period_date == today)
        )
        ds_result = await session.execute(
            delete(DailyRunSummary)
            .where(DailyRunSummary.user_id == user_id)
            .where(DailyRunSummary.run_date == today)
        )
        await session.commit()
    log.info(
        "--reset-today: user=%s cleared rate_limit_rows=%s daily_summary_rows=%s",
        user_id, rl_result.rowcount, ds_result.rowcount,
    )


class _SilentBot:
    """Stub that swallows send_message calls. Used with --quiet."""
    async def send_message(self, *args, **kwargs) -> None:  # noqa: D401
        log.info("(notification suppressed) chat_id=%s text=%r",
                 args[0] if args else kwargs.get("chat_id"),
                 (args[1] if len(args) > 1 else kwargs.get("text", ""))[:80])


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--user-id", type=int, default=None,
                   help="Run only for this user. Default: all active users.")
    p.add_argument("--quiet", action="store_true",
                   help="Skip the post-run Telegram notification.")
    p.add_argument("--reset-today", action="store_true",
                   help="Wipe this user's RateLimitCounter + DailyRunSummary "
                        "for today before running, so scrape/score actually "
                        "re-execute. Requires --user-id. Does NOT delete "
                        "jobs / applications / outreach history.")
    return p.parse_args()


async def amain() -> None:
    args = parse_args()
    if args.reset_today and args.user_id is None:
        print("FATAL: --reset-today requires --user-id", file=sys.stderr)
        sys.exit(2)
    if args.reset_today:
        await _reset_today_for_user(args.user_id)

    if args.quiet:
        bot: Bot | _SilentBot = _SilentBot()
        # No real bot session to close, no get_me() to call.
        if args.user_id is not None:
            log.info("manual run: user=%s quiet=True", args.user_id)
            await _run_one_user(bot, args.user_id)  # type: ignore[arg-type]
        else:
            log.info("manual run: all active users quiet=True")
            await run_daily_for_all_users(bot)  # type: ignore[arg-type]
        return

    bot = Bot(
        token=os.environ["TELEGRAM_BOT_TOKEN"],
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    try:
        me = await bot.get_me()
        log.info("notifying via @%s", me.username)
        if args.user_id is not None:
            log.info("manual run: user=%s", args.user_id)
            await _run_one_user(bot, args.user_id)
        else:
            log.info("manual run: all active users")
            await run_daily_for_all_users(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except (KeyboardInterrupt, SystemExit):
        log.info("interrupted; exiting")
