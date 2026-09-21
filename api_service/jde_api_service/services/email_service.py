"""
Outbound email for invitations and password resets.

No email provider is configured anywhere in this project (checked:
no SMTP/SendGrid/SES/Mailgun config exists in either repo) -- so this
module ships two things: the interface a real provider would implement,
and a DEV-PREVIEW implementation that is the default and that never
sends anything anywhere. It exists so the rest of the invitation/reset
flow can be built and verified now, without a live provider and without
ever emailing a real person by accident.

To wire up real delivery later: implement EmailService.send() against
whatever provider is chosen (e.g. SendGrid, SES, Postmark, or plain
SMTP via smtplib -- all fine here, this interface doesn't assume one),
set JDE_EMAIL_PROVIDER=<name> and that provider's own credentials as
environment variables (never source control), and swap
get_email_service()'s branch below. Nothing else in this codebase
needs to change -- every caller already goes through this interface.

Until then, JDE_EMAIL_DEV_PREVIEW controls what "sending" an email
does:
  - unset/true (the default): DevPreviewEmailService. Nothing is sent.
    The invitation/reset link is returned in the API response to the
    ADMIN who triggered it (never emailed, never logged) so it can be
    handed to the invited person out of band during this prototype
    phase -- see routers/auth_admin.py's own comment on exactly which
    responses carry it.
  - false: NullEmailService. Also sends nothing, but does not surface
    the link anywhere either -- the safe default once a real provider
    is expected but not yet wired, so a misconfiguration fails loud
    (no link the admin can use) rather than silently working via the
    preview path.

Neither implementation ever sends to a real inbox. Do not add one that
does until a provider is actually configured and the user has asked
for it -- see this module's own docstring in the design conversation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass(frozen=True)
class OutgoingEmail:
    to: str
    subject: str
    body: str
    # Present for invitation/reset emails -- the dev-preview path
    # surfaces this directly; a real provider would embed it in `body`
    # instead and leave this unused by the caller.
    action_url: Optional[str] = None


class EmailService(Protocol):
    def send(self, email: OutgoingEmail) -> None: ...

    @property
    def is_dev_preview(self) -> bool: ...


class DevPreviewEmailService:
    """Sends nothing. Callers that need to hand the recipient a link
    during this prototype phase read it back from the send() argument
    via is_dev_preview, not from here -- see auth_admin.py."""

    is_dev_preview = True

    def send(self, email: OutgoingEmail) -> None:
        return None


class NullEmailService:
    """Sends nothing and never exposes the link either -- see this
    module's own docstring for when this is the safer default."""

    is_dev_preview = False

    def send(self, email: OutgoingEmail) -> None:
        return None


def get_email_service() -> EmailService:
    dev_preview = os.environ.get("JDE_EMAIL_DEV_PREVIEW", "true").strip().lower() not in ("0", "false", "no")
    return DevPreviewEmailService() if dev_preview else NullEmailService()
