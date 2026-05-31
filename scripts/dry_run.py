"""End-to-end smoke test for the Week 2 pipeline.

Run with:
    python -m scripts.dry_run

What it does:
    1. Verifies required env vars are present.
    2. Seeds (or updates) one test user with low daily_scan_limit=2 so the
       rate limiter is guaranteed to trigger and you can see the code path
       working.
    3. Loads TenantContext for that user.
    4. Runs run_pipeline_for_user() \u2014 hits Adzuna India, upserts jobs,
       scores them via Groq, writes Application rows.
    5. Prints a detailed summary: scrape count, score count, top scored
       jobs, rate-limit state, DailyRunSummary contents.

Re-runnable: uses ON CONFLICT for the user rows, and the pipeline's own
dedup means scraping a second time just reports "0 new jobs". The rate
limiter resets at UTC midnight.

Flags:
    --user-id N         Telegram user ID to seed (default: 839198672)
    --tier {free,paid}  Subscription tier (default: free)
    --reset             SAFE: clear today's RateLimitCounter + DailyRunSummary
                        for this user so the pipeline can re-run cleanly.
                        Does NOT touch jobs, applications, prefs, or creds.
    --wipe-jobs         DESTRUCTIVE: also delete all Job / Application /
                        OutreachLog rows for this user. Use only on test
                        users you own.
    --seed-prefs        Overwrite UserPreferences with --role / --location.
                        REFUSED for existing active users unless --force is
                        also passed (existing prefs are printed first so you
                        can see what would be destroyed). For brand-new
                        users, initial prefs are always created from
                        --role / --location regardless of this flag.
    --seed-creds        Overwrite UserCredentials from TEST_SMTP_* /
                        TEST_APOLLO_API_KEY env vars. Same active-user
                        guard as --seed-prefs.
    --force             Override the active-user guard for --seed-prefs /
                        --seed-creds. Required when targeting a real user.
    --role STR          Role keyword used only when seeding prefs
                        (default: 'python developer')
    --location STR      Location used only when seeding prefs
                        (default: 'Bengaluru')
    --no-send           Exercise recruiter lookup + email rendering but SKIP
                        the actual SMTP call. OutreachLog rows are written
                        with error='[NO-SEND] dry-run' and applications
                        are NOT marked as sent. Safe for repeated runs
                        against real recruiter emails. RECOMMENDED for
                        first run.

Optional env vars for end-to-end outreach testing:
    TEST_SMTP_EMAIL          Your Gmail address (e.g. you@gmail.com). Used as
                             both the SMTP login and From: header. Recruiter
                             replies land directly in this inbox.
    TEST_SMTP_PASSWORD       Gmail app password (16 chars; spaces OK —
                             stripped before use). Get one at
                             https://myaccount.google.com/apppasswords.
    TEST_APOLLO_API_KEY      Apollo key for the test user (user-supplied path).
                             The pooled HUNTER_API_KEY from .env is used
                             regardless of tier as the operator fallback.

If TEST_SMTP_EMAIL + TEST_SMTP_PASSWORD are both set AND --no-send is NOT
passed, outreach will attempt REAL email delivery. With --tier free + 5
outreach limit, that's up to 5 actual emails sent to real recruiters from
your Gmail. Use a throwaway test address if you're not ready for that.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

# Show INFO-level logs from our packages so the dry-run output explains
# what the outreach phase is doing (recruiter hits/misses, SMTP failures, etc.).
# Keep third-party libs at WARNING to avoid asyncpg/httpx noise.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
for noisy in ("httpx", "httpcore", "asyncio", "sqlalchemy.engine", "aiosmtplib"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# Validate env BEFORE importing modules that read it at import time.
_REQUIRED = ["DATABASE_URL", "ENCRYPTION_KEY", "GROQ_API_KEY", "ADZUNA_APP_ID", "ADZUNA_APP_KEY"]
_missing = [k for k in _REQUIRED if not os.environ.get(k)]
if _missing:
    print(f"ERROR: missing env vars: {', '.join(_missing)}")
    print("Add them to .env and rerun.")
    sys.exit(1)

from sqlalchemy import delete, select  # noqa: E402

from core.crypto import encrypt  # noqa: E402
from core.db import get_session  # noqa: E402
from core.models import (  # noqa: E402
    Application, DailyRunSummary, Job, OutreachLog, RateLimitCounter, SubscriptionTier,
    User, UserCredentials, UserPreferences, UserStatus,
)
from core.pipeline import run_pipeline_for_user  # noqa: E402
from core.repositories import rate_limits as rl  # noqa: E402
from core.tenant import load_tenant_context  # noqa: E402


# ---------- pretty printing ----------
def banner(text: str) -> None:
    print()
    print("=" * 72)
    print(f"  {text}")
    print("=" * 72)


async def seed_user(
    user_id: int,
    tier: str,
    role: str,
    location: str,
    reset: bool,
    wipe_jobs: bool,
    seed_prefs: bool,
    seed_creds: bool,
    force: bool,
) -> None:
    """Idempotent user seed.

    Safety rules:
    - `reset` only clears today's RateLimitCounter + DailyRunSummary.
    - `wipe_jobs` is required to also delete Job/Application/OutreachLog rows.
    - For an EXISTING active user, `seed_prefs` and `seed_creds` are refused
      unless `force` is also passed. Brand-new users always get initial
      prefs/creds created (nothing to destroy).
    """
    async with get_session() as session:
        existing_user = await session.get(User, user_id)
        existing_prefs = await session.get(UserPreferences, user_id)
        existing_creds = await session.get(UserCredentials, user_id)

        is_active_real_user = (
            existing_user is not None and existing_user.status == UserStatus.active
        )

        # ----- safety guards: refuse to clobber a real user's data -----
        if is_active_real_user and seed_prefs and not force:
            print(f"REFUSED: user {user_id} is active and already has preferences:")
            if existing_prefs is not None:
                print(f"  role_keywords = {existing_prefs.role_keywords}")
                print(f"  locations     = {existing_prefs.locations}")
                print(f"  skills        = {existing_prefs.skills}")
                print(f"  min_score     = {existing_prefs.min_score}")
            else:
                print("  (no UserPreferences row exists)")
            print("Pass --force to overwrite with --role/--location values.")
            sys.exit(3)

        if is_active_real_user and seed_creds and not force:
            print(f"REFUSED: user {user_id} is active and already has credentials:")
            if existing_creds is not None:
                print(f"  smtp_email      = {existing_creds.smtp_email}")
                print(f"  candidate_name  = {existing_creds.candidate_name}")
                print(f"  has_smtp_pw     = {existing_creds.smtp_password_encrypted is not None}")
                print(f"  has_apollo_key  = {existing_creds.apollo_api_key_encrypted is not None}")
            else:
                print("  (no UserCredentials row exists)")
            print("Pass --force to overwrite from TEST_SMTP_* env vars.")
            sys.exit(3)

        if is_active_real_user and wipe_jobs and not force:
            print(f"REFUSED: user {user_id} is active. --wipe-jobs would delete their "
                  f"Job/Application/OutreachLog history. Pass --force to proceed.")
            sys.exit(3)

        # ----- safe reset: today's counters + daily summary only -----
        if reset:
            today = datetime.now(timezone.utc).date()
            print(f"--reset: clearing today's RateLimitCounter + DailyRunSummary for user {user_id}")
            await session.execute(
                delete(RateLimitCounter)
                .where(RateLimitCounter.user_id == user_id)
                .where(RateLimitCounter.period_date == today)
            )
            await session.execute(
                delete(DailyRunSummary)
                .where(DailyRunSummary.user_id == user_id)
                .where(DailyRunSummary.run_date == today)
            )

        # ----- destructive wipe: opt-in only -----
        if wipe_jobs:
            print(f"--wipe-jobs: deleting all Job/Application/OutreachLog rows for user {user_id}")
            await session.execute(delete(OutreachLog).where(OutreachLog.user_id == user_id))
            await session.execute(delete(Application).where(Application.user_id == user_id))
            await session.execute(delete(Job).where(Job.user_id == user_id))

        user = existing_user
        tier_enum = SubscriptionTier.paid if tier == "paid" else SubscriptionTier.free
        # Force LOW scan limit on free tier so the rate limiter triggers
        # within seconds during the smoke test. Paid keeps defaults.
        scan_limit = 50 if tier == "paid" else 2
        outreach_limit = 15 if tier == "paid" else 5

        if user is None:
            # Brand-new user: safe to create everything from defaults.
            user = User(
                id=user_id,
                telegram_chat_id=user_id,
                username="smoke_test",
                first_name="Smoke",
                subscription_tier=tier_enum,
                status=UserStatus.active,
                daily_scan_limit=scan_limit,
                daily_outreach_limit=outreach_limit,
            )
            session.add(user)
            user_was_created = True
        else:
            # Existing user: only touch tier + limits (cheap to reset for smoke tests).
            user.subscription_tier = tier_enum
            user.status = UserStatus.active
            user.daily_scan_limit = scan_limit
            user.daily_outreach_limit = outreach_limit
            user_was_created = False

        prefs = existing_prefs
        if prefs is None:
            # No prefs row exists — safe to create initial values.
            prefs = UserPreferences(
                user_id=user_id,
                role_keywords=[role],
                locations=[location],
                skills=["python", "fastapi", "postgres"],
                min_score=70,
            )
            session.add(prefs)
            print(f"created initial UserPreferences: role={role!r} location={location!r}")
        elif seed_prefs:
            # Guarded above; only reached for new users or when --force was passed.
            prefs.role_keywords = [role]
            prefs.locations = [location]
            prefs.min_score = 70
            print(f"--seed-prefs: overwrote UserPreferences: role={role!r} location={location!r}")
        else:
            print(f"keeping existing UserPreferences: roles={prefs.role_keywords} "
                  f"locations={prefs.locations}")

        creds = existing_creds
        # Optional real credentials for end-to-end outreach testing. Read
        # from env so we never commit them to dry_run.py or git history.
        # Outbound goes via Gmail SMTP using the user's own app password.
        test_smtp_email = os.environ.get("TEST_SMTP_EMAIL")
        # Gmail app password (16 chars). Spaces in env value are tolerated.
        test_smtp_pw_raw = os.environ.get("TEST_SMTP_PASSWORD", "")
        test_smtp_password = test_smtp_pw_raw.replace(" ", "") or None
        smtp_pw_enc = encrypt(test_smtp_password) if test_smtp_password else None
        # Apollo (user-supplied recruiter lookup; Hunter pool is operator-side).
        test_apollo_key = os.environ.get("TEST_APOLLO_API_KEY")
        apollo_key_enc = encrypt(test_apollo_key) if test_apollo_key else None

        if creds is None:
            # No creds row exists — safe to create initial values from env.
            creds = UserCredentials(
                user_id=user_id,
                smtp_email=test_smtp_email or "smoke-test@example.com",
                smtp_password_encrypted=smtp_pw_enc,
                apollo_api_key_encrypted=apollo_key_enc,
                candidate_name="Smoke Test",
            )
            session.add(creds)
            print("created initial UserCredentials")
        elif seed_creds:
            # Guarded above; only reached for new users or when --force was passed.
            if test_smtp_email:
                creds.smtp_email = test_smtp_email
            if test_smtp_password:
                creds.smtp_password_encrypted = smtp_pw_enc
            if test_apollo_key:
                creds.apollo_api_key_encrypted = apollo_key_enc
            print(f"--seed-creds: refreshed UserCredentials from env "
                  f"(smtp_email={creds.smtp_email})")
        else:
            print(f"keeping existing UserCredentials (smtp_email={creds.smtp_email})")

        # commit happens automatically on __aexit__
    action = "created" if user_was_created else "updated"
    print(f"{action} user_id={user_id} tier={tier} scan_limit={scan_limit} outreach_limit={outreach_limit}")
    has_pool_hunter = bool(os.environ.get("HUNTER_API_KEY"))
    if test_smtp_email and test_smtp_password:
        print(f"  Outreach: REAL via Gmail SMTP, from={test_smtp_email}")
    elif test_smtp_email and not test_smtp_password:
        print(f"  Outreach: SKIPPED \u2014 TEST_SMTP_EMAIL set but TEST_SMTP_PASSWORD missing")
    else:
        print("  Outreach: SKIPPED \u2014 set TEST_SMTP_EMAIL + TEST_SMTP_PASSWORD to enable")
    if test_apollo_key:
        print("  Recruiter lookup: user-Apollo + "
              + ("operator-Hunter pool" if has_pool_hunter else "NO Hunter pool"))
    elif has_pool_hunter:
        print("  Recruiter lookup: operator-Hunter pool ONLY (set TEST_APOLLO_API_KEY to add user-Apollo)")
    else:
        print("  Recruiter lookup: NONE \u2014 outreach phase will be skipped "
              "(set TEST_APOLLO_API_KEY and/or HUNTER_API_KEY)")


async def run_pipeline(user_id: int, *, no_send: bool = False) -> None:
    async with get_session() as session:
        ctx = await load_tenant_context(session, user_id)
        if ctx is None:
            print(f"ERROR: TenantContext not loadable for user {user_id} — seeding failed?")
            sys.exit(2)

        banner("RUNNING PIPELINE")
        print(f"  user: {ctx.user_id} ({ctx.user.first_name})")
        print(f"  tier: {ctx.user.subscription_tier.value}  is_paid={ctx.is_paid}")
        print(f"  limits: scan={ctx.user.daily_scan_limit}/day  outreach={ctx.user.daily_outreach_limit}/day")
        print(f"  prefs: roles={ctx.preferences.role_keywords}  locations={ctx.preferences.locations}")
        print(f"  min_score: {ctx.preferences.min_score}")
        print(f"  no_send: {no_send}")

        summary = await run_pipeline_for_user(session, ctx, no_send=no_send)
        # session commits on exit


async def report(user_id: int) -> None:
    async with get_session() as session:
        banner("RESULTS")

        # Daily summary
        today = datetime.now(timezone.utc).date()
        summary = await session.get(DailyRunSummary, (user_id, today))
        if summary:
            print(f"DailyRunSummary({today}):")
            print(f"  jobs_scraped     = {summary.jobs_scraped}")
            print(f"  jobs_scored      = {summary.jobs_scored}")
            print(f"  outreach_sent    = {summary.outreach_sent}")
            print(f"  outreach_failed  = {summary.outreach_failed}")
            print(f"  error            = {summary.error or '(none)'}")
        else:
            print("No DailyRunSummary row \u2014 pipeline did not complete cleanly.")

        # Rate-limit state
        scan_count = await rl.current_count(session, user_id, "scan")
        outreach_count = await rl.current_count(session, user_id, "outreach")
        print()
        print(f"RateLimitCounter (today, UTC):")
        print(f"  scan      = {scan_count}")
        print(f"  outreach  = {outreach_count}")

        # Top scored applications
        stmt = (
            select(Application, Job)
            .join(Job, Job.id == Application.job_id)
            .where(Application.user_id == user_id)
            .order_by(Application.score.desc())
            .limit(5)
        )
        rows = (await session.execute(stmt)).all()
        print()
        print(f"Top {len(rows)} scored jobs:")
        if not rows:
            print("  (none \u2014 scrape may have returned 0 jobs)")
        for app, job in rows:
            mark = "MATCH" if app.score >= 70 else "skip "
            print(f"  [{mark}] score={app.score:5.1f}  {job.company[:25]:25}  {job.title[:50]}")
            print(f"          {app.score_reason}")

        # Sanity: how many total jobs were scraped overall?
        from sqlalchemy import func
        total_jobs = await session.scalar(
            select(func.count(Job.id)).where(Job.user_id == user_id)
        )
        total_apps = await session.scalar(
            select(func.count(Application.id)).where(Application.user_id == user_id)
        )
        print()
        print(f"Lifetime totals for user {user_id}: jobs={total_jobs}  applications={total_apps}")

        # Recent outreach attempts \u2014 most useful for debugging "why did N fail?"
        log_stmt = (
            select(OutreachLog, Application, Job)
            .join(Application, Application.id == OutreachLog.application_id)
            .join(Job, Job.id == Application.job_id)
            .where(OutreachLog.user_id == user_id)
            .order_by(OutreachLog.sent_at.desc())
            .limit(10)
        )
        log_rows = (await session.execute(log_stmt)).all()
        print()
        print(f"Recent outreach attempts ({len(log_rows)}):")
        if not log_rows:
            print("  (none \u2014 either no candidates found or no recruiter providers configured)")
        for ol, app, job in log_rows:
            status = "OK   " if ol.error is None else "FAIL "
            err = f"  err={ol.error}" if ol.error else ""
            print(f"  [{status}] {ol.sent_at.strftime('%H:%M:%S')}  app={ol.application_id}  "
                  f"{job.company[:20]:20} -> {ol.to_email}{err}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--user-id", type=int, default=839198672)
    p.add_argument("--tier", choices=["free", "paid"], default="free")
    p.add_argument(
        "--reset", action="store_true",
        help="SAFE: clear today's RateLimitCounter + DailyRunSummary for this user.",
    )
    p.add_argument(
        "--wipe-jobs", action="store_true",
        help="DESTRUCTIVE: also delete all Job/Application/OutreachLog rows. "
             "Refused for active users unless --force is also passed.",
    )
    p.add_argument(
        "--seed-prefs", action="store_true",
        help="Overwrite UserPreferences with --role/--location. "
             "Refused for existing active users unless --force is also passed.",
    )
    p.add_argument(
        "--seed-creds", action="store_true",
        help="Overwrite UserCredentials from TEST_SMTP_* env. "
             "Refused for existing active users unless --force is also passed.",
    )
    p.add_argument(
        "--force", action="store_true",
        help="Override the active-user safety guard for --seed-prefs / "
             "--seed-creds / --wipe-jobs.",
    )
    p.add_argument("--role", default="python developer",
                   help="Role keyword used only when seeding prefs.")
    p.add_argument("--location", default="Bengaluru",
                   help="Location used only when seeding prefs.")
    p.add_argument(
        "--no-send", action="store_true",
        help="Exercise recruiter lookup + rendering, skip actual SMTP send.",
    )
    return p.parse_args()


async def main() -> None:
    args = parse_args()
    banner("SEEDING TEST USER")
    await seed_user(
        args.user_id,
        args.tier,
        args.role,
        args.location,
        reset=args.reset,
        wipe_jobs=args.wipe_jobs,
        seed_prefs=args.seed_prefs,
        seed_creds=args.seed_creds,
        force=args.force,
    )
    if args.no_send:
        banner("NO-SEND MODE: SMTP calls will be SKIPPED")
        print("  Recruiter lookups will run for real (uses Hunter pool / Apollo quota).")
        print("  Email rendering will run for real.")
        print("  No emails will actually be delivered.")
        print("  Applications stay status=scored so a later real run picks them up.")
    else:
        banner("LIVE MODE: REAL EMAILS WILL BE SENT IF SMTP CREDS PRESENT")
    await run_pipeline(args.user_id, no_send=args.no_send)
    await report(args.user_id)
    print()
    if args.no_send:
        print("done. (NO-SEND mode — no emails were delivered.)")
    else:
        print("done.")


if __name__ == "__main__":
    asyncio.run(main())
