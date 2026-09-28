"""
Live calls to a customer's AIS server for delivery verification.

Both use the customer's own JD Edwards connection -- address, certificate,
environment, role and encrypted credential saved in Administration -- never
a server-wide login:

  * read_processing_option: one processing-option value of one version,
    through Jade's governed discovery (an approved read for that
    application|version; the request is validated, logged and bounded like
    every other discovery read).
  * run_orchestration: one approved test orchestration, through the same
    verified-TLS, single-destination transport. An orchestration is an
    action in JDE, so the caller (functional.py) records the attempt BEFORE
    calling and settles it afterwards; a call that may have reached JDE but
    did not answer cleanly is recorded as unknown, never guessed.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Optional

from ..discovery import profile_service, service as discovery_service, transport
from ..services import credential_crypto

ORCHESTRATION_PATH = "/jderest/v3/orchestrator/{name}"
_ORCHESTRATION_NAME = re.compile(r"^[A-Za-z0-9_\-]{1,100}$")


class LiveUnavailable(RuntimeError):
    """The live call cannot be made (no connection, not enabled, read not
    approved, outside the window...). Nothing was sent."""


class LiveCallFailed(RuntimeError):
    """The call was made and failed."""

    def __init__(self, message: str, *, sent: bool) -> None:
        super().__init__(message)
        self.sent = sent


@dataclass
class LiveValue:
    value: Optional[str]
    found: bool
    observation_id: Optional[str]
    detail: str


def read_processing_option(company_id: str, story_id: str, actor_user_id: Optional[str], application: str,
                           version: str, option: str) -> LiveValue:
    grant, reason = discovery_service.grant_for_story(story_id, company_id, agent_run_id=None,
                                                      actor_user_id=actor_user_id, purpose="delivery_verification")
    if grant is None:
        raise LiveUnavailable(f"the JD Edwards connection cannot read it: {reason}")
    raw: list[dict] = []
    try:
        # The approved read may name the options it covers; otherwise it covers
        # the whole version, and the one option is picked from the answer.
        profile = discovery_service.profile_service.load(company_id)
        read = discovery_service._approved_read(profile["config"], "processing_option_values") if profile else None
        fields = [option] if read is not None and read.fields else []
        evidence = discovery_service.execute_read(grant, "processing_option_values", f"{application}|{version}",
                                                  fields, None, 10, raw_out=raw)
    except discovery_service.DiscoveryBlocked as exc:
        raise LiveUnavailable(f"the JD Edwards connection cannot read it: {exc}") from None
    except discovery_service.DiscoveryFailed as exc:
        raise LiveUnavailable(f"the live read failed: {exc}") from None
    row = next((r for r in raw if str(r.get("option")) == option), None)
    obs = evidence.get("observation_id")
    if row is None:
        return LiveValue(None, False, obs, f"{application}|{version} returned no value for option {option}")
    value = row.get("value")
    return LiveValue(None if value is None else str(value), True, obs,
                     f"read live from {application}|{version} option {option} ({obs})")


def run_orchestration(company_id: str, name: str, payload: dict) -> dict[str, Any]:
    """Call one orchestration on the customer's AIS and return its answer.
    Raises LiveUnavailable (nothing sent) or LiveCallFailed."""
    if not _ORCHESTRATION_NAME.match(name or ""):
        raise LiveUnavailable(f"{name!r} is not a valid orchestration name")
    profile = profile_service.load(company_id)
    if profile is None or profile.get("disabled"):
        raise LiveUnavailable("this customer has no enabled JD Edwards connection")
    if not profile_service.is_active(profile):
        raise LiveUnavailable("the JD Edwards connection is not enabled for its current settings (Test connection "
                              "and Enable in Administration)")
    config = profile["config"]
    if transport.breaker_open(company_id):
        raise LiveUnavailable("the connection's circuit breaker is open after repeated failures; try again later")
    try:
        client = transport.transport_for(company_id, config, live_transport=discovery_service.LIVE_HTTP_TRANSPORT,
                                         trust=profile_service.trust_for(company_id, config))
        username, password = profile_service.credential(company_id)
    except (transport.DestinationNotAllowed, credential_crypto.CredentialUnreadable) as exc:
        raise LiveUnavailable(str(exc)) from None
    try:
        session = client.authenticate(username, password, config.environment, config.role)
    except transport.TransportError as exc:
        raise LiveCallFailed(f"sign-in to AIS failed: {exc}", sent=False) from None
    started = time.time()
    try:
        answer = client._send("POST", ORCHESTRATION_PATH.format(name=name), token=session.token, body=payload or {})
    except transport.AuthenticationFailed as exc:
        raise LiveCallFailed(f"AIS refused the orchestration: {exc}", sent=True) from None
    except transport.TransportError as exc:
        raise LiveCallFailed(str(exc), sent=True) from None
    finally:
        client.logout(session)
    return {"orchestration": name, "answer": answer, "seconds": round(time.time() - started, 2),
            "environment": config.environment}
