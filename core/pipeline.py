"""Per-user pipeline orchestrator.

Run once per user per day (driven by APScheduler in week 4):
    1. Scrape jobs from Adzuna for each (role x location) pair, respecting
       the user's daily_scan_limit.
    2. Upsert into `jobs` (dedup via (user_id, source, external_id)).
    3. Score newly-arrived jobs via Groq, create `applications` rows.
    4. Outreach: pick top-scored applications, lookup recruiter via Hunter,
       send email via user's SMTP, respect daily_outreach_limit.
    5. Write a `DailyRunSummary` row.

Errors in any stage are caught and recorded in summary.error \u2014 we never let
one user's failure block another user's run.

Transaction boundaries:
- Stages 1-3 share one session (the caller's). A crash there rolls back the
  whole scrape/score batch \u2014 acceptable, we'll re-run.
- Stage 4 (outreach) opens a FRESH session per send. This means:
    * Each rate-limit slot consumption is durable even if a later send crashes
    * Each OutreachLog row is committed independently
    * One bad SMTP send doesn't invalidate earlier successful sends
  Critical for billing integrity \u2014 a paid user shouldn't get refunded their
  daily quota because send #4 failed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_session
from core.enrich.recruiter_finder import build_recruiter_finder
from core.mailer.smtp_sender import SMTPSender
from core.models import DailyRunSummary, JobSource
from core.outreach import OutreachOutcome, send_outreach_for_application
from core.repositories import applications as apps_repo
from core.repositories import contacts as contacts_repo
from core.repositories import jobs as jobs_repo
from core.repositories import rate_limits as rl
from core.scoring.groq_scorer import GroqScorer
from core.scrapers.adzuna import AdzunaScraper
from core.scrapers.query_expansion import expand_role
from core.tenant import TenantContext, load_tenant_context

log = logging.getLogger(__name__)


async def run_pipeline_for_user(
    session: AsyncSession,
    ctx: TenantContext,
    *,
    http_client: httpx.AsyncClient | None = None,
    scorer: GroqScorer | None = None,
    no_send: bool = False,
) -> DailyRunSummary:
    """Run today's pipeline for one user. Idempotent within a day (rate
    counter prevents double-scraping; ON CONFLICT prevents dup jobs).
    """
    # SQLAlchemy `default=0` fires at flush, not construction \u2014 so unset
    # Integer attributes start as None, and `summary.jobs_scraped += n`
    # would raise TypeError. Initialise all counters explicitly here.
    # Use UTC date (matches RateLimitCounter.period_date convention) so
    # late-IST runs don't write rows under tomorrow's date while readers
    # query today's date.
    summary = DailyRunSummary(
        user_id=ctx.user_id,
        run_date=datetime.now(timezone.utc).date(),
        jobs_scraped=0,
        jobs_scored=0,
        outreach_sent=0,
        outreach_failed=0,
        replies_received=0,
    )
    scorer = scorer or GroqScorer()

    # Diagnostic: surface credential + preference state at the top of the
    # run. When a real user reports "ran but no jobs found / no emails sent",
    # the first thing we want in the log is whether SMTP / recruiter keys
    # were even loaded and which roles/locations we're actually scraping.
    creds = ctx.credentials
    prefs = ctx.preferences
    smtp_status = (
        f"smtp=OK<{creds.smtp_email}>"
        if creds.smtp_email and creds.smtp_password_encrypted is not None
        else f"smtp=MISSING(email={'set' if creds.smtp_email else 'none'},"
             f"pw={'set' if creds.smtp_password_encrypted is not None else 'none'})"
    )
    apollo_status = "apollo=OK" if creds.apollo_api_key_encrypted is not None else "apollo=MISSING"
    log.info(
        "pipeline start: user=%s tier=%s status=%s %s %s no_send=%s",
        ctx.user_id, ctx.user.subscription_tier.value, ctx.user.status.value,
        smtp_status, apollo_status, no_send,
    )
    log.info(
        "pipeline prefs: user=%s roles=%r locations=%r min_score=%s scan_limit=%s outreach_limit=%s",
        ctx.user_id, prefs.role_keywords, prefs.locations,
        getattr(prefs, "min_score", "?"),
        ctx.user.daily_scan_limit, ctx.user.daily_outreach_limit,
    )
    # The pipeline silently scrapes nothing when role_keywords is empty
    # (the `for query in ... or [""]` falls through via `if not query`).
    # This is the #1 cause of "no new jobs found" for users whose
    # onboarding wizard did not commit role/location data. Make it loud.
    if not prefs.role_keywords:
        log.warning(
            "pipeline: user %s has EMPTY role_keywords — nothing will be scraped. "
            "User must re-run /start to set roles.", ctx.user_id,
        )
    if not prefs.locations:
        log.warning(
            "pipeline: user %s has EMPTY locations — will fall back to empty-location "
            "Adzuna query (likely 0 results). User should re-run /start.", ctx.user_id,
        )

    try:
        # ---------- 1+2. Scrape & upsert ----------
        scraper = AdzunaScraper(client=http_client)
        async with scraper:
            # Expand each user-entered role into 1-3 closely-related queries
            # (e.g. "python developer" -> + "python engineer", "backend python
            # developer") so a single role configured by the user reaches
            # 3x more recruiter postings. Dedup is per (user, source,
            # external_id) so overlap between expanded queries is harmless.
            expanded_queries: list[str] = []
            seen_queries: set[str] = set()
            for raw_role in ctx.preferences.role_keywords or []:
                for q in expand_role(raw_role):
                    key = q.lower()
                    if key in seen_queries:
                        continue
                    seen_queries.add(key)
                    expanded_queries.append(q)
            if expanded_queries != list(ctx.preferences.role_keywords or []):
                log.info(
                    "pipeline: user=%s role expansion %r -> %r",
                    ctx.user_id, ctx.preferences.role_keywords, expanded_queries,
                )
            for query in expanded_queries or [""]:
                if not query:
                    continue
                for loc in ctx.preferences.locations or [""]:
                    allowed, _count = await rl.reserve_slot(
                        session, ctx.user_id, "scan",
                        limit=ctx.user.daily_scan_limit,
                    )
                    if not allowed:
                        log.info("user %s hit scan limit, stopping scrape", ctx.user_id)
                        break
                    try:
                        # Fetch up to 60 results (3 Adzuna pages of 20) with a
                        # 3-day freshness window. Wider net than the original
                        # 20/7-day defaults so the daily run keeps surfacing
                        # fresh listings even when the top-of-results stays
                        # stable day-to-day (dedup is per-user, so re-seen
                        # IDs are correctly suppressed by upsert_scraped_jobs).
                        scraped = await scraper.search(
                            query, loc, limit=60, max_days_old=3,
                        )
                    except httpx.HTTPStatusError as e:
                        log.warning("adzuna http %s for user %s query=%r loc=%r",
                                    e.response.status_code, ctx.user_id, query, loc)
                        continue
                    new_count = await jobs_repo.upsert_scraped_jobs(
                        session, ctx, scraped, JobSource.adzuna,
                    )
                    log.info(
                        "scrape: user=%s role=%r loc=%r adzuna_returned=%s new_after_dedup=%s",
                        ctx.user_id, query, loc, len(scraped), new_count,
                    )
                    summary.jobs_scraped += new_count
                else:
                    continue
                break  # broke out of inner loop because of rate limit

        # ---------- 3. Score ----------
        unscored = await jobs_repo.get_unscored_jobs(session, ctx, limit=50)
        log.info("scoring: user=%s unscored_jobs_found=%s", ctx.user_id, len(unscored))
        for job in unscored:
            try:
                score, reason = await scorer.score(job, ctx.preferences)
            except Exception as e:
                log.warning("scorer failed user=%s job=%s: %s", ctx.user_id, job.id, e)
                score, reason = 0.0, f"scorer-error: {type(e).__name__}"
            await apps_repo.create_scored_application(session, ctx, job, score, reason)
            summary.jobs_scored += 1

    except Exception as e:
        log.exception("scrape/score crashed for user %s", ctx.user_id)
        summary.error = f"{type(e).__name__}: {e}"[:500]

    # Persist scrape/score so outreach (which uses fresh sessions) sees it.
    await session.commit()
    log.info(
        "scrape/score done: user=%s jobs_scraped(new)=%s jobs_scored=%s error=%s",
        ctx.user_id, summary.jobs_scraped, summary.jobs_scored, summary.error or "none",
    )

    # ---------- 4. Outreach (own session per send) ----------
    sent, failed, no_recruiter, hunter_quota_exhausted = await _run_outreach_phase(ctx, no_send=no_send)
    summary.outreach_sent = sent
    summary.outreach_failed = failed
    # In-memory flags (not persisted) consumed by scheduler._notify_user
    # to decorate today's Telegram summary with the right nudges.
    summary.hunter_quota_exhausted = hunter_quota_exhausted  # type: ignore[attr-defined]
    summary.no_recruiter_count = no_recruiter  # type: ignore[attr-defined]

    # Use merge() so a same-day re-run UPDATEs instead of duplicate-PK-erroring.
    # In production this matters because APScheduler may retry a crashed user.
    await session.merge(summary)
    log.info(
        "pipeline done: user=%s jobs_scraped=%s jobs_scored=%s outreach_sent=%s "
        "outreach_failed=%s hunter_quota_exhausted=%s",
        ctx.user_id, summary.jobs_scraped, summary.jobs_scored,
        summary.outreach_sent, summary.outreach_failed, hunter_quota_exhausted,
    )
    return summary


async def _run_outreach_phase(ctx: TenantContext, *, no_send: bool = False) -> tuple[int, int, int, bool]:
    """Send up to `daily_outreach_limit` emails today.

    Returns ``(sent, failed, no_recruiter, hunter_quota_exhausted)``. The
    last element is True when Hunter returned HTTP 429 during this run —
    caller (scheduler) appends the /upgrade pitch to the Telegram
    notification. ``no_recruiter`` is the count of candidate applications
    where neither a manual contact nor Hunter could surface a recruiter —
    used to decide whether to nudge the user to /add_contacts.

    Skips silently (returns 0,0,False) when:
    - User has no Hunter key (free tier without one provided)
    - User has no SMTP credentials (onboarding incomplete or paused outreach)

    Each send gets its own DB session so failures are isolated.

    When ``no_send=True``: SMTP is NOT called — we still exercise recruiter
    lookup, rendering, and logging, but never hit the wire.
    """
    hunter = build_recruiter_finder(ctx)
    if hunter is None:
        return (0, 0, 0, False)

    # SMTPSender is built per-user from their encrypted Gmail app password.
    # In no-send mode we still need a sender object for the
    # `async with sender:` lifecycle below; it's never actually invoked
    # because send_outreach_for_application short-circuits on no_send.
    sender = SMTPSender.from_credentials(ctx.credentials)
    if sender is None:
        if no_send:
            log.info("user %s has no SMTP creds; proceeding in NO-SEND mode with stub", ctx.user_id)
            sender = SMTPSender(email="dry-run@example.invalid", password="unused")
        else:
            log.info("user %s has no SMTP credentials; outreach skipped", ctx.user_id)
            return (0, 0, 0, False)

    # Pre-load the user's manual contact list ONCE per run, keyed by
    # lower(company). Each per-send call gets the same map so a hit avoids
    # a Hunter lookup entirely.
    async with get_session() as session:
        manual_contacts = await contacts_repo.get_company_map(session, ctx.user_id)
    if manual_contacts:
        log.info("user %s has %d manual contacts with company tags",
                 ctx.user_id, len(manual_contacts))

    # Grab candidate IDs in one short-lived session so we don't hold a
    # transaction open while making slow HTTP calls.
    async with get_session() as session:
        candidates = await apps_repo.get_outreach_ready(
            session, ctx,
            limit=ctx.user.daily_outreach_limit * 3,  # over-fetch; many will have no recruiter
        )
        candidate_ids = [a.id for a in candidates]

    if not candidate_ids:
        log.info("user %s has no outreach-ready applications today", ctx.user_id)
        return (0, 0, 0, False)

    sent = 0
    failed = 0
    no_recruiter = 0
    hunter_quota_exhausted = False
    async with hunter, sender:
        for app_id in candidate_ids:
            if sent >= ctx.user.daily_outreach_limit:
                log.info("user %s reached outreach quota (%s sent); stopping",
                         ctx.user_id, sent)
                break
            # Each attempt = its own transaction. The orchestrator handles
            # the slot reservation INTERNALLY (only after Hunter finds a
            # recruiter), so a long string of no-recruiter results doesn't
            # burn the user's daily quota.
            async with get_session() as send_session:
                # Refresh tenant context inside this session so any
                # encrypted-column access stays attached.
                send_ctx = await load_tenant_context(send_session, ctx.user_id)
                if send_ctx is None:
                    break

                outcome = await send_outreach_for_application(
                    send_session, send_ctx, app_id,
                    hunter=hunter, smtp=sender,
                    daily_outreach_limit=ctx.user.daily_outreach_limit,
                    no_send=no_send,
                    manual_contacts=manual_contacts,
                )
            # Outcome accounting happens AFTER the session closes (counters
            # are local Python ints, not DB state).
            if outcome is OutreachOutcome.sent:
                sent += 1
            elif outcome is OutreachOutcome.smtp_failed:
                failed += 1
            elif outcome is OutreachOutcome.no_recruiter:
                no_recruiter += 1
            elif outcome is OutreachOutcome.rate_limited:
                # Server-side enforcement said stop. Honour it.
                break
            elif outcome is OutreachOutcome.hunter_quota_exhausted:
                # Hunter 429 \u2014 don't burn more lookups today; tell the
                # caller so it can pitch /upgrade in the Telegram summary.
                hunter_quota_exhausted = True
                break
            # OutreachOutcome.invalid: just skip silently, already logged

    log.info("user %s outreach phase: sent=%s smtp_failed=%s no_recruiter=%s "
             "hunter_quota_exhausted=%s (quota=%s, candidates_tried=%s)",
             ctx.user_id, sent, failed, no_recruiter, hunter_quota_exhausted,
             ctx.user.daily_outreach_limit, len(candidate_ids))
    return (sent, failed, no_recruiter, hunter_quota_exhausted)
