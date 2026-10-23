"""
Hand-offs to a named implementer: the work a person does outside Jade.

Before this, a "Human Implementation" route ended in a label ("implemented by
a person outside JADE") with no way to record the work or finish the story,
and the human actions of a mixed design were a list of text. Now (review,
changes 4 and 7):

  * Jade prepares a SPECIFICATION from the current design: what to do, in what
    order, the objects concerned, the acceptance criteria to meet, how it will
    be verified, the rollback, and the answers recorded on the open points. It
    is downloadable as a PDF for the implementer.
  * For the Human Implementation route the Application Manager AUTHORISES the
    hand-off of exactly this design revision (the same design-approval record
    and approval policy as the technical route). For the other routes the
    route's own approval authorises the human items (the exact change, or the
    design approval of a technical design).
  * Each HAND-OFF ITEM gets a named implementer and is recorded as completed
    with an evidence reference -- never just a label.
  * For the Human Implementation route a person records the VERIFICATION of
    the result against the acceptance criteria.

Items are derived from the current design revision, so a new design asks for
the work again; earlier records stay in the history. Every record is also
written to the story's hash-chained evidence log.

Nothing here contacts JD Edwards.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

KIND = "human_handoffs"
HUMAN_ROUTE = "Human Implementation"
# No JD Edwards change, but the requirement may still need work by people (a
# guide written and published, training, a process step, a check). Audit
# 23 Oct (F00): that work is handed over and recorded like any other, and the
# business accepts the outcome, before the story closes.
NO_CHANGE_ROUTE = "Resolve without Change"
PEOPLE_ROUTES = (HUMAN_ROUTE, NO_CHANGE_ROUTE)
TECHNICAL_ROUTES = {"Technical Agent", "Mixed"}
RECORDER_ROLES = ("product_manager", "admin", "cnc_operator")

# On a technical route these are the package's own recorded milestones
# (check-in, build, CNC activation), not separate hand-offs.
_MILESTONE_RE = re.compile(r"\b(cnc|omw|check[- ]?in|build|activat|deploy|promot)", re.I)
# On every route, promotion beyond DEV is the release hand-over after the
# as-built record (done by CNC outside Jade), not work recorded before it.
_RELEASE_RE = re.compile(r"\b(promot\w*|production|after the as-built)", re.I)


# Steps of a no-change design that are not work for anyone ("no change needed",
# "nothing to configure") -- not hand-off items.
_NO_WORK_RE = re.compile(r"^\s*(no (jd edwards |jde )?(change|configuration|development)|nothing (to|is) "
                         r"(change|configure|build))", re.I)


class HandoffRefused(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _doc_id(company_id: str, story_id: str) -> str:
    return f"{company_id}:{story_id}"


def _load(company_id: str, story_id: str) -> dict:
    from jde_mcp_server import docstore

    return docstore.get(KIND, _doc_id(company_id, story_id)) or {
        "company_id": company_id, "story_id": story_id, "implementers": [], "completions": [], "verifications": []}


def _save(doc: dict) -> None:
    from jde_mcp_server import docstore

    docstore.put(KIND, _doc_id(doc["company_id"], doc["story_id"]), doc, company_id=doc["company_id"])


def _evidence(story_id: str, actor: str, detail: str, event: str) -> Optional[str]:
    try:
        from jde_mcp_server.evidence import capture_evidence

        return capture_evidence(story_id, {"event": event, "stage": "delivery", "actor": actor,
                                           "detail": detail})["entry_hash"]
    except Exception:  # noqa: BLE001 -- the record itself is kept either way
        return None


# ---------------------------------------------------------------------
# What the design asks people to do
# ---------------------------------------------------------------------
def _design_revision(change) -> Optional[int]:
    try:
        from ..discovery import baseline

        b = baseline.current_for_story(change.customer_id, change.id)
        return b["design_revision"] if b else None
    except Exception:  # noqa: BLE001
        return None


def items_for(change, design_revision: Optional[int] = None) -> list[dict]:
    """The hand-off items of the current design, with stable ids."""
    d, spec = change.architect_decision, change.implementation_spec
    if d is None:
        return []
    route = d.recommended_route
    rev = design_revision if design_revision is not None else (_design_revision(change) or 0)
    texts: list[str] = []
    if route == HUMAN_ROUTE:
        texts = [s for s in (spec.sequence if spec else []) if s.strip()]
        if not texts:
            texts = ["Implement the solution as specified"]
    elif route == NO_CHANGE_ROUTE:
        texts = [s for s in (spec.sequence if spec else []) if s.strip() and not _NO_WORK_RE.search(s)]
    for h in (spec.human_actions_required if spec else []):
        h = h.strip()
        if not h or h in texts:
            continue
        if _RELEASE_RE.search(h) or (route in TECHNICAL_ROUTES and _MILESTONE_RE.search(h)):
            continue
        texts.append(h)
    from ..services.design_quality import is_decision_step

    try:
        recorded = {r["item_id"] for r in _load(change.customer_id, change.id).get("completions", [])}
    except Exception:  # noqa: BLE001 -- no record yet
        recorded = set()
    out = []
    for i, t in enumerate(texts, start=1):
        digest = hashlib.sha256(t.strip().lower().encode()).hexdigest()[:6]
        item_id = f"H{rev}-{i}-{digest}"
        # A decision that determines scope, or approving the design itself, is
        # not implementation work (audit F06): it is settled before
        # authorisation. Ids keep their original position, and anything already
        # recorded stays visible with its evidence.
        if is_decision_step(t) and item_id not in recorded:
            continue
        out.append({"id": item_id, "position": i, "text": t, "design_revision": rev})
    return out


def authorisation(change) -> Optional[dict]:
    """What authorises the human items, or None while nothing does."""
    d = change.architect_decision
    if d is None:
        return None
    route = d.recommended_route
    if route == NO_CHANGE_ROUTE:
        c = no_change_confirmation(change)
        return {"kind": "no_change_confirmation", "by": c["by"], "at": c["at"],
                "design_revision": c.get("design_revision")} if c else None
    if route == HUMAN_ROUTE or route in TECHNICAL_ROUTES:
        try:
            from ..discovery import baseline
            from ..technical import store as tstore

            b = baseline.current_for_story(change.customer_id, change.id)
            if b is None:
                return None
            a = tstore.design_approval_for(change.customer_id, change.id, b["design_revision"])
            return {"kind": "design_approval", "by": a["approved_by"], "at": a["approved_at"],
                    "design_revision": a["design_revision"]} if a else None
        except Exception:  # noqa: BLE001
            return None
    ap = change.change_approval
    if ap is not None and ap.status == "approved":
        return {"kind": "exact_change", "by": ap.approved_by or "", "at": ap.approved_at or ""}
    return None


def no_change_confirmation(change) -> Optional[dict]:
    """The Application Manager's confirmation of the current no-change
    finding, or None. A newer design asks again."""
    rev = _design_revision(change)
    rows = [r for r in _load(change.customer_id, change.id).get("no_change_confirmations", [])
            if r.get("design_revision") == rev]
    return rows[-1] if rows else None


def confirm_no_change(change, *, actor_name: str, actor_user_id: str, roles: set[str], note: str) -> dict:
    """Confirm the Architect's finding that no JD Edwards change is needed.
    It authorises the remaining work by people, if any; it never closes the
    story (the business accepts the outcome first)."""
    if not roles & {"product_manager", "admin"}:
        raise HandoffRefused("only the Application Manager confirms a resolution without change")
    d = change.architect_decision
    if d is None or d.recommended_route != NO_CHANGE_ROUTE:
        raise HandoffRefused("the Architect has not concluded that no change is needed")
    if no_change_confirmation(change) is not None:
        raise HandoffRefused("this finding is already confirmed")
    rev = _design_revision(change)
    items = items_for(change, rev)
    entry = {"design_revision": rev, "by": actor_name, "by_user_id": actor_user_id, "at": _now(),
             "note": note.strip()[:1000], "work_items": [i["text"] for i in items]}
    entry["evidence_entry_hash"] = _evidence(
        change.id, actor_name, "no JD Edwards change needed: finding confirmed"
        + (f"; {len(items)} item(s) of work by people remain" if items else "")
        + (f" -- {entry['note']}" if entry["note"] else ""), "no_change_confirmed")
    doc = _load(change.customer_id, change.id)
    doc.setdefault("no_change_confirmations", []).append(entry)
    doc.setdefault("history", []).append({"event": "no_change_confirmed", **entry})
    _save(doc)
    return entry


def state(change) -> dict:
    """Everything about the story's hand-offs, read now."""
    rev = _design_revision(change)
    items = items_for(change, rev)
    doc = _load(change.customer_id, change.id)
    names = {}
    for r in doc.get("implementers", []):
        names[r["item_id"]] = r
    done = {}
    for r in doc.get("completions", []):
        done[r["item_id"]] = r
    for it in items:
        it["implementer"] = (names.get(it["id"]) or {}).get("name", "")
        it["completion"] = done.get(it["id"])
    verifications = [v for v in doc.get("verifications", []) if v.get("design_revision") == rev]
    return {"route": change.architect_decision.recommended_route if change.architect_decision else None,
            "design_revision": rev, "items": items, "authorisation": authorisation(change),
            "verification": verifications[-1] if verifications else None,
            "all_complete": bool(items) and all(it["completion"] for it in items)}


# ---------------------------------------------------------------------
# The specification
# ---------------------------------------------------------------------
def specification(change) -> dict:
    """The implementer's specification, from the current design (a snapshot)."""
    from ..services import issues

    d, spec, us = change.architect_decision, change.implementation_spec, change.user_story
    if d is None:
        raise HandoffRefused("the story has no design yet")
    st = state(change)
    answers = [r for r in issues.resolutions(change.customer_id, change.id).values() if r.get("answer")]
    result = d.result
    body = {
        "story_id": change.id, "title": change.title, "design_revision": st["design_revision"],
        "route": d.recommended_route,
        "need": us.statement if us else change.original_request,
        "conclusion": (result.conclusion if result and result.conclusion else d.existing_functionality_found),
        "changes": [c.model_dump() for c in (result.changes if result else [])],
        "steps": list(spec.sequence) if spec else [],
        "handoff_items": [{"id": i["id"], "text": i["text"], "implementer": i["implementer"]} for i in st["items"]],
        "objects": list(d.objects_affected),
        "acceptance_criteria": [{"id": a.id, "text": a.text} for a in (us.acceptance_criteria if us else [])],
        "validation": spec.validation_approach if spec else "",
        "rollback": d.rollback_strategy,
        "answers": [{"question": a["issue_title"], "answer": a["answer"], "by": a["by"], "at": a["at"][:10]}
                    for a in sorted(answers, key=lambda r: r["at"])],
        "uncertainty": list(result.uncertainty) if result else [],
        "authorisation": st["authorisation"],
        "generated_at": _now(),
    }
    material = {k: v for k, v in body.items() if k != "generated_at"}
    body["sha256"] = hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()
    return body


def _latin1(text: Any) -> str:
    """The PDF core fonts cover Latin-1; anything else is replaced readably."""
    t = str(text or "")
    t = (t.replace("→", "->").replace("—", "-").replace("–", "-").replace("‘", "'").replace("’", "'")
         .replace("“", '"').replace("”", '"').replace("…", "...").replace("•", "-").replace("✓", "v"))
    return t.encode("latin-1", "replace").decode("latin-1")


def specification_pdf(change) -> bytes:
    from fpdf import FPDF

    s = specification(change)
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.set_margins(18, 16, 18)
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin

    def heading(text: str, size: int = 12) -> None:
        pdf.ln(3)
        pdf.set_font("Helvetica", "B", size)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(width, 6, _latin1(text))
        pdf.set_font("Helvetica", "", 10)

    def para(text: str) -> None:
        pdf.set_font("Helvetica", "", 10)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(width, 5, _latin1(text))

    def bullets(rows: list[str], numbered: bool = False) -> None:
        if not rows:
            para("None.")
            return
        for i, r in enumerate(rows, start=1):
            pdf.set_x(pdf.l_margin)
            pdf.multi_cell(width, 5, _latin1(f"{i}. {r}" if numbered else f"- {r}"))

    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(2, 74, 50)
    pdf.cell(width, 5, "JADE by ConsultIQ - Implementation specification", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    heading(f"{s['title']} ({s['story_id']})", 15)
    para(f"Design revision {s['design_revision']} - route: {s['route']} - generated {s['generated_at'][:16].replace('T', ' ')} UTC")
    auth = s["authorisation"]
    para(f"Authorised by {auth['by']} on {str(auth['at'])[:10]}." if auth else
         "NOT YET AUTHORISED: do not implement until the Application Manager authorises this specification.")
    heading("What the business needs")
    para(s["need"] or "-")
    heading("JADE's conclusion")
    para(s["conclusion"] or "-")
    if s["changes"]:
        heading("What changes")
        for c in s["changes"]:
            status = {"verified": "verified in DEV", "not_yet_verified": "not yet verified",
                      "proposed": "proposed"}.get(c.get("status"), c.get("status"))
            para(f"- {c['what']}: {c.get('current') or '(not read)'} -> {c.get('proposed') or '(to be defined)'} [{status}]")
    heading("Your work")
    bullets([f"{i['text']}" + (f" (implementer: {i['implementer']})" if i["implementer"] else "")
             for i in s["handoff_items"]], numbered=True)
    if s["steps"] and [st for st in s["steps"]] != [i["text"] for i in s["handoff_items"]]:
        heading("Full implementation sequence (for context)")
        bullets(s["steps"], numbered=True)
    heading("JD Edwards objects concerned")
    para(", ".join(s["objects"]) or "None recorded.")
    heading("Acceptance criteria the result must meet")
    bullets([f"{a['id']}: {a['text']}" for a in s["acceptance_criteria"]])
    heading("How it will be verified")
    para(s["validation"] or "-")
    heading("If it must be undone")
    para(s["rollback"] or "-")
    if s["answers"]:
        heading("Answers recorded on the open points")
        for a in s["answers"]:
            para(f"Q: {a['question']}")
            para(f"A: {a['answer']} ({a['by']}, {a['at']})")
    if s["uncertainty"]:
        heading("What JADE could not verify")
        bullets(s["uncertainty"])
    heading("When you are done")
    para("Record each item as completed in JADE (Application Management, the change's Delivery), with an evidence "
         "reference: an OMW project, a screenshot, a document or a ticket. JADE keeps it in the story's evidence log.")
    pdf.ln(4)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(103, 110, 106)
    para(f"Specification fingerprint (SHA-256): {s['sha256']}")
    return bytes(pdf.output())


# ---------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------
def _require(roles: set[str]) -> None:
    if not roles & set(RECORDER_ROLES):
        raise HandoffRefused("recording hand-off work needs the Application Manager, CNC or Administrator role")


def authorise(change, *, actor_name: str, actor_user_id: str, roles: set[str], note: str) -> dict:
    """Authorise the hand-off of exactly the current design revision
    (Human Implementation route), under the company's approval policy."""
    from jde_mcp_server import approval, authority
    from jde_mcp_server.scope import load_company_scope, require_approval_policy

    from ..discovery import baseline
    from ..technical import service as tservice, store as tstore

    d = change.architect_decision
    if d is None or d.recommended_route != HUMAN_ROUTE:
        raise HandoffRefused("only a design routed to Human Implementation is authorised here; other routes have "
                             "their own approval")
    a = tservice.assignment(change.customer_id, change.id)
    if a["baseline_status"] == "needs_reassessment":
        raise HandoffRefused("the design is flagged for reassessment; re-run the Architect first")
    if tstore.design_approval_for(change.customer_id, change.id, a["design_revision"]):
        raise HandoffRefused(f"design revision {a['design_revision']} is already authorised")
    policy = require_approval_policy(load_company_scope(change.customer_id))
    matched = sorted(set(roles) & set(policy["exact_change_approver_roles"]))
    if not matched:
        raise approval.ApproverNotAuthorised("authorising an implementation needs a role the company's approval "
                                             "policy allows")
    authority.require_current_approver(actor_user_id, change.customer_id, policy["exact_change_approver_roles"])
    row = tstore.add_design_approval(
        company_id=change.customer_id, story_id=change.id, design_revision=a["design_revision"],
        baseline_id=a["baseline_id"], manifest_sha256=a["manifest_sha256"], approved_by=actor_name,
        approver_user_id=actor_user_id, roles=matched, note=note)
    try:
        baseline.set_design_approval(change.id, row)
    except LookupError:
        pass
    _evidence(change.id, actor_name, f"implementation by a person authorised for design revision "
                                     f"{a['design_revision']}" + (f": {note}" if note else ""), "handoff_authorised")
    return row


def _item(change, item_id: str) -> dict:
    found = next((i for i in items_for(change) if i["id"] == item_id), None)
    if found is None:
        raise HandoffRefused("this hand-off item is not part of the current design (it may have been replaced by a "
                             "newer design)")
    return found


def assign(change, item_id: str, *, name: str, actor_name: str, roles: set[str]) -> dict:
    _require(roles)
    it = _item(change, item_id)
    if not name.strip():
        raise HandoffRefused("name the implementer (a person or team)")
    doc = _load(change.customer_id, change.id)
    entry = {"item_id": it["id"], "name": name.strip()[:120], "by": actor_name, "at": _now()}
    doc["implementers"] = [r for r in doc.get("implementers", []) if r["item_id"] != it["id"]] + [entry]
    doc.setdefault("history", []).append({"event": "assigned", **entry})
    _save(doc)
    return entry


def complete(change, item_id: str, *, implementer: str, evidence_reference: str, note: str, actor_name: str,
             actor_user_id: str, roles: set[str]) -> dict:
    _require(roles)
    it = _item(change, item_id)
    if authorisation(change) is None:
        raise HandoffRefused("this work is not authorised yet: the design (or its exact change) must be approved "
                             "before work done outside JADE is recorded")
    if len(evidence_reference.strip()) < 3:
        raise HandoffRefused("give an evidence reference: an OMW project, a screenshot, a document or a ticket")
    doc = _load(change.customer_id, change.id)
    if any(r["item_id"] == it["id"] for r in doc.get("completions", [])):
        raise HandoffRefused("this item is already recorded as completed")
    who = implementer.strip() or next((r["name"] for r in doc.get("implementers", []) if r["item_id"] == it["id"]),
                                      "") or actor_name
    entry = {"item_id": it["id"], "text": it["text"], "design_revision": it["design_revision"],
             "implementer": who[:120], "evidence_reference": evidence_reference.strip()[:300],
             "note": note.strip()[:1000], "by": actor_name, "by_user_id": actor_user_id, "at": _now()}
    entry["evidence_entry_hash"] = _evidence(change.id, actor_name, f"hand-off item completed by {who}: {it['text']} "
                                                                    f"(evidence: {entry['evidence_reference']})",
                                             "handoff_completed")
    doc.setdefault("completions", []).append(entry)
    doc.setdefault("history", []).append({"event": "completed", **entry})
    _save(doc)
    return entry


def verify(change, *, passed: bool, evidence_reference: str, note: str, actor_name: str, actor_user_id: str,
           roles: set[str]) -> dict:
    """The person's verification of a Human Implementation result."""
    _require(roles)
    st = state(change)
    if st["route"] not in PEOPLE_ROUTES:
        raise HandoffRefused("this route is verified through its own test, not here")
    if not st["items"]:
        raise HandoffRefused("there is no work by people to verify on this story")
    if not st["all_complete"]:
        raise HandoffRefused("record every hand-off item as completed first")
    if not note.strip() or len(evidence_reference.strip()) < 3:
        raise HandoffRefused("say what was checked against the acceptance criteria, with an evidence reference")
    entry = {"design_revision": st["design_revision"], "passed": bool(passed),
             "evidence_reference": evidence_reference.strip()[:300], "note": note.strip()[:1000],
             "by": actor_name, "by_user_id": actor_user_id, "at": _now()}
    entry["evidence_entry_hash"] = _evidence(change.id, actor_name, f"result {'VERIFIED' if passed else 'NOT verified'}"
                                                                    f" by {actor_name}: {entry['note']} (evidence: "
                                                                    f"{entry['evidence_reference']})",
                                             "handoff_verified")
    doc = _load(change.customer_id, change.id)
    doc.setdefault("verifications", []).append(entry)
    doc.setdefault("history", []).append({"event": "verified", **entry})
    _save(doc)
    return entry
