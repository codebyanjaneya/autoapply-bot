"""Async Resend client \u2014 transactional sender on our own domain.

Architecture (replaces the per-user-Gmail SMTP approach):
    From:     "<Candidate Name> (via AutoApply) <outreach@<our-domain>>"
    Reply-To: <user's personal email>
    To:       <recruiter email>

Why this beats per-user SMTP:
    - Zero user setup \u2014 no Gmail app passwords, no 2FA dance, no OAuth
    - We control SPF/DKIM/DMARC on a real domain \u2014 deliverability is good
      from day one
    - One API key for the whole service, billed centrally

When the recruiter replies, the reply lands in the user's personal inbox
(Reply-To header), so user owns the conversation from that point onward.

Required env:
    RESEND_API_KEY        e.g. re_xxxxxxxxxxxxxxxxxx
    RESEND_FROM_DOMAIN    e.g. autoapply.in  (must be verified in Resend dashboard)

Optional env:
    RESEND_FROM_LOCAL     local-part of the From address. Default: 'outreach'.
                           Override to 'jobs', 'apply', etc.
"""
from __future__ import annotations

import base64
import logging
import os

import httpx

log = logging.getLogger(__name__)

_BASE_URL = "https://api.resend.com/emails"
_TIMEOUT = 30.0


class ResendError(Exception):
    """Raised on any non-2xx from Resend so the outreach orchestrator can
    log it like any other SMTP-layer failure."""


class ResendSender:
    """Stateless sender. Reuses one httpx.AsyncClient across many sends."""

    def __init__(
        self,
        api_key: str,
        from_domain: str,
        from_local: str = "outreach",
        client: httpx.AsyncClient | None = None,
    ):
        self.api_key = api_key
        self.from_domain = from_domain
        self.from_local = from_local
        self.from_address_bare = f"{from_local}@{from_domain}"
        self._client = client
        self._owns_client = client is None

    @classmethod
    def from_env(cls) -> "ResendSender | None":
        """Build from env vars. Returns None (caller skips outreach) when
        ``RESEND_API_KEY`` is missing.

        Env precedence for the From address:
        1. ``RESEND_FROM_EMAIL`` (full address, e.g. ``onboarding@resend.dev``)
           — takes priority. This is the simplest config and matches the
           Resend free-tier shared sender.
        2. ``RESEND_FROM_DOMAIN`` (+ optional ``RESEND_FROM_LOCAL``, default
           ``outreach``) — used when you've verified your own domain.
        """
        api_key = os.environ.get("RESEND_API_KEY")
        if not api_key:
            log.error("ResendSender: RESEND_API_KEY env not set; outreach disabled")
            return None

        from_email = (os.environ.get("RESEND_FROM_EMAIL") or "").strip()
        if from_email and "@" in from_email:
            from_local, from_domain = from_email.split("@", 1)
            return cls(api_key=api_key, from_domain=from_domain, from_local=from_local)

        from_domain = os.environ.get("RESEND_FROM_DOMAIN")
        from_local = os.environ.get("RESEND_FROM_LOCAL", "outreach")
        if not from_domain:
            log.error(
                "ResendSender: set RESEND_FROM_EMAIL (or RESEND_FROM_DOMAIN); outreach disabled"
            )
            return None
        return cls(api_key=api_key, from_domain=from_domain, from_local=from_local)

    async def __aenter__(self) -> "ResendSender":
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(_TIMEOUT, connect=5.0),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        reply_to: str,
        from_name: str | None = None,
        attachment: bytes | None = None,
        attachment_filename: str | None = None,
    ) -> str:
        """Send one transactional email via Resend. Returns Resend's message ID.

        Raises :class:`ResendError` on any failure \u2014 caller (outreach.py)
        catches and writes to OutreachLog.

        The ``from_name`` argument is rendered as
            "<from_name> (via AutoApply) <outreach@domain.in>"
        which is honest disclosure (matches the 'via mailchimp.com' UX users
        already recognise) and keeps DMARC happy because the From: domain is
        ours, not the user's.
        """
        if self._client is None:
            async with self:
                return await self.send(
                    to=to, subject=subject, body=body, reply_to=reply_to,
                    from_name=from_name, attachment=attachment,
                    attachment_filename=attachment_filename,
                )

        if from_name:
            from_header = f'"{_sanitise_name(from_name)} (via AutoApply)" <{self.from_address_bare}>'
        else:
            from_header = self.from_address_bare

        payload: dict = {
            "from": from_header,
            "to": [to],
            "subject": subject,
            "text": body,
            "reply_to": reply_to,
        }
        if attachment is not None:
            payload["attachments"] = [{
                "filename": attachment_filename or "resume.pdf",
                "content": base64.b64encode(attachment).decode("ascii"),
            }]

        try:
            resp = await self._client.post(_BASE_URL, json=payload)
        except httpx.HTTPError as e:
            raise ResendError(f"network: {type(e).__name__}: {e}") from e

        if resp.status_code >= 400:
            # Resend returns {"name": "...", "message": "..."} on errors.
            try:
                err = resp.json()
                detail = f"{err.get('name', 'error')}: {err.get('message', resp.text)}"
            except ValueError:
                detail = resp.text[:200]
            raise ResendError(f"{resp.status_code}: {detail}")

        try:
            return (resp.json() or {}).get("id", "")
        except ValueError:
            return ""


def _sanitise_name(name: str) -> str:
    """Strip characters that would break the From: header quoting.

    Email RFC 5322 forbids unescaped double-quotes and CR/LF inside a quoted
    display name; we replace both with safe alternatives. Keeps the rest as-is
    (Unicode letters are fine \u2014 Resend handles UTF-8 properly).
    """
    return name.replace('"', "'").replace("\r", " ").replace("\n", " ").strip()
