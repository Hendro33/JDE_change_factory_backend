"""
The Phase 2 gate (design document Section 3.5) as actual code, not a
documented expectation.

A story that has not been explicitly approved by a human here cannot be
read by any Phase 3 tool -- see require_approved() below, which every
Architect/Functional/Technical Agent tool calls before doing anything
else (Section 3.5, Gate 2). No agent has a tool that can approve a
story. That is deliberate: approval is a human action taken outside
agent orchestration, exactly like the PreToolUse write-approval hook is
a human action outside the agent's own reasoning.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

from .config import settings

BACKLOG_DIR = os.environ.get("JDE_BACKLOG_DIR", "./backlog")


class BacklogError(RuntimeError):
    pass


class StoryNotApproved(BacklogError):
    """Raised by every Phase 3 tool when a story hasn't cleared Gate 2.
    This is the actual gate -- catching this exception is the only way
    a Phase 3 tool call can proceed, and it can't be caught by an agent
    talking its way past it."""


# Lifecycle state (design document Section 16.1). The pilot implements
# the subset of the full 12-state target model that the stories the
# starter kit actually handles pass through -- EXECUTING, TESTING,
# VALIDATED, CNC_HANDOFF and CLOSED are not separately tracked here
# because those stages aren't yet discrete tool calls in this code
# (they're the Functional Agent's write/test/report steps themselves).
# Extending this as those stages become real tools is a pilot-ready ->
# product-ready step (Section 18), not a pilot blocker.
_ALLOWED_TRANSITIONS = {
    "BACKLOG_READY": {"APPROVED", "REJECTED"},
    "APPROVED": {"RESOLVED_WITHOUT_CHANGE"},  # further transitions (ARCHITECTING etc.) happen via approval.py once a change is proposed
    "REJECTED": set(),
    "RESOLVED_WITHOUT_CHANGE": set(),
}


def _set_state(record: dict, new_state: str) -> None:
    current = record.get("state")
    if current is not None and new_state not in _ALLOWED_TRANSITIONS.get(current, set()):
        raise BacklogError(f"illegal state transition: {current} -> {new_state}")
    record["state"] = new_state


def _path(story_id: str) -> str:
    os.makedirs(BACKLOG_DIR, exist_ok=True)
    return os.path.join(BACKLOG_DIR, f"{story_id}.json")


def _load(story_id: str) -> Optional[dict]:
    p = _path(story_id)
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(story_id: str, record: dict) -> None:
    with open(_path(story_id), "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)


# ---------------------------------------------------------------------
# Phase 1 -> Phase 2 handoff. Callable by the Check Agent ONLY on a
# story that has already passed the full Section 5.2 checklist -- this
# module trusts that the caller enforced Gate 1; it does not re-check
# the quality criteria itself.
# ---------------------------------------------------------------------
def propose_to_backlog(
    story_id: str,
    user_story: str,
    business_impact: dict,
    rough_complexity_signal: str,
    source: str = "",
) -> dict:
    """Record a quality-gated story in the backlog. Sets status to
    'backlog' -- NOT 'approved'. Nothing further happens to it until a
    human runs backlog_review.py."""
    if _load(story_id) is not None:
        raise BacklogError(f"story {story_id} already exists in the backlog")
    record = {
        "story_id": story_id,
        "user_story": user_story,
        "business_impact": business_impact,          # Section 3.6 criteria
        "rough_complexity_signal": rough_complexity_signal,  # heuristic only, Section 3.6
        "source": source,
        "status": "backlog",
        "state": "BACKLOG_READY",
        "proposed_at": time.time(),
        "decision": None,       # "approved" | "rejected", set only by a human
        "decided_by": None,
        "decided_at": None,
        "decision_note": None,
    }
    _save(story_id, record)
    return record


# ---------------------------------------------------------------------
# Phase 2. Human-only. There is deliberately no MCP tool wrapping these
# -- they are called from backlog_review.py, run directly by a person,
# never by an agent.
# ---------------------------------------------------------------------
def list_pending() -> list[dict]:
    if not os.path.isdir(BACKLOG_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(BACKLOG_DIR)):
        if fn.endswith(".json"):
            rec = _load(fn[:-5])
            if rec and rec["status"] == "backlog":
                out.append(rec)
    return out


def approve(story_id: str, decided_by: str, note: str = "") -> dict:
    record = _load(story_id)
    if record is None:
        raise BacklogError(f"no such story: {story_id}")
    _set_state(record, "APPROVED")
    record.update({
        "status": "approved",
        "decision": "approved",
        "decided_by": decided_by,
        "decided_at": time.time(),
        "decision_note": note,
    })
    _save(story_id, record)
    return record


def reject(story_id: str, decided_by: str, note: str) -> dict:
    record = _load(story_id)
    if record is None:
        raise BacklogError(f"no such story: {story_id}")
    if not note:
        raise BacklogError("a rejection must include a note -- 'no' with no reason isn't enough to send back to Improve")
    _set_state(record, "REJECTED")
    record.update({
        "status": "rejected",
        "decision": "rejected",
        "decided_by": decided_by,
        "decided_at": time.time(),
        "decision_note": note,
    })
    _save(story_id, record)
    return record


# ---------------------------------------------------------------------
# "Resolve without Change" (Section 15.6). A legitimate, evidenced
# ending for a story where the Architect determines existing JDE
# functionality or configuration already satisfies the requirement --
# not a Functional or Technical Agent candidate at all, and JDE is
# never touched. Callable only on an already-approved story, and only
# terminal: a resolved story does not later become an executed one
# under the same story_id.
# ---------------------------------------------------------------------
def resolve_without_change(story_id: str, resolution_note: str, resolved_by: str) -> dict:
    if not resolution_note:
        raise BacklogError("resolve_without_change requires a note explaining what existing capability satisfies the requirement")
    record = require_approved(story_id)
    _set_state(record, "RESOLVED_WITHOUT_CHANGE")
    record.update({
        "status": "resolved_without_change",
        "resolution_note": resolution_note,
        "resolved_by": resolved_by,
        "resolved_at": time.time(),
    })
    _save(story_id, record)
    return record


# ---------------------------------------------------------------------
# Phase 3 gate. Every Architect/Functional/Technical Agent tool calls
# this FIRST, before touching JDE. This is Gate 2 as code.
# ---------------------------------------------------------------------
def require_approved(story_id: str) -> dict:
    record = _load(story_id)
    if record is None:
        raise StoryNotApproved(
            f"story {story_id} has no backlog record at all. "
            "Phase 3 tools only ever operate on stories that went through "
            "Phase 2 (Section 3.5) -- propose it to the backlog and get it "
            "approved first."
        )
    if record["status"] != "approved":
        raise StoryNotApproved(
            f"story {story_id} is not approved (current status: "
            f"{record['status']}). A human must approve it via "
            "backlog_review.py before any Phase 3 tool can act on it."
        )
    return record


def record_story_revision(story_id: str, user_story: str, *, revision: int, revised_by: str, reason: str) -> dict:
    """A person-applied revision of an approved story's text (for example
    accepted process-analysis findings). The previous text is kept in the
    record's history; the approval decision itself is not changed -- the
    caller flags dependent designs and approvals for reassessment."""
    record = require_approved(story_id)
    record.setdefault("story_revisions", []).append({
        "revision": revision, "previous_user_story": record["user_story"], "revised_by": revised_by,
        "reason": reason, "revised_at": time.time()})
    record["user_story"] = user_story
    _save(story_id, record)
    return record


def get_approved_story(story_id: str) -> dict:
    """The one call an Architect subagent should make first. Returns the
    full backlog record if -- and only if -- Gate 2 has been cleared."""
    return require_approved(story_id)
