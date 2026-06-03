"""Composite recruiter lookup: Hunter \u2192 Apollo fallback.

Order of operations per company:
1. Hunter (per-user key for free, pooled for paid).
2. If Hunter returns None *or* raises HunterQuotaExhausted, try Apollo
   (shared operator key, optional).
3. If both providers are exhausted (or the only available one is), raise
   :class:`HunterQuotaExhausted` so the caller stops the loop and pitches
   /upgrade (we reuse Hunter's exception so existing pipeline code paths
   don't need to learn a new type).

Quota state is per-run, kept on the instance. Once a provider raises 429
we stop calling it until the next pipeline invocation.
"""
from __future__ import annotations

import logging
from typing import Any

from core.enrich.apollo import ApolloClient, ApolloQuotaExhausted, build_apollo_client
from core.enrich.hunter import (
    HunterClient, HunterEmailUndeliverable, HunterQuotaExhausted, Recruiter,
    build_hunter_client,
)
from core.tenant import TenantContext

log = logging.getLogger(__name__)


class RecruiterFinder:
    def __init__(self, hunter: HunterClient | None, apollo: ApolloClient | None):
        self.hunter = hunter
        self.apollo = apollo
        self._hunter_exhausted = False
        self._apollo_exhausted = False

    async def __aenter__(self) -> "RecruiterFinder":
        if self.hunter is not None:
            await self.hunter.__aenter__()
        if self.apollo is not None:
            await self.apollo.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self.hunter is not None:
            await self.hunter.__aexit__(*exc)
        if self.apollo is not None:
            await self.apollo.__aexit__(*exc)

    async def find_recruiter(self, company: str) -> Recruiter | None:
        # --- 1. Hunter (primary) ---
        if self.hunter is not None and not self._hunter_exhausted:
            try:
                result = await self.hunter.find_recruiter(company)
                if result is not None:
                    return result
            except HunterEmailUndeliverable as e:
                # Verifier flagged this address as undeliverable/risky.
                # Don't burn the Apollo quota chasing the same company —
                # Apollo would likely surface the same domain's catch-all.
                # Return None so the pipeline records no_recruiter and skips.
                log.info("recruiter-finder: hunter verify blocked %s for company=%r; skipping send", e, company)
                return None
            except HunterQuotaExhausted:
                self._hunter_exhausted = True
                log.warning("recruiter-finder: Hunter exhausted; using Apollo for remainder of run")

        # --- 2. Apollo (fallback) ---
        if self.apollo is not None and not self._apollo_exhausted:
            try:
                result = await self.apollo.find_recruiter(company)
                if result is not None:
                    return result
            except ApolloQuotaExhausted:
                self._apollo_exhausted = True
                log.warning("recruiter-finder: Apollo exhausted")

        # --- 3. Out of options? ---
        # Raise only when EVERY available provider is exhausted. A plain
        # "no recruiter found but quota remains" stays a None return (free).
        hunter_dead = self.hunter is None or self._hunter_exhausted
        apollo_dead = self.apollo is None or self._apollo_exhausted
        if hunter_dead and apollo_dead and (self._hunter_exhausted or self._apollo_exhausted):
            raise HunterQuotaExhausted("all recruiter providers exhausted")
        return None


def build_recruiter_finder(ctx: TenantContext) -> RecruiterFinder | None:
    """Build the composite finder for ``ctx``. Returns None only when
    Hunter pool is unconfigured AND the user has no Apollo key \u2014 i.e.
    there's no way to find any recruiter at all, so outreach should be
    skipped entirely (caller logs and bails)."""
    hunter = build_hunter_client(ctx)
    apollo = build_apollo_client(ctx)
    if hunter is None and apollo is None:
        return None
    return RecruiterFinder(hunter=hunter, apollo=apollo)
