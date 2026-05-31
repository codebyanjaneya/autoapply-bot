"""Application repository \u2014 tenant-scoped."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Application, AppStatus, Job
from core.tenant import TenantContext


async def create_scored_application(
    session: AsyncSession,
    ctx: TenantContext,
    job: Job,
    score: float,
    reason: str,
) -> Application:
    """Create an Application row from a fresh Groq score.

    Status: 'scored' if score >= user.min_score, else 'skipped'.
    """
    if job.user_id != ctx.user_id:
        # Defensive: should never happen if get_unscored_jobs was used, but
        # we're paranoid about cross-tenant writes.
        raise ValueError(f"Job {job.id} belongs to user {job.user_id}, not {ctx.user_id}")

    status = AppStatus.scored if score >= ctx.preferences.min_score else AppStatus.skipped
    app = Application(
        user_id=ctx.user_id,
        job_id=job.id,
        score=score,
        score_reason=reason,
        status=status,
    )
    session.add(app)
    await session.flush()
    return app


async def get_outreach_ready(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    limit: int,
) -> list[Application]:
    """Applications scored above threshold, not yet sent."""
    stmt = (
        select(Application)
        .where(Application.user_id == ctx.user_id)
        .where(Application.status == AppStatus.scored)
        .order_by(Application.score.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())
