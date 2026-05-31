"""Job repository \u2014 every query is tenant-scoped.

Convention: functions take `ctx: TenantContext` as the second arg (after
session) and use `ctx.user_id` in every WHERE clause. No exceptions.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Application, Job, JobSource
from core.scrapers.base import ScrapedJob
from core.tenant import TenantContext


async def upsert_scraped_jobs(
    session: AsyncSession,
    ctx: TenantContext,
    scraped: list[ScrapedJob],
    source: JobSource,
) -> int:
    """Insert scraped jobs for this user; skip duplicates.

    Uses Postgres ON CONFLICT on (user_id, source, external_id) DO NOTHING.
    Returns the number of NEW rows inserted (excludes duplicates).
    """
    if not scraped:
        return 0

    rows = [
        {
            "user_id": ctx.user_id,
            "source": source,
            "external_id": s.external_id,
            "url": s.url,
            "title": s.title,
            "company": s.company,
            "location": s.location,
            "description": s.description,
            "salary_min": s.salary_min,
            "salary_max": s.salary_max,
            "salary_currency": s.salary_currency,
            "salary_is_predicted": s.salary_is_predicted,
            "posted_at": s.posted_at,
        }
        for s in scraped
    ]
    stmt = (
        pg_insert(Job)
        .values(rows)
        .on_conflict_do_nothing(
            index_elements=["user_id", "source", "external_id"],
        )
    )
    result = await session.execute(stmt)
    return result.rowcount or 0


async def get_unscored_jobs(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    limit: int = 50,
) -> list[Job]:
    """Jobs for this user that have no Application row yet."""
    stmt = (
        select(Job)
        .outerjoin(
            Application,
            (Application.job_id == Job.id) & (Application.user_id == ctx.user_id),
        )
        .where(Job.user_id == ctx.user_id)
        .where(Application.id.is_(None))
        .order_by(Job.scraped_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())
