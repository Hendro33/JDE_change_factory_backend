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
    PasswordResetLinkOut,
    UpdateMembershipInput,
)
from ..services import auth_service, invitation_service, membership_service
from ..services.registry import get_business_domain_service
from ..services.email_service import OutgoingEmail, get_email_service

router = APIRouter(prefix="/admin/users", tags=["company-users"])

# Query param on the frontend's root path -- see routers/auth.py's own
# comment on why this is not a distinct path (no client-side router or
# server-side rewrite exists in that frontend).
_INVITE_QUERY_KEY = "acceptInvitation"


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


def _require_company_domains(domain_ids, company_id: str) -> None:
    """Domain assignments grant Domain Owner authority, so each one must
    be a real domain of this same company."""
    service = get_business_domain_service()
    unknown = sorted(d for d in set(domain_ids) if service.get_for_customer(d, company_id) is None)
    if unknown:
        raise HTTPException(status_code=422, detail=f"not business domains of this company: {', '.join(unknown)}")


@router.post("/invite", response_model=InvitationOut)
def invite_user(payload: InviteInput, ctx: AuthContext = Depends(require_role("admin"))) -> InvitationOut:
    _require_company_domains(payload.domain_ids, ctx.customer_id)
    inv, raw_token = invitation_service.create_invitation(
        ctx.customer_id, payload.email, list(payload.roles), list(payload.domain_ids), invited_by=ctx.identity.id
    )
    link = f"{_frontend_origin()}?{_INVITE_QUERY_KEY}={raw_token}"
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
    link = f"{_frontend_origin()}?{_INVITE_QUERY_KEY}={raw_token}"
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


@router.post("/{membership_id}/password-reset-link", response_model=PasswordResetLinkOut)
def issue_password_reset_link(
    membership_id: str, ctx: AuthContext = Depends(require_role("admin"))
) -> PasswordResetLinkOut:
    """The Admin-side replacement for the anonymous preview link. It is
    refused when the person also belongs to a company where the caller is
    not an Admin: a reset link takes over the whole account, so an Admin
    of one company must not be able to take over someone else's access."""
    member = _membership_in_company_or_404(membership_id, ctx.customer_id)
    elsewhere = [
        c["company_id"] for c in membership_service.companies_for_user(member["user_id"])
        if "admin" not in membership_service.roles_for(ctx.identity.id, c["company_id"])
    ]
    if elsewhere:
        raise HTTPException(
            status_code=403,
            detail="this person also belongs to a company you are not an Admin of, so you cannot reset their password",
        )
    user = auth_service.get_user_by_id(member["user_id"])
    if user is None or not user.is_active:
        raise HTTPException(status_code=404, detail="no such active user")
    raw_token = auth_service.create_password_reset_token(user.id)
    link = f"{_frontend_origin()}?resetToken={raw_token}"
    email_service = get_email_service()
    email_service.send(OutgoingEmail(
        to=user.email, subject="Reset your Jade password", body=f"Reset your password: {link}", action_url=link,
    ))
    return PasswordResetLinkOut(
        sent=not email_service.is_dev_preview, preview_url=link if email_service.is_dev_preview else None,
    )


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
    _require_company_domains(payload.domain_ids, ctx.customer_id)
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
