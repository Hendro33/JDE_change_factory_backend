"""The current authorisation of a change's exact change, for every screen.

The approval record says what was decided; whether it can still be used is
decided by the same checks the delivery gate runs (approval expiry, the
approver's current authority, the current policy, the same company). A
screen that shows "Authorised" from the record alone is wrong once the
approval expires, so the lifecycle carries this projection instead.
Read-only: nothing is approved, renewed or executed here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from ..models.lifecycle import Authorisation, Lifecycle, PastApproval


def _iso(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    return str(value)


def _day(value: Any) -> str:
    iso = _iso(value)
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%d %b %Y, %H:%M")
    except ValueError:
        return iso


def current(change) -> Optional[Authorisation]:
    ap = change.change_approval
    if change.exact_change is None and ap is None:
        return None
    if ap is None or not ap.approval_id:
        return Authorisation(state="pending", label="Not authorised yet",
                             detail="The exact change waits for the Application Manager's decision.")
    try:
        from jde_mcp_server import approval
        cur = approval.current_authorisation(ap.approval_id)
    except Exception:  # noqa: BLE001 -- unreadable: say so, never claim it is valid
        return Authorisation(state="invalidated", label="Authorisation could not be checked",
                             detail="JADE could not re-check this approval; the delivery gate decides.")
    state = cur.get("state", "pending")
    by, at, exp = cur.get("by"), cur.get("at"), cur.get("expires_at")
    history = [PastApproval(by=h.get("approved_by"), at=_iso(h.get("approved_at")), expires_at=_iso(h.get("expires_at")),
                            superseded_at=_iso(h.get("superseded_at")), reason=_reason(h.get("superseded_because", "")))
               for h in cur.get("history", [])]
    base = dict(by=by, at=_iso(at), expires_at=_iso(exp), history=history)
    if state == "valid":
        return Authorisation(state="valid", label="Authorised",
                             detail=f"By {by} on {_day(at)}; valid until {_day(exp)}.", **base)
    if state == "used":
        return Authorisation(state="used", label="Authorised · applied in DEV",
                             detail=f"Authorised by {by} on {_day(at)} and applied.", **base)
    if state == "expired":
        return Authorisation(state="expired", label="Authorisation expired", renewable=True,
                             detail=f"Authorised by {by} on {_day(at)}; expired on {_day(exp)} before it was applied. "
                                    "Authorise it again to deliver it.", **base)
    if state == "invalidated":
        return Authorisation(state="invalidated", label="Authorisation no longer valid", renewable=True,
                             detail=f"Authorised by {by} on {_day(at)}. {_reason(cur.get('reason', ''))} "
                                    "Authorise it again with a current approver.", **base)
    if state == "rejected":
        return Authorisation(state="rejected", label="Rejected", detail=f"Rejected by {by} on {_day(at)}.", **base)
    return Authorisation(state="pending", label="Not authorised yet",
                         detail="The exact change waits for the Application Manager's decision.", **base)


def _reason(raw: str) -> str:
    """The gate's refusal in business words (the same wording as the open point)."""
    if not raw:
        return ""
    try:
        from .issues import _gate_issue
        return _gate_issue(raw).title
    except Exception:  # noqa: BLE001
        return "The approval no longer meets the current approval rules."


def apply(change, lc: Lifecycle) -> Lifecycle:
    lc.authorisation = current(change)
    return lc
