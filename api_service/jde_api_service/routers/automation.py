"""Routine automation and notifications (redesign step 4)."""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException

from ..dependencies import AuthContext, require_customer_access
from ..models.base import ApiModel

router = APIRouter(tags=["automation"])


class AutomationSettingsInput(ApiModel):
    enabled: Optional[bool] = None
    jira_pickup: Optional[bool] = None


class DigestInput(ApiModel):
    digest: Literal["off", "daily"]


def _require_manager(ctx: AuthContext) -> None:
    if not set(ctx.roles) & {"admin", "product_manager"}:
        raise HTTPException(status_code=403, detail="Routine automation is set by an Administrator or the "
                                                    "Application Manager.")


def _view(customer_id: str) -> dict:
    from ..services import automation

    s = automation.settings_for(customer_id)
    return {"enabled": bool(s["enabled"]), "jiraPickup": bool(s["jira_pickup"]),
            "serverEnabled": automation.server_enabled(), "updatedBy": s["updated_by"], "updatedAt": s["updated_at"],
            "setupChangedAt": s["setup_changed_at"], "setupChanged": s["setup_changed"],
            "lastJiraPickupAt": s["last_jira_pickup_at"], "maxRunsPerDay": automation.MAX_RUNS,
            "recheckHours": automation.recheck_hours(), "jiraPickupMinutes": automation.jira_pickup_minutes(),
            "events": [{"at": e["at"], "storyId": e.get("story_id"), "kind": e["kind"], "action": e["action"],
                        "detail": e["detail"]} for e in automation.events(customer_id)]}


@router.get("/automation")
def get_automation(ctx: AuthContext = Depends(require_customer_access)) -> dict:
    _require_manager(ctx)
    return _view(ctx.customer_id)


@router.put("/automation")
def put_automation(payload: AutomationSettingsInput, ctx: AuthContext = Depends(require_customer_access)) -> dict:
    from ..services import automation

    _require_manager(ctx)
    automation.update_settings(ctx.customer_id, enabled=payload.enabled, jira_pickup=payload.jira_pickup,
                               by=ctx.identity.display_name)
    return _view(ctx.customer_id)


@router.get("/notifications")
def get_notifications(ctx: AuthContext = Depends(require_customer_access)) -> dict:
    from ..services import membership_service, notifications
    from ..services.email_service import get_email_service

    domains = membership_service.domain_ids_for_membership(ctx.identity.id, ctx.customer_id)
    feed = notifications.for_user(ctx.customer_id, ctx.identity.id, ctx.roles, domains)
    return {"items": [{"id": i["id"], "at": i["at"], "kind": i["kind"], "title": i["title"], "text": i["text"],
                       "storyId": i["story_id"], "tab": i["tab"], "unread": i["unread"]} for i in feed["items"]],
            "unread": feed["unread"], "digest": feed["digest"],
            "emailAvailable": bool(getattr(get_email_service(), "configured", False))}


@router.post("/notifications/read")
def read_notifications(ctx: AuthContext = Depends(require_customer_access)) -> dict:
    from ..services import notifications

    notifications.mark_read(ctx.customer_id, ctx.identity.id)
    return get_notifications(ctx)


@router.put("/notifications/preferences")
def put_preferences(payload: DigestInput, ctx: AuthContext = Depends(require_customer_access)) -> dict:
    from ..services import notifications

    notifications.set_digest(ctx.customer_id, ctx.identity.id, payload.digest)
    return get_notifications(ctx)
