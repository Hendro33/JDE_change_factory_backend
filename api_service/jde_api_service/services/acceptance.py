"""
Business acceptance of the outcome (review, change 7).

Three outcomes are kept apart: verified in DEV, accepted by the business, and
handed over for release. The Domain Owner accepts the outcome after it is
verified in DEV; the release and CNC hand-off then follow with the existing
authorised release roles (the as-built record cannot be finalised before).

An acceptance is bound to exactly what was verified (the "basis": the route,
the approved change or package and its verification record). A later delivery
or verification makes an earlier acceptance stale, and it is asked for again.
A "not accepted" needs a reason and stops automatic progression until the
Application Manager acts on it.

Who records it: the Domain Owner of the story's domain; or the Application
Manager or an Administrator on a named business person's behalf.

History is append-only (document kind "business_acceptance"); the latest
entry on the current basis is the current decision.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

KIND = "business_acceptance"


class AcceptanceRefused(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _doc_id(company_id: str, story_id: str) -> str:
    return f"{company_id}:{story_id}"


def _doc(company_id: str, story_id: str) -> dict:
    try:
        from jde_mcp_server import docstore

        return docstore.get(KIND, _doc_id(company_id, story_id)) or {}
    except Exception:  # noqa: BLE001 -- no store yet
        return {}


def basis(change, tech: Optional[dict] = None) -> Optional[str]:
    """What the acceptance is bound to, or None while nothing is verified."""
    route = change.architect_decision.recommended_route if change.architect_decision else None
    parts: dict[str, Any] = {"route": route}
    if route in ("Technical Agent", "Mixed"):
        rec = (tech or {}).get("record") or {}
        pkg = (tech or {}).get("package") or {}
        ver = rec.get("verification") or {}
        if not (ver.get("passed") and ver.get("runtime_is_approved_artifact")):
            return None
        parts.update(package=pkg.get("revision"), change_id=pkg.get("change_id"),
                     verification=ver.get("evidence_entry_hash") or ver.get("at"))
    elif route == "Human Implementation":
        from ..delivery import handoff

        v = handoff.state(change)["verification"]
        if not v or not v.get("passed"):
            return None
        parts.update(design_revision=v.get("design_revision"), verification=v.get("evidence_entry_hash") or v["at"])
    else:
        ec = change.exact_change
        ex = ec.execution if ec else None
        ver = (ex.verification if ex else None) or {}
        if not (ex and ex.write_state == "applied" and ex.test_state == "completed" and ver.get("passed") is True):
            return None
        parts.update(change_id=change.change_approval.approval_id if change.change_approval else None,
                     verification=ver.get("evidence_entry_hash") or ver.get("at"))
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def current(change, tech: Optional[dict] = None) -> Optional[dict]:
    """The latest decision on the current basis, or None."""
    b = basis(change, tech)
    if b is None:
        return None
    rows = [r for r in _doc(change.customer_id, change.id).get("history", []) if r.get("basis") == b]
    return rows[-1] if rows else None


def history(change) -> list[dict]:
    return list(_doc(change.customer_id, change.id).get("history", []))


def record(change, *, decision: str, note: str, on_behalf_of: str, actor_name: str, actor_user_id: str,
           roles: set[str], tech: Optional[dict] = None) -> dict:
    if decision not in ("accepted", "not_accepted"):
        raise AcceptanceRefused("the decision is 'accepted' or 'not_accepted'")
    b = basis(change, tech)
    if b is None:
        raise AcceptanceRefused("there is no verified outcome to accept yet: it is verified in DEV first")
    if decision == "not_accepted" and len(note.strip()) < 5:
        raise AcceptanceRefused("say what does not work for the business, so the Application Manager can act on it")
    is_owner = "domain_owner" in roles
    on_behalf_of = on_behalf_of.strip()
    if not is_owner:
        if not roles & {"product_manager", "admin"}:
            raise AcceptanceRefused("the outcome is accepted by the Domain Owner, or recorded on their behalf by the "
                                    "Application Manager")
        if len(on_behalf_of) < 2:
            raise AcceptanceRefused("name the business person who accepted (or did not accept) the outcome")
    entry = {"decision": decision, "note": note.strip()[:1000], "basis": b, "by": actor_name,
             "by_user_id": actor_user_id, "on_behalf_of": on_behalf_of[:120] if not is_owner else "", "at": _now()}
    try:
        from jde_mcp_server.evidence import capture_evidence

        who = f"{entry['on_behalf_of']} (recorded by {actor_name})" if entry["on_behalf_of"] else actor_name
        entry["evidence_entry_hash"] = capture_evidence(change.id, {
            "event": "business_acceptance", "stage": "validation", "actor": actor_name,
            "detail": f"outcome {'ACCEPTED' if decision == 'accepted' else 'NOT accepted'} by the business: {who}"
                      + (f": {entry['note']}" if entry["note"] else "")})["entry_hash"]
    except Exception:  # noqa: BLE001
        pass
    from jde_mcp_server import docstore

    with docstore.transaction():
        doc = docstore.get(KIND, _doc_id(change.customer_id, change.id)) or {
            "company_id": change.customer_id, "story_id": change.id, "history": []}
        doc["history"] = list(doc.get("history", [])) + [entry]
        docstore.put(KIND, _doc_id(change.customer_id, change.id), doc, company_id=change.customer_id)
    return entry
