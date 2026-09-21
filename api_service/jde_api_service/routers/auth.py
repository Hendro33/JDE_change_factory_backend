"""
Login, logout, password reset, and invitation acceptance.

Deliberately NOT here: any public "create an account" / "register"
endpoint. This service is invite-only (design requirement for this
increment) -- the only ways a users row can be created are accepting a
still-valid invitation (accept_invitation below) or the one
server-environment-controlled bootstrap path (services/bootstrap_service.py,
never reachable over HTTP).
"""

from __future__ import annotations

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response

from ..config import settings
from ..dependencies import Identity, resolve_identity
from ..models.auth import (
    AcceptInvitationInput,
    ForgotPasswordInput,
    ForgotPasswordResult,
    InvitationPreview,
    LoginInput,
    MeOut,
    ResetPasswordInput,
)
from ..services import auth_service, invitation_service
from ..services.email_service import OutgoingEmail, get_email_service

router = APIRouter(prefix="/auth", tags=["auth"])

_RESET_PATH = "/reset-password"
_INVITE_PATH = "/accept-invitation"


def _frontend_origin() -> str:
    # Best-effort only, for building a human-followable link in the
    # dev-preview response -- never used for anything security-relevant
    # (CORS/cookie decisions use settings.allowed_origins/cookie_* directly).
    return settings.allowed_origins[0] if settings.allowed_origins else ""


def _set_session_cookie(response: Response, raw_token: str) -> None:
    response.set_cookie(
        key=auth_service.SESSION_COOKIE_NAME,
        value=raw_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,  # type: ignore[arg-type]
        max_age=auth_service.SESSION_TTL_DAYS * 24 * 3600,
        path="/",
    )


@router.post("/login", response_model=MeOut)
def login(payload: LoginInput, response: Response) -> MeOut:
    user = auth_service.authenticate(payload.email, payload.password)
    if user is None:
        raise HTTPException(status_code=401, detail="incorrect email or password")
    raw_token = auth_service.create_session(user.id)
    _set_session_cookie(response, raw_token)
    return MeOut(user_id=user.id, email=user.email, display_name=user.display_name)


@router.post("/logout")
def logout(
    response: Response,
    jde_session: str | None = Cookie(default=None, alias=auth_service.SESSION_COOKIE_NAME),
) -> dict:
    # Revokes the server-side session record (not just the cookie) so
    # the same raw token can't be replayed if it leaked before logout --
    # and never 401s just because the cookie was already missing or
    # expired, since the end state (signed out) is the same either way.
    if jde_session:
        auth_service.revoke_session(jde_session)
    response.delete_cookie(auth_service.SESSION_COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me", response_model=MeOut)
def me(identity: Identity = Depends(resolve_identity)) -> MeOut:
    user = auth_service.get_user_by_id(identity.id)
    assert user is not None  # resolve_identity already confirmed this session's user exists and is active
    return MeOut(user_id=user.id, email=user.email, display_name=user.display_name)


@router.post("/forgot-password", response_model=ForgotPasswordResult)
def forgot_password(payload: ForgotPasswordInput) -> ForgotPasswordResult:
    """Always returns ok=true -- whether the email is registered is
    never revealed to the caller (see auth_service.authenticate's own
    comment on the same principle for login). preview_url is only ever
    populated in dev-preview mode (no real email provider configured,
    see email_service.py), and even then only when the account exists
    -- so in dev-preview mode this endpoint DOES reveal registration
    status to whoever calls it. That is an accepted, explicitly
    documented prototype trade-off (email_service.py's own docstring):
    fine for an invite-only internal pilot, not for a public product."""
    user = auth_service.get_user_by_email(payload.email)
    if user is None or not user.is_active:
        return ForgotPasswordResult(ok=True, preview_url=None)

    raw_token = auth_service.create_password_reset_token(user.id)
    email_service = get_email_service()
    link = f"{_frontend_origin()}{_RESET_PATH}?token={raw_token}"
    email_service.send(OutgoingEmail(
        to=user.email, subject="Reset your Jade password",
        body=f"Reset your password: {link}", action_url=link,
    ))
    return ForgotPasswordResult(ok=True, preview_url=link if email_service.is_dev_preview else None)


@router.post("/reset-password")
def reset_password(payload: ResetPasswordInput) -> dict:
    user_id = auth_service.consume_password_reset_token(payload.token)
    if user_id is None:
        raise HTTPException(status_code=400, detail="invalid or expired reset link")
    auth_service.set_password(user_id, payload.new_password)
    # A password reset invalidates every existing session -- a stolen
    # session token is exactly the scenario a reset is meant to recover
    # from.
    auth_service.revoke_all_sessions_for_user(user_id)
    return {"ok": True}


@router.get("/invitation/{token}/preview", response_model=InvitationPreview)
def preview_invitation(token: str) -> InvitationPreview:
    from ..services.customer_service import get_registry

    inv = invitation_service.get_by_token(token)
    if inv is None:
        return InvitationPreview(email="", company_name="", roles=[], requires_password=False, valid=False, reason="invitation not found")
    if inv["status"] != "pending":
        return InvitationPreview(
            email=inv["email"], company_name="", roles=inv["roles"], requires_password=False,
            valid=False, reason=f"invitation is {inv['status']}",
        )
    company = get_registry().get_customer(inv["company_id"])
    existing_user = auth_service.get_user_by_email(inv["email"])
    return InvitationPreview(
        email=inv["email"], company_name=company.name if company else inv["company_id"],
        roles=inv["roles"], requires_password=existing_user is None, valid=True,
    )


@router.post("/accept-invitation", response_model=MeOut)
def accept_invitation_route(payload: AcceptInvitationInput, response: Response) -> MeOut:
    """New user: password is required, and the response logs them
    straight in. Existing user: must already be signed in as the
    invited email -- this route never silently attaches an invitation
    to whichever session cookie happens to be present."""
    inv = invitation_service.get_by_token(payload.token)
    if inv is None or inv["status"] != "pending":
        raise HTTPException(status_code=400, detail="invalid, expired, or already-used invitation")

    existing_user = auth_service.get_user_by_email(inv["email"])
    if existing_user is not None:
        raise HTTPException(
            status_code=409,
            detail="an account with this email already exists -- sign in first, then open the invitation link again",
        )

    try:
        user, _membership_id = invitation_service.accept_invitation(
            payload.token, password=payload.password, display_name=payload.display_name,
        )
    except invitation_service.InvalidInvitation as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    raw_token = auth_service.create_session(user.id)
    _set_session_cookie(response, raw_token)
    return MeOut(user_id=user.id, email=user.email, display_name=user.display_name)


@router.post("/accept-invitation/existing-user", response_model=MeOut)
def accept_invitation_existing_user(
    payload: AcceptInvitationInput, identity: Identity = Depends(resolve_identity),
) -> MeOut:
    """The existing-user counterpart to accept_invitation_route above:
    the caller must already be signed in, as the exact email the
    invitation was sent to."""
    inv = invitation_service.get_by_token(payload.token)
    if inv is None or inv["status"] != "pending":
        raise HTTPException(status_code=400, detail="invalid, expired, or already-used invitation")

    user = auth_service.get_user_by_id(identity.id)
    assert user is not None
    if user.email.lower() != inv["email"].lower():
        raise HTTPException(status_code=403, detail="this invitation was sent to a different email address")

    try:
        invitation_service.accept_invitation(payload.token, password=None, display_name=None)
    except invitation_service.InvalidInvitation as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return MeOut(user_id=user.id, email=user.email, display_name=user.display_name)
