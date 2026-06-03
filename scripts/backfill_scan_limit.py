"""One-shot: raise legacy free-tier users from daily_scan_limit=2 to 20.

The model default is now 20 (core/models.py) and FREE_SCANS_PER_DAY=20
(core/payments/subscription_service.py), but users who signed up while the
default was 2 still have the old value persisted.

Usage:
    python -m scripts.backfill_scan_limit            # dry-run, prints affected users
    python -m scripts.backfill_scan_limit --apply    # actually update
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("backfill_scan_limit")

from sqlalchemy import select, update  # noqa: E402

from core.db import get_session  # noqa: E402
from core.models import User  # noqa: E402

OLD_LIMIT = 2
NEW_LIMIT = 20


async def main(apply: bool) -> int:
    async with get_session() as session:
        rows = (
            await session.execute(
                select(User.id, User.username, User.daily_scan_limit).where(
                    User.daily_scan_limit == OLD_LIMIT
                )
            )
        ).all()

        if not rows:
            log.info("No users found with daily_scan_limit=%s. Nothing to do.", OLD_LIMIT)
            return 0

        log.info("Found %d user(s) with daily_scan_limit=%s:", len(rows), OLD_LIMIT)
        for r in rows:
            log.info("  telegram_id=%s username=%s current=%s", r.id, r.username, r.daily_scan_limit)

        if not apply:
            log.info("Dry-run. Re-run with --apply to update %d row(s) to %s.", len(rows), NEW_LIMIT)
            return len(rows)

        result = await session.execute(
            update(User)
            .where(User.daily_scan_limit == OLD_LIMIT)
            .values(daily_scan_limit=NEW_LIMIT)
        )
        await session.commit()
        log.info("Updated %d row(s) to daily_scan_limit=%s.", result.rowcount, NEW_LIMIT)
        return result.rowcount


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Actually perform the update (default: dry-run).")
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply))
