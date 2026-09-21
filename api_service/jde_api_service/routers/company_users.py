"""
Admin > Users: company member list (active/inactive/pending
invitations), inviting, role/domain assignment, deactivate/reactivate,
resend/revoke invitation.

Everything here requires the Admin role on the company in question
(require_role("admin")) -- Admin grants user management, never business
approval or agent-execution authority on its own (models/auth.py's own
docstring). "Last active Admin" protection lives in membership_service.py
(LastAdminError), not here -- this router just turns that into a 409.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..config import settings
from ..dependencies import AuthContext, require_role
from ..models.auth import (
    CompanyUsersOut,
    InvitationOut,
    InviteInput,
    MembershipOut,
    UpdateMembershipInput,
)
from ..services import invitation_service, membership_service
from ..services.email_service import OutgoingEmail, get_email_service

router = APIRouter(prefix="/admin/users", tags=["company-users"])

_INVITE_PATH = "/accept-invitation"


def _frontend_origin() -> str:
    return settings.allowed_origins[0] if settings.allowed_origins else ""


def _membership_out(m: dict) -> MembershipOut:
    return MembershipOut(
        membership_id=m["membership_id"], user_id=m["user_id"], email=m["email"],
        display_name=m["display_name"], status=m["status"], roles=m["roles"], domain_ids=m["domain_ids"],
    )


def _invitation_out(inv: dict, *, invited_by_display_name: str, preview_url: str | None = None) -> InvitationOut:
    return InvitationOut(
        id=inv["id"], email=inv["email"], roles=inv["roles"], domain_ids=inv["domain_ids"],
        status=inv["status"], created_at=inv["created_at"], expires_at=inv["expires_at"],
        invited_by_display_name=invited_by_display_name, preview_url=preview_url,
    )


def _display_name_for(user_id: str) -> str:
    from ..services import auth_service

    user = auth_service.get_user_by_id(user_id)
    return user.display_name if user else user_id


@router.get("", response_model=CompanyUsersOut)
def list_company_users(ctx: AuthContext = Depends(require_role("admin"))) -> CompanyUsersOut:
    members = [_membership_out(m) for m in membership_service.list_company_members(ctx.customer_id)]
    invitations = [
        _invitation_out(inv, invited_by_display_name=_display_name_for(inv["invited_by"]))
        for inv in invitation_service.list_for_company(ctx.customer_id)
    ]
    return CompanyUsersOut(members=members, invitations=invitations)


@router.post("/invite", response_model=InvitationOut)
def invite_user(payload: InviteInput, ctx: AuthContext = Depends(require_role("admin"))) -> InvitationOut:
    inv, raw_token = invitation_service.create_invitation(
        ctx.customer_id, payload.email, list(payload.roles), list(payload.domain_ids), invited_by=ctx.identity.id
    )
    link = f"{_frontend_origin()}{_INVITE_PATH}?token={raw_token}"
    email_service = get_email_service()
    email_service.send(OutgoingEmail(
        to=payload.email, subject="You've been invited to Jade",
        body=f"You've been invited to join a company on Jade: {link}", action_url=link,
    ))
    return _invitation_out(
        inv, invited_by_display_name=ctx.identity.display_name,
        preview_url=link if email_service.is_dev_preview else None,
    )


@router.post("/invitations/{invitation_id}/resend", response_model=InvitationOut)
def resend_invitation(invitation_id: str, ctx: AuthContext = Depends(require_role("admin"))) -> InvitationOut:
    inv = invitation_service.get_invitation(invitation_id)
    if inv is None or inv["company_id"] != ctx.customer_id:
        raise HTTPException(status_code=404, detail="no such invitation")
    inv, raw_token = invitation_service.resend_invitation(invitation_id, actor_user_id=ctx.identity.id)
    link = f"{_frontend_origin()}{_INVITE_PATH}?token={raw_token}"
    email_service = get_email_service()
    email_service.send(OutgoingEmail(
        to=inv["email"], subject="You've been invited to Jade",
        body=f"You've been invited to join a company on Jade: {link}", action_url=link,
    ))
    return _invitation_out(
        inv, invited_by_display_name=ctx.identity.display_name,
        preview_url=link if email_service.is_dev_preview else None,
    )


@router.post("/invitations/{invitation_id}/revoke", response_model=InvitationOut)
def revoke_invitation(invitation_id: str, ctx: AuthContext = Depends(require_role("admin"))) -> InvitationOut:
    inv = invitation_service.get_invitation(invitation_id)
    if inv is None or inv["company_id"] != ctx.customer_id:
        raise HTTPException(status_code=404, detail="no such invitation")
    inv = invitation_service.revoke_invitation(invitation_id, actor_user_id=ctx.identity.id)
    return _invitation_out(inv, invited_by_display_name=_display_name_for(inv["invited_by"]))


def _membership_in_company_or_404(membership_id: str, company_id: str) -> dict:
    m = membership_service.get_membership_by_id(membership_id)
    if m is None or m["company_id"] != company_id:
        raise HTTPException(status_code=404, detail="no such company member")
    return m


@router.put("/{membership_id}/roles", response_model=MembershipOut)
def update_roles(
    membership_id: str, payload: UpdateMembershipInput, ctx: AuthContext = Depends(require_role("admin"))
) -> MembershipOut:
    _membership_in_company_or_404(membership_id, ctx.customer_id)
    try:
        membership_service.update_membership_roles(
            membership_id, set(payload.roles), set(payload.domain_ids), actor_user_id=ctx.identity.id
        )
    except membership_service.LastAdminError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    updated = next(
        m for m in membership_service.list_company_members(ctx.customer_id) if m["membership_id"] == membership_id
    )
    return _membership_out(updated)


@router.post("/{membership_id}/deactivate", response_model=MembershipOut)
def deactivate(membership_id: str, ctx: AuthContext = Depends(require_role("admin"))) -> MembershipOut:
    _membership_in_company_or_404(membership_id, ctx.customer_id)
    try:
        membership_service.set_membership_status(membership_id, "inactive", actor_user_id=ctx.identity.id)
    except membership_service.LastAdminError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    updated = next(
        m for m in membership_service.list_company_members(ctx.customer_id) if m["membership_id"] == membership_id
    )
    return _membership_out(updated)


@router.post("/{membership_id}/reactivate", response_model=MembershipOut)
def reactivate(membership_id: str, ctx: AuthContext = Depends(require_role("admin"))) -> MembershipOut:
    _membership_in_company_or_404(membership_id, ctx.customer_id)
    membership_service.set_membership_status(membership_id, "active", actor_user_id=ctx.identity.id)
    updated = next(
        m for m in membership_service.list_company_members(ctx.customer_id) if m["membership_id"] == membership_id
    )
    return _membership_out(updated)
