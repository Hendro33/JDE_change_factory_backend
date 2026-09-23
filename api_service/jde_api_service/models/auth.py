"""
Login, company membership, roles and invitations.

Four roles, each mapping onto an EXISTING responsibility already coded
elsewhere in this service rather than a new concept:
  - domain_owner: the existing Domain Owner intervention/approval
    workflow (routers/domain_governance.py's start/edit/ask/
    request-reconsideration/approve/reject), scoped to the specific
    business domains this membership is assigned to.
  - product_manager: the existing Application Manager sprint/build
    decision (routers/domain_governance.py's
    application-manager-approve/reject) -- same gate, renamed to match
    the role name requested for this increment.
  - admin: company user management, invitations, access assignments,
    settings and integrations (Jira). Deliberately NOT business
    approval or agent-execution authority -- an Admin without also
    holding domain_owner/product_manager cannot approve or reject
    anything.
  - dashboard_viewer: read-only. Enforced by require_write_access
    (dependencies.py) rejecting any POST/PUT/DELETE/PATCH from a
    membership whose only role is dashboard_viewer.

A user can hold more than one role on the same company (e.g. admin AND
domain_owner), and belongs to company memberships independently per
company -- see company_memberships/membership_roles in
persistence/migrations.py.
"""

from __future__ import annotations

from typing import Literal, Optional

from .base import ApiModel

Role = Literal["domain_owner", "product_manager", "admin", "dashboard_viewer"]
ALL_ROLES: tuple[Role, ...] = ("domain_owner", "product_manager", "admin", "dashboard_viewer")

MembershipStatus = Literal["active", "inactive"]
InvitationStatus = Literal["pending", "accepted", "revoked", "expired"]


class LoginInput(ApiModel):
    email: str
    password: str


class MeOut(ApiModel):
    user_id: str
    email: str
    display_name: str


class ForgotPasswordInput(ApiModel):
    email: str


class ForgotPasswordResult(ApiModel):
    # Deliberately uninformative either way -- see auth_service.authenticate's
    # own comment on why a login/reset flow must not reveal whether an
    # email is registered.
    ok: bool = True
    # Always None. This endpoint is anonymous, so returning the link here
    # would hand any caller a working reset link for any account. Without
    # an email provider, a company Admin issues the link instead
    # (POST /admin/users/{membership_id}/password-reset-link).
    preview_url: Optional[str] = None


class PasswordResetLinkOut(ApiModel):
    # True once a real email provider delivers the link; False in
    # dev-preview mode, where preview_url is the link for the Admin to hand over.
    sent: bool
    preview_url: Optional[str] = None


class ResetPasswordInput(ApiModel):
    token: str
    new_password: str


class AcceptInvitationInput(ApiModel):
    token: str
    # Required only when accepting creates a brand-new user (no
    # account with this email exists yet). An existing user accepts
    # with no password -- they just need to already be logged in, or
    # log in as part of the same call. See auth.py's own
    # accept_invitation for the exact rule.
    password: Optional[str] = None
    display_name: Optional[str] = None


class InvitationPreview(ApiModel):
    """What an invitation link resolves to, shown before the invited
    person decides whether to set a password (new user) or just sign
    in (existing user)."""

    email: str
    company_name: str
    roles: list[Role]
    requires_password: bool
    valid: bool
    reason: Optional[str] = None


class MembershipOut(ApiModel):
    membership_id: str
    user_id: str
    email: str
    display_name: str
    status: MembershipStatus
    roles: list[Role]
    domain_ids: list[str] = []


class InvitationOut(ApiModel):
    id: str
    email: str
    roles: list[Role]
    domain_ids: list[str] = []
    status: InvitationStatus
    created_at: str
    expires_at: str
    invited_by_display_name: str
    # Only present immediately after creation/resend, and only in
    # dev-preview mode -- see email_service.py. Never stored, never
    # returned by the list endpoint afterwards.
    preview_url: Optional[str] = None


class InviteInput(ApiModel):
    email: str
    roles: list[Role]
    domain_ids: list[str] = []


class UpdateMembershipInput(ApiModel):
    roles: list[Role]
    domain_ids: list[str] = []


class CompanyUsersOut(ApiModel):
    members: list[MembershipOut]
    invitations: list[InvitationOut]
