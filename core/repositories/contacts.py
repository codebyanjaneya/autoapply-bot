"""User-supplied recruiter contacts (week 6 /add_contacts).

Every query is tenant-scoped via ``user_id``.
"""
from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import UserContact

# Per-user safety cap. /add_contacts will reject batches that would push the
# user above this. Prevents a runaway CSV upload from filling the table.
MAX_CONTACTS_PER_USER = 500


async def list_contacts(session: AsyncSession, user_id: int) -> list[UserContact]:
    stmt = (
        select(UserContact)
        .where(UserContact.user_id == user_id)
        .order_by(UserContact.company.asc().nulls_last(), UserContact.email.asc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def count_contacts(session: AsyncSession, user_id: int) -> int:
    stmt = select(func.count()).select_from(UserContact).where(UserContact.user_id == user_id)
    return int((await session.execute(stmt)).scalar_one())


async def add_contacts(
    session: AsyncSession,
    user_id: int,
    items: list[dict],
) -> int:
    """Insert ``items`` (dicts with ``email`` + optional ``company``/``name``).

    Returns the number of NEW rows actually inserted (existing
    ``(user_id, email)`` pairs are skipped via ON CONFLICT DO NOTHING).
    """
    if not items:
        return 0
    rows = [
        {
            "user_id": user_id,
            "email": (it["email"] or "").strip().lower(),
            "company": (it.get("company") or None),
            "name": (it.get("name") or None),
        }
        for it in items
        if (it.get("email") or "").strip()
    ]
    if not rows:
        return 0
    stmt = (
        pg_insert(UserContact)
        .values(rows)
        .on_conflict_do_nothing(constraint="uq_user_contacts_user_email")
    )
    result = await session.execute(stmt)
    return result.rowcount or 0


async def clear_contacts(session: AsyncSession, user_id: int) -> int:
    stmt = delete(UserContact).where(UserContact.user_id == user_id)
    result = await session.execute(stmt)
    return result.rowcount or 0


async def find_by_company(
    session: AsyncSession,
    user_id: int,
    company: str,
) -> UserContact | None:
    """First contact whose ``company`` matches ``company`` case-insensitively.

    Returns None if no match (caller falls through to Hunter).
    """
    if not company:
        return None
    stmt = (
        select(UserContact)
        .where(UserContact.user_id == user_id)
        .where(func.lower(UserContact.company) == company.strip().lower())
        .order_by(UserContact.added_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_company_map(session: AsyncSession, user_id: int) -> dict[str, UserContact]:
    """Pre-load a ``{lower(company): contact}`` map for the outreach loop.

    Contacts with no company are excluded (can't auto-match). When multiple
    contacts share a company, the most-recently-added one wins.
    """
    rows = await list_contacts(session, user_id)
    out: dict[str, UserContact] = {}
    for c in rows:
        if not c.company:
            continue
        out[c.company.strip().lower()] = c  # last-write-wins; list_contacts is alpha-sorted
    return out
