"""
The Functional Agent's only JDE read: the current value of the exact
target its own exact change names (application / version / option), for
the pre-write confirmation and rollback value. It cannot name any other
object. The story must be approved, the change must belong to the story
and to the story's company (from its intake link), and it must be pending
or approved -- a rejected change reads nothing.
"""

from __future__ import annotations

from .ais_client import LiveReadUnavailable, client
from .approval import ChangeApprovalError, _load
from .backlog import require_approved
from .scope import company_for_story


def read_approved_target(story_id: str, change_id: str) -> dict:
    require_approved(story_id)
    record = _load(change_id)
    if record is None or record.get("story_id") != story_id:
        raise ChangeApprovalError(f"no change {change_id} for story {story_id}")
    if record.get("company_id") != company_for_story(story_id):
        raise ChangeApprovalError(f"change {change_id} does not belong to this story's company -- refusing")
    if record.get("status") not in ("pending", "approved"):
        raise ChangeApprovalError(f"change {change_id} is {record.get('status')}; nothing to read for it")
    op = record["operation"]
    target = {"application": op.get("application"), "version": op.get("version"), "option": op.get("option")}
    try:
        value = client.read_processing_option_value(target["application"], target["version"], target["option"])
    except LiveReadUnavailable as exc:
        return {"change_id": change_id, "target": target, "available": False, "reason": str(exc)}
    return {"change_id": change_id, "target": target, "available": True, "current_value": value,
            "note": "read for this change's exact target only; the write re-checks everything itself"}
