"""Hunter.io recruiter email lookup.

Tier-aware key selection:
  - Paid users  -> shared pooled key from env HUNTER_API_KEY
  - Free users  -> their own key, stored encrypted on UserCredentials

The pool key is the operator's monthly Hunter subscription; free-tier users
bring their own (free Hunter accounts give 25 lookups/month).

Endpoint: GET https://api.hunter.io/v2/domain-search
Docs:     https://hunter.io/api-documentation/v2#domain-search

We pass `company=<name>` and let Hunter resolve the domain \u2014 cheaper and
more reliable than guessing the domain ourselves. `department=hr` biases
toward HR/recruiting emails over engineering or sales contacts.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

from core.crypto import decrypt
from core.enrich.domain_overrides import resolve_domain
from core.tenant import TenantContext

log = logging.getLogger(__name__)

_BASE_URL = "https://api.hunter.io/v2/domain-search"
_VERIFY_URL = "https://api.hunter.io/v2/email-verifier"

# Hunter email-verifier statuses we treat as a hard "do not send":
# - undeliverable: mailbox doesn't exist / domain rejects
# - risky:        catch-all, disposable, role-based; high bounce odds
# Everything else (deliverable, unknown, or any unrecognised value) is
# treated as send-OK so a flaky verifier never blocks outreach.
_BLOCKING_VERIFY_STATUSES = frozenset({"undeliverable", "risky"})


def _verify_emails_enabled() -> bool:
    """Whether to hit the /email-verifier endpoint after each domain-search.

    OFF by default — verification doubles the per-lookup API cost (1 search
    + 1 verify) which halves the free plan's 25/month quota. Flip
    ``HUNTER_VERIFY_EMAILS=true`` once we move to a paid Hunter plan with
    headroom.
    """
    return os.environ.get("HUNTER_VERIFY_EMAILS", "").strip().lower() in ("1", "true", "yes", "on")


class HunterQuotaExhausted(Exception):
    """Raised when Hunter returns HTTP 429 — free plan's 25/month limit hit
    (or pooled key's monthly quota). Caller should stop the outreach loop
    and notify the user (free tier: pitch /upgrade; pool: alert operator)."""


class HunterEmailUndeliverable(Exception):
    """Raised when Hunter's email-verifier marks the candidate address as
    ``undeliverable`` or ``risky``. The recruiter_finder catches this and
    falls through to the next provider (Apollo) — and if nothing else
    surfaces a valid email, the pipeline treats the job as no_recruiter
    and skips sending. Net effect: protects sender reputation from known
    bounces and spam-trap-prone catch-alls."""


@dataclass(slots=True)
class Recruiter:
    email: str
    source: str        # 'hunter-user' | 'hunter-pool' | 'apollo' for analytics
    name: str | None = None
    position: str | None = None


class HunterClient:
    def __init__(self, api_key: str, source_label: str = "hunter", client: httpx.AsyncClient | None = None):
        self.api_key = api_key
        self.source_label = source_label
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "HunterClient":
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=5.0),
                follow_redirects=True,
            )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def find_recruiter(self, company: str) -> Recruiter | None:
        """Return the first HR-ish email for `company`, or None.

        Returns None (never raises) for:
        - No domain resolved for the company name
        - No matching emails returned
        - HTTP 4xx (e.g. 429 rate limit, 401 invalid key)

        Logs all failures so the operator can spot a bad pooled key quickly.
        """
        if self._client is None:
            async with self:
                return await self.find_recruiter(company)

        # If we have a hand-curated domain for this company, use it directly
        # — bypasses Hunter's name-to-domain resolver, which guesses wrong
        # for many Indian companies (e.g. Uplers -> uplers.nl).
        override_domain = resolve_domain(company)
        if override_domain:
            log.info("hunter: company=%r using domain override=%r", company, override_domain)
            params: dict[str, str | int] = {
                "domain": override_domain,
                "api_key": self.api_key,
                "department": "hr",
                "limit": 5,
            }
        else:
            params = {
                "company": company,
                "api_key": self.api_key,
                "department": "hr",
                "limit": 5,
            }
        try:
            resp = await self._client.get(_BASE_URL, params=params)
        except httpx.HTTPError as e:
            log.warning("hunter: network error for company=%r: %s", company, e)
            return None

        if resp.status_code == 401:
            log.error("hunter: 401 unauthorized \u2014 key invalid or revoked")
            return None
        if resp.status_code == 429:
            # Could be per-second rate-limit OR monthly-quota exhaustion.
            # Both manifest as 429; Hunter's body distinguishes via the
            # "details" string. We treat both as quota-exhausted so the
            # caller stops the loop — burning more 429s won't help today.
            log.warning("hunter: 429 quota/rate-limit for company=%r: %s",
                        company, resp.text[:200])
            raise HunterQuotaExhausted(resp.text[:200] or "Hunter returned 429")
        if resp.status_code >= 400:
            log.warning("hunter: %s for company=%r: %s", resp.status_code, company, resp.text[:200])
            return None

        data = (resp.json() or {}).get("data") or {}
        emails = data.get("emails") or []
        resolved_domain = data.get("domain")
        if not emails:
            # 200 OK but empty: either Hunter couldn't resolve the company name
            # to a domain, or the domain has no HR-department emails in their
            # database. Distinguish the two for the operator.
            if resolved_domain:
                log.info("hunter: company=%r resolved to domain=%r but no HR emails found",
                         company, resolved_domain)
            else:
                log.info("hunter: company=%r could not be resolved to any domain",
                         company)
            return None

        first = emails[0]
        full_name = " ".join(filter(None, [first.get("first_name"), first.get("last_name")])) or None
        log.info("hunter: company=%r domain=%r returned %d HR email(s); using %s (%s)",
                 company, resolved_domain, len(emails), first.get("value"), first.get("position") or "no-title")

        # Verify deliverability before handing the address to the mailer.
        # Opt-in via HUNTER_VERIFY_EMAILS=true — each verify call burns one
        # more unit of Hunter quota, so default-off keeps the monthly cap
        # intact. Fails OPEN: any verifier error lets the send proceed.
        if _verify_emails_enabled():
            await self._verify_or_raise(first["value"])

        return Recruiter(
            email=first["value"],
            source=self.source_label,
            name=full_name,
            position=first.get("position"),
        )

    async def _verify_or_raise(self, email: str) -> None:
        assert self._client is not None
        try:
            resp = await self._client.get(
                _VERIFY_URL,
                params={"email": email, "api_key": self.api_key},
            )
        except httpx.HTTPError as e:
            log.warning("hunter-verify: network error for %s: %s — failing open", email, e)
            return

        if resp.status_code >= 400:
            log.warning("hunter-verify: HTTP %s for %s: %s — failing open",
                        resp.status_code, email, resp.text[:200])
            return

        try:
            data = (resp.json() or {}).get("data") or {}
        except ValueError:
            log.warning("hunter-verify: non-JSON body for %s — failing open", email)
            return

        status = (data.get("status") or "").lower()
        if status in _BLOCKING_VERIFY_STATUSES:
            log.info("hunter-verify: %s -> status=%s, blocking send", email, status)
            raise HunterEmailUndeliverable(f"{email}: {status}")

        log.info("hunter-verify: %s -> status=%s, ok to send", email, status or "unknown")


def build_hunter_client(ctx: TenantContext) -> HunterClient | None:
    """Return a Hunter client for this user, preferring their own key.

    Resolution order:
      1. User's own key from ``user_credentials.hunter_api_key_encrypted``
         (set via /sethunterkey). Free Hunter accounts give 25 lookups/month
         per user, so this scales linearly with the user base.
      2. Shared operator pool from env ``HUNTER_API_KEY``. Used as a
         safety-net for users who haven't supplied their own key yet, and
         was the only path historically.
      3. None — Hunter disabled for this run, the finder falls back to Apollo.

    ``source_label`` on the returned client distinguishes 'hunter-user' vs
    'hunter-pool' in OutreachLog rows so we can see in /stats how much of
    the load each path carries.
    """
    enc = ctx.credentials.hunter_api_key_encrypted
    if enc is not None:
        try:
            user_key = decrypt(enc)
        except Exception as e:
            log.error("user %s Hunter key decrypt failed: %s — falling back to pool", ctx.user_id, e)
        else:
            log.info("hunter: user=%s using personal key", ctx.user_id)
            return HunterClient(user_key, source_label="hunter-user")

    pool_key = os.environ.get("HUNTER_API_KEY")
    if not pool_key:
        log.warning("HUNTER_API_KEY env not set and user %s has no personal key; "
                    "Hunter lookups disabled (will rely on Apollo only)", ctx.user_id)
        return None
    return HunterClient(pool_key, source_label="hunter-pool")
