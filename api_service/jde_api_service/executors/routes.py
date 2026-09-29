"""
Which route an approved configuration item takes into the customer's DEV
system:

  ais      -- AIS form requests drive the JD Edwards configuration
              application for the item (User Defined Codes P0004A, the
              set-up applications for the tables in ais.FORM_MAPS).
  browser  -- an agent operates the customer's web client in a browser on
              Jade's server, with a screenshot of every step: what AIS
              cannot do (a version's processing options, a batch version's
              data selection and sequencing).
  person   -- JD Edwards cannot accommodate the item through either route
              (for example a set-up table no form map covers). A person
              applies it in DEV and records it.

The route of each item is decided by Jade from the item itself when the
change set is proposed -- never by the model -- and is part of what the
Application Manager approves ("executor": "agent" or "person"). Whether an
agent item can run NOW also depends on the customer's settings: agent
execution switched on (per customer and per capability), the write user
saved and the settings tested. An agent item that cannot run because an
Admin switched agent execution off may be handed to a person, and that is
recorded.
"""

from __future__ import annotations

from typing import Optional

AIS, BROWSER, PERSON = "ais", "browser", "person"
ROUTE_LABELS = {AIS: "agent, through AIS form requests", BROWSER: "agent, in the JD Edwards web client",
                PERSON: "a person, in JD Edwards"}


def route_for(item: dict) -> tuple[str, str]:
    """(route, why) for one normalised configuration item."""
    from . import ais

    kind = item["kind"]
    if kind == "udc_value" or kind == "setup_row":
        form = ais.form_map_for(item)
        if form is not None:
            return AIS, f"{form['application']} ({form['title']}) through AIS form requests"
        return PERSON, (f"no AIS form map covers table {item.get('table')}, and the web client route does not "
                        "either -- a person applies it")
    if kind == "processing_option":
        return BROWSER, ("AIS reads a version's processing options but cannot save them: the agent sets the value "
                         "in the web client (Work With Versions, Processing Options)")
    if kind in ("version_data_selection", "version_data_sequencing"):
        what = "data selection" if kind == "version_data_selection" else "data sequencing"
        return BROWSER, f"AIS cannot change a batch version's {what}: the agent enters it in the web client"
    return PERSON, f"no agent route for {kind} items"


def executor_for(route: str) -> str:
    return "person" if route == PERSON else "agent"


def switchable_capabilities() -> list[str]:
    """The configuration capabilities agent execution can be switched per."""
    from jde_mcp_server import capability_catalog

    out = []
    for cap in capability_catalog.list_capabilities():
        enforcement = cap.get("enforcement") or {}
        if enforcement.get("item_kind") or enforcement.get("tool") == "set_processing_option":
            out.append(cap["capability_id"])
    return out


def readiness(company_id: str, route: str, capability_id: Optional[str] = None) -> tuple[bool, str]:
    """Can an agent take this route for this customer NOW? (ready, reason)."""
    from ..discovery import profile_service
    from . import browser, settings

    if route == PERSON:
        return False, "a person applies this item"
    s = settings.load(company_id)
    if capability_id:
        on, why = settings.capability_enabled(s, capability_id)
        if not on:
            return False, why
    elif s is not None and not s["agent_execution_enabled"]:
        return False, "agent execution is switched off for this customer by an Admin"
    if s is None:
        return False, ("agent execution is not set up: an Admin enters the DEV write user under Administration > "
                       "Systems & Connections > JDE")
    if settings.credential_storage(s) != "encrypted":
        return False, f"the DEV write user is not usable ({settings.credential_storage(s)})"
    profile = profile_service.load(company_id)
    if not profile_service.is_active(profile):
        return False, ("the JD Edwards connection is not enabled, so Jade cannot read the item before and after the "
                       "change")
    if route == AIS:
        check = settings.check_state(s, "ais_write_sign_in")
        if check["state"] != "ok":
            return False, f"the DEV write user's AIS sign-in test is {check['state']}: test the settings again"
        return True, "ready"
    if route == BROWSER:
        if not s["config"].web_client_url:
            return False, "no JD Edwards web client address is saved"
        check = settings.check_state(s, "web_client")
        if check["state"] != "ok":
            return False, f"the web client test is {check['state']}: test the settings again"
        ok, why = browser.available()
        if not ok:
            return False, why
        return True, "ready"
    return False, f"unknown route {route}"
