"""
Outbound e-mail for invitations and password resets.

Real delivery goes through SMTP, configured in the server environment (on
Azure: App Settings, the password from Key Vault). Azure Communication
Services Email, Microsoft 365, SendGrid and most providers offer SMTP
submission, so one implementation covers every customer set-up:

    JDE_SMTP_HOST        e.g. smtp.azurecomm.net / smtp.office365.com
    JDE_SMTP_PORT        default 587
    JDE_SMTP_STARTTLS    default true (STARTTLS on 587); "ssl" for implicit TLS (465)
    JDE_SMTP_USERNAME    optional
    JDE_SMTP_PASSWORD    optional (never logged)
    JDE_MAIL_FROM        the sender address, e.g. jade@consultiq.nl
    JDE_PUBLIC_URL       the address people open Jade at (links in e-mails)

Without JDE_SMTP_HOST, nothing is e-mailed, and every caller says so: an
Administrator gets the invitation / reset link to hand over personally, and
the self-service "forgot password" page tells the person to ask their
Administrator. A failed delivery is reported as failed -- never as sent.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Optional, Protocol

logger = logging.getLogger("jde_api_service")


@dataclass(frozen=True)
class OutgoingEmail:
    to: str
    subject: str
    body: str
    # The link the e-mail is about (invitation / reset). An Administrator is
    # shown it only when it could not be e-mailed.
    action_url: Optional[str] = None


@dataclass(frozen=True)
class DeliveryResult:
    sent: bool
    detail: str = ""


class EmailService(Protocol):
    configured: bool

    def send(self, email: OutgoingEmail) -> DeliveryResult: ...


class SmtpEmailService:
    configured = True

    def __init__(self, *, host: str, port: int, security: str, username: str, password: str, sender: str) -> None:
        self.host, self.port, self.security = host, port, security
        self.username, self.password, self.sender = username, password, sender

    def send(self, email: OutgoingEmail) -> DeliveryResult:
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = email.to
        msg["Subject"] = email.subject
        msg.set_content(email.body)
        context = ssl.create_default_context()
        try:
            if self.security == "ssl":
                server = smtplib.SMTP_SSL(self.host, self.port, timeout=20, context=context)
            else:
                server = smtplib.SMTP(self.host, self.port, timeout=20)
            with server:
                if self.security == "starttls":
                    server.starttls(context=context)
                if self.username:
                    server.login(self.username, self.password)
                server.send_message(msg)
        except (OSError, smtplib.SMTPException) as exc:
            logger.warning("e-mail to %s could not be sent: %s", email.to, type(exc).__name__)
            return DeliveryResult(False, f"the e-mail could not be sent ({type(exc).__name__}); check the mail server "
                                         "settings")
        return DeliveryResult(True, f"e-mailed to {email.to}")


class NotConfiguredEmailService:
    """No mail server: nothing is sent, and callers say so."""

    configured = False

    def send(self, email: OutgoingEmail) -> DeliveryResult:
        return DeliveryResult(False, "e-mail is not set up on this server (JDE_SMTP_HOST)")


def get_email_service() -> EmailService:
    host = os.environ.get("JDE_SMTP_HOST", "").strip()
    if not host:
        return NotConfiguredEmailService()
    mode = os.environ.get("JDE_SMTP_STARTTLS", "true").strip().lower()
    security = "ssl" if mode == "ssl" else "none" if mode in ("0", "false", "no") else "starttls"
    return SmtpEmailService(
        host=host, port=int(os.environ.get("JDE_SMTP_PORT", "465" if security == "ssl" else "587")),
        security=security, username=os.environ.get("JDE_SMTP_USERNAME", "").strip(),
        password=os.environ.get("JDE_SMTP_PASSWORD", ""),
        sender=os.environ.get("JDE_MAIL_FROM", "").strip() or os.environ.get("JDE_SMTP_USERNAME", "").strip(),
    )


def public_url(fallback: str = "") -> str:
    """Where people open Jade -- the base of every link in an e-mail."""
    return (os.environ.get("JDE_PUBLIC_URL", "").strip() or fallback).rstrip("/")
