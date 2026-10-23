"""When the current step of a story began, from recorded events only.

"Waiting N days" and "completed this month" must not be inferred from a
record's generic updated-at time (findings 7 and 8, post-step-3 review):
that moves for reasons that have nothing to do with the story's progress,
and for an approved backlog story it never moves after approval at all.
The current step began at the latest recorded lifecycle event: the request
received, the story proposed, a domain decision, the design, the exact
change proposed or approved, an attempt, a test, a hand-off item, the
business acceptance, the evidence log, the as-built record. For a finished
story that event is its completion. When none is recorded, nothing is shown.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional


def _norm(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _events(change, lc) -> Iterable[Any]:
    yield change.created_at
    for e in change.evidence or []:
        yield e.captured_at
    for ap in (change.story_approval, change.change_approval):
        if ap is not None:
            yield ap.approved_at
    if change.architect_decision is not None:
        yield change.architect_decision.decided_at
    ec = change.exact_change
    if ec is not None and ec.execution is not None:
        yield ec.execution.last_attempt_at
    if change.test_result is not None:
        yield change.test_result.ran_at
    hv = getattr(change, "human_validation", None)
    if hv is not None:
        yield getattr(hv, "validated_at", None)
    cl = getattr(change, "closure", None)
    if cl is not None:
        yield getattr(cl, "closed_at", None)
    for w in lc.work_items or []:
        yield w.completed_at
    # A request still being turned into a story: the run's own last step.
    if lc.phase == "understand":
        yield change.updated_at
    try:
        from .registry import get_domain_review_service

        review = get_domain_review_service().get(change.id)
        if review is not None:
            for ap in (review.domain_owner_approval, review.application_manager_approval):
                if ap is not None:
                    yield ap.approved_at
            for v in review.history or []:
                yield v.captured_at
    except Exception:  # noqa: BLE001
        pass
    try:
        from jde_mcp_server import approval

        for rec in approval.list_changes_for_story(change.id):
            yield rec.get("created_at")
            yield rec.get("approved_at")
    except Exception:  # noqa: BLE001
        pass
    try:
        from .lifecycle import _asbuilt_final

        final = _asbuilt_final(change.customer_id, change.id)
        if final:
            yield final.get("finalised_at")
    except Exception:  # noqa: BLE001
        pass


def since(change, lc) -> Optional[str]:
    stamps = [s for s in (_norm(v) for v in _events(change, lc)) if s]
    return max(stamps) if stamps else None


def apply(change, lc):
    lc.since = since(change, lc)
    return lc
