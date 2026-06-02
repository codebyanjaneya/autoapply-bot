"""Mailer package.

Two senders share the same async-context-manager + ``send()`` signature:

* :class:`SMTPSender` \u2014 per-user Gmail SMTP. The original v1 path. Requires
  the deploy host to have outbound :587 reachability.
* :class:`ResendSender` \u2014 Resend HTTPS API on our verified domain. From: is
  ``outreach@<RESEND_FROM_DOMAIN>``, Reply-To is the user's own email, so
  recruiter replies still land in the user's inbox.

Selection rule (:func:`is_managed_sending` / :func:`build_outreach_sender`):
if ``RESEND_API_KEY`` is set in env, we use Resend for ALL outreach and
onboarding no longer asks for a Gmail app password. Otherwise we fall back
to per-user SMTP (local dev / self-hosted deployments with open :587).
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Union

from core.mailer.resend_sender import ResendError, ResendSender
from core.mailer.smtp_sender import SMTPSender

if TYPE_CHECKING:
    from core.tenant import TenantContext

log = logging.getLogger(__name__)

# Either sender type satisfies the duck-typed contract used by
# core/outreach.py: ``async with sender:`` + ``await sender.send(...)``.
AnySender = Union[SMTPSender, ResendSender]


def is_managed_sending() -> bool:
    """True iff a Resend API key is configured. When True, onboarding skips
    the Gmail app-password step and the pipeline uses Resend for outreach.
    """
    return bool(os.environ.get("RESEND_API_KEY"))


def build_outreach_sender(ctx: "TenantContext") -> "AnySender | None":
    """Pick the right sender for this run.

    * Managed mode (RESEND_API_KEY set): one :class:`ResendSender` built
      from env. ``ctx`` is unused in this branch.
    * Self-hosted mode: per-user :class:`SMTPSender` from the user's
      encrypted Gmail credentials.

    Returns ``None`` when the chosen path can't be built (Resend env
    incomplete, or user has no SMTP creds). Caller logs + skips outreach.
    """
    if is_managed_sending():
        sender = ResendSender.from_env()
        if sender is None:
            log.error(
                "managed sending enabled but ResendSender.from_env() returned None; "
                "check RESEND_FROM_DOMAIN"
            )
        return sender
    return SMTPSender.from_credentials(ctx.credentials)


__all__ = [
    "SMTPSender", "ResendSender", "ResendError",
    "AnySender", "is_managed_sending", "build_outreach_sender",
]
