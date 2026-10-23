"""Closing the loop with the source (register H3, H4, C4, F4; V11 §3.4, §6.3).

* A business question on a Jira-sourced story is also posted to the ticket,
  once, and a follow-up note when it is answered in JADE (C4).
* The accepted outcome is written back to the ticket in business terms (H3).
* The requester confirms "solved" or "not solved"; "not solved" opens a new,
  linked request at intake with the reason -- the finished story keeps its
  record (H4). An improvement raised after authorisation becomes a linked
  follow-up request, never silent scope growth (F4).

Every write to Jira is idempotent (one per key) and never raises: a failure is
recorded on the story and retried on the next sweep.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

WRITEBACK_KIND = "jira_writebacks"
CONFIRM_KIND = "requester_confirmations"
LINKS_KIND = "linked_requests"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _doc_id(company_id: str, story_id: str) -> str:
    return f"{company_id}:{story_id}"


def jira_key(change) -> str:
    key = (change.source_reference or "").strip()
    return key if re.fullmatch(r"[A-Z][A-Z0-9_]+-\d+", key) else ""


def writebacks(company_id: str, story_id: str) -> dict:
    from jde_mcp_server import docstore

    return docstore.get(WRITEBACK_KIND, _doc_id(company_id, story_id)) or {"posted": {}, "failed": {}}


def _post(company_id: str, change, key: str, body: str) -> Optional[bool]:
    """Post one comment to the story's Jira ticket, once per key. Returns True
    when posted now, False when it failed (recorded), None when nothing to do."""
    from jde_mcp_server import docstore

    issue = jira_key(change)
    if not issue:
        return None
    doc = writebacks(company_id, change.id)
    if key in doc["posted"]:
        return None
    try:
        from .registry import get_jira_gateway, get_jira_integration_service

        cfg = get_jira_integration_service().get_for_customer(company_id)
        if cfg is None or not cfg.base_url:
            return None
        get_jira_gateway(company_id).add_comment(base_url=cfg.base_url, issue_key=issue, body=body,
                                                 internal=getattr(cfg, "comment_visibility", "") == "internal")
    except Exception as exc:  # noqa: BLE001 -- recorded and retried; never breaks the work
        doc["failed"][key] = {"at": _now(), "error": str(exc)[:300]}
        docstore.put(WRITEBACK_KIND, _doc_id(company_id, change.id), doc, company_id=company_id)
        return False
    doc["posted"][key] = {"at": _now(), "body": body}
    doc["failed"].pop(key, None)
    docstore.put(WRITEBACK_KIND, _doc_id(company_id, change.id), doc, company_id=company_id)
    return True


def sync_questions(company_id: str, change) -> int:
    """C4: post each open business question once; note an answer once."""
    if not jira_key(change) or change.lifecycle is None:
        return 0
    n = 0
    for i in change.lifecycle.issues or []:
        if i.kind not in ("business", "decision") or i.owner in ("jade", "none"):
            continue
        if i.resolution is None:
            body = (f"JADE needs an answer to continue: {i.title}"
                    + (f"\nJADE proposes: {i.proposed_answer}" if i.proposed_answer else "")
                    + "\nPlease answer in JADE (My Work), so the answer is recorded with the story.")
            n += bool(_post(company_id, change, f"q:{i.id}", body))
        elif i.resolution.answer:
            n += bool(_post(company_id, change, f"a:{i.id}",
                            f"Answered in JADE by {i.resolution.by}: {i.title} -- {i.resolution.answer}"))
    return n


def post_outcome(company_id: str, change) -> Optional[bool]:
    """H3: the accepted outcome, in business terms."""
    lc = change.lifecycle
    card = lc.outcome_card if lc else None
    if card is None or not card.business_accepted.done:
        return None
    lines = [f"Outcome: {card.headline}"]
    if card.what_changed:
        lines.append("What changed: " + "; ".join(card.what_changed))
    if card.verified:
        lines.append("What was verified: " + "; ".join(card.verified))
    lines.append(f"Accepted by {card.business_accepted.by}.")
    lines.append("Please confirm in JADE whether this solved your request.")
    return _post(company_id, change, f"outcome:{card.business_accepted.at}", "\n".join(lines))


def sweep(company_id: str, changes: list) -> int:
    """Run by routine automation: questions and accepted outcomes to Jira."""
    n = 0
    for c in changes:
        try:
            n += sync_questions(company_id, c)
            n += bool(post_outcome(company_id, c))
        except Exception:  # noqa: BLE001
            logger.exception("Jira write-back failed for %s", c.id)
    return n


# -- linked requests (H4 not solved, F4 follow-up) --------------------------------------------
def _link(company_id: str, story_id: str, entry: dict) -> None:
    from jde_mcp_server import docstore

    doc_id = _doc_id(company_id, story_id)
    with docstore.transaction():
        doc = docstore.get(LINKS_KIND, doc_id) or {"links": []}
        doc["links"].append(entry)
        docstore.put(LINKS_KIND, doc_id, doc, company_id=company_id)


def links(company_id: str, story_id: str) -> list[dict]:
    from jde_mcp_server import docstore

    return (docstore.get(LINKS_KIND, _doc_id(company_id, story_id)) or {}).get("links", [])


def open_linked_request(company_id: str, change, *, kind: str, text: str, by: str, by_user_id: str) -> str:
    """A new request at intake, linked to this story, carrying the reason."""
    from ..models.change_request import ChangeRequestCreate
    from .registry import get_change_request_service

    title_prefix = {"not_solved": "Not solved", "follow_up": "Follow-up"}[kind]
    raw = (f"{title_prefix} of {change.id} ({change.title}).\n\n{text.strip()}\n\n"
           f"Original request:\n{change.original_request or ''}")
    cr = get_change_request_service().create_direct(
        ChangeRequestCreate(title=f"{title_prefix}: {change.title}"[:200], business_source=change.source,
                            source_reference=change.source_reference or "", raw_content=raw,
                            priority=change.priority if change.priority else None),
        customer_id=company_id, requester=by)
    entry = {"kind": kind, "request_id": cr.id, "text": text.strip(), "by": by, "by_user_id": by_user_id, "at": _now()}
    _link(company_id, change.id, entry)
    try:
        from jde_mcp_server.evidence import capture_evidence

        capture_evidence(change.id, {"event": f"linked_request_{kind}", "stage": "closure", "actor": by,
                                     "detail": f"{title_prefix}: new request {cr.id} -- {text.strip()}"})
    except Exception:  # noqa: BLE001
        pass
    return cr.id


def confirm(company_id: str, change, *, solved: bool, note: str, by: str, by_user_id: str,
            on_behalf_of: str = "") -> dict:
    """H4: the requester's confirmation, recorded once per accepted outcome."""
    from jde_mcp_server import docstore

    card = change.lifecycle.outcome_card if change.lifecycle else None
    if card is None or not card.business_accepted.done:
        raise ValueError("the requester confirms after the business accepted the outcome")
    if not solved and len(note.strip()) < 5:
        raise ValueError("say what is still not solved, so the new request carries it")
    doc_id = _doc_id(company_id, change.id)
    existing = docstore.get(CONFIRM_KIND, doc_id) or {"confirmations": []}
    if any(c.get("for_acceptance") == card.business_accepted.at for c in existing["confirmations"]):
        raise ValueError("the requester already confirmed this outcome")
    entry = {"solved": solved, "note": note.strip(), "by": by, "by_user_id": by_user_id,
             "on_behalf_of": on_behalf_of.strip(), "at": _now(), "for_acceptance": card.business_accepted.at}
    if not solved:
        entry["new_request"] = open_linked_request(company_id, change, kind="not_solved", text=note, by=by,
                                                   by_user_id=by_user_id)
    existing["confirmations"].append(entry)
    docstore.put(CONFIRM_KIND, doc_id, existing, company_id=company_id)
    who = on_behalf_of.strip() or by
    _post(company_id, change, f"confirm:{entry['at']}",
          f"The requester ({who}) confirmed in JADE: " + ("solved." if solved else f"not solved -- {note.strip()} "
                                                          f"(new request {entry.get('new_request')})"))
    return entry


def confirmation(company_id: str, story_id: str) -> Optional[dict]:
    from jde_mcp_server import docstore

    rows = (docstore.get(CONFIRM_KIND, _doc_id(company_id, story_id)) or {}).get("confirmations", [])
    return rows[-1] if rows else None
