"""Per-application outreach: lookup recruiter, render email, send, log.

Rate-limit semantics:
  Quota = emails actually sent (or attempted at SMTP layer).
  A failed Hunter lookup is FREE \u2014 we do not consume the user's daily
  outreach slot if there's no recruiter to email. Otherwise a free-tier
  user with a niche industry / bad Hunter coverage would burn all 5 slots
  finding nothing.

  The slot is reserved INSIDE this function, AFTER Hunter returns a
  recruiter but BEFORE we call SMTP. This means:
    - no recruiter found        -> no slot consumed, no log row
    - recruiter found + sent    -> slot consumed (success)
    - recruiter found + SMTP fail -> slot consumed (don't retry-loop the
                                     same broken creds today)
    - quota already at limit    -> no slot consumed beyond the rejection
                                   (reserve_slot increments, sees over-limit,
                                   pipeline stops looping)

Each call runs in its own session (opened by pipeline._run_outreach_phase)
so an SMTP crash on attempt N doesn't roll back N-1's successful state.
"""
from __future__ import annotations

import enum
import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from core.enrich.hunter import HunterQuotaExhausted, Recruiter
from core.enrich.recruiter_finder import RecruiterFinder
from core.mailer.smtp_sender import SMTPSender
from core.models import Application, AppStatus, Job, OutreachLog, UserContact
from core.repositories import rate_limits as rl
from core.tenant import TenantContext

log = logging.getLogger(__name__)


class OutreachOutcome(str, enum.Enum):
    """How the caller (pipeline) should account for this attempt."""

    sent = "sent"                       # email delivered (or NO-SEND simulated)
    smtp_failed = "smtp_failed"         # recruiter found, SMTP raised
    no_recruiter = "no_recruiter"       # Hunter returned nothing \u2014 FREE
    rate_limited = "rate_limited"       # daily quota exhausted \u2014 caller should stop loop
    hunter_quota_exhausted = "hunter_quota_exhausted"  # Hunter 429 \u2014 caller stops & notifies
    invalid = "invalid"                 # app/job missing or tenant mismatch \u2014 skip


async def send_outreach_for_application(
    session: AsyncSession,
    ctx: TenantContext,
    application_id: int,
    *,
    hunter: RecruiterFinder,
    smtp: SMTPSender,
    daily_outreach_limit: int,
    no_send: bool = False,
    manual_contacts: dict[str, UserContact] | None = None,
) -> OutreachOutcome:
    """Run the outreach pipeline for one application.

    Returns an :class:`OutreachOutcome` describing what happened so the
    pipeline can update counters and decide whether to keep looping.

    ``manual_contacts`` maps ``lower(company) -> UserContact``. When the
    job's company hits in this map we use that email DIRECTLY and skip the
    Hunter lookup entirely (saves quota + the contact is a known person
    rather than a guessed pattern). Source is logged as ``manual``.

    Logging is intentionally chatty at INFO level so ``dry_run.py`` output
    shows exactly which company was looked up, whether Hunter found a
    recruiter, and the precise failure reason on errors.
    """
    app = await session.get(Application, application_id)
    if app is None or app.user_id != ctx.user_id:
        log.error("outreach[app=%s] INVALID: missing or wrong tenant for user %s",
                  application_id, ctx.user_id)
        return OutreachOutcome.invalid
    job = await session.get(Job, app.job_id)
    if job is None:
        log.error("outreach[app=%s] INVALID: job %s gone", app.id, app.job_id)
        return OutreachOutcome.invalid

    company = job.company
    log.info("outreach[app=%s] lookup company=%r (score=%.1f)", app.id, company, app.score)

    # ---------- 1. Manual contact short-circuit (FREE \u2014 no slot consumed yet) ----------
    # If the user pasted a recruiter email tagged with this company via
    # /add_contacts, use it directly and skip Hunter. Big quota win for
    # users with known recruiter contacts.
    recruiter: Recruiter | None = None
    if manual_contacts and company:
        match = manual_contacts.get(company.strip().lower())
        if match is not None:
            recruiter = Recruiter(
                email=match.email,
                source="manual",
                name=match.name,
                position=None,
            )
            log.info(
                "outreach[app=%s] MANUAL CONTACT hit for company=%r email=%s",
                app.id, company, recruiter.email,
            )

    # ---------- 1b. Recruiter lookup (FREE \u2014 no slot consumed yet) ----------
    if recruiter is None:
        try:
            recruiter = await hunter.find_recruiter(company)
        except HunterQuotaExhausted as e:
            # Free plan's 25/month gone (or pooled key exhausted). No slot
            # consumed; pipeline will halt the loop and notify the user.
            log.warning("outreach[app=%s] HUNTER QUOTA EXHAUSTED: %s", app.id, e)
            return OutreachOutcome.hunter_quota_exhausted
        except Exception as e:
            # HunterClient already catches HTTP/network errors and returns None.
            # An exception here means something genuinely unexpected (bug, OOM, ...).
            log.exception("outreach[app=%s] HUNTER CRASHED for company=%r: %s",
                          app.id, company, e)
            app.status = AppStatus.skipped
            _append_note(app, f"hunter-crashed: {type(e).__name__}: {e}")
            return OutreachOutcome.no_recruiter

    if recruiter is None:
        log.info("outreach[app=%s] NO RECRUITER for company=%r \u2014 skipping (no slot consumed)",
                 app.id, company)
        app.status = AppStatus.skipped
        _append_note(app, f"no-recruiter-found:{company}")
        return OutreachOutcome.no_recruiter

    log.info("outreach[app=%s] FOUND recruiter=%s source=%s name=%r position=%r",
             app.id, recruiter.email, recruiter.source, recruiter.name, recruiter.position)

    # ---------- 2. Reserve rate-limit slot (only now that we have a real target) ----------
    allowed, count = await rl.reserve_slot(
        session, ctx.user_id, "outreach", limit=daily_outreach_limit,
    )
    if not allowed:
        log.info("outreach[app=%s] RATE LIMITED at count=%s/%s \u2014 stopping",
                 app.id, count, daily_outreach_limit)
        # Don't mark the app as skipped \u2014 it should be picked up tomorrow.
        return OutreachOutcome.rate_limited

    # ---------- 3. Compose ----------
    subject, body = _render_email(ctx, job, app, recruiter)

    # ---------- 3a. Dry-run short-circuit ----------
    if no_send:
        log.info("outreach[app=%s] [NO-SEND] would email %s subject=%r",
                 app.id, recruiter.email, subject)
        session.add(OutreachLog(
            user_id=ctx.user_id,
            application_id=app.id,
            to_email=recruiter.email,
            subject=subject,
            error="[NO-SEND] dry-run: SMTP skipped",
        ))
        _append_note(app, f"[NO-SEND] would email -> {recruiter.email}")
        # Leave status as `scored` so a real run later picks it up again.
        return OutreachOutcome.sent

    # ---------- 4. Send ----------
    # User's stored Gmail address IS the SMTP login AND the From: header,
    # so recruiter replies land in their inbox natively (no Reply-To hack).
    # SMTPSender was built from these credentials in the pipeline, so this
    # guard is a defence-in-depth sanity check.
    if not ctx.credentials.smtp_email:
        log.error("outreach[app=%s] user %s has no SMTP email; can't send",
                  app.id, ctx.user_id)
        _append_note(app, "missing-smtp-email")
        return OutreachOutcome.smtp_failed
    try:
        await smtp.send(
            to=recruiter.email,
            subject=subject,
            body=body,
            from_name=ctx.credentials.candidate_name or ctx.user.first_name,
            attachment=ctx.credentials.resume_pdf,
            attachment_filename=ctx.credentials.resume_filename,
        )
    except Exception as e:
        # Slot stays consumed: a wrong app password / Gmail quota issue
        # shouldn't loop forever burning through candidate apps.
        log.warning("outreach[app=%s] SMTP FAILED to=%s: %s: %s",
                    app.id, recruiter.email, type(e).__name__, e)
        session.add(OutreachLog(
            user_id=ctx.user_id,
            application_id=app.id,
            to_email=recruiter.email,
            subject=subject,
            error=f"{type(e).__name__}: {e}"[:500],
        ))
        app.status = AppStatus.error
        _append_note(app, f"smtp-error: {type(e).__name__}: {e}")
        return OutreachOutcome.smtp_failed

    # ---------- 5. Success ----------
    log.info("outreach[app=%s] SENT to=%s", app.id, recruiter.email)
    app.status = AppStatus.sent
    app.sent_at = datetime.now(timezone.utc)
    app.recruiter_email = recruiter.email
    app.recruiter_source = recruiter.source
    _append_note(app, f"outreach -> {recruiter.email}")
    session.add(OutreachLog(
        user_id=ctx.user_id,
        application_id=app.id,
        to_email=recruiter.email,
        subject=subject,
    ))
    return OutreachOutcome.sent


def _append_note(app: Application, fragment: str) -> None:
    """Notes are append-only audit trail. Newline-separated."""
    prefix = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] "
    app.notes = (app.notes + "\n" + prefix + fragment).strip()


def _render_email(
    ctx: TenantContext,
    job: Job,
    app: Application,
    recruiter: Recruiter,
) -> tuple[str, str]:
    """Plain-text outreach template.

    Kept simple on purpose:
    - No HTML \u2014 plain text avoids spam filters keyed on bare HTML emails
    - No tracking pixels, no shortened links \u2014 also spam triggers
    - Includes the Groq score reason so the email is personalised to the role
    """
    candidate = ctx.credentials.candidate_name or ctx.user.first_name or "Applicant"
    salutation = f"Hi {recruiter.name}," if recruiter.name else "Hi,"
    reason_block = (
        f"Based on the role description, my background looks aligned:\n"
        f"{app.score_reason}\n"
    ) if app.score_reason else ""

    subject = f"{job.title} role at {job.company} \u2014 {candidate}"
    body = (
        f"{salutation}\n"
        f"\n"
        f"I came across the {job.title} opening at {job.company} and wanted to introduce myself. "
        f"I'm {candidate}.\n"
        f"\n"
        f"{reason_block}"
        f"My resume is attached. Happy to chat at a time that works for you.\n"
        f"\n"
        f"Best regards,\n"
        f"{candidate}\n"
    )
    return subject, body
