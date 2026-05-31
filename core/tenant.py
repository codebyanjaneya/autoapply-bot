"""Tenant context \u2014 the bundle of per-user state every worker function needs.

Pattern: load once at the entry point (Telegram handler / scheduled job),
pass explicitly through the call stack. NEVER read user state via a global
or via `current_user()` magic \u2014 explicit beats implicit, and it makes
cross-tenant data leaks impossible to write accidentally.

Rule: a function that takes `TenantContext` MUST use `ctx.user_id` in every
database query's WHERE clause. A function that does not take `TenantContext`
MUST NOT touch any per-user table.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from core.models import User, UserCredentials, UserPreferences


@dataclass(frozen=True, slots=True)
class TenantContext:
    user_id: int                  # Telegram user ID == users.id
    user: User
    preferences: UserPreferences
    credentials: UserCredentials

    @property
    def is_paid(self) -> bool:
        return self.user.subscription_tier.value == "paid"


async def load_tenant_context(session: AsyncSession, user_id: int) -> TenantContext | None:
    """Load the full per-user state. Returns None if the user doesn't exist
    or hasn't completed onboarding (missing prefs / credentials).
    """
    user = await session.get(User, user_id)
    if user is None:
        return None
    # Eager-loaded by ORM relationships when the User row was fetched in a session
    # that has them configured, but safest to await explicitly for clarity.
    prefs = await session.get(UserPreferences, user_id)
    creds = await session.get(UserCredentials, user_id)
    if prefs is None or creds is None:
        return None
    return TenantContext(
        user_id=user_id,
        user=user,
        preferences=prefs,
        credentials=creds,
    )
