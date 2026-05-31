"""Rate limit counter \u2014 atomic per-user-per-day-per-action increment.

Pattern: "reserve a slot" before performing an action.

    allowed, count = await reserve_slot(session, user_id, "outreach", limit=5)
    if not allowed:
        raise RateLimitExceeded(f"already at {count}/{limit}")
    # \u2026 perform the action \u2026

The reserve is atomic via INSERT ... ON CONFLICT DO UPDATE RETURNING. If two
workers race for the last slot, exactly one gets `count <= limit` and the
other gets `count > limit`. Telemetry-wise the counter may exceed the limit
by a few \u2014 that's intentional: it tells us "this user tried to spam".
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import RateLimitCounter


async def reserve_slot(
    session: AsyncSession,
    user_id: int,
    action: str,
    *,
    limit: int,
) -> tuple[bool, int]:
    """Atomically increment the counter and return (allowed, new_count).

    `allowed` is True iff `new_count <= limit`.
    `period_date` is always UTC \u2014 keeps the day boundary consistent for users
    in any timezone. Display can convert.
    """
    today_utc = datetime.now(timezone.utc).date()
    stmt = (
        pg_insert(RateLimitCounter)
        .values(user_id=user_id, action=action, period_date=today_utc, count=1)
        .on_conflict_do_update(
            index_elements=["user_id", "action", "period_date"],
            set_={"count": RateLimitCounter.__table__.c.count + 1},
        )
        .returning(RateLimitCounter.count)
    )
    result = await session.execute(stmt)
    new_count = result.scalar_one()
    return (new_count <= limit, new_count)


async def current_count(
    session: AsyncSession,
    user_id: int,
    action: str,
    *,
    on_date: date | None = None,
) -> int:
    """Read-only \u2014 used by /status command."""
    on_date = on_date or datetime.now(timezone.utc).date()
    counter = await session.get(RateLimitCounter, (user_id, action, on_date))
    return counter.count if counter else 0


class RateLimitExceeded(Exception):
    """Raised when an action is denied because the user is at quota."""

    def __init__(self, action: str, count: int, limit: int):
        self.action = action
        self.count = count
        self.limit = limit
        super().__init__(f"{action}: {count}/{limit} for the day")
