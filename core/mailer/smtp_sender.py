"""Async SMTP sender using the user's own Gmail app password.

Architecture:
- Each user provides their own SMTP credentials at onboarding (Gmail app
  password, 16 chars from https://myaccount.google.com/apppasswords).
- We store the password encrypted on UserCredentials.smtp_password_encrypted
  (Fernet, see core/crypto.py).
- At send time we decrypt, send via aiosmtplib over STARTTLS:587, and
  discard the plaintext immediately.
- The From: address is the user's own email \u2014 essential for DKIM/SPF
  alignment and to avoid "via gmail.com" warnings in the recipient's client.

Why Gmail app passwords and not OAuth?
- For MVP: zero OAuth dance, zero token refresh code, works for any Google
  Workspace or personal Gmail with 2FA enabled (which Google now requires
  for app passwords anyway).
- OAuth is the right v2 move because revocation is per-app, not per-password.
  Punt to post-launch.
"""
from __future__ import annotations

import logging
from email.message import EmailMessage

import aiosmtplib

from core.crypto import decrypt
from core.models import UserCredentials

log = logging.getLogger(__name__)

_HOST = "smtp.gmail.com"
_PORT = 465  # STARTTLS (plain socket upgraded to TLS via EHLO)
_TIMEOUT = 30.0  # seconds; Gmail occasionally takes a beat


class SMTPSender:
    """Stateless sender. Builds a fresh connection per send \u2014 simpler than
    pooling, and Gmail throttles long-lived connections aggressively.
    """

    def __init__(self, email: str, password: str, from_name: str | None = None):
        self.email = email
        self._password = password
        self.from_name = from_name

    @classmethod
    def from_credentials(cls, creds: UserCredentials) -> "SMTPSender | None":
        """Build a sender from a UserCredentials row, decrypting at the edge.

        Returns None if the user hasn't connected SMTP yet (onboarding skipped
        the step, or they paused outreach). The caller logs and continues.
        """
        if not creds.smtp_email or creds.smtp_password_encrypted is None:
            return None
        try:
            password = decrypt(creds.smtp_password_encrypted)
        except Exception as e:
            log.error("SMTP password decrypt failed for user %s: %s", creds.user_id, e)
            return None
        return cls(
            email=creds.smtp_email,
            password=password,
            from_name=creds.candidate_name,
        )

    async def __aenter__(self) -> "SMTPSender":
        # No connection pool — every send opens its own SMTP session.
        # Implementing the context-manager protocol lets pipeline.py treat
        # SMTPSender and ResendSender interchangeably (``async with sender:``).
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        reply_to: str | None = None,
        from_name: str | None = None,
        attachment: bytes | None = None,
        attachment_filename: str | None = None,
    ) -> None:
        """Send one email. Raises aiosmtplib.SMTPException on failure — caller
        is expected to catch and log to OutreachLog.

        ``reply_to`` / ``from_name`` are accepted for parity with
        :class:`core.mailer.resend_sender.ResendSender`. With Gmail SMTP the
        From: address IS the user's own inbox, so replies naturally land
        there — ``reply_to`` only sets an explicit Reply-To header when the
        caller wants replies routed somewhere else (rarely used).
        """
        display_name = from_name or self.from_name
        msg = EmailMessage()
        msg["From"] = f"{display_name} <{self.email}>" if display_name else self.email
        msg["To"] = to
        msg["Subject"] = subject
        if reply_to:
            msg["Reply-To"] = reply_to
        msg.set_content(body)

        if attachment is not None:
            msg.add_attachment(
                attachment,
                maintype="application",
                subtype="pdf",
                filename=attachment_filename or "resume.pdf",
            )

        await aiosmtplib.send(
            msg,
            hostname=_HOST,
            port=_PORT,
            use_tls=True,
            username=self.email,
            password=self._password,
            timeout=_TIMEOUT,
        )

    async def verify(self) -> tuple[bool, str]:
        """Quick connect+STARTTLS+login+quit cycle to validate credentials.

        Used by the onboarding wizard so the user finds out *immediately* if
        their app password is wrong, instead of discovering it tomorrow when
        the scheduler's pipeline silently fails on every outreach.

        Returns ``(True, "")`` on success, ``(False, "reason")`` otherwise.
        Never raises.

        TLS note: we pass ``use_tls=True`` to the constructor so connect()
        performs the STARTTLS upgrade atomically on port 587. Calling
        ``smtp.starttls()`` afterwards would raise "Connection already using
        TLS" because aiosmtplib's connect() already upgraded the channel.
        """
        smtp = aiosmtplib.SMTP(
            hostname=_HOST,
            port=_PORT,
            use_tls=True,
            timeout=10.0,
        )
        try:
            await smtp.connect()
            await smtp.login(self.email, self._password)
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
        finally:
            try:
                await smtp.quit()
            except Exception:
                pass  # quit failures don't invalidate the test
        return True, ""
