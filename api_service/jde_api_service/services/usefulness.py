"""Usefulness and completion, not text generation (register M7, GPT F09).

Measured from recorded events only:
* routine steps started by JADE vs started by a person (manual transitions);
* time open work is waiting on people vs on JADE, right now;
* repeat questions: an open question that duplicates one already answered;
* verified outcomes: verified in DEV and accepted by the business;
* stories the Domain Owner approved without a revision (revisions counted).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional


def _days(iso: Optional[str], now: datetime) -> Optional[float]:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return max(0.0, (now - t).total_seconds() / 86400)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 3}


def measure(company_id: str, changes: list, *, days: int = 30) -> list[dict]:
    from ..persistence.db import connection
    from . import automation, issues
    from .registry import get_domain_review_service

    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).isoformat()

    auto = sum(1 for e in automation.events(company_id, limit=10_000)
               if e.get("action") == "started" and (e.get("at") or "") >= since)
    with connection() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM ai_runs WHERE company_id = ? AND started_at >= ? AND driver IN "
                             "('orchestration_driver', 'architecture_driver')", (company_id, since)).fetchone()["n"]
    manual = max(0, int(total) - auto)

    people = jade = 0.0
    for c in changes:
        lc = c.lifecycle
        if lc is None or lc.phase == "done" or lc.next_action.owner == "none":
            continue
        d = _days(lc.since, now) or 0.0
        if lc.next_action.owner == "jade":
            jade += d
        else:
            people += d

    repeats = 0
    for c in changes:
        lc = c.lifecycle
        if lc is None or lc.phase == "done":
            continue
        answered = [r for r in issues.resolutions(company_id, c.id).values() if r.get("answer")]
        for i in lc.issues or []:
            if i.resolution is not None or i.kind not in ("business", "decision"):
                continue
            w = _words(i.title)
            if w and any(len(w & _words(r.get("issue_title", ""))) / len(w) >= 0.7 for r in answered):
                repeats += 1

    verified = accepted = not_accepted = 0
    for c in changes:
        card = c.lifecycle.outcome_card if c.lifecycle else None
        if card is None:
            continue
        if card.dev_verified.done and (card.dev_verified.at or "") >= since:
            verified += 1
            if card.business_accepted.done:
                accepted += 1
            elif card.not_accepted is not None:
                not_accepted += 1

    approved = first_time = 0
    reviews = get_domain_review_service()
    for c in changes:
        r = reviews.get(c.id)
        if r is None or r.domain_owner_approval is None or r.domain_owner_approval.status != "approved":
            continue
        if (r.domain_owner_approval.approved_at or "") < since:
            continue
        approved += 1
        if len(r.history) <= 1:
            first_time += 1

    return [
        {"key": "manual_starts", "label": f"Routine steps started by a person ({days} days)", "value": manual,
         "detail": f"{auto} started by JADE itself, {manual} by a person (intake and solutioning runs).",
         "definition": "Agent runs for intake and solutioning, minus the starts routine automation recorded."},
        {"key": "waiting", "label": "Waiting on people vs JADE (days, now)",
         "value": round(people, 1), "detail": f"{people:.1f} days waiting on people, {jade:.1f} on JADE, across open work.",
         "definition": "The sum, over open stories, of the days since the current step began, by who owns it."},
        {"key": "repeat_questions", "label": "Repeat questions (open now)", "value": repeats,
         "detail": "Open questions that duplicate one already answered on the same story.",
         "definition": "An open question whose words overlap 70% or more with an answered one."},
        {"key": "verified_outcomes", "label": f"Verified outcomes ({days} days)", "value": verified,
         "detail": f"{accepted} accepted by the business, {not_accepted} not accepted, {verified - accepted - not_accepted} awaiting.",
         "definition": "Changes verified in DEV in the period, with what the business decided."},
        {"key": "first_time_right", "label": f"Stories approved without a revision ({days} days)",
         "value": first_time, "detail": f"{first_time} of {approved} approved stories needed no revision (Domain Owner "
                                         "edits and reviewer revisions counted).",
         "definition": "Domain Owner approvals in the period whose story has a single version."},
    ]
