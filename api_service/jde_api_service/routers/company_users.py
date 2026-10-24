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
    CreateUserInput,
    InvitationOut,
    InviteInput,
    MembershipOut,
    MembershipStatusInput,
    PasswordResetLinkOut,
    SetPasswordInput,
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
    from ..services.email_service import public_url

    return public_url(settings.allowed_origins[0] if settings.allowed_origins else "")


def _membership_out(m: dict) -> MembershipOut:
    return MembershipOut(
        membership_id=m["membership_id"], user_id=m["user_id"], email=m["email"],
        display_name=m["display_name"], status=m["status"], roles=m["roles"], domain_ids=m["domain_ids"],
        revision=m["revision"],
    )


def _invitation_out(inv: dict, *, invited_by_display_name: str, preview_url: str | None = None,
                    delivery=None) -> InvitationOut:
    return InvitationOut(
        id=inv["id"], email=inv["email"], roles=inv["roles"], domain_ids=inv["domain_ids"],
        status=inv["status"], created_at=inv["created_at"], expires_at=inv["expires_at"],
        invited_by_display_name=invited_by_display_name,
        # Test phase (Hendro, 24 Oct): the Administrator always sees the link too, so a test user
        # can be set up without a mailbox. Only Administrators can call these endpoints.
        preview_url=preview_url,
        email_sent=bool(delivery and delivery.sent), email_detail=delivery.detail if delivery else "",
    )


def _invitation_email(to: str, link: str) -> OutgoingEmail:
    return OutgoingEmail(
        to=to, subject="You have been invited to Jade",
        body=("You have been invited to Jade, ConsultIQ's JD Edwards change platform.\n\n"
              f"Accept the invitation and choose your password here:\n{link}\n\n"
              "The link is personal and expires. If you did not expect this, ignore this e-mail."),
        action_url=link)


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
    delivery = get_email_service().send(_invitation_email(payload.email, link))
    return _invitation_out(inv, invited_by_display_name=ctx.identity.display_name, preview_url=link,
                           delivery=delivery)


@router.post("/invitations/{invitation_id}/resend", response_model=InvitationOut)
def resend_invitation(invitation_id: str, ctx: AuthContext = Depends(require_role("admin"))) -> InvitationOut:
    inv = invitation_service.get_invitation(invitation_id)
    if inv is None or inv["company_id"] != ctx.customer_id:
        raise HTTPException(status_code=404, detail="no such invitation")
    inv, raw_token = invitation_service.resend_invitation(invitation_id, actor_user_id=ctx.identity.id)
    link = f"{_frontend_origin()}?{_INVITE_QUERY_KEY}={raw_token}"
    delivery = get_email_service().send(_invitation_email(inv["email"], link))
    return _invitation_out(inv, invited_by_display_name=ctx.identity.display_name, preview_url=link,
                           delivery=delivery)


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
    delivery = get_email_service().send(OutgoingEmail(
        to=user.email, subject="Reset your Jade password",
        body=(f"Your Jade administrator started a password reset for you.\n\nChoose a new password here:\n{link}\n\n"
              "The link is personal and expires."), action_url=link,
    ))
    return PasswordResetLinkOut(sent=delivery.sent, preview_url=link,
                                detail=delivery.detail)


def _membership_in_company_or_404(membership_id: str, company_id: str) -> dict:
    m = membership_service.get_membership_by_id(membership_id)
    if m is None or m["company_id"] != company_id:
        raise HTTPException(status_code=404, detail="no such company member")
    return m


def _member_out(membership_id: str, company_id: str) -> MembershipOut:
    return _membership_out(next(
        m for m in membership_service.list_company_members(company_id) if m["membership_id"] == membership_id
    ))


# Role, domain and status changes carry the revision the Admin loaded
# (409 if someone changed the membership since, 428 if absent) and
# re-check, inside the same write transaction, that the caller is STILL
# an Admin of this company: two Admins editing at once, or an Admin
# demoted mid-request, cannot leave a stale decision applied.
@router.put("/{membership_id}/roles", response_model=MembershipOut)
def update_roles(
    membership_id: str, payload: UpdateMembershipInput, ctx: AuthContext = Depends(require_role("admin"))
) -> MembershipOut:
    _membership_in_company_or_404(membership_id, ctx.customer_id)
    _require_company_domains(payload.domain_ids, ctx.customer_id)
    try:
        membership_service.update_membership_roles(
            membership_id, set(payload.roles), set(payload.domain_ids), actor_user_id=ctx.identity.id,
            expected_revision=payload.expected_revision, as_admin=True,
        )
    except membership_service.LastAdminError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except membership_service.ActorNoLongerAuthorised as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    return _member_out(membership_id, ctx.customer_id)


def _set_status(membership_id: str, status: str, payload: MembershipStatusInput | None, ctx: AuthContext) -> MembershipOut:
    _membership_in_company_or_404(membership_id, ctx.customer_id)
    try:
        membership_service.set_membership_status(
            membership_id, status, actor_user_id=ctx.identity.id,
            expected_revision=payload.expected_revision if payload else None, as_admin=True,
        )
    except membership_service.LastAdminError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except membership_service.ActorNoLongerAuthorised as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    return _member_out(membership_id, ctx.customer_id)


@router.post("/{membership_id}/deactivate", response_model=MembershipOut)
def deactivate(
    membership_id: str, payload: MembershipStatusInput | None = None, ctx: AuthContext = Depends(require_role("admin"))
) -> MembershipOut:
    return _set_status(membership_id, "inactive", payload, ctx)


@router.post("/{membership_id}/reactivate", response_model=MembershipOut)
def reactivate(
    membership_id: str, payload: MembershipStatusInput | None = None, ctx: AuthContext = Depends(require_role("admin"))
) -> MembershipOut:
    return _set_status(membership_id, "active", payload, ctx)


# -- Creating people and setting passwords directly (Hendro, 24 Oct) --------------------------
# The usual pattern in business applications: the Administrator creates a person with a
# temporary password, or sets a new one, right on the user screen. No e-mail or link needed.
MIN_PASSWORD_LENGTH = 8


def _check_new_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=422, detail=f"the password must be at least {MIN_PASSWORD_LENGTH} characters")


def _admin_everywhere_or_403(user_id: str, ctx: AuthContext) -> None:
    """A password change takes over the whole account, so the Administrator must be an
    Administrator of every company this person belongs to (the reset-link rule)."""
    elsewhere = [c["company_id"] for c in membership_service.companies_for_user(user_id)
                 if "admin" not in membership_service.roles_for(ctx.identity.id, c["company_id"])]
    if elsewhere:
        raise HTTPException(status_code=403, detail="this person also belongs to a company you are not an Admin of, "
                                                    "so you cannot set their password")


@router.post("", response_model=MembershipOut)
def create_user(payload: CreateUserInput, ctx: AuthContext = Depends(require_role("admin"))) -> MembershipOut:
    """Create a person with a password and give them roles in this company. Someone who already has a
    login is added to this company with these roles (their password is not changed here)."""
    email = payload.email.strip()
    if "@" not in email:
        raise HTTPException(status_code=422, detail="enter a valid e-mail address")
    if not payload.roles:
        raise HTTPException(status_code=422, detail="choose at least one role")
    _require_company_domains(payload.domain_ids, ctx.customer_id)
    user = auth_service.get_user_by_email(email)
    if user is None:
        _check_new_password(payload.password)
        user = auth_service.create_user(email, payload.password, payload.display_name.strip() or email)
    existing = next((m for m in membership_service.list_company_members(ctx.customer_id) if m["user_id"] == user.id), None)
    if existing is not None:
        membership_service.update_membership_roles(existing["membership_id"], set(payload.roles), set(payload.domain_ids),
                                                   actor_user_id=ctx.identity.id, as_admin=True)
        if existing.get("status") != "active":
            membership_service.set_membership_status(existing["membership_id"], "active", actor_user_id=ctx.identity.id, as_admin=True)
        membership_id = existing["membership_id"]
    else:
        membership_id = membership_service.create_membership(user.id, ctx.customer_id, payload.roles, payload.domain_ids,
                                                             created_by=ctx.identity.id)
    # A pending invitation for the same address in this company is no longer needed.
    for inv in invitation_service.list_for_company(ctx.customer_id):
        if inv["email"].lower() == email.lower() and inv["status"] == "pending":
            invitation_service.revoke_invitation(inv["id"], actor_user_id=ctx.identity.id)
    return _member_out(membership_id, ctx.customer_id)


@router.post("/{membership_id}/password")
def set_password(membership_id: str, payload: SetPasswordInput, ctx: AuthContext = Depends(require_role("admin"))) -> dict:
    """Set a new password for a person; their open sessions end."""
    member = _membership_in_company_or_404(membership_id, ctx.customer_id)
    _check_new_password(payload.password)
    _admin_everywhere_or_403(member["user_id"], ctx)
    auth_service.set_password(member["user_id"], payload.password)
    auth_service.revoke_all_sessions_for_user(member["user_id"])
    return {"ok": True}
