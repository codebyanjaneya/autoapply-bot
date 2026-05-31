"""Apollo.io recruiter email lookup \u2014 fallback when Hunter returns nothing.

Apollo's free tier gives ~50 email credits/month, which combined with
Hunter's 25 = ~75 lookups/month per free user. We only call Apollo when
Hunter draws a blank, so most lookups still come out of the Hunter bucket
(by design \u2014 Hunter's email-finder is more accurate per our manual spot-checks).

Endpoint: POST https://api.apollo.io/api/v1/mixed_people/search
Docs:     https://docs.apollo.io/reference/people-search

Free-tier email visibility:
- Apollo masks emails as ``email_not_unlocked@domain.com`` until the user
  spends a credit to "unlock" each one. We treat masked emails as None
  (no point sending to a placeholder address) and rely on the search
  results' ``email_status`` field to skip ``unavailable`` / ``bounced``
  entries.
- Unlock-on-search is automatic on the paid plan; on free it's
  hit-or-miss. So Apollo's effective contribution to free-tier coverage
  is "some of those 50 credits", not all.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from core.crypto import decrypt
from core.enrich.domain_overrides import resolve_domain
from core.enrich.hunter import Recruiter  # reuse the dataclass for consistency
from core.tenant import TenantContext

log = logging.getLogger(__name__)

_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/search"

_TITLES = [
    "Recruiter", "Senior Recruiter", "Technical Recruiter",
    "Talent Acquisition", "Talent Acquisition Specialist",
    "HR", "HR Manager", "HR Business Partner", "Human Resources",
    "Hiring Manager",
]


class ApolloQuotaExhausted(Exception):
    """Raised on HTTP 429 from Apollo \u2014 monthly credit limit hit."""


class ApolloClient:
    """Mirrors :class:`HunterClient`'s shape so RecruiterFinder can treat
    them interchangeably."""

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None):
        self.api_key = api_key
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "ApolloClient":
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=5.0),
                follow_redirects=True,
            )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def find_recruiter(self, company: str) -> Recruiter | None:
        """Return the first HR-ish person with a real email, or None.

        Raises :class:`ApolloQuotaExhausted` on HTTP 429. All other 4xx/5xx
        return None (logged) so a single bad request doesn't kill the run.
        """
        if self._client is None:
            async with self:
                return await self.find_recruiter(company)

        # Prefer the curated domain when we have one \u2014 Apollo's
        # name-to-domain resolver is also imperfect for Indian companies.
        body: dict[str, Any] = {
            "person_titles": _TITLES,
            "page": 1,
            "per_page": 5,
        }
        override_domain = resolve_domain(company)
        if override_domain:
            body["q_organization_domains"] = override_domain
            log.info("apollo: company=%r using domain override=%r", company, override_domain)
        else:
            body["q_organization_name"] = company

        headers = {
            "Cache-Control": "no-cache",
            "Content-Type": "application/json",
            "X-Api-Key": self.api_key,
        }
        try:
            resp = await self._client.post(_SEARCH_URL, json=body, headers=headers)
        except httpx.HTTPError as e:
            log.warning("apollo: network error for company=%r: %s", company, e)
            return None

        if resp.status_code == 401:
            log.error("apollo: 401 unauthorized \u2014 key invalid or revoked")
            return None
        if resp.status_code == 403:
            # Apollo's free plan returns 403 API_INACCESSIBLE on
            # mixed_people/search \u2014 the People Search endpoint requires a
            # paid plan (~$49/mo). This is expected for every free-tier
            # user, so log at DEBUG and swallow silently rather than
            # spamming WARNING for every company we look up.
            log.debug(
                "apollo: 403 (likely free-plan API_INACCESSIBLE) for company=%r: %s",
                company, resp.text[:200],
            )
            return None
        if resp.status_code == 429:
            log.warning("apollo: 429 quota/rate-limit for company=%r: %s",
                        company, resp.text[:200])
            raise ApolloQuotaExhausted(resp.text[:200] or "Apollo returned 429")
        if resp.status_code >= 400:
            log.warning("apollo: %s for company=%r: %s",
                        resp.status_code, company, resp.text[:200])
            return None

        data = resp.json() or {}
        people = data.get("people") or []
        if not people:
            log.info("apollo: company=%r no people returned", company)
            return None

        for person in people:
            email = person.get("email")
            status = (person.get("email_status") or "").lower()
            # Skip masked / unverified placeholders.
            if not email or "email_not_unlocked" in email:
                continue
            if status in {"unavailable", "bounced"}:
                continue
            full_name = " ".join(filter(None, [person.get("first_name"), person.get("last_name")])) or None
            log.info("apollo: company=%r picked %s (%s, status=%s)",
                     company, email, person.get("title") or "no-title", status or "unknown")
            return Recruiter(
                email=email,
                source="apollo",
                name=full_name,
                position=person.get("title"),
            )

        log.info("apollo: company=%r had %d people but no usable emails (all masked / unverified)",
                 company, len(people))
        return None


def build_apollo_client(ctx: TenantContext) -> ApolloClient | None:
    """Construct an Apollo client from the user's stored key, or None
    when the user hasn't supplied one yet (free tier without onboarding
    step 6 completed, or they used /skip).

    Apollo is the USER-SUPPLIED recruiter-lookup provider (Apollo accepts
    Gmail signups; Hunter requires a work email and is therefore
    operator-only \u2014 see :func:`core.enrich.hunter.build_hunter_client`).

    Each user brings their own ~50 free credits/month, so per-user
    quotas don't block other users when one runs out.
    """
    enc = ctx.credentials.apollo_api_key_encrypted
    if enc is None:
        log.info("user %s has no Apollo key stored; Apollo lookups disabled for this run", ctx.user_id)
        return None
    try:
        key = decrypt(enc)
    except Exception as e:
        log.error("user %s Apollo key decrypt failed: %s", ctx.user_id, e)
        return None
    return ApolloClient(key)
